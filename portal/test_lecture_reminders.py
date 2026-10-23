from datetime import datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AcademicSession,
    Course,
    CourseReminderDispatch,
    Department,
    Faculty,
    Institution,
    LecturerCourseRegistration,
    StudentCourseRegistration,
    User,
    reset_current_institution,
    set_current_institution,
)
from .services import dispatch_due_course_reminders


class LectureReminderTests(TestCase):
    """Service and endpoint coverage using the shared-table tenant boundary."""

    reminder_now = timezone.make_aware(datetime(2030, 1, 7, 9, 30), ZoneInfo("Africa/Lagos"))

    def make_tenant_course(self, code, subdomain):
        institution = Institution.objects.create(
            name=f"{code} University",
            institution_code=code,
            institution_type=Institution.Type.UNIVERSITY,
            email=f"{subdomain}@example.test",
            subdomain=subdomain,
            status=Institution.Status.ACTIVE,
        )
        token = set_current_institution(institution)
        try:
            faculty = Faculty.objects.create(name="Science", code="SCI")
            department = Department.objects.create(
                name="Computer Science", code="CSC", faculty=faculty
            )
            session = AcademicSession.objects.create(name="2030/2031", is_current=True)
            lecturer = User.all_objects.create_user(
                username=f"{subdomain}-lecturer",
                password="safe-password-123",
                role=User.Role.LECTURER,
                institution=institution,
                department=department,
                is_approved=True,
                email=f"lecturer-{subdomain}@example.test",
            )
            student = User.all_objects.create_user(
                username=f"{subdomain}-student",
                password="safe-password-123",
                role=User.Role.STUDENT,
                institution=institution,
                department=department,
                id_number=f"{code}-STUDENT",
                email=f"student-{subdomain}@example.test",
            )
            course = Course.objects.create(
                department=department,
                academic_session=session,
                lecturer=lecturer,
                code="CSC405",
                title="Database Management Systems",
                level="400",
                schedule_day="Monday",
                schedule_time=time(10, 0),
                venue="Lecture Theatre 2",
            )
            LecturerCourseRegistration.objects.create(lecturer=lecturer, course=course)
            StudentCourseRegistration.objects.create(
                student=student, course=course, session=session, registered_by=student
            )
        finally:
            reset_current_institution(token)
        return institution, course, lecturer, student

    def test_formal_reminders_are_private_and_do_not_repeat(self):
        _, course, lecturer, student = self.make_tenant_course("FIRST", "first")
        unregistered = User.all_objects.create_user(
            username="first-unregistered",
            password="safe-password-123",
            role=User.Role.STUDENT,
            institution=course.institution,
            department=course.department,
            id_number="FIRST-UNREGISTERED",
            email="unregistered@example.test",
        )

        summary = dispatch_due_course_reminders(now=self.reminder_now)
        dispatch_due_course_reminders(now=self.reminder_now)

        self.assertEqual(summary["courses_due"], 1)
        self.assertEqual(summary["email_delivered"], 2)
        self.assertEqual(
            {tuple(message.to) for message in mail.outbox},
            {(lecturer.email,), (student.email,)},
        )
        self.assertTrue(all(not message.bcc for message in mail.outbox))
        self.assertTrue(any("Dear Lecturer" in message.body for message in mail.outbox))
        self.assertTrue(any("Dear Student" in message.body for message in mail.outbox))
        self.assertTrue(all(course.title in message.body for message in mail.outbox))
        self.assertFalse(any(unregistered.email in message.to for message in mail.outbox))
        self.assertEqual(
            CourseReminderDispatch.all_objects.filter(
                course=course, channel=CourseReminderDispatch.Channel.EMAIL, delivered_at__isnull=False
            ).count(),
            2,
        )

    def test_matching_course_codes_remain_tenant_isolated(self):
        first, first_course, first_lecturer, first_student = self.make_tenant_course("FIRST", "first")
        second, second_course, second_lecturer, second_student = self.make_tenant_course("SECOND", "second")

        dispatch_due_course_reminders(now=self.reminder_now)

        self.assertEqual(first_course.code, second_course.code)
        self.assertEqual(
            set(
                CourseReminderDispatch.all_objects.filter(course=first_course).values_list(
                    "recipient_id", flat=True
                )
            ),
            {first_lecturer.id, first_student.id},
        )
        self.assertEqual(
            set(
                CourseReminderDispatch.all_objects.filter(course=second_course).values_list(
                    "recipient_id", flat=True
                )
            ),
            {second_lecturer.id, second_student.id},
        )
        self.assertEqual(
            set(CourseReminderDispatch.all_objects.values_list("institution_id", flat=True)),
            {first.id, second.id},
        )

    def test_outside_reminder_window_and_started_lectures_are_skipped(self):
        _, course, _, _ = self.make_tenant_course("FIRST", "first")

        dispatch_due_course_reminders(now=self.reminder_now.replace(hour=9, minute=24))
        dispatch_due_course_reminders(now=self.reminder_now.replace(hour=10, minute=0))

        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(CourseReminderDispatch.all_objects.filter(course=course).exists())

    @override_settings(EDUCONNECT_CRON_SECRET="test-cron-secret")
    @patch("portal.views.dispatch_due_course_reminders", return_value={"courses_due": 1, "email_delivered": 2, "email_failed": 0, "alerts_delivered": 0})
    def test_cron_endpoint_requires_secret_and_returns_only_summary(self, dispatch):
        url = reverse("portal:internal-lecture-reminders")

        missing = self.client.post(url)
        rejected = self.client.post(url, HTTP_X_EDUCONNECT_CRON_SECRET="wrong")
        accepted = self.client.post(url, HTTP_X_EDUCONNECT_CRON_SECRET="test-cron-secret")

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json(), {"detail": "Unauthorized."})
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json(), {"status": "ok", "courses_due": 1, "email_delivered": 2, "email_failed": 0, "alerts_delivered": 0})
        dispatch.assert_called_once_with()

    @override_settings(EDUCONNECT_CRON_SECRET="test-cron-secret")
    @patch("portal.views.dispatch_due_course_reminders")
    def test_cron_endpoint_rejects_unsupported_method_without_processing(self, dispatch):
        response = self.client.get(
            reverse("portal:internal-lecture-reminders"),
            HTTP_X_EDUCONNECT_CRON_SECRET="test-cron-secret",
        )

        self.assertEqual(response.status_code, 405)
        dispatch.assert_not_called()

    @override_settings(EDUCONNECT_CRON_SECRET="test-cron-secret")
    @patch("portal.views.dispatch_due_course_reminders", side_effect=RuntimeError("smtp credentials leaked"))
    def test_cron_endpoint_masks_processing_errors(self, dispatch):
        response = self.client.post(
            reverse("portal:internal-lecture-reminders"),
            HTTP_X_EDUCONNECT_CRON_SECRET="test-cron-secret",
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Reminder processing failed."})
        self.assertNotIn("smtp credentials leaked", response.content.decode())
        dispatch.assert_called_once_with()

    @override_settings(EDUCONNECT_CRON_SECRET="")
    def test_cron_endpoint_fails_closed_without_configuration(self):
        response = self.client.post(reverse("portal:internal-lecture-reminders"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Service unavailable."})

    def test_institution_timezone_controls_the_reminder_window(self):
        institution, course, lecturer, student = self.make_tenant_course("FIRST", "first")
        institution.timezone = "America/New_York"
        institution.save(update_fields=["timezone"])

        # Monday 09:30 in New York, despite being 14:30 UTC. The same instant
        # is not in the 25–35 minute window in the server's Lagos timezone.
        now = timezone.make_aware(datetime(2030, 1, 7, 14, 30), ZoneInfo("UTC"))
        summary = dispatch_due_course_reminders(now=now)

        self.assertEqual(summary["courses_due"], 1)
        self.assertEqual(summary["email_delivered"], 2)
        self.assertEqual(
            set(CourseReminderDispatch.all_objects.filter(course=course).values_list("recipient_id", flat=True)),
            {lecturer.id, student.id},
        )
