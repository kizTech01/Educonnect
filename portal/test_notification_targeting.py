from django.test import TestCase
from django.urls import reverse

from .models import (
    AcademicSession,
    Course,
    Department,
    Faculty,
    Notification,
    NotificationRecipient,
    Programme,
    StudentCourseRegistration,
    User,
)


class LecturerMessageTargetingTests(TestCase):
    def setUp(self):
        self.faculty = Faculty.objects.create(name="Science", code="SCI")
        self.department = Department.objects.create(
            faculty=self.faculty,
            name="Biological Sciences",
            code="BIO",
        )
        self.lecturer = User.objects.create_user(
            username="message-lecturer",
            password="test-password",
            role=User.Role.LECTURER,
            department=self.department,
            first_name="Ada",
            last_name="Lecturer",
            email="lecturer@example.test",
        )
        self.session = AcademicSession.objects.create(name="MSG-CURRENT-TEST", is_current=True)

    def make_student(self, username, *, department=None, level="100", programme=None):
        return User.objects.create_user(
            username=username,
            password="test-password",
            role=User.Role.STUDENT,
            department=department or self.department,
            programme=programme,
            level=level,
            email=f"{username}@example.test",
        )

    def make_course(self, code, level="100"):
        return Course.objects.create(
            department=self.department,
            lecturer=self.lecturer,
            code=code,
            title=f"Course {code}",
            level=level,
        )

    def post_message(self, subject, **target_fields):
        self.client.force_login(self.lecturer)
        return self.client.post(
            reverse("portal:lecturer-messages"),
            {"subject": subject, "body": "Please read this update.", **target_fields},
        )

    def recipients_for(self, subject):
        notification = Notification.objects.get(sender=self.lecturer, subject=subject)
        return set(
            NotificationRecipient.objects.filter(notification=notification).values_list("student_id", flat=True)
        )

    def test_department_and_level_target_includes_students_without_course_registrations(self):
        matching_student = self.make_student("department-level-100", level="100")
        other_level_student = self.make_student("department-level-200", level="200")
        other_faculty = Faculty.objects.create(name="Arts", code="ART")
        other_department = Department.objects.create(
            faculty=other_faculty,
            name="History",
            code="HIS",
        )
        other_department_student = self.make_student(
            "other-department-100",
            department=other_department,
            level="100",
        )

        response = self.post_message(
            "Department level update",
            departments=[str(self.department.pk)],
            level="100",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.recipients_for("Department level update"),
            {matching_student.pk},
        )
        self.assertNotIn(other_level_student.pk, self.recipients_for("Department level update"))
        self.assertNotIn(other_department_student.pk, self.recipients_for("Department level update"))

    def test_programme_target_filters_students_and_requires_one_selected_department(self):
        programme = Programme.objects.create(
            department=self.department,
            name="Microbiology",
            code="MIC",
        )
        matching_student = self.make_student("microbiology-200", level="200", programme=programme)
        other_level_student = self.make_student("microbiology-100", level="100", programme=programme)
        self.client.force_login(self.lecturer)

        response = self.client.post(
            reverse("portal:lecturer-messages"),
            {
                "subject": "Programme update",
                "body": "Programme notice.",
                "departments": [str(self.department.pk)],
                "programmes": [str(programme.pk)],
                "level": "200",
            },
        )

        self.assertEqual(response.status_code, 302)
        notification = Notification.objects.get(sender=self.lecturer, subject="Programme update")
        self.assertEqual(set(notification.programmes.values_list("id", flat=True)), {programme.pk})
        self.assertEqual(
            set(notification.recipients.values_list("student_id", flat=True)),
            {matching_student.pk},
        )
        self.assertNotIn(other_level_student.pk, set(notification.recipients.values_list("student_id", flat=True)))

        second_department = Department.objects.create(
            faculty=self.faculty,
            name="Chemistry",
            code="CHM",
        )
        second_programme = Programme.objects.create(
            department=second_department,
            name="Industrial Chemistry",
            code="ICH",
        )
        invalid_response = self.client.post(
            reverse("portal:lecturer-messages"),
            {
                "subject": "Invalid multi-department programme target",
                "body": "This should be rejected.",
                "departments": [str(self.department.pk), str(second_department.pk)],
                "programmes": [str(programme.pk), str(second_programme.pk)],
            },
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertFalse(
            Notification.objects.filter(
                sender=self.lecturer,
                subject="Invalid multi-department programme target",
            ).exists()
        )

    def test_course_target_only_reaches_current_registrants_of_selected_courses(self):
        course_one = self.make_course("BIO101")
        course_two = self.make_course("BIO201")
        unselected_course = self.make_course("BIO301")
        enrolled_one = self.make_student("registered-bio101")
        enrolled_two = self.make_student("registered-bio201", level="200")
        stale_selected_course = self.make_student("stale-bio101")
        unselected_course_student = self.make_student("registered-bio301")
        previous_session = AcademicSession.objects.create(name="MSG-OLD-TEST", is_current=False)

        StudentCourseRegistration.objects.create(
            student=enrolled_one,
            course=course_one,
            session=self.session,
            registered_by=enrolled_one,
        )
        StudentCourseRegistration.objects.create(
            student=enrolled_two,
            course=course_two,
            session=self.session,
            registered_by=enrolled_two,
        )
        StudentCourseRegistration.objects.create(
            student=stale_selected_course,
            course=course_one,
            session=previous_session,
            registered_by=stale_selected_course,
        )
        StudentCourseRegistration.objects.create(
            student=stale_selected_course,
            course=unselected_course,
            session=self.session,
            registered_by=stale_selected_course,
        )
        StudentCourseRegistration.objects.create(
            student=unselected_course_student,
            course=unselected_course,
            session=self.session,
            registered_by=unselected_course_student,
        )

        response = self.post_message(
            "Selected course update",
            courses=[str(course_one.pk), str(course_two.pk)],
            departments=[str(self.department.pk)],
            level="100",
        )

        self.assertEqual(response.status_code, 302)
        notification = Notification.objects.get(sender=self.lecturer, subject="Selected course update")
        self.assertEqual(self.recipients_for("Selected course update"), {enrolled_one.pk, enrolled_two.pk})
        self.assertFalse(notification.departments.exists())
        self.assertFalse(notification.level)
