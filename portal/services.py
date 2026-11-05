import json
import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    AcademicSession,
    Course,
    CourseMaterial,
    CourseReminderDispatch,
    MaterialAccess,
    NotificationRecipient,
    StudentCourseRegistration,
    User,
    UserAlert,
    PushSubscription,
    AuditLog,
    Payment,
    Subscription,
    SubscriptionPlan,
    SubscriptionPaymentGateway,
    SubscriptionEvent,
    SubscriptionNotification,
    AccountActivation,
    EmailVerificationOTP,
)

PAYSTACK_API_BASE = "https://api.paystack.co"
PAYSTACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
NOTIFICATION_RECIPIENT_BATCH_SIZE = 500
NOTIFICATION_EMAIL_BATCH_SIZE = 100
LECTURE_REMINDER_LEAD_MINUTES = 30
# GitHub Actions and other external schedulers can start a little early or
# late.  A symmetric five-minute window keeps the reminder close to its
# promised time without ever notifying after the class starts.
LECTURE_REMINDER_TOLERANCE_MINUTES = 5
REMINDER_DELIVERY_CLAIM_MINUTES = 5
ACCOUNT_ACTIVATION_TTL = timedelta(hours=24)
EMAIL_VERIFICATION_OTP_TTL = timedelta(minutes=15)
LEGACY_PROVISIONED_PASSWORD = "educonnect"
FREE_TRIAL_PLAN_NAME = "Six-month free trial"
FREE_TRIAL_DURATION_DAYS = 183
logger = logging.getLogger(__name__)


def provisioned_password_for(institution):
    """Return the temporary password used for accounts provisioned by one institution."""
    subdomain = (getattr(institution, "subdomain", "") or "").strip().lower()
    return f"{subdomain}educonnect" if subdomain else "educonnect"


def upgrade_legacy_provisioned_password(user, supplied_password):
    """Accept the current portal password once for accounts provisioned before it changed.

    Only accounts that still have the former temporary password are eligible.
    A successful upgrade replaces that legacy password immediately; it is not a
    second persistent password or a general fallback authentication path.
    """
    if not user or user.role not in {User.Role.ADMIN, User.Role.LECTURER}:
        return False
    current_password = provisioned_password_for(user.institution)
    if (
        current_password == LEGACY_PROVISIONED_PASSWORD
        or supplied_password != current_password
        or not user.check_password(LEGACY_PROVISIONED_PASSWORD)
    ):
        return False
    user.set_password(current_password)
    update_fields = ["password"]
    if user.role == User.Role.ADMIN:
        # The administrator must confirm control of the account email again
        # before the newly upgraded temporary password yields a dashboard.
        user.email_verified = False
        update_fields.append("email_verified")
    user.save(update_fields=update_fields)
    return True


def issue_account_activation(user):
    """Disable password sign-in until the account owner sets a password."""
    if not user or not user.pk:
        raise ValueError("A saved user is required before issuing activation.")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    token = secrets.token_urlsafe(32)
    AccountActivation.objects.update_or_create(
        user=user,
        defaults={
            "token_hash": AccountActivation.token_hash_for(token),
            "expires_at": timezone.now() + ACCOUNT_ACTIVATION_TTL,
            "consumed_at": None,
        },
    )
    return token


def send_account_activation_email(user, activation_url):
    """Send an activation link without logging or returning the secret token."""
    if not user or not user.is_active or not user.email:
        return False
    portal_name = getattr(user.institution, "name", "") or "EduConnect"
    message = (
        f"Hello {user.full_name},\n\n"
        f"Set your {portal_name} password using this one-time link:\n{activation_url}\n\n"
        "The link expires in 24 hours. If you did not expect this account, contact your institution."
    )
    try:
        return EmailMessage(
            subject=f"Activate your {portal_name} account",
            body=message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@educonnect.local"),
            to=[user.email],
        ).send(fail_silently=False) == 1
    except Exception:
        logger.warning("Unable to send account activation email for user_id=%s", user.pk)
        return False


def send_email_verification_otp(user):
    """Deliver a fresh, short-lived onboarding code without persisting the code itself."""
    if not user or not user.is_active or not user.email:
        return False
    code = f"{secrets.randbelow(1_000_000):06d}"
    EmailVerificationOTP.objects.update_or_create(
        user=user,
        defaults={
            "code_hash": EmailVerificationOTP.code_hash_for(code),
            "expires_at": timezone.now() + EMAIL_VERIFICATION_OTP_TTL,
            "attempts": 0,
        },
    )
    portal_name = getattr(user.institution, "name", "") or "EduConnect"
    message = (
        f"Hello {user.full_name},\n\n"
        f"Your {portal_name} account verification code is: {code}\n\n"
        "Enter this code after signing in with your email address and temporary password. "
        "The code expires in 15 minutes. If you did not expect this account, contact your institution."
    )
    try:
        return EmailMessage(
            subject=f"Verify your {portal_name} account",
            body=message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@educonnect.local"),
            to=[user.email],
        ).send(fail_silently=False) == 1
    except Exception:
        logger.warning("Unable to send email-verification OTP for user_id=%s", user.pk)
        return False


