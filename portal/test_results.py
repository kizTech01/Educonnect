from django.test import TestCase
from django.urls import reverse

from .models import (
    AcademicSession,
    Course,
    CourseResult,
    Department,
    Faculty,
    LecturerCourseRegistration,
    RoleAssignment,
    StudentCourseRegistration,
    User,
    get_default_institution,
)


class ResultWorkflowTests(TestCase):
    def setUp(self):
        self.institution = get_default_institution()
        self.session = AcademicSession.objects.create(name="2032/2033", is_current=True)
        faculty = Faculty.objects.create(name="Result Science", code="RSC")
        self.department = Department.objects.create(name="Result Computing", code="RCS", faculty=faculty)
        self.lecturer = User.objects.create_user(
            username="result-lecturer", password="safe-password-123", role=User.Role.LECTURER,
            department=self.department, is_approved=True,
        )
        self.exam_officer = User.objects.create_user(
            username="result-exam-officer", password="safe-password-123", role=User.Role.LECTURER,
            department=self.department, is_approved=True,
        )
        RoleAssignment.objects.create(
            user=self.exam_officer, role=User.Role.EXAM_OFFICER, department=self.department,
            assigned_by=self.lecturer,
        )
        self.student = User.objects.create_user(
            username="result-student", password="safe-password-123", role=User.Role.STUDENT,
            department=self.department,
        )
        self.course = Course.objects.create(
            department=self.department, lecturer=self.lecturer, academic_session=self.session,
            code="RCS301", title="Result Systems", level="300", credit_units=3,
        )
        LecturerCourseRegistration.objects.create(lecturer=self.lecturer, course=self.course)
        StudentCourseRegistration.objects.create(student=self.student, course=self.course, session=self.session)

    def test_lecturer_submission_exam_officer_approval_and_student_publication(self):
        self.client.force_login(self.lecturer)
        response = self.client.post(reverse("portal:lecturer-results"), {
            "action": "submit", "student": self.student.pk, "course": self.course.pk,
            "session": self.session.pk, "score": "72.5",
        })
        self.assertRedirects(response, reverse("portal:lecturer-results"))
        result = CourseResult.objects.get(student=self.student, course=self.course, session=self.session)
        self.assertEqual(result.status, CourseResult.Status.SUBMITTED)
        self.assertEqual(result.grade, "A")
        self.assertEqual(result.grade_point, 5)

        self.client.force_login(self.exam_officer)
        session = self.client.session
        session["active_role"] = User.Role.EXAM_OFFICER
        session.save()
        response = self.client.post(reverse("portal:exam-results"), {
            "result_id": result.pk, "action": "approve", "note": "Verified",
        })
        self.assertRedirects(response, reverse("portal:exam-results"))
        result.refresh_from_db()
        self.assertEqual(result.status, CourseResult.Status.APPROVED)

        response = self.client.post(reverse("portal:exam-results"), {
            "result_id": result.pk, "action": "publish", "note": "Approved for release",
        })
        self.assertRedirects(response, reverse("portal:exam-results"))
        result.refresh_from_db()
        self.assertEqual(result.status, CourseResult.Status.PUBLISHED)

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-results"))
        self.assertContains(response, "RCS301")
        self.assertContains(response, "72.5")

    def test_lecturer_cannot_submit_for_an_unassigned_course(self):
        other_course = Course.objects.create(
            department=self.department, academic_session=self.session,
            code="RCS302", title="Other Results", level="300",
        )
        StudentCourseRegistration.objects.create(student=self.student, course=other_course, session=self.session)
        self.client.force_login(self.lecturer)
        response = self.client.post(reverse("portal:lecturer-results"), {
            "action": "submit", "student": self.student.pk, "course": other_course.pk,
            "session": self.session.pk, "score": "60",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CourseResult.objects.filter(course=other_course).exists())
