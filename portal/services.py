import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from .models import (
    Course,
    CourseMaterial,
    CourseReminderDispatch,
    MaterialAccess,
    NotificationRecipient,
    StudentCourseRegistration,
    User,
    UserAlert,
)

PAYSTACK_API_BASE = "https://api.paystack.co"
PAYSTACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
NOTIFICATION_RECIPIENT_BATCH_SIZE = 500
NOTIFICATION_EMAIL_BATCH_SIZE = 100
logger = logging.getLogger(__name__)


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
    except URLError as exc:
        raise ValueError("Unable to reach Paystack right now. Please try again shortly.") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("Paystack returned an unreadable response.") from exc

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
    if not data:
        raise ValueError("Paystack did not return transaction details.")
    return data


def default_material_status(material: CourseMaterial):
    return MaterialAccess.Status.AVAILABLE if material.is_free else MaterialAccess.Status.PENDING


def sync_material_access_for_material(material: CourseMaterial):
    registrations = StudentCourseRegistration.objects.filter(course=material.course).select_related("student")
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


def send_course_reminder_email(course: Course, recipients=None):
    recipients_by_email = {}
    if recipients is None:
        recipients = []
        if course.lecturer_id:
            recipients.append(course.lecturer)
        recipients.extend(
            User.objects.filter(
                role=User.Role.STUDENT,
                student_registrations__course=course,
            ).distinct()
        )

    for user in recipients:
        if not user or user.role not in {User.Role.STUDENT, User.Role.LECTURER} or not user.email:
            continue
        email = user.email.strip()
        if email:
            recipients_by_email.setdefault(email.lower(), {"email": email, "recipient_ids": set()})["recipient_ids"].add(user.id)

    if not recipients_by_email:
        return set(), set(), ""

    schedule_day = course.schedule_day or "the scheduled day to be announced"
    schedule_time = course.schedule_time.strftime("%I:%M %p") if course.schedule_time else "the scheduled time to be announced"
    venue = course.venue or "the announced venue"
    message = (
        f"This is a class reminder for {course.title} ({course.code}). "
        f"Please note that the class holds on {schedule_day} at {schedule_time} in {venue}."
    )
    recipient_groups = list(recipients_by_email.values())
    delivered_recipient_ids = set()
    failed_recipient_ids = set()
    error_message = ""
    connection = get_connection(fail_silently=False)

    try:
        connection.open()
        for start in range(0, len(recipient_groups), NOTIFICATION_EMAIL_BATCH_SIZE):
            batch = recipient_groups[start : start + NOTIFICATION_EMAIL_BATCH_SIZE]
            sent_messages = connection.send_messages(
                [
                    EmailMessage(
                        subject=f"Class Reminder: {course.code}",
                        body=message,
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        bcc=[item["email"] for item in batch],
                        connection=connection,
                    )
                ]
            )
            if sent_messages != 1:
                raise RuntimeError("The email provider did not accept the class reminder batch.")
            for item in batch:
                delivered_recipient_ids.update(item["recipient_ids"])
    except Exception as exc:
        error_message = str(exc) or exc.__class__.__name__
        logger.exception("Unable to send class reminder email for course %s", course.code)
        for item in recipient_groups:
            failed_recipient_ids.update(item["recipient_ids"])
        failed_recipient_ids.difference_update(delivered_recipient_ids)
    finally:
        connection.close()

    return delivered_recipient_ids, failed_recipient_ids, error_message


def deliver_notification(notification):
    students = User.objects.filter(role=User.Role.STUDENT, is_active=True)
    department_ids = list(notification.departments.values_list("id", flat=True))
    course_ids = list(notification.courses.values_list("id", flat=True))

    if department_ids:
        students = students.filter(department_id__in=department_ids)
    if notification.level:
        students = students.filter(level=notification.level)
    if course_ids:
        students = students.filter(student_registrations__course_id__in=course_ids).distinct()
    elif notification.sender.role == User.Role.LECTURER and not department_ids:
        students = students.filter(student_registrations__course__lecturer=notification.sender).distinct()

    recipient_rows = []
    for student_id in students.distinct().values_list("id", flat=True).iterator(
        chunk_size=NOTIFICATION_RECIPIENT_BATCH_SIZE
    ):
        recipient_rows.append(NotificationRecipient(notification=notification, student_id=student_id))
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

    if notification.sender.role == User.Role.LECTURER:
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
    connection = get_connection(fail_silently=True)
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
    connection.send_messages(email_messages)