def grant_free_trial_subscription(institution):
    """Give each newly provisioned institution one six-month trial."""
    trial_plan, _ = SubscriptionPlan.objects.get_or_create(
        name=FREE_TRIAL_PLAN_NAME,
        defaults={
            "description": "Complimentary access for new Educonnect institutions.",
            "price": Decimal("0.00"),
            "billing_period": SubscriptionPlan.BillingPeriod.CUSTOM,
            "custom_duration_days": FREE_TRIAL_DURATION_DAYS,
            "is_active": True,
            "is_trial": True,
        },
    )
    today = timezone.localdate()
    return Subscription.objects.get_or_create(
        institution=institution,
        plan=trial_plan,
        defaults={
            "start_date": today,
            "end_date": today + timedelta(days=FREE_TRIAL_DURATION_DAYS),
            "status": Subscription.Status.TRIAL,
            "amount": Decimal("0.00"),
        },
    )


def paystack_amount_in_kobo(amount):
    try:
        normalized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Payment amount must be a valid positive number.") from exc
    if not normalized.is_finite() or normalized <= 0:
        raise ValueError("Payment amount must be a valid positive number.")
    return int(normalized * 100)


def _paystack_request(secret_key, path, payload=None):
    if not secret_key:
        raise ValueError("Paystack secret key is not configured.")
    operation = path.split("/", 1)[0]
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Accept": "application/json",
        "User-Agent": PAYSTACK_USER_AGENT,
    }
    data = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
        method = "POST"
    request = Request(
        f"{PAYSTACK_API_BASE}/{path.lstrip('/')}",
        data=data,
        headers=headers,
        method=method,
    )
    http_status = None
    try:
        with urlopen(request, timeout=5) as response:
            http_status = getattr(response, "status", None)
            try:
                body = response.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                logger.warning(
                    "paystack_api_request_failed operation=%s http_status=%s failure_kind=invalid_encoding",
                    operation,
                    http_status,
                )
                raise ValueError("Paystack returned an unreadable response.") from exc
    except HTTPError as exc:
        logger.warning(
            "paystack_api_request_failed operation=%s http_status=%s failure_kind=http_error",
            operation,
            exc.code,
        )
        if 500 <= exc.code < 600:
            raise ValueError("Unable to reach Paystack right now. Please try again shortly.") from exc
        raw_body = exc.read().decode("utf-8", errors="replace").strip()
        try:
            error_payload = json.loads(raw_body)
        except json.JSONDecodeError:
            error_payload = {}
        message = error_payload.get("message") if isinstance(error_payload, dict) else None
        if message:
            raise ValueError(f"Paystack rejected the request: {message}") from exc
        if raw_body:
            raise ValueError(f"Paystack rejected the request (HTTP {exc.code}): {raw_body[:200]}") from exc
        message = "Paystack rejected the request."
        raise ValueError(message) from exc
    # ``urlopen`` raises ``URLError`` for DNS and connection failures, but a
    # read timeout can be raised directly as ``TimeoutError``/``OSError``.
    # Treat both as a recoverable gateway failure so a browser callback never
    # becomes an unhandled 500 merely because Paystack is temporarily slow.
    except (URLError, OSError) as exc:
        logger.warning(
            "paystack_api_request_failed operation=%s failure_kind=transport error_type=%s",
            operation,
            type(exc).__name__,
        )
        raise ValueError("Unable to reach Paystack right now. Please try again shortly.") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.warning(
            "paystack_api_request_failed operation=%s http_status=%s failure_kind=invalid_json",
            operation,
            http_status,
        )
        raise ValueError("Paystack returned an unreadable response.") from exc

    if not isinstance(parsed, dict):
        logger.warning(
            "paystack_api_request_failed operation=%s http_status=%s failure_kind=invalid_response_shape",
            operation,
            http_status,
        )
        raise ValueError("Paystack returned an unreadable response.")

    if not parsed.get("status"):
        logger.warning(
            "paystack_api_request_failed operation=%s http_status=%s failure_kind=provider_rejected",
            operation,
            http_status,
        )
        raise ValueError(parsed.get("message") or "Paystack could not process the request.")
    logger.info("paystack_api_request_succeeded operation=%s http_status=%s", operation, http_status)
    return parsed


def initialize_paystack_transaction(*, secret_key, email, amount, reference, callback_url, metadata=None):
    payload = {
        "email": email,
        "amount": paystack_amount_in_kobo(amount),
        "reference": reference,
        "callback_url": callback_url,
    }
    if metadata:
        payload["metadata"] = metadata
    logger.info("paystack_transaction_initialization_started reference=%s", reference)
    try:
        response = _paystack_request(secret_key, "transaction/initialize", payload=payload)
    except Exception as exc:
        logger.warning(
            "paystack_transaction_initialization_failed reference=%s error_type=%s",
            reference,
            type(exc).__name__,
        )
        raise
    data = response.get("data") or {}
    authorization_url = data.get("authorization_url") if isinstance(data, dict) else None
    try:
        parsed_url = urlsplit(authorization_url) if isinstance(authorization_url, str) else None
        valid_authorization_url = bool(
            parsed_url
            and parsed_url.scheme.lower() == "https"
            and parsed_url.hostname
            and parsed_url.hostname.lower() == "checkout.paystack.com"
            and parsed_url.port is None
            and not parsed_url.username
            and not parsed_url.password
            and parsed_url.path.strip("/")
            and not parsed_url.fragment
        )
    except ValueError:
        valid_authorization_url = False
    if not valid_authorization_url:
        logger.warning(
            "paystack_transaction_initialization_failed reference=%s failure_kind=invalid_authorization_url",
            reference,
        )
        raise ValueError("Paystack did not return an authorization URL.")
    data["authorization_url"] = authorization_url
    logger.info("paystack_transaction_initialization_succeeded reference=%s", reference)
    return data


