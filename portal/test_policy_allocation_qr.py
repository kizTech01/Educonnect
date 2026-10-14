from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse

from .automation import _allocation_key, import_course_allocations
from .models import (
    AcademicSession, Course, CourseAllocationUpload, Department, DepartmentalPayment,
    Faculty, Institution, LecturerCourseRegistration, Programme,
    StudentCourseRegistration, User,
)


class AllocationReconciliationTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            name="Test University", institution_code="TEST-U", email="test@example.test", subdomain="test-u",
            status=Institution.Status.ACTIVE,
        )
        faculty = Faculty.objects.create(institution=self.institution, name="Science", code="SCI")
        self.department = Department.objects.create(institution=self.institution, faculty=faculty, name="Computer Science", code="CSC")
        self.programme = Programme.objects.create(institution=self.institution, department=self.department, name="Computer Science", code="BSC-CS")
        self.session = AcademicSession.objects.create(institution=self.institution, name="2026/2027", is_current=True)
        self.lecturers = [
            User.objects.create_user(
                username=f"LEC-CS-{number:03d}", id_number=f"LEC-CS-{number:03d}", password="pass1234",
                role=User.Role.LECTURER, institution=self.institution, department=self.department,
            ) for number in range(1, 33)
        ]

    def upload(self, rows):
        content = "Lecturer ID,Course Code,Course Title,Unit\n" + "\n".join(rows) + "\n"
        return CourseAllocationUpload.objects.create(
            institution=self.institution, department=self.department,
            file=SimpleUploadedFile("allocation.csv", content.encode(), content_type="text/csv"),
        )

    def test_all_32_ids_normalise_and_do_not_duplicate_courses(self):
        existing = Course.objects.create(
            institution=self.institution, department=self.department, academic_session=self.session,
            code="CSC101", title="Existing", level="100", semester="first",
        )
        existing.programmes.add(self.programme)
        rows = [
            f"LEC‑CS‑{number:03d}, csc-{100 + number:03d} ,Course {number},3"
            for number in range(1, 33)
        ]
        upload = self.upload(rows)
        import_course_allocations(upload)
        self.assertEqual(LecturerCourseRegistration.objects.filter(is_active=True).count(), 32)
        self.assertEqual(Course.objects.filter(department=self.department, code="CSC101").count(), 1)
        self.assertEqual(Course.objects.filter(department=self.department).count(), 32)
        self.assertEqual(_allocation_key(" LEC‑CS - 001 "), "LECCS001")

        duplicate = self.upload(rows)
        self.assertIn("unchanged", import_course_allocations(duplicate))
        self.assertEqual(LecturerCourseRegistration.objects.filter(is_active=True).count(), 32)

    def test_reassignment_keeps_student_course_registrations_intact(self):
        first = self.upload([
            "LEC-CS-001,CSC101,Intro,3",
            "LEC-CS-001,CSC102,Programming,3",
        ])
        import_course_allocations(first)
        kept = Course.objects.get(code="CSC101")
        removed = Course.objects.get(code="CSC102")
        student = User.objects.create_user(
            username="student", id_number="MAT-001", password="pass1234", role=User.Role.STUDENT,
            institution=self.institution, department=self.department, programme=self.programme, level="100",
        )
        kept_registration = StudentCourseRegistration.objects.create(
            institution=self.institution, student=student, course=kept, session=self.session,
        )
        removed_registration = StudentCourseRegistration.objects.create(
            institution=self.institution, student=student, course=removed, session=self.session,
        )
        second = self.upload([" lec cs 002 ,CSC101,Intro,3"])
        import_course_allocations(second)

        kept.refresh_from_db(); removed.refresh_from_db()
        kept_registration.refresh_from_db(); removed_registration.refresh_from_db()
        self.assertEqual(kept.lecturer, self.lecturers[1])
        self.assertTrue(kept_registration.is_active)
        self.assertTrue(removed_registration.is_active)
        self.assertIsNone(removed_registration.superseded_by_allocation)
        self.assertTrue(LecturerCourseRegistration.objects.filter(course=removed, is_active=False).exists())

    def test_failed_import_rolls_back_new_courses(self):
        upload = self.upload(["LEC-CS-001,CSC101,Intro,3"])
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                import_course_allocations(upload)
                raise RuntimeError("simulate failure after import")
        self.assertFalse(Course.objects.filter(department=self.department, code="CSC101").exists())


class ExamCardVerificationTests(TestCase):
    def setUp(self):
        institution = Institution.objects.create(
            name="QR University", institution_code="QR-U", email="qr@example.test", subdomain="qr-u",
        )
        faculty = Faculty.objects.create(institution=institution, name="Science", code="SCI")
        department = Department.objects.create(institution=institution, faculty=faculty, name="Computer Science", code="CSC")
        session = AcademicSession.objects.create(institution=institution, name="2026/2027", is_current=True)
        self.student = User.objects.create_user(
            username="qr-student", id_number="QR/001", password="pass1234", role=User.Role.STUDENT,
            institution=institution, department=department, level="100",
        )
        self.course = Course.objects.create(
            institution=institution, department=department, academic_session=session,
            code="CSC101", title="Introduction", level="100", semester="first",
        )
        StudentCourseRegistration.objects.create(institution=institution, student=self.student, course=self.course, session=session)
        self.payment = DepartmentalPayment.objects.create(
            institution=institution, student=self.student, department=department, session=session,
            status=DepartmentalPayment.Status.PAID,
            exam_card_approval_status=DepartmentalPayment.ExamCardApprovalStatus.APPROVED,
        )

    def test_opaque_token_verifies_and_regeneration_invalidates_old_card(self):
        token = self.payment.issue_exam_card_token()
        # A secure random token may coincidentally contain digits from a
        # database PK; it must simply not encode or be derived from that PK.
        self.assertGreaterEqual(len(token), 40)
        self.assertNotEqual(token, str(self.payment.pk))
        response = self.client.get(reverse("portal:verify-exam-card", args=[token]))
        self.assertContains(response, "Exam Card Verified")
        self.assertNotContains(response, "Payment status")

        replacement = self.payment.issue_exam_card_token()
        self.assertNotEqual(token, replacement)
        self.assertEqual(self.client.get(reverse("portal:verify-exam-card", args=[token])).status_code, 404)
        self.assertContains(self.client.get(reverse("portal:verify-exam-card", args=[replacement])), "Valid")

    def test_invalid_and_cancelled_cards_do_not_disclose_student_details(self):
        self.assertEqual(self.client.get(reverse("portal:verify-exam-card", args=["x" * 43])).status_code, 404)
        token = self.payment.issue_exam_card_token()
        self.payment.status = DepartmentalPayment.Status.FAILED
        self.payment.save(update_fields=["status", "updated_at"])
        response = self.client.get(reverse("portal:verify-exam-card", args=[token]))
        self.assertEqual(response.status_code, 410)
        self.assertNotIn(self.student.id_number.encode(), response.content)

    def test_exam_card_pdf_embeds_qr_image(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:download-exam-card", args=[self.payment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/Im1", response.content)
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.exam_card_token_hash)


class PolicyPageTests(TestCase):
    def test_all_global_policy_pages_are_public(self):
        for slug in ("privacy", "terms", "refunds", "acceptable-use", "cookies", "dpa", "security", "sla"):
            response = self.client.get(reverse("portal:policy-detail", args=[slug]))
            self.assertEqual(response.status_code, 200)