def create_alert(recipient, alert_type, title, body, target_url, dedupe_key):
    if not recipient or recipient.role not in {User.Role.STUDENT, User.Role.LECTURER}:
        return None
    UserAlert.objects.get_or_create(
        recipient=recipient,
        dedupe_key=dedupe_key,
        defaults={
            "alert_type": alert_type,
            "title": title,
            "body": body,
            "target_url": target_url,
        },
    )


def _course_occurrence_datetime(course, now):
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
    return timezone.make_aware(naive, timezone.get_current_timezone())


def dispatch_due_course_reminders(now=None):
    now = now or timezone.localtime()
    candidate_courses = Course.objects.select_related("lecturer", "department").filter(
        schedule_day=now.strftime("%A"),
        schedule_time__isnull=False,
    )
    for course in candidate_courses:
        class_starts_at = _course_occurrence_datetime(course, now)
        if not class_starts_at:
            continue
        reminder_at = class_starts_at - timedelta(minutes=30)
        if not (reminder_at <= now < class_starts_at):
            continue

        recipients = []
        if course.lecturer_id:
            recipients.append(course.lecturer)
        recipients.extend(
            User.objects.filter(
                role=User.Role.STUDENT,
                student_registrations__course=course,
            ).distinct()
        )

        email_targets = []
        email_dispatches = {}
        for recipient in recipients:
            if not recipient or recipient.role not in {User.Role.STUDENT, User.Role.LECTURER}:
                continue
            if recipient.email_class_reminders and recipient.email:
                reminder_dispatch, _ = CourseReminderDispatch.objects.get_or_create(
                    course=course,
                    recipient=recipient,
                    scheduled_for=class_starts_at,
                    channel=CourseReminderDispatch.Channel.EMAIL,
                )
                if reminder_dispatch.delivered_at is None:
                    email_targets.append(recipient)
                    email_dispatches[recipient.id] = reminder_dispatch.id
            if recipient.browser_alerts_enabled and recipient.class_reminder_alerts_enabled:
                alert_dispatch, created = CourseReminderDispatch.objects.get_or_create(
                    course=course,
                    recipient=recipient,
                    scheduled_for=class_starts_at,
                    channel=CourseReminderDispatch.Channel.ALERT,
                )
                if created:
                    create_alert(
                        recipient=recipient,
                        alert_type=UserAlert.AlertType.CLASS_REMINDER,
                        title=f"Class starts in 30 minutes: {course.code}",
                        body=(
                            f"{course.title} starts at {course.schedule_time.strftime('%I:%M %p')} "
                            f"in {course.venue or 'the announced venue'}."
                        ),
                        target_url=reverse("portal:dashboard"),
                        dedupe_key=f"class-reminder:{course.id}:{recipient.id}:{class_starts_at.isoformat()}",
                    )
                    alert_dispatch.delivered_at = timezone.now()
                    alert_dispatch.last_attempt_at = alert_dispatch.delivered_at
                    alert_dispatch.attempt_count = 1
                    alert_dispatch.save(update_fields=["delivered_at", "last_attempt_at", "attempt_count", "updated_at"])

        if email_targets:
            delivered_ids, failed_ids, error_message = send_course_reminder_email(course, recipients=email_targets)
            attempted_at = timezone.now()
            if delivered_ids:
                CourseReminderDispatch.objects.filter(
                    pk__in=[email_dispatches[recipient_id] for recipient_id in delivered_ids]
                ).update(
                    delivered_at=attempted_at,
                    last_attempt_at=attempted_at,
                    attempt_count=F("attempt_count") + 1,
                    last_error="",
                    updated_at=attempted_at,
                )
            if failed_ids:
                CourseReminderDispatch.objects.filter(
                    pk__in=[email_dispatches[recipient_id] for recipient_id in failed_ids]
                ).update(
                    last_attempt_at=attempted_at,
                    attempt_count=F("attempt_count") + 1,
                    last_error=error_message[:255],
                    updated_at=attempted_at,
                )


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

    admin_user, created = User.objects.get_or_create(
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
