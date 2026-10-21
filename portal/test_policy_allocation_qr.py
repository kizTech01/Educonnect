from io import BytesIO
from xml.sax.saxutils import escape
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .automation import _allocation_key, import_course_allocations
from .services import deliver_notification
from .models import (
    AcademicSession, Course, CourseAllocationUpload, Department, DepartmentalPayment,
    Faculty, Institution, LecturerCourseRegistration, Programme, CoursePayment,
    Notification, NotificationRecipient, StudentCourseRegistration, User,
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
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

    def word_table_upload(self, rows):
        """Build a minimal DOCX table like departmental allocation documents."""
        def cell(value):
            paragraphs = "".join(
                f"<w:p><w:r><w:t>{escape(paragraph)}</w:t></w:r></w:p>"
                for paragraph in value.split("\n")
            )
            return f"<w:tc>{paragraphs}</w:tc>"

        table_rows = "".join(
            "<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>"
            for row in rows
        )
        document_xml = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:tbl>{table_rows}</w:tbl></w:body></w:document>"
        )
        content = BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        return CourseAllocationUpload.objects.create(
            institution=self.institution,
            department=self.department,
            file=SimpleUploadedFile(
                "allocation.docx", content.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        )

    def test_word_allocation_table_expands_each_lecturers_course_list(self):
        upload = self.word_table_upload([
            ("Lecturer Name", "Lecturer ID", "Allocated Courses (Course Code - Course Title)"),
            ("Prof. E. J. Garba", "LEC-CS-001", "CSC 101 - Introduction\nCSC 102 - Data Structures"),
            ("Prof. A. Y. Gital", "LEC-CS-002", "CSC 103 - Programming"),
        ])

        summary = import_course_allocations(upload)

        self.assertIn("3 allocations assigned", summary)
        self.assertEqual(
            set(LecturerCourseRegistration.objects.filter(is_active=True).values_list("course__code", flat=True)),
            {"CSC101", "CSC102", "CSC103"},
        )
        self.assertEqual(Course.objects.get(code="CSC102").lecturer, self.lecturers[0])
        self.assertEqual(Course.objects.get(code="CSC103").lecturer, self.lecturers[1])

    def test_allocation_rows_inherit_a_vertically_merged_lecturer_id(self):
        upload = self.upload([
            "LEC-CS-001,CSC101,Introduction,3",
            ",CSC102,Data Structures,3",
        ])

        summary = import_course_allocations(upload)

        self.assertIn("2 allocations assigned", summary)
        self.assertEqual(Course.objects.get(code="CSC102").lecturer, self.lecturers[0])

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

    def test_starting_a_new_current_session_unregisters_only_its_institution(self):
        course = Course.objects.create(
            institution=self.institution,
            department=self.department,
            academic_session=self.session,
            code="CSC101",
            title="Introduction",
            level="100",
        )
        student = User.objects.create_user(
            username="rollover-student", password="pass1234", role=User.Role.STUDENT,
            institution=self.institution, department=self.department,
        )
        course.lecturer = self.lecturers[0]
        course.save(update_fields=["lecturer", "updated_at"])
        LecturerCourseRegistration.objects.create(
            institution=self.institution, lecturer=self.lecturers[0], course=course,
        )
        StudentCourseRegistration.objects.create(
            institution=self.institution, student=student, course=course, session=self.session,
        )
        payment = CoursePayment.objects.create(
            institution=self.institution, student=student, course=course, session=self.session,
            amount="0.00",
        )

        other_institution = Institution.objects.create(
            name="Other University", institution_code="OTHER-U", email="other@example.test", subdomain="other-u",
        )
        other_faculty = Faculty.objects.create(institution=other_institution, name="Science", code="SCI")
        other_department = Department.objects.create(
            institution=other_institution, faculty=other_faculty, name="Physics", code="PHY",
        )
        other_session = AcademicSession.objects.create(
            institution=other_institution, name="2026/2027", is_current=True,
        )
        other_lecturer = User.objects.create_user(
            username="other-lecturer", password="pass1234", role=User.Role.LECTURER,
            institution=other_institution, department=other_department,
        )
        other_course = Course.objects.create(
            institution=other_institution, department=other_department, academic_session=other_session,
            lecturer=other_lecturer, code="PHY101", title="Physics", level="100",
        )
        other_registration = LecturerCourseRegistration.objects.create(
            institution=other_institution, lecturer=other_lecturer, course=other_course,
        )

        AcademicSession.objects.create(
            institution=self.institution, name="2027/2028", is_current=True,
        )

        course.refresh_from_db()
        payment.refresh_from_db()
        other_course.refresh_from_db()
        other_registration.refresh_from_db()
        self.assertIsNone(course.lecturer)
        self.assertFalse(LecturerCourseRegistration.objects.get(course=course).is_active)
        self.assertFalse(StudentCourseRegistration.objects.get(student=student, course=course).is_active)
        self.assertFalse(payment.is_active_for_registration)
        self.assertEqual(other_course.lecturer, other_lecturer)
        self.assertTrue(other_registration.is_active)

    def test_lecturer_message_reaches_only_current_active_course_registrations(self):
        course = Course.objects.create(
            institution=self.institution,
            department=self.department,
            academic_session=self.session,
            lecturer=self.lecturers[0],
            code="CSC102",
            title="Programming",
            level="100",
        )
        current_student = User.objects.create_user(
            username="current-student", password="pass1234", role=User.Role.STUDENT,
            institution=self.institution, department=self.department,
        )
        former_student = User.objects.create_user(
            username="former-student", password="pass1234", role=User.Role.STUDENT,
            institution=self.institution, department=self.department,
        )
        old_session = AcademicSession.objects.create(
            institution=self.institution, name="2025/2026", is_current=False,
        )
        StudentCourseRegistration.objects.create(
            institution=self.institution, student=current_student, course=course, session=self.session,
        )
        StudentCourseRegistration.objects.create(
            institution=self.institution, student=former_student, course=course, session=old_session,
        )
        notification = Notification.objects.create(
            institution=self.institution, sender=self.lecturers[0], subject="Class update", body="Bring your notes.",
        )

        deliver_notification(notification)

        self.assertTrue(NotificationRecipient.objects.filter(notification=notification, student=current_student).exists())
        self.assertFalse(NotificationRecipient.objects.filter(notification=notification, student=former_student).exists())


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
        self.assertContains(response, self.payment.exam_card_semester)
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
        # /Im3 is reserved for the generated verification QR. /Im1 is the
        # optional institution logo, so checking it would not prove that a QR
        # code is present for institutions without uploaded branding.
        self.assertIn(b"/Im3 Do", response.content)
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.exam_card_token_hash)


class PolicyPageTests(TestCase):
    def test_all_global_policy_pages_are_public(self):
        for slug in ("privacy", "terms", "refunds", "acceptable-use", "cookies", "dpa", "security", "sla"):
            response = self.client.get(reverse("portal:policy-detail", args=[slug]))
            self.assertEqual(response.status_code, 200)
