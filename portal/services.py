import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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
    SubscriptionPlanDuration,
    SubscriptionPaymentGateway,
    SubscriptionEvent,
    SubscriptionNotification,
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
# Imported lecturers and institution administrators receive this same
# temporary credential. They can replace it through the email verification
# password-reset flow.
TEMPORARY_ACCOUNT_PASSWORD = "educonnect"
FREE_TRIAL_PLAN_NAME = "Six-month free trial"
FREE_TRIAL_DURATION_DAYS = 183
logger = logging.getLogger(__name__)


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
    normalized = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(normalized * 100)


def _paystack_request(secret_key, path, payload=None):
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
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            payload = {}
        message = payload.get("message")
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
        raise ValueError("Unable to reach Paystack right now. Please try again shortly.") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("Paystack returned an unreadable response.") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Paystack returned an unreadable response.")

    if not parsed.get("status"):
        raise ValueError(parsed.get("message") or "Paystack could not process the request.")
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
    response = _paystack_request(secret_key, "transaction/initialize", payload=payload)
    data = response.get("data") or {}
    if not data.get("authorization_url"):
        raise ValueError("Paystack did not return an authorization URL.")
    return data


def verify_paystack_transaction(secret_key, reference):
    response = _paystack_request(secret_key, f"transaction/verify/{quote(reference)}")
    data = response.get("data") or {}
    if not isinstance(data, dict) or not data:
        raise ValueError("Paystack did not return transaction details.")
    return data


def subscription_paystack_secret_key():
    """Use the super-admin gateway, with environment configuration as fallback."""
    gateway = SubscriptionPaymentGateway.objects.filter(
        slug="subscription",
        is_active=True,
    ).first()
    return (gateway.paystack_secret_key if gateway and gateway.is_configured else settings.PAYSTACK_SECRET_KEY)


def create_subscription_payment(*, institution, plan_duration, email, callback_url):
    """Create a pending payment for the selected plan duration and its amount."""
    plan = plan_duration.plan
    if (
        not plan.is_active
        or plan.is_trial
        or not plan_duration.is_active
        or plan_duration.duration_days not in {183, 365, 730, 1825}
    ):
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
        duration_days=plan_duration.duration_days,
        reference=f"SUB-{timezone.now():%Y%m%d}-{uuid4().hex[:14].upper()}",
        amount=plan_duration.price,
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
    except Exception:
        # Keep the auditable pending attempt, but do not imply checkout started.
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
        expected_kobo = paystack_amount_in_kobo(payment.amount)
        verified_amount = verification.get("amount")
        if (
            verification.get("status") != "success"
            or verification.get("reference") != payment.reference
            or verified_amount != expected_kobo
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
        if payment.status == Payment.Status.SUCCESS:
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


def send_course_reminder_email(course: Course, recipients, scheduled_for, recipient_kinds):
    """Send one private, role-specific email per recipient.

    A provider can accept one message and reject another, so the result is
    reported per recipient for safe retries by ``CourseReminderDispatch``.
    """
    delivered_recipient_ids, failed_recipient_ids = set(), set()
    errors = []
    connection = get_connection(fail_silently=False)
    try:
        connection.open()
        for recipient in recipients:
            if not recipient.email:
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
                        to=[recipient.email.strip()],
                        connection=connection,
                    )
                ])
                if sent_messages != 1:
                    raise RuntimeError("The email provider did not accept the lecture reminder.")
                delivered_recipient_ids.add(recipient.id)
            except Exception as exc:
                failed_recipient_ids.add(recipient.id)
                errors.append(str(exc) or exc.__class__.__name__)
                logger.exception("Unable to send lecture reminder email for course %s", course.pk)
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        failed_recipient_ids.update(recipient.id for recipient in recipients)
        errors.append(error)
        logger.exception("Unable to open lecture reminder email connection for course %s", course.pk)
    finally:
        connection.close()
    return delivered_recipient_ids, failed_recipient_ids, "; ".join(errors)[:255]


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
    if notification.sender.has_role(User.Role.LECTURER):
        transaction.on_commit(lambda: send_lecturer_message_emails(notification))