def verify_paystack_transaction(secret_key, reference):
    logger.info("paystack_transaction_verification_started reference=%s", reference)
    try:
        response = _paystack_request(secret_key, f"transaction/verify/{quote(reference)}")
    except Exception as exc:
        logger.warning(
            "paystack_transaction_verification_failed reference=%s error_type=%s",
            reference,
            type(exc).__name__,
        )
        raise
    data = response.get("data") or {}
    if not isinstance(data, dict) or not data:
        logger.warning(
            "paystack_transaction_verification_failed reference=%s failure_kind=missing_verification_data",
            reference,
        )
        raise ValueError("Paystack did not return transaction details.")
    logger.info("paystack_transaction_verification_succeeded reference=%s", reference)
    return data


def subscription_paystack_credentials():
    """Return credentials configured by the Super Admin for subscriptions."""
    gateway = SubscriptionPaymentGateway.objects.filter(
        slug="subscription",
        is_active=True,
    ).first()
    if gateway and gateway.is_configured:
        return gateway.paystack_public_key, gateway.paystack_secret_key
    return "", ""


def subscription_paystack_secret_key():
    return subscription_paystack_credentials()[1]


def create_subscription_payment(*, institution, plan, email, callback_url):
    """Create a pending payment using the plan's own price and billing period."""
    if not plan.is_active or plan.is_trial or plan.price <= 0 or plan.duration_days <= 0:
        raise ValueError("That subscription plan is unavailable.")
    secret_key = subscription_paystack_secret_key()
    if not secret_key:
        raise ValueError("Subscription billing is not configured. Please contact Educonnect support.")
    subscription = institution.current_subscription
    if subscription is None:
        today = timezone.localdate()
        subscription = Subscription.objects.create(
            institution=institution,
            plan=plan,
            start_date=today,
            end_date=today,
            status=Subscription.Status.TRIAL,
            amount=Decimal("0.00"),
        )
    payment = Payment.objects.create(
        institution=institution,
        subscription=subscription,
        plan=plan,
        duration_days=plan.duration_days,
        reference=f"SUB-{timezone.now():%Y%m%d}-{uuid4().hex[:14].upper()}",
        amount=plan.price,
    )
    try:
        transaction_data = initialize_paystack_transaction(
            secret_key=secret_key,
            email=email,
            amount=payment.amount,
            reference=payment.reference,
            callback_url=callback_url,
            metadata={"kind": "subscription", "institution_id": institution.id, "payment_id": payment.id},
        )
    except ValueError as exc:
        exc.payment = payment
        raise
    return payment, transaction_data


def finalize_subscription_payment(*, reference, verification):
    """Atomically apply a verified payment once; safe for callbacks and retries."""
    expected_kobo = None
    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .select_related("institution", "subscription", "plan")
            .filter(reference=reference)
            .first()
        )
        if payment is None:
            return None, False
        if payment.status == Payment.Status.SUCCESS:
            return payment, False
        expected_kobo = paystack_amount_in_kobo(payment.amount)
        verified_amount = verification.get("amount")
        if (
            verification.get("status") != "success"
            or verification.get("reference") != payment.reference
            or verified_amount != expected_kobo
            or (verification.get("currency") or "").upper() != payment.currency.upper()
        ):
            payment.status = Payment.Status.FAILED
            payment.gateway_response = verification
            payment.save(update_fields=["status", "gateway_response", "updated_at"])
            AuditLog.objects.create(
                institution=payment.institution,
                action="subscription_payment_failed",
                description=f"Paystack verification failed for {payment.reference}.",
                object_type="Payment",
                object_id=str(payment.id),
            )
            return payment, False
        subscription = Subscription.objects.select_for_update().get(pk=payment.subscription_id)
        today = timezone.localdate()
        baseline = max(subscription.end_date, today)
        subscription.plan = payment.plan
        subscription.amount = payment.amount
        subscription.end_date = baseline + timedelta(days=payment.duration_days or payment.plan.duration_days)
        subscription.status = Subscription.Status.ACTIVE
        subscription.payment_reference = payment.reference
        subscription.save()

        # A new tenant is often provisioned as Pending.  A verified first
        # payment must activate it as well as its subscription, otherwise the
        # middleware would keep redirecting its administrator back to billing.
        institution = payment.institution
        if institution.status in {institution.Status.PENDING, institution.Status.EXPIRED}:
            institution.status = institution.Status.ACTIVE
            institution.save(update_fields=["status", "updated_at"])

        payment.status = Payment.Status.SUCCESS
        payment.paid_at = timezone.now()
        payment.gateway_response = verification
        payment.receipt_number = f"EC-{payment.paid_at:%Y%m%d}-{payment.id:06d}"
        payment.save(update_fields=["status", "paid_at", "gateway_response", "receipt_number", "updated_at"])
        SubscriptionEvent.objects.create(
            subscription=subscription,
            payment=payment,
            event_type="renewed",
            description=f"Subscription extended to {subscription.end_date:%d %B %Y}.",
        )
        AuditLog.objects.create(
            institution=payment.institution,
            action="subscription_renewed",
            description=f"Payment {payment.reference} extended the subscription.",
            object_type="Subscription",
            object_id=str(subscription.id),
        )
    return payment, True


