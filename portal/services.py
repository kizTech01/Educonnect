import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
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

DEFAULT_ADMIN_USERNAME = getattr(settings, "BOOTSTRAP_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = getattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "mauyola")
PAYSTACK_API_BASE = "https://api.paystack.co"
PAYSTACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_PAYSTACK_SECRET_KEY = getattr(
    settings,
    "PAYSTACK_SECRET_KEY",
    "removed-paystack-test-key",
)
DEFAULT_PAYSTACK_PUBLIC_KEY = getattr(
    settings,
    "PAYSTACK_PUBLIC_KEY",
    "pk_test_fd50b599b31f0cfb316e4460621ca9921b7945ff",
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
    recipient_list = []
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

    seen = set()
    for user in recipients:
        if not user or user.role not in {User.Role.STUDENT, User.Role.LECTURER} or not user.email:
            continue
        email_key = user.email.strip().lower()
        if email_key in seen:
            continue
        seen.add(email_key)
        recipient_list.append(user.email)

    if not recipient_list:
        return

    schedule_day = course.schedule_day or "the scheduled day to be announced"
    schedule_time = course.schedule_time.strftime("%I:%M %p") if course.schedule_time else "the scheduled time to be announced"
    venue = course.venue or "the announced venue"
    message = (
        f"This is a class reminder for {course.title} ({course.code}). "
        f"Please note that the class holds on {schedule_day} at {schedule_time} in {venue}."
    )
    send_mail(
        subject=f"Class Reminder: {course.code}",
        message=message,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@educonnect.local"),
        recipient_list=recipient_list,
        fail_silently=True,
    )


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

    recipients = [
        NotificationRecipient(notification=notification, student=student)
        for student in students.distinct()
    ]
    NotificationRecipient.objects.bulk_create(recipients, ignore_conflicts=True)

    alert_rows = []
    for recipient in NotificationRecipient.objects.filter(notification=notification).select_related("student"):
        if not recipient.student.browser_alerts_enabled:
            continue
        alert_rows.append(
            UserAlert(
                recipient=recipient.student,
                alert_type=UserAlert.AlertType.MESSAGE,
                title=notification.subject,
                body=notification.body,
                target_url=reverse("portal:student-messages") + f"?open={recipient.id}",
                dedupe_key=f"message:{notification.id}:{recipient.student_id}",
            )
        )
    UserAlert.objects.bulk_create(alert_rows, ignore_conflicts=True)


def create_alert(recipient, alert_type, title, body, target_url, dedupe_key):
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
        for recipient in recipients:
            if not recipient or recipient.role not in {User.Role.STUDENT, User.Role.LECTURER}:
                continue
            if recipient.email_class_reminders and recipient.email:
                _, created = CourseReminderDispatch.objects.get_or_create(
                    course=course,
                    recipient=recipient,
                    scheduled_for=class_starts_at,
                    channel=CourseReminderDispatch.Channel.EMAIL,
                )
                if created:
                    email_targets.append(recipient)
            if recipient.browser_alerts_enabled and recipient.class_reminder_alerts_enabled:
                _, created = CourseReminderDispatch.objects.get_or_create(
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

        if email_targets:
            send_course_reminder_email(course, recipients=email_targets)


def process_due_course_reminders():
    try:
        with transaction.atomic():
            dispatch_due_course_reminders()
    except Exception:
        return


def ensure_default_admin_user():
    admin_user, created = User.objects.get_or_create(
        username=DEFAULT_ADMIN_USERNAME,
        defaults={
            "role": User.Role.ADMIN,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
            "is_approved": True,
        },
    )

    updates = []
    if created:
        admin_user.set_password(DEFAULT_ADMIN_PASSWORD)
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