def send_lecturer_message_emails(notification):
    """Send a lecturer announcement to every recipient without exposing student emails."""
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
    if not alert.recipient.browser_alerts_enabled or not all(
        (settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY, settings.VAPID_CLAIMS_EMAIL)
    ):
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.error("Browser push is configured but pywebpush is not installed.")
        return 0

    payload = json.dumps({
        "title": alert.title,
        "body": alert.body,
        "target_url": alert.target_url,
        "tag": f"educonnect-{alert.id}",
    })
    delivered = 0
    for subscription in PushSubscription.objects.filter(recipient=alert.recipient).iterator():
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{settings.VAPID_CLAIMS_EMAIL}"},
                content_encoding="aes128gcm",
            )
            delivered += 1
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                subscription.delete()
            else:
                logger.warning("Unable to send browser push for alert %s: %s", alert.pk, exc)
        except Exception:
            logger.exception("Unable to send browser push for alert %s", alert.pk)
    if delivered:
        UserAlert.objects.filter(pk=alert.pk, browser_delivered_at__isnull=True).update(
            browser_delivered_at=timezone.now(),
            updated_at=timezone.now(),
        )
    return delivered


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


def create_alert(recipient, alert_type, title, body, target_url, dedupe_key):
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
    if created:
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
    summary = {"courses_due": 0, "email_delivered": 0, "email_failed": 0, "alerts_delivered": 0}
    # Institutions may use different timezones.  Filter by their local day
    # inside the loop rather than the server's day, so a midnight boundary
    # never skips an otherwise due reminder.
    candidate_courses = Course.all_objects.select_related("lecturer", "institution").filter(
        institution__isnull=False,
        institution__status="active",
        schedule_time__isnull=False,
        is_available=True,
    ).filter(academic_session__is_current=True).exclude(
        schedule_day="",
    ).exclude(code="").exclude(title="").exclude(venue="")
    for course in candidate_courses:
        local_now = now.astimezone(_course_timezone(course))
        if course.schedule_day != local_now.strftime("%A"):
            continue
        class_starts_at = _course_occurrence_datetime(course, local_now, local_now.tzinfo)
        if not class_starts_at:
            continue
        minutes_until_start = (class_starts_at - local_now).total_seconds() / 60
        if not (
            LECTURE_REMINDER_LEAD_MINUTES - LECTURE_REMINDER_TOLERANCE_MINUTES
            <= minutes_until_start
            <= LECTURE_REMINDER_LEAD_MINUTES + LECTURE_REMINDER_TOLERANCE_MINUTES
        ):
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

        email_targets, email_dispatches, recipient_kinds = [], {}, {}
        for recipient, recipient_kind in recipients.values():
            if recipient.email_class_reminders and recipient.email:
                dispatch_id = _claim_reminder_delivery(
                    course, recipient, class_starts_at, CourseReminderDispatch.Channel.EMAIL, timezone.now()
                )
                if dispatch_id:
                    email_targets.append(recipient)
                    email_dispatches[recipient.id] = dispatch_id
                    recipient_kinds[recipient.id] = recipient_kind
            if recipient.browser_alerts_enabled and recipient.class_reminder_alerts_enabled:
                dispatch_id = _claim_reminder_delivery(
                    course, recipient, class_starts_at, CourseReminderDispatch.Channel.ALERT, timezone.now()
                )
                if dispatch_id:
                    try:
                        title, body = _lecture_reminder_content(course, class_starts_at, recipient_kind)
                        create_alert(
                            recipient=recipient,
                            alert_type=UserAlert.AlertType.CLASS_REMINDER,
                            title=title,
                            body=body,
                            target_url=reverse("portal:dashboard"),
                            dedupe_key=f"class-reminder:{course.id}:{recipient.id}:{class_starts_at.isoformat()}",
                        )
                    except Exception as exc:
                        _finish_reminder_delivery({recipient.id: dispatch_id}, set(), {recipient.id}, timezone.now(), str(exc))
                        logger.exception("Unable to create lecture reminder alert for course %s", course.pk)
                    else:
                        _finish_reminder_delivery({recipient.id: dispatch_id}, {recipient.id}, set(), timezone.now())
                        summary["alerts_delivered"] += 1

        if email_targets:
            delivered_ids, failed_ids, error_message = send_course_reminder_email(
                course, email_targets, class_starts_at, recipient_kinds
            )
            _finish_reminder_delivery(email_dispatches, delivered_ids, failed_ids, timezone.now(), error_message)
            summary["email_delivered"] += len(delivered_ids)
            summary["email_failed"] += len(failed_ids)
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