def verify_and_finalize_subscription_payment(reference):
    secret_key = subscription_paystack_secret_key()
    if not secret_key:
        raise ValueError("Platform Paystack billing is not configured.")
    verification = verify_paystack_transaction(secret_key, reference)
    return finalize_subscription_payment(reference=reference, verification=verification)


def send_subscription_expiry_notifications(today=None):
    """Idempotent daily email notifications; scheduler-friendly and channel-ready."""
    today = today or timezone.localdate()
    sent = 0
    for subscription in Subscription.objects.select_related("institution", "plan").exclude(
        status__in=[Subscription.Status.CANCELLED, Subscription.Status.SUSPENDED]
    ):
        days = (subscription.end_date - today).days
        if days not in {60, 30, 7, 1, 0}:
            continue
        subject = "Your Educonnect subscription has expired" if days == 0 else (
            "Your Educonnect subscription expires tomorrow" if days == 1 else
            f"Your Educonnect subscription expires in {days} days"
        )
        with transaction.atomic():
            notification, _ = SubscriptionNotification.objects.select_for_update().get_or_create(
                subscription=subscription,
                days_before_expiry=days,
                channel="email",
                defaults={"status": SubscriptionNotification.Status.PENDING},
            )
            if notification.status == SubscriptionNotification.Status.SENT:
                continue
            notification.attempt_count += 1
            try:
                EmailMessage(
                    subject,
                    f"{subscription.institution.name}: {subject}.",
                    settings.DEFAULT_FROM_EMAIL,
                    [subscription.institution.email],
                ).send(fail_silently=False)
            except Exception as exc:
                notification.status = SubscriptionNotification.Status.FAILED
                notification.last_error = str(exc)[:1000]
                notification.save(update_fields=["status", "attempt_count", "last_error"])
                logger.exception(
                    "Subscription expiry notification failed for subscription %s.",
                    subscription.id,
                )
                continue
            notification.status = SubscriptionNotification.Status.SENT
            notification.sent_at = timezone.now()
            notification.last_error = ""
            notification.save(update_fields=["status", "attempt_count", "last_error", "sent_at"])
            sent += 1
    return sent


def default_material_status(material: CourseMaterial):
    return MaterialAccess.Status.AVAILABLE if material.is_free else MaterialAccess.Status.PENDING


def sync_material_access_for_material(material: CourseMaterial):
    registrations = StudentCourseRegistration.objects.filter(
        course=material.course, is_active=True,
    ).select_related("student")
    target_status = default_material_status(material)
    for registration in registrations:
        access, created = MaterialAccess.objects.get_or_create(
            material=material,
            student=registration.student,
            defaults={"status": target_status},
        )
        if not created and access.status != MaterialAccess.Status.BLOCKED:
            if material.is_free:
                access.status = MaterialAccess.Status.AVAILABLE
            elif access.status == MaterialAccess.Status.AVAILABLE:
                access.status = MaterialAccess.Status.PENDING
            access.save(update_fields=["status", "updated_at"])


def sync_material_access_for_registration(registration: StudentCourseRegistration):
    materials = CourseMaterial.objects.filter(course=registration.course)
    for material in materials:
        MaterialAccess.objects.get_or_create(
            material=material,
            student=registration.student,
            defaults={"status": default_material_status(material)},
        )


def _lecture_reminder_content(course, scheduled_for, recipient_kind):
    """Build the approved formal message from a complete course schedule."""
    start_time = timezone.localtime(scheduled_for, _course_timezone(course)).strftime("%I:%M %p")
    day_and_date = timezone.localtime(scheduled_for, _course_timezone(course)).strftime("%A, %d %B %Y")
    if recipient_kind == "lecturer":
        return (
            f"Lecture Reminder — {course.code}: {course.title}",
            "Dear Lecturer,\n\n"
            f"This is a reminder that your lecture for {course.title} ({course.code}) is "
            f"scheduled to commence at {start_time} on {day_and_date}, at {course.venue}.\n\n"
            "Please make the necessary preparations for the scheduled lecture.\n\n"
            "Thank you.\n\nEduConnect Academic Notifications",
        )
    return (
        f"Upcoming Lecture Reminder — {course.code}: {course.title}",
        "Dear Student,\n\n"
        f"This is a reminder that your lecture for {course.title} ({course.code}) is "
        f"scheduled to commence at {start_time} on {day_and_date}, at {course.venue}.\n\n"
        "Please prepare accordingly and arrive at the venue on time.\n\n"
        "Thank you.\n\nEduConnect Academic Notifications",
    )


def _increment_reason(summary, reason):
    summary["skipped"] += 1
    summary["skipped_reasons"][reason] = summary["skipped_reasons"].get(reason, 0) + 1


def _vapid_subject():
    """Return a valid VAPID contact subject without changing the configured value."""
    contact = settings.VAPID_CLAIMS_EMAIL.strip()
    return contact if contact.lower().startswith("mailto:") else f"mailto:{contact}"


def _webpush_client():
    """Delay the optional pywebpush import until a browser push is attempted."""
    from pywebpush import WebPushException, webpush
    return WebPushException, webpush


