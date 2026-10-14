from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import (
    AcademicSession,
    Department,
    DepartmentalPayment,
    DepartmentalPaymentDocument,
    ExamCardAIConfiguration,
    ExamCardAIReferenceDocument,
    Faculty,
    Institution,
    RoleAssignment,
    User,
)
from .views import _exam_card_ai_verification


class ExamOfficerWorkflowTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            name="Workflow University", institution_code="FLOW-U", email="workflow@example.test", subdomain="flow-u",
        )
        faculty = Faculty.objects.create(institution=self.institution, name="Science", code="SCI")
        self.department = Department.objects.create(
            institution=self.institution, faculty=faculty, name="Computer Science", code="CSC",
        )
        self.session = AcademicSession.objects.create(
            institution=self.institution, name="2026/2027", is_current=True,
        )
        self.hod = User.objects.create_user(
            username="hod", password="safe-password-123", role=User.Role.LECTURER,
            institution=self.institution, department=self.department,
        )
        self.department.head_of_department = self.hod
        self.department.save(update_fields=["head_of_department"])
        self.first_lecturer = User.objects.create_user(
            username="lecturer-one", id_number="LEC-001", password="safe-password-123", role=User.Role.LECTURER,
            institution=self.institution, department=self.department,
        )
        self.second_lecturer = User.objects.create_user(
            username="lecturer-two", id_number="LEC-002", password="safe-password-123", role=User.Role.LECTURER,
            institution=self.institution, department=self.department,
        )

    def test_hod_replaces_instead_of_accumulating_exam_officers(self):
        self.client.force_login(self.hod)
        url = reverse("portal:hod-exam-officer")
        self.assertEqual(self.client.post(url, {"lecturer_id": self.first_lecturer.pk}).status_code, 302)
        self.assertEqual(self.client.post(url, {"lecturer_id": self.second_lecturer.pk}).status_code, 302)

        active_assignments = RoleAssignment.objects.filter(
            department=self.department, role=User.Role.EXAM_OFFICER, is_active=True,
        )
        self.assertEqual(active_assignments.count(), 1)
        self.assertEqual(active_assignments.get().user, self.second_lecturer)
        self.assertTrue(self.first_lecturer.has_role(User.Role.LECTURER))
        self.assertTrue(self.second_lecturer.has_role(User.Role.LECTURER))

    def test_ai_requires_matching_typed_sample_session_and_student_identity(self):
        student = User.objects.create_user(
            username="student", id_number="STU-001", first_name="Ada", last_name="Student",
            password="safe-password-123", role=User.Role.STUDENT,
            institution=self.institution, department=self.department,
        )
        payment = DepartmentalPayment.objects.create(
            institution=self.institution, student=student, department=self.department, session=self.session,
            status=DepartmentalPayment.Status.PAID,
        )
        DepartmentalPaymentDocument.objects.create(
            payment=payment,
            category=DepartmentalPaymentDocument.Category.COURSE_FORM,
            title="Course Form",
            file=SimpleUploadedFile(
                "course-form.txt", b"Ada Student\nSTU-001\nAcademic session: 2026/2027\nFirst Semester",
            ),
        )
        configuration = ExamCardAIConfiguration.objects.create(
            institution=self.institution, department=self.department, academic_session=self.session,
            semester="first", automatic_processing_enabled=True, require_white_form=False, require_school_receipt=False,
            require_course_form=True, verify_student_identifier=True, verify_academic_session=True,
        )
        ExamCardAIReferenceDocument.objects.create(
            institution=self.institution, configuration=configuration,
            category=DepartmentalPaymentDocument.Category.COURSE_FORM,
            title="Course Form",
            file=SimpleUploadedFile("course-form-sample.txt", b"Course form sample\nAcademic session: 2026/2027\nFirst Semester"),
        )

        verification = _exam_card_ai_verification(payment, configuration)
        self.assertEqual(verification["outcome"], "approved")
        self.assertTrue(verification["checks"]["student_identity_matches_required_documents"])
        self.assertTrue(verification["checks"]["sample_sessions_match"])

        reference = configuration.reference_documents.get()
        reference.file.save("wrong-session.txt", SimpleUploadedFile("wrong-session.txt", b"Academic session: 2025/2026\nFirst Semester"))
        verification = _exam_card_ai_verification(payment, configuration)
        self.assertEqual(verification["outcome"], "pending_review")
        self.assertFalse(verification["checks"]["sample_sessions_match"])