def send_course_reminder_email(course: Course, recipients, scheduled_for, recipient_kinds):
    """Send one private, role-specific email per recipient.

    A provider can accept one message and reject another, so the result is
    reported per recipient for safe retries by ``CourseReminderDispatch``.
    """
    delivered_recipient_ids, failed_recipient_ids, skipped_recipient_ids = set(), set(), set()
    errors = []
    connection = get_connection(fail_silently=False)
    try:
        connection.open()
        for recipient in recipients:
            recipient_email = (recipient.email or "").strip()
            if not recipient_email:
                skipped_recipient_ids.add(recipient.id)
                continue
            subject, body = _lecture_reminder_content(
                course, scheduled_for, recipient_kinds[recipient.id]
            )
            try:
                sent_messages = connection.send_messages([
                    EmailMessage(
                        subject=subject,
                        body=body,
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        to=[recipient_email],
                        connection=connection,
                    )
                ])
                if sent_messages != 1:
                    raise RuntimeError("The email provider did not accept the lecture reminder.")
                delivered_recipient_ids.add(recipient.id)
            except Exception as exc:
                failed_recipient_ids.add(recipient.id)
                errors.append(exc.__class__.__name__)
                logger.error(
                    "Lecture reminder email rejected. course_id=%s recipient_id=%s error_type=%s",
                    course.pk, recipient.pk, exc.__class__.__name__,
                )
    except Exception as exc:
        error = exc.__class__.__name__
        failed_recipient_ids.update(recipient.id for recipient in recipients)
        errors.append(error)
        logger.error(
            "Lecture reminder SMTP connection failed. course_id=%s error_type=%s",
            course.pk, exc.__class__.__name__,
        )
    finally:
        connection.close()
    return delivered_recipient_ids, failed_recipient_ids, skipped_recipient_ids, "; ".join(errors)[:255]


def deliver_notification(notification):
    # Messages and reminders must follow the active academic registration.
    # Without this, a lecturer could unintentionally notify students from a
    # previous session simply because their historical registration remains.
    current_session = AcademicSession.all_objects.filter(
        institution_id=notification.institution_id,
        is_current=True,
    ).first()
    students = User.all_objects.filter(
        institution_id=notification.institution_id,
        role=User.Role.STUDENT,
        is_active=True,
    )
    department_ids = list(notification.departments.values_list("id", flat=True))
    course_ids = list(notification.courses.values_list("id", flat=True))

    if department_ids:
        students = students.filter(department_id__in=department_ids)
    if notification.level:
        students = students.filter(level=notification.level)
    if course_ids:
        students = students.filter(
            student_registrations__course_id__in=course_ids,
            student_registrations__is_active=True,
        ).distinct()
    elif notification.sender.has_role(User.Role.LECTURER) and not department_ids:
        students = students.filter(
            Q(student_registrations__course__lecturer=notification.sender)
            | Q(
                student_registrations__course__lecturer_registrations__lecturer=notification.sender,
                student_registrations__course__lecturer_registrations__is_active=True,
            ),
            student_registrations__is_active=True,
        ).distinct()
    if current_session:
        students = students.filter(student_registrations__session=current_session)

    recipient_rows = []
    for student_id in students.distinct().values_list("id", flat=True).iterator(
        chunk_size=NOTIFICATION_RECIPIENT_BATCH_SIZE
    ):
        recipient_rows.append(
            NotificationRecipient(institution=notification.institution, notification=notification, student_id=student_id)
        )
        if len(recipient_rows) == NOTIFICATION_RECIPIENT_BATCH_SIZE:
            NotificationRecipient.objects.bulk_create(recipient_rows, ignore_conflicts=True)
            recipient_rows.clear()
    if recipient_rows:
        NotificationRecipient.objects.bulk_create(recipient_rows, ignore_conflicts=True)

    alert_rows = []
    browser_alert_recipients = NotificationRecipient.objects.filter(
        notification=notification,
        student__browser_alerts_enabled=True,
    ).values_list("id", "student_id")
    for recipient_id, student_id in browser_alert_recipients.iterator(
        chunk_size=NOTIFICATION_RECIPIENT_BATCH_SIZE
    ):
        alert_rows.append(
            UserAlert(
                institution=notification.institution,
                recipient_id=student_id,
                alert_type=UserAlert.AlertType.MESSAGE,
                title=notification.subject,
                body=notification.body,
                target_url=reverse("portal:student-messages") + f"?open={recipient_id}",
                dedupe_key=f"message:{notification.id}:{student_id}",
            )
        )
        if len(alert_rows) == NOTIFICATION_RECIPIENT_BATCH_SIZE:
            UserAlert.objects.bulk_create(alert_rows, ignore_conflicts=True)
            alert_rows.clear()
    if alert_rows:
        UserAlert.objects.bulk_create(alert_rows, ignore_conflicts=True)

    transaction.on_commit(lambda: send_notification_browser_pushes(notification))
    # Every message delivered to a student inbox is also emailed to that
    # student, including messages sent by HODs and other authorised staff.
    transaction.on_commit(lambda: send_lecturer_message_emails(notification))


def send_lecturer_message_emails(notification):
    """Email every delivered student message without exposing recipient emails."""
    recipient_emails = (
        NotificationRecipient.objects.filter(
            notification=notification,
            student__is_active=True,
        )
        .exclude(student__email="")
        .values_list("student__email", flat=True)
        .iterator(chunk_size=NOTIFICATION_EMAIL_BATCH_SIZE)
    )
    emails = []
    seen = set()
    for raw_email in recipient_emails:
        email = raw_email.strip()
        email_key = email.lower()
        if email and email_key not in seen:
            seen.add(email_key)
            emails.append(email)

    if not emails:
        return

    message = (
        f"{notification.sender.full_name} sent you a message through EduConnect.\n\n"
        f"Subject: {notification.subject}\n\n"
        f"{notification.body}\n\n"
        "Sign in to EduConnect to view the message in your inbox."
    )
    # A swallowed SMTP exception makes a lecturer-facing success banner look
    # trustworthy while no recipient ever receives the message.
    connection = get_connection(fail_silently=False)
    email_messages = [
        EmailMessage(
            subject=f"EduConnect: {notification.subject}",
            body=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            bcc=emails[start : start + NOTIFICATION_EMAIL_BATCH_SIZE],
            connection=connection,
        )
        for start in range(0, len(emails), NOTIFICATION_EMAIL_BATCH_SIZE)
    ]
    try:
        sent_messages = connection.send_messages(email_messages)
        if sent_messages != len(email_messages):
            raise RuntimeError("The email provider did not accept every lecturer message batch.")
    except Exception:
        logger.exception("Unable to send lecturer message email for notification %s", notification.pk)
    finally:
        connection.close()


def send_browser_push_alert(alert):
    """Send one durable alert to the recipient's registered devices.

    The UserAlert record is retained when push is unavailable or rejected, so
    recipients still see the notification from the in-app alert feed.
    """
    result = {"attempted": 0, "accepted": 0, "failed": 0, "skipped": 0, "skipped_reasons": {}}
    if not alert.recipient.browser_alerts_enabled:
        _increment_reason(result, "browser_alerts_disabled")
        return result
    if not all((settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY, settings.VAPID_CLAIMS_EMAIL)):
        _increment_reason(result, "vapid_not_configured")
        return result
    try:
        WebPushException, webpush = _webpush_client()
    except ImportError:
        logger.error("Browser push is configured but pywebpush is not installed.")
        _increment_reason(result, "push_library_unavailable")
        return result

    payload = json.dumps({
        "title": alert.title,
        "body": alert.body,
        "target_url": alert.target_url,
        "tag": f"educonnect-{alert.id}",
    })
    subscriptions = PushSubscription.all_objects.filter(
        institution_id=alert.institution_id,
        recipient=alert.recipient,
    )
    if not subscriptions.exists():
        _increment_reason(result, "no_active_subscription")
        return result
    for subscription in subscriptions.iterator():
        result["attempted"] += 1
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": _vapid_subject()},
                content_encoding="aes128gcm",
            )
            result["accepted"] += 1
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                subscription.delete()
                logger.info(
                    "Removed expired browser push subscription. alert_id=%s status_code=%s",
                    alert.pk, status_code,
                )
            else:
                logger.warning(
                    "Browser push rejected. alert_id=%s status_code=%s error_type=%s",
                    alert.pk, status_code, exc.__class__.__name__,
                )
            result["failed"] += 1
        except Exception as exc:
            result["failed"] += 1
            logger.error(
                "Browser push failed. alert_id=%s error_type=%s",
                alert.pk, exc.__class__.__name__,
            )
    if result["accepted"]:
        UserAlert.all_objects.filter(pk=alert.pk, browser_delivered_at__isnull=True).update(
            browser_delivered_at=timezone.now(),
            updated_at=timezone.now(),
        )
    return result


def send_notification_browser_pushes(notification):
    """Push lecturer messages after their transaction has committed."""
    dedupe_prefix = f"message:{notification.id}:"
    alerts = UserAlert.all_objects.filter(
        institution_id=notification.institution_id,
        dedupe_key__startswith=dedupe_prefix,
        browser_delivered_at__isnull=True,
    ).select_related("recipient")
    for alert in alerts.iterator():
        send_browser_push_alert(alert)


def create_alert(recipient, alert_type, title, body, target_url, dedupe_key, deliver_browser_push=True):
    if not recipient or not recipient.has_role(User.Role.STUDENT, User.Role.LECTURER):
        return None
    alert, created = UserAlert.objects.get_or_create(
        recipient=recipient,
        dedupe_key=dedupe_key,
        defaults={
            "institution": recipient.institution,
            "alert_type": alert_type,
            "title": title,
            "body": body,
            "target_url": target_url,
        },
    )
    if created and deliver_browser_push:
        send_browser_push_alert(alert)
    return alert


def _course_timezone(course):
    timezone_name = getattr(getattr(course, "department", None), "institution", None)
    timezone_name = getattr(timezone_name, "timezone", "") or settings.TIME_ZONE
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid institution timezone %r for course %s; using %s.", timezone_name, course.pk, settings.TIME_ZONE)
        return timezone.get_current_timezone()


def _course_occurrence_datetime(course, now, course_timezone=None):
    if not course.schedule_day or not course.schedule_time:
        return None
    weekday_lookup = {
        "Monday": 0,
        "Tuesday": 1,
        "Wednesday": 2,
        "Thursday": 3,
        "Friday": 4,
        "Saturday": 5,
        "Sunday": 6,
    }
    weekday = weekday_lookup.get(course.schedule_day)
    if weekday is None:
        return None
    days_ahead = weekday - now.weekday()
    target_date = now.date() + timedelta(days=days_ahead)
    naive = datetime.combine(target_date, course.schedule_time)
    return timezone.make_aware(naive, course_timezone or timezone.get_current_timezone())


def _claim_reminder_delivery(course, recipient, scheduled_for, channel, claimed_at):
    """Claim one recipient/channel delivery without holding a DB lock for SMTP.

    A stale claim is recoverable after a worker crash.  SMTP acceptance followed
    by a process crash cannot be made exactly-once, so that rare case remains a
    provider-level duplicate risk and is intentionally documented.
    """
    stale_before = claimed_at - timedelta(minutes=REMINDER_DELIVERY_CLAIM_MINUTES)
    with transaction.atomic():
        dispatch, _ = CourseReminderDispatch.all_objects.select_for_update().get_or_create(
            course=course,
            recipient=recipient,
            scheduled_for=scheduled_for,
            channel=channel,
            defaults={"institution": course.institution},
        )
        if dispatch.institution_id != course.institution_id:
            logger.error("Skipping reminder dispatch %s with an invalid tenant binding.", dispatch.pk)
            return None
        if dispatch.delivered_at is not None:
            return None
        if dispatch.delivery_claimed_at and dispatch.delivery_claimed_at > stale_before:
            return None
        dispatch.delivery_claimed_at = claimed_at
        dispatch.last_attempt_at = claimed_at
        dispatch.attempt_count = F("attempt_count") + 1
        dispatch.save(update_fields=["delivery_claimed_at", "last_attempt_at", "attempt_count", "updated_at"])
        return dispatch.pk


def _finish_reminder_delivery(dispatch_ids, delivered_ids, failed_ids, attempted_at, error_message=""):
    if delivered_ids:
        CourseReminderDispatch.all_objects.filter(
            pk__in=[dispatch_ids[recipient_id] for recipient_id in delivered_ids]
        ).update(
            delivered_at=attempted_at,
            delivery_claimed_at=None,
            last_attempt_at=attempted_at,
            last_error="",
            updated_at=attempted_at,
        )
    if failed_ids:
        CourseReminderDispatch.all_objects.filter(
            pk__in=[dispatch_ids[recipient_id] for recipient_id in failed_ids]
        ).update(
            delivery_claimed_at=None,
            last_attempt_at=attempted_at,
            last_error=error_message[:255],
            updated_at=attempted_at,
        )


def dispatch_due_course_reminders(now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.get_current_timezone())
    summary = {
        "executed_at": now.isoformat(),
        "timezone": str(now.tzinfo),
        "candidate_courses": 0,
        "courses_due": 0,
        "courses_skipped": {},
        "eligible_recipients": {"lecturers": 0, "students": 0},
        "email": {"attempted": 0, "accepted": 0, "failed": 0, "skipped": 0, "skipped_reasons": {}},
        "browser_push": {"attempted": 0, "accepted": 0, "failed": 0, "skipped": 0, "skipped_reasons": {}},
    }

    def skip_course(course, reason):
        summary["courses_skipped"][reason] = summary["courses_skipped"].get(reason, 0) + 1
        logger.info(
            "Lecture reminder course skipped. course_id=%s institution_id=%s reason=%s",
            course.pk, course.institution_id, reason,
        )

    def merge_push_result(result):
        for key in ("attempted", "accepted", "failed", "skipped"):
            summary["browser_push"][key] += result[key]
        for reason, count in result["skipped_reasons"].items():
            summary["browser_push"]["skipped_reasons"][reason] = (
                summary["browser_push"]["skipped_reasons"].get(reason, 0) + count
            )

    # Institutions may use different timezones.  Filter by their local day
    # inside the loop rather than the server's day, so a midnight boundary
    # never skips an otherwise due reminder.
    candidate_courses = Course.all_objects.select_related(
        "lecturer", "institution", "academic_session", "department__institution"
    ).filter(
        institution__isnull=False,
    )
    for course in candidate_courses:
        summary["candidate_courses"] += 1
        if course.institution.status != "active":
            skip_course(course, "institution_inactive")
            continue
        if not course.is_available:
            skip_course(course, "course_unavailable")
            continue
        if not course.academic_session_id or not course.academic_session.is_current:
            skip_course(course, "academic_session_not_current")
            continue
        if not all((course.code, course.title, course.venue, course.schedule_day, course.schedule_time)):
            skip_course(course, "incomplete_schedule")
            continue
        local_now = now.astimezone(_course_timezone(course))
        if course.schedule_day != local_now.strftime("%A"):
            skip_course(course, "weekday_mismatch")
            continue
        class_starts_at = _course_occurrence_datetime(course, local_now, local_now.tzinfo)
        if not class_starts_at:
            skip_course(course, "invalid_schedule")
            continue
        minutes_until_start = (class_starts_at - local_now).total_seconds() / 60
        if not (
            LECTURE_REMINDER_LEAD_MINUTES - LECTURE_REMINDER_TOLERANCE_MINUTES
            <= minutes_until_start
            <= LECTURE_REMINDER_LEAD_MINUTES + LECTURE_REMINDER_TOLERANCE_MINUTES
        ):
            skip_course(course, "outside_reminder_window")
            continue
        summary["courses_due"] += 1

        recipients = {}
        if course.lecturer_id and course.lecturer.institution_id == course.institution_id and course.lecturer.is_active and course.lecturer.is_approved:
            recipients[course.lecturer_id] = (course.lecturer, "lecturer")
        for allocation in course.lecturer_registrations.select_related("lecturer").filter(
            institution_id=course.institution_id, is_active=True,
            lecturer__institution_id=course.institution_id, lecturer__is_active=True,
            lecturer__is_approved=True,
        ):
            recipients[allocation.lecturer_id] = (allocation.lecturer, "lecturer")
        students = User.all_objects.filter(
            institution_id=course.institution_id,
            role=User.Role.STUDENT,
            is_active=True,
            student_registrations__institution_id=course.institution_id,
            student_registrations__course=course,
            student_registrations__session=course.academic_session,
            student_registrations__session__is_current=True,
            student_registrations__is_active=True,
        ).distinct()
        for student in students:
            recipients.setdefault(student.id, (student, "student"))

        for _, recipient_kind in recipients.values():
            summary["eligible_recipients"][f"{recipient_kind}s"] += 1

        email_targets, email_dispatches, recipient_kinds = [], {}, {}
        for recipient, recipient_kind in recipients.values():
            recipient_email = (recipient.email or "").strip()
            if not recipient.email_class_reminders:
                _increment_reason(summary["email"], "preference_disabled")
            elif not recipient_email:
                _increment_reason(summary["email"], "missing_email_address")
            else:
                dispatch_id = _claim_reminder_delivery(
                    course, recipient, class_starts_at, CourseReminderDispatch.Channel.EMAIL, timezone.now()
                )
                if dispatch_id:
                    email_targets.append(recipient)
                    email_dispatches[recipient.id] = dispatch_id
                    recipient_kinds[recipient.id] = recipient_kind
                else:
                    _increment_reason(summary["email"], "already_delivered_or_claimed")
            if not recipient.browser_alerts_enabled:
                _increment_reason(summary["browser_push"], "browser_alerts_disabled")
            elif not recipient.class_reminder_alerts_enabled:
                _increment_reason(summary["browser_push"], "class_reminder_preference_disabled")
            else:
                dispatch_id = _claim_reminder_delivery(
                    course, recipient, class_starts_at, CourseReminderDispatch.Channel.ALERT, timezone.now()
                )
                if dispatch_id:
                    try:
                        title, body = _lecture_reminder_content(course, class_starts_at, recipient_kind)
                        alert = create_alert(
                            recipient=recipient,
                            alert_type=UserAlert.AlertType.CLASS_REMINDER,
                            title=title,
                            body=body,
                            target_url=reverse("portal:dashboard"),
                            dedupe_key=f"class-reminder:{course.id}:{recipient.id}:{class_starts_at.isoformat()}",
                            deliver_browser_push=False,
                        )
                    except Exception as exc:
                        _finish_reminder_delivery(
                            {recipient.id: dispatch_id}, set(), {recipient.id}, timezone.now(), exc.__class__.__name__
                        )
                        summary["browser_push"]["failed"] += 1
                        logger.exception("Unable to create lecture reminder alert for course %s", course.pk)
                    else:
                        push_result = send_browser_push_alert(alert)
                        merge_push_result(push_result)
                        if push_result["accepted"]:
                            _finish_reminder_delivery({recipient.id: dispatch_id}, {recipient.id}, set(), timezone.now())
                        else:
                            _finish_reminder_delivery(
                                {recipient.id: dispatch_id}, set(), {recipient.id}, timezone.now(),
                                "Browser push was not accepted by a subscription provider.",
                            )
                else:
                    _increment_reason(summary["browser_push"], "already_delivered_or_claimed")

        if email_targets:
            summary["email"]["attempted"] += len(email_targets)
            delivered_ids, failed_ids, skipped_ids, error_message = send_course_reminder_email(
                course, email_targets, class_starts_at, recipient_kinds
            )
            _finish_reminder_delivery(
                email_dispatches, delivered_ids, failed_ids | skipped_ids, timezone.now(), error_message
            )
            summary["email"]["accepted"] += len(delivered_ids)
            summary["email"]["failed"] += len(failed_ids)
            for _ in skipped_ids:
                _increment_reason(summary["email"], "missing_email_address")
    logger.info("Lecture reminder execution completed. summary=%s", json.dumps(summary, sort_keys=True))
    return summary


def process_due_course_reminders():
    try:
        with transaction.atomic():
            dispatch_due_course_reminders()
    except Exception:
        return


def ensure_default_admin_user():
    username = settings.BOOTSTRAP_ADMIN_USERNAME
    password = settings.BOOTSTRAP_ADMIN_PASSWORD
    if not username or not password:
        return None

    admin_user, created = User.all_objects.get_or_create(
        username=username,
        defaults={
            "role": User.Role.ADMIN,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
            "is_approved": True,
            "email": settings.BOOTSTRAP_ADMIN_EMAIL,
        },
    )

    updates = []
    if created:
        admin_user.set_password(password)
        updates.append("password")
    if admin_user.role != User.Role.ADMIN:
        admin_user.role = User.Role.ADMIN
        updates.append("role")
    if not admin_user.is_staff:
        admin_user.is_staff = True
        updates.append("is_staff")
    if not admin_user.is_superuser:
        admin_user.is_superuser = True
        updates.append("is_superuser")
    if not admin_user.is_active:
        admin_user.is_active = True
        updates.append("is_active")
    if not admin_user.is_approved:
        admin_user.is_approved = True
        updates.append("is_approved")
    if updates:
        admin_user.save()
    return admin_user
