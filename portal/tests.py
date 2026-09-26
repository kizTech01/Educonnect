from io import BytesIO
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.test import Client
from django.test import RequestFactory
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.datastructures import MultiValueDict

from .models import (
    AcademicSession,
    AuditLog,
    AIAutomationJob,
    Course,
    CourseAllocationUpload,
    CoursePayment,
    CoursePaymentGateway,
    CourseStudentGroup,
    CourseStudentGroupMembership,
    CourseReminderDispatch,
    CourseMaterial,
    Department,
    Faculty,
    DepartmentLecturerUpload,
    DepartmentPaymentGateway,
    DepartmentCoursePaymentGateway,
    DepartmentalAssociation,
    DepartmentalFee,
    DepartmentalPayment,
    Handbook,
    InstitutionProfile,
    Institution,
    MaterialAccess,
    Notification,
    NotificationAttachment,
    NotificationRecipient,
    LecturerCourseRegistration,
    Programme,
    StudentCourseRegistration,
    User,
    UserAlert,
    RoleAssignment,
    get_default_institution,
    set_current_institution,
    reset_current_institution,
)
from .services import dispatch_due_course_reminders, ensure_default_admin_user
from .views import AI_ROLE_EXAMPLES, _ai_answer, sidebar_links
from .automation import import_course_allocations, import_department_lecturers, import_programmes


class InstitutionalRBACTests(TestCase):
    def setUp(self):
        self.institution = get_default_institution()
        self.department = Department.objects.create(name="Secure Computing", code="SEC")
        self.hod = User.objects.create_user(
            username="secure-hod", password="pass1234", role=User.Role.LECTURER,
            department=self.department, is_approved=True,
        )
        self.department.head_of_department = self.hod
        self.department.save(update_fields=["head_of_department"])
        self.lecturer = User.objects.create_user(
            username="secure-lecturer", password="pass1234", role=User.Role.LECTURER,
            department=self.department, is_approved=True,
        )

    def test_hod_assigns_exam_officer_without_replacing_lecturer_role(self):
        self.client.force_login(self.hod)
        response = self.client.post(reverse("portal:hod-exam-officer"), {"lecturer_id": self.lecturer.id})

        self.assertRedirects(response, reverse("portal:hod-exam-officer"))
        self.lecturer.refresh_from_db()
        self.assertEqual(self.lecturer.role, User.Role.LECTURER)
        self.assertTrue(self.lecturer.has_role(User.Role.EXAM_OFFICER))
        self.assertTrue(RoleAssignment.objects.filter(
            user=self.lecturer, role=User.Role.EXAM_OFFICER, department=self.department,
        ).exists())
        self.assertTrue(AuditLog.objects.filter(action="exam_officer_assigned", user=self.hod).exists())

    def test_role_assignment_rejects_a_cross_tenant_department(self):
        other = Institution.objects.create(
            name="Other University", institution_code="OTHER-SEC", email="other@example.test",
            subdomain="other-sec", status=Institution.Status.ACTIVE,
        )
        foreign_faculty = Faculty.all_objects.create(name="Other Faculty", code="OTH", institution=other)
        foreign_department = Department.all_objects.create(
            name="Other Department", code="ODEP", faculty=foreign_faculty, institution=other,
        )
        with self.assertRaises(ValidationError):
            RoleAssignment.objects.create(
                institution=self.institution, user=self.lecturer, role=User.Role.EXAM_OFFICER,
                department=foreign_department, assigned_by=self.hod,
            )

    def test_ai_uses_the_selected_role_not_another_assigned_role(self):
        RoleAssignment.objects.create(
            institution=self.institution,
            user=self.hod,
            role=User.Role.HOD,
            department=self.department,
            assigned_by=self.hod,
        )
        User.objects.create_user(
            username="secure-student",
            password="pass1234",
            role=User.Role.STUDENT,
            department=self.department,
        )

        lecturer_answer = _ai_answer(self.hod, User.Role.LECTURER, "How many students are there?")
        hod_answer = _ai_answer(self.hod, User.Role.HOD, "How many students are there?")

        self.assertIn("assigned courses", lecturer_answer)
        self.assertIn("1 student", hod_answer)
        self.assertNotEqual(lecturer_answer, hod_answer)

    def test_ai_examples_cover_every_institutional_role(self):
        self.assertEqual(set(AI_ROLE_EXAMPLES), set(User.Role.values))

    def test_ai_automation_url_is_not_exposed(self):
        self.client.force_login(self.hod)
        self.assertEqual(self.client.get(reverse("portal:ai-automation")).status_code, 403)

        administrator = User.objects.create_user(
            username="secure-admin", password="pass1234", role=User.Role.ADMIN,
        )
        self.client.force_login(administrator)
        self.assertEqual(self.client.get(reverse("portal:ai-automation")).status_code, 200)

    def test_ai_automation_requires_analysis_approval_then_execution(self):
        administrator = User.objects.create_user(
            username="automation-admin", password="pass1234", role=User.Role.ADMIN,
        )
        self.client.force_login(administrator)
        upload = SimpleUploadedFile(
            "lecturers.csv",
            b"name,email,lecturer_id,department_code\nAda Lovelace,ada@example.test,LEC-001,SEC\n",
            content_type="text/csv",
        )
        response = self.client.post(
            reverse("portal:ai-automation"),
            {"action": "analyze", "file": upload, "command": "Create lecturer accounts from this file"},
        )
        self.assertRedirects(response, reverse("portal:ai-automation"))
        job = AIAutomationJob.objects.get()
        self.assertEqual(job.status, AIAutomationJob.Status.ANALYZED)
        self.assertFalse(User.objects.filter(username="LEC-001").exists())

        response = self.client.post(reverse("portal:ai-automation"), {"action": "approve", "job_id": job.id})
        self.assertRedirects(response, reverse("portal:ai-automation"))
        job.refresh_from_db()
        self.assertEqual(job.status, AIAutomationJob.Status.APPROVED)

        response = self.client.post(reverse("portal:ai-automation"), {"action": "execute", "job_id": job.id})
        self.assertRedirects(response, reverse("portal:ai-automation"))
        job.refresh_from_db()
        self.assertEqual(job.status, AIAutomationJob.Status.EXECUTED)
        self.assertTrue(User.objects.filter(username="LEC-001", role=User.Role.LECTURER).exists())
        self.assertTrue(AuditLog.objects.filter(action="ai_automation_executed", object_id=str(job.id)).exists())

class DepartmentAutomationTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Computer Science", code="CSC")
        self.course = Course.objects.create(
            department=self.department,
            code="CSC508",
            title="Structured Programming",
            level="500",
            semester="second",
        )
        self.continuation_course = Course.objects.create(
            department=self.department,
            code="CSC510",
            title="System Modelling",
            level="500",
            semester="second",
        )

    def test_allocation_upload_registers_the_matched_department_lecturer(self):
        lecturer_upload = DepartmentLecturerUpload.objects.create(
            department=self.department,
            file=SimpleUploadedFile(
                "lecturers.csv",
                b"Lecturer ID,Lecturer Name,Phone Number\nLEC-CS-001,Prof. E. J. Garba,08030000001\nLEC-CS-002,Dr. K. O. Oluborode,08030000002\n",
                content_type="text/csv",
            ),
        )

        self.assertIn("2 accounts created", import_department_lecturers(lecturer_upload))
        lecturer = User.objects.get(username="LEC-CS-001")
        co_lecturer = User.objects.get(username="LEC-CS-002")
        self.assertEqual(lecturer.department, self.department)

        allocation_upload = CourseAllocationUpload.objects.create(
            department=self.department,
            file=SimpleUploadedFile(
                "allocation.csv",
                b"LECTURER,CODE,COURSE TITLE,UNIT\nE. J. Garba,CSC508(E),Structured Programming,3\n,CSC510,System Modelling,3\nDr. K. O. Oluborode,CSC508,Structured Programming,3\n,CSC424,Distributed Computing,3\n",
                content_type="text/csv",
            ),
        )

        self.assertIn("4 allocations assigned", import_course_allocations(allocation_upload))
        self.course.refresh_from_db()
        self.assertEqual(self.course.lecturer, lecturer)


        self.assertTrue(LecturerCourseRegistration.objects.filter(lecturer=lecturer, course=self.course).exists())
        self.assertTrue(LecturerCourseRegistration.objects.filter(lecturer=lecturer, course=self.continuation_course).exists())
        self.assertTrue(LecturerCourseRegistration.objects.filter(lecturer=co_lecturer, course=self.course).exists())
        created_course = Course.objects.get(code="CSC424")
        self.assertEqual(created_course.level, "400")
        self.assertTrue(LecturerCourseRegistration.objects.filter(lecturer=co_lecturer, course=created_course).exists())

    def test_scanned_allocation_uses_vision_records(self):
        lecturer = User.objects.create_user(
            username="LEC-CS-001",
            password="password",
            role=User.Role.LECTURER,
            department=self.department,
            first_name="Prof.",
            last_name="E. J. Garba",
        )
        allocation_upload = CourseAllocationUpload.objects.create(
            department=self.department,
            file=SimpleUploadedFile("allocation.jpg", b"image-bytes", content_type="image/jpeg"),
        )

        with patch("portal.automation._ai_extract_image", return_value=[
            {"lecturer_name": "Prof. E. J. Garba", "course_code": "CSC508"},
        ]):
            self.assertIn("1 allocations assigned", import_course_allocations(allocation_upload))

        self.course.refresh_from_db()
        self.assertEqual(self.course.lecturer, lecturer)

    def test_programme_import_uses_ai_records_and_keeps_the_selected_department(self):
        with patch("portal.automation._ai_extract", return_value=[
            {"programme_code": "BSC-CS", "programme_name": "Computer Science", "award": "BSc", "duration_years": "4"},
        ]):
            summary = import_programmes(
                SimpleUploadedFile("programmes.csv", b"programme catalogue", content_type="text/csv"),
                self.department,
            )

        programme = Programme.objects.get(code="BSC-CS")
        self.assertIn("1 created", summary)
        self.assertEqual(programme.department, self.department)
        self.assertEqual(programme.duration_years, 4)


class CurriculumCohortTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Computer Science", code="CSC")
        self.old_session = AcademicSession.objects.create(name="2040/2041", is_current=True)

    def upload_handbook(self, session, content):
        handbook = Handbook.objects.create(
            department=self.department,
            title="Computer Science Handbook",
            academic_session=session.name,
            file=SimpleUploadedFile("handbook.txt", content, content_type="text/plain"),
        )
        from .automation import import_handbook_courses

        import_handbook_courses(handbook)
        return handbook

    def create_student(self, username, curriculum=None):
        return User.objects.create_user(
            username=username,
            password="pass1234",
            role=User.Role.STUDENT,
            department=self.department,
            curriculum=curriculum,
            level="100",
            id_number=f"ID-{username}",
            email=f"{username}@example.com",
        )

    def test_new_handbook_only_changes_curriculum_for_new_students(self):
        self.upload_handbook(self.old_session, b"CSC101 Old Foundations 100 2 units")
        old_curriculum = self.department.curricula.get(effective_session=self.old_session)
        returning_student = self.create_student("returning", curriculum=old_curriculum)

        new_session = AcademicSession.objects.create(name="2041/2042", is_current=True)
        self.upload_handbook(new_session, b"CSC101 New Foundations 100 3 units")
        new_curriculum = self.department.curricula.get(effective_session=new_session)
        self.assertNotEqual(old_curriculum, new_curriculum)

        response = self.client.post(
            reverse("portal:student-signup"),
            {
                "username": "freshstudent",
                "first_name": "Fresh",
                "last_name": "Student",
                "email": "freshstudent@example.com",
                "id_number": "STU-FRESH",
                "department": self.department.id,
                "level": "100",
                "phone_number": "08000000000",
                "password1": "pass1234",
                "password2": "pass1234",
            },
        )
        self.assertEqual(response.status_code, 302)
        new_student = User.objects.get(username="freshstudent")
        self.assertEqual(new_student.curriculum, new_curriculum)

        self.client.force_login(returning_student)
        returning_catalog = self.client.get(reverse("portal:student-courses"), {"search": "CSC101"})
        self.assertContains(returning_catalog, "Old Foundations")
        self.assertNotContains(returning_catalog, "New Foundations")

        self.client.force_login(new_student)
        new_catalog = self.client.get(reverse("portal:student-courses"), {"search": "CSC101"})
        self.assertContains(new_catalog, "New Foundations")
        self.assertNotContains(new_catalog, "Old Foundations")

        old_course = old_curriculum.courses.get(code="CSC101")
        blocked_registration = self.client.post(
            reverse("portal:student-courses"),
            {"action": "register-course", "course_id": old_course.id},
        )
        self.assertEqual(blocked_registration.status_code, 404)

    def test_latest_handbook_stays_in_force_when_a_new_session_has_none(self):
        self.upload_handbook(self.old_session, b"CSC102 Continuing Curriculum 100 2 units")
        old_curriculum = self.department.curricula.get(effective_session=self.old_session)
        AcademicSession.objects.create(name="2041/2042", is_current=True)

        response = self.client.post(
            reverse("portal:student-signup"),
            {
                "username": "newwithoutreplacement",
                "first_name": "New",
                "last_name": "Student",
                "email": "newwithoutreplacement@example.com",
                "id_number": "STU-CONTINUING",
                "department": self.department.id,
                "level": "100",
                "phone_number": "08000000000",
                "password1": "pass1234",
                "password2": "pass1234",
            },
        )

        self.assertEqual(response.status_code, 302)
        student = User.objects.get(username="newwithoutreplacement")
        self.assertEqual(student.curriculum, old_curriculum)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DashboardAndCourseFlowTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Computer Science", code="CSC")
        self.lecturer = User.objects.create_user(
            username="lecturer1",
            password="pass1234",
            role=User.Role.LECTURER,
            department=self.department,
            is_approved=True,
            first_name="Ada",
            last_name="Lecturer",
            email="lecturer@example.com",
        )
        self.student = User.objects.create_user(
            username="student1",
            password="pass1234",
            role=User.Role.STUDENT,
            department=self.department,
            id_number="STU-001",
            first_name="John",
            last_name="Student",
            email="student@example.com",
        )
        self.admin = User.objects.create_user(
            username="admin1",
            password="pass1234",
            role=User.Role.ADMIN,
        )
        self.factory = RequestFactory()
        self.free_course = Course.objects.create(
            department=self.department,
            lecturer=self.lecturer,
            code="CSC101",
            title="Intro to Computing",
            level="100",
            file=SimpleUploadedFile("free.pdf", b"free course file", content_type="application/pdf"),
            is_free=True,
        )
        self.paid_course = Course.objects.create(
            department=self.department,
            lecturer=self.lecturer,
            code="CSC201",
            title="Data Structures",
            level="200",
            schedule_day="Monday",
            venue="LT 1",
            file=SimpleUploadedFile("paid.pdf", b"paid course file", content_type="application/pdf"),
            is_free=False,
            amount=Decimal("2500.00"),
        )
        CoursePaymentGateway.objects.create(
            slug="courses",
            paystack_public_key="pk_test_course",
            paystack_secret_key="sk_test_course",
        )
        DepartmentCoursePaymentGateway.objects.create(
            department=self.department,
            paystack_public_key="pk_test_course",
            paystack_secret_key="sk_test_course",
        )
        DepartmentPaymentGateway.objects.create(
            department=self.department,
            paystack_public_key="pk_test_department",
            paystack_secret_key="sk_test_department",
        )
        self.session, _ = AcademicSession.objects.get_or_create(name="2026/2027", defaults={"is_current": True})
        if not self.session.is_current:
            self.session.is_current = True
            self.session.save(update_fields=["is_current", "updated_at"])
        for code, name, amount in [
            ("departmental_fee", "Departmental Fee", Decimal("3000.00")),
            ("acf", "ACF", Decimal("1000.00")),
            ("mssn", "MSSN", Decimal("1000.00")),
        ]:
            association, _ = DepartmentalAssociation.objects.get_or_create(
                code=code,
                defaults={"name": name, "is_constant": True},
            )
            DepartmentalFee.objects.update_or_create(
                session=self.session,
                department=self.department,
                association=association,
                defaults={"amount": amount},
            )

    def test_student_dashboard_shows_department_and_course_actions(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.free_course, registered_by=self.student)
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-dashboard"))

        self.assertContains(response, self.department.name)
        self.assertContains(response, reverse("portal:download-course-file", args=[self.free_course.id]))
        self.assertContains(response, 'name="action" value="pay-course"', html=False)

    def test_student_departmental_shows_assignment_message_when_department_missing(self):
        self.student.department = None
        self.student.save(update_fields=["department"])

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-departmental"))

        self.assertContains(response, "Your account does not have a department yet.")
        self.assertContains(response, reverse("portal:profile"))

    def test_student_signup_redirects_to_dashboard(self):
        response = self.client.post(
            reverse("portal:student-signup"),
            {
                "username": "newstudent",
                "first_name": "New",
                "last_name": "Student",
                "email": "newstudent@example.com",
                "id_number": "STU-NEW",
                "department": self.department.id,
                "level": "100",
                "phone_number": "08000000000",
                "password1": "pass1234",
                "password2": "pass1234",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("portal:student-dashboard"))

    def test_lecturer_without_department_can_open_departmental_download(self):
        self.lecturer.department = None
        self.lecturer.save(update_fields=["department"])
        session, _ = AcademicSession.objects.get_or_create(name="2030/2031", defaults={"is_current": True})
        association, _ = DepartmentalAssociation.objects.get_or_create(
            code="departmental_fee",
            defaults={"name": "Departmental Fee", "is_constant": True},
        )
        DepartmentalFee.objects.get_or_create(
            session=session,
            department=self.department,
            association=association,
            defaults={"amount": Decimal("4000.00")},
        )
        DepartmentalPayment.objects.create(
            student=self.student,
            department=self.department,
            session=session,
            status=DepartmentalPayment.Status.PAID,
            total_amount=Decimal("4000.00"),
            association_summary="Departmental Fee",
        )

        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:lecturer-departmental"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Departmental Download")

    def test_admin_departmental_page_lists_student_documents_on_click(self):
        from .models import AcademicSession, DepartmentalAssociation, DepartmentalFee, DepartmentalPayment, DepartmentalPaymentDocument

        session, _ = AcademicSession.objects.get_or_create(name="2031/2032", defaults={"is_current": True})
        association, _ = DepartmentalAssociation.objects.get_or_create(
            code="departmental_fee",
            defaults={"name": "Departmental Fee", "is_constant": True},
        )
        DepartmentalFee.objects.get_or_create(
            session=session,
            department=self.department,
            association=association,
            defaults={"amount": Decimal("4000.00")},
        )
        payment = DepartmentalPayment.objects.create(
            student=self.student,
            department=self.department,
            session=session,
            status=DepartmentalPayment.Status.PAID,
            total_amount=Decimal("4000.00"),
            association_summary="Departmental Fee",
        )
        DepartmentalPaymentDocument.objects.create(
            payment=payment,
            category=DepartmentalPaymentDocument.Category.WHITE_FORM,
            title="White Form",
            file=SimpleUploadedFile("white.pdf", b"white", content_type="application/pdf"),
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse("portal:admin-departmental-download"), {"payment": payment.id})

        self.assertContains(response, self.student.full_name)
        self.assertContains(response, "White Form")
        self.assertContains(response, reverse("portal:download-departmental-document", args=[payment.documents.first().id]))

    def test_staff_student_view_includes_all_paid_departmental_sessions_and_documents(self):
        from .models import DepartmentalPaymentDocument

        past_session = AcademicSession.objects.create(name="2025/2026", is_current=False)
        past_payment = DepartmentalPayment.objects.create(
            student=self.student,
            department=self.department,
            session=past_session,
            status=DepartmentalPayment.Status.PAID,
            total_amount=Decimal("3500.00"),
            association_summary="Departmental Fee, ACF",
        )
        current_payment = DepartmentalPayment.objects.create(
            student=self.student,
            department=self.department,
            session=self.session,
            status=DepartmentalPayment.Status.PAID,
            total_amount=Decimal("4000.00"),
            association_summary="Departmental Fee, MSSN",
        )
        past_document = DepartmentalPaymentDocument.objects.create(
            payment=past_payment,
            category=DepartmentalPaymentDocument.Category.MEDICAL_FITNESS,
            title="medical-fitness.pdf",
            file=SimpleUploadedFile("medical-fitness.pdf", b"medical", content_type="application/pdf"),
        )
        current_document = DepartmentalPaymentDocument.objects.create(
            payment=current_payment,
            category=DepartmentalPaymentDocument.Category.WHITE_FORM,
            title="white.pdf",
            file=SimpleUploadedFile("white.pdf", b"white", content_type="application/pdf"),
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse("portal:admin-departmental-download"), {"payment": current_payment.id})

        self.assertContains(response, past_session.name)
        self.assertContains(response, self.session.name)
        self.assertContains(response, "Medical fitness")
        self.assertContains(response, reverse("portal:download-departmental-document", args=[past_document.id]))
        self.assertContains(response, reverse("portal:download-departmental-document", args=[current_document.id]))

        self.client.force_login(self.lecturer)
        lecturer_response = self.client.get(reverse("portal:lecturer-departmental"), {"payment": current_payment.id})

        self.assertContains(lecturer_response, past_session.name)
        self.assertContains(lecturer_response, reverse("portal:download-departmental-document", args=[past_document.id]))

    def test_paid_student_can_upload_additional_departmental_documents(self):
        from .models import DepartmentalPaymentDocument

        payment = DepartmentalPayment.objects.create(
            student=self.student,
            department=self.department,
            session=self.session,
            status=DepartmentalPayment.Status.PAID,
            total_amount=Decimal("4000.00"),
            association_summary="Departmental Fee, ACF",
        )

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:student-departmental"),
            {
                "action": "add-documents",
                "additional_documents": [
                    SimpleUploadedFile("extra-one.pdf", b"one", content_type="application/pdf"),
                    SimpleUploadedFile("extra-two.pdf", b"two", content_type="application/pdf"),
                ],
            },
        )

        self.assertRedirects(response, reverse("portal:student-departmental"))
        self.assertEqual(
            DepartmentalPaymentDocument.objects.filter(
                payment=payment,
                category=DepartmentalPaymentDocument.Category.ADDITIONAL,
            ).count(),
            2,
        )

    def test_admin_cannot_create_a_departmental_association(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f'{reverse("portal:admin-departmental-fees")}?department={self.department.id}',
            {
                "action": "create-association",
                "department_id": self.department.id,
                "name": "Computer Society Levy",
            },
        )

        self.assertRedirects(response, f'{reverse("portal:admin-departmental-fees")}?department={self.department.id}')
        self.assertFalse(DepartmentalAssociation.objects.filter(name="Computer Society Levy").exists())

    def test_registered_paid_course_without_payment_shows_pay_even_without_file(self):
        paid_course_without_file = Course.objects.create(
            department=self.department,
            lecturer=self.lecturer,
            code="CSC301",
            title="Algorithms",
            level="300",
            is_free=False,
            amount=Decimal("1800.00"),
        )
        StudentCourseRegistration.objects.create(
            student=self.student,
            course=paid_course_without_file,
            registered_by=self.student,
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-courses"))

        self.assertContains(response, 'name="action" value="pay-course"', html=False)
        self.assertContains(response, "Payment 1800.00")

    def test_student_courses_shows_download_and_remove_together_for_paid_course_after_payment(self):
        registration = StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-courses"))

        self.assertContains(response, reverse("portal:download-course-file", args=[self.paid_course.id]))
        self.assertContains(response, reverse("portal:student-course-delete", args=[registration.id]))
        self.assertContains(response, "Remove")

    def test_student_can_reregister_paid_course_without_new_payment(self):
        registration = StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )

        self.client.force_login(self.student)
        remove_response = self.client.post(reverse("portal:student-course-delete", args=[registration.id]))
        self.assertEqual(remove_response.status_code, 302)
        self.assertFalse(StudentCourseRegistration.objects.filter(student=self.student, course=self.paid_course).exists())

        page_response = self.client.get(reverse("portal:student-courses"), {"search": self.paid_course.code})
        self.assertContains(page_response, self.paid_course.code)
        self.assertContains(page_response, 'name="action" value="register-course"', html=False)
        self.assertNotContains(page_response, 'name="action" value="pay-course"', html=False)

        register_response = self.client.post(
            reverse("portal:student-courses"),
            {
                "action": "register-course",
                "course_id": self.paid_course.id,
            },
        )

        self.assertEqual(register_response.status_code, 302)
        self.assertTrue(StudentCourseRegistration.objects.filter(student=self.student, course=self.paid_course).exists())
        self.assertEqual(CoursePayment.objects.filter(student=self.student, course=self.paid_course).count(), 1)

    def test_student_courses_shows_download_button_for_free_registered_course(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.free_course, registered_by=self.student)

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-courses"))

        self.assertContains(response, reverse("portal:download-course-file", args=[self.free_course.id]))

    def test_student_course_filter_combines_search_and_level(self):
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("portal:student-courses"),
            {"level": "100", "search": "computing"},
        )

        self.assertContains(response, self.free_course.code)
        self.assertNotContains(response, self.paid_course.code)

    def test_student_course_filter_rejects_another_department(self):
        other_department = Department.objects.create(name="Mathematics", code="MTH")
        other_course = Course.objects.create(
            department=other_department,
            code="MTH101",
            title="Calculus",
            level="100",
            is_free=True,
        )
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("portal:student-courses"),
            {"department": other_department.id, "search": "calculus"},
        )

        self.assertContains(response, "Select a valid choice")
        self.assertNotContains(response, other_course.code)

    def test_lecturer_can_amend_paid_course_amount(self):
        self.client.force_login(self.lecturer)
        response = self.client.post(
            reverse("portal:lecturer-courses"),
            {
                "action": "update-course",
                "course_id": self.paid_course.id,
                "title": self.paid_course.title,
                "description": self.paid_course.description,
                "level": self.paid_course.level,
                "semester": self.paid_course.semester,
                "credit_units": self.paid_course.credit_units,
                "schedule_day": self.paid_course.schedule_day,
                "schedule_time": "",
                "venue": self.paid_course.venue,
                "amount": "3200.00",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.paid_course.refresh_from_db()
        self.assertEqual(self.paid_course.amount, Decimal("3200.00"))

    def test_lecturer_amount_keeps_course_paid_even_if_free_flag_is_checked(self):
        self.client.force_login(self.lecturer)
        response = self.client.post(
            reverse("portal:lecturer-courses"),
            {
                "action": "update-course",
                "course_id": self.paid_course.id,
                "title": self.paid_course.title,
                "description": self.paid_course.description,
                "level": self.paid_course.level,
                "semester": self.paid_course.semester,
                "credit_units": self.paid_course.credit_units,
                "schedule_day": self.paid_course.schedule_day,
                "schedule_time": "",
                "venue": self.paid_course.venue,
                "is_free": "on",
                "amount": "3200.00",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.paid_course.refresh_from_db()
        self.assertFalse(self.paid_course.is_free)
        self.assertEqual(self.paid_course.amount, Decimal("3200.00"))

    def test_student_sidebar_hides_downloads_menu(self):
        request = self.factory.get(reverse("portal:student-dashboard"))
        request.user = self.student
        request.resolver_match = None

        labels = [item["label"] for item in sidebar_links(request)]

        self.assertNotIn("Downloads", labels)

    def test_lecturer_sidebar_uses_paid_courses_label(self):
        request = self.factory.get(reverse("portal:lecturer-dashboard"))
        request.user = self.lecturer
        request.resolver_match = None

        paid_courses_item = next(item for item in sidebar_links(request) if item["label"] == "Paid courses")
        labels = [item["label"] for item in sidebar_links(request)]

        self.assertIn("Paid courses", labels)
        self.assertEqual(paid_courses_item["url"], f'{reverse("portal:lecturer-courses")}?fee_type=paid')
        self.assertNotIn("Manage Students", labels)

    def test_lecturer_menu_uses_register_courses_label(self):
        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:lecturer-dashboard"))

        self.assertContains(response, "Register Courses")
        self.assertNotContains(response, "My Courses")

    def test_sidebar_keeps_timetable_and_handbook_outside_institution_admin_navigation(self):
        student_request = self.factory.get(reverse("portal:student-dashboard"))
        student_request.user = self.student
        student_request.resolver_match = None
        lecturer_request = self.factory.get(reverse("portal:lecturer-dashboard"))
        lecturer_request.user = self.lecturer
        lecturer_request.resolver_match = None
        admin_request = self.factory.get(reverse("portal:admin-dashboard"))
        admin_request.user = self.admin
        admin_request.resolver_match = None

        self.assertIn("Timetable and Handbook", [item["label"] for item in sidebar_links(student_request)])
        self.assertIn("Timetable and Handbook", [item["label"] for item in sidebar_links(lecturer_request)])
        self.assertNotIn("Timetable and Handbook", [item["label"] for item in sidebar_links(admin_request)])
        self.assertNotIn("Documents", [item["label"] for item in sidebar_links(student_request)])
        self.assertNotIn("Documents", [item["label"] for item in sidebar_links(lecturer_request)])
        self.assertNotIn("Documents", [item["label"] for item in sidebar_links(admin_request)])

    def test_lecturer_paid_courses_menu_shows_only_paid_registered_courses(self):
        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:lecturer-courses"), {"fee_type": "paid"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paid Courses")
        self.assertContains(response, self.paid_course.code)
        self.assertNotContains(response, self.free_course.code)

    def test_lecturer_can_remove_registered_course_and_register_again(self):
        LecturerCourseRegistration.objects.create(lecturer=self.lecturer, course=self.paid_course)
        self.client.force_login(self.lecturer)

        remove_response = self.client.post(
            reverse("portal:lecturer-courses"),
            {
                "action": "remove-course",
                "course_id": self.paid_course.id,
            },
        )

        self.assertEqual(remove_response.status_code, 302)
        self.paid_course.refresh_from_db()
        self.assertIsNone(self.paid_course.lecturer)
        self.assertFalse(LecturerCourseRegistration.objects.filter(lecturer=self.lecturer, course=self.paid_course).exists())

        page_response = self.client.get(reverse("portal:lecturer-courses"), {"search": self.paid_course.code})
        self.assertContains(page_response, self.paid_course.code)
        self.assertContains(page_response, 'name="action" value="register-course"', html=False)

        register_response = self.client.post(
            reverse("portal:lecturer-courses"),
            {
                "action": "register-course",
                "course_id": self.paid_course.id,
            },
        )

        self.assertEqual(register_response.status_code, 302)
        self.paid_course.refresh_from_db()
        self.assertEqual(self.paid_course.lecturer, self.lecturer)
        self.assertTrue(LecturerCourseRegistration.objects.filter(lecturer=self.lecturer, course=self.paid_course).exists())

    def test_blank_course_gateway_stays_unconfigured_until_admin_saves_keys(self):
        CoursePaymentGateway.objects.all().delete()

        from .views import course_payment_gateway

        gateway = course_payment_gateway()

        self.assertEqual(gateway.paystack_public_key, "")
        self.assertEqual(gateway.paystack_secret_key, "")
        self.assertFalse(gateway.is_configured)

    def test_blank_department_gateway_stays_unconfigured(self):
        DepartmentPaymentGateway.objects.filter(department=self.department).update(
            paystack_public_key="",
            paystack_secret_key="",
        )

        from .views import ensure_department_gateway_credentials

        gateway = ensure_department_gateway_credentials(self.department)

        self.assertEqual(gateway.paystack_public_key, "")
        self.assertEqual(gateway.paystack_secret_key, "")
        self.assertFalse(gateway.is_configured)

    def test_admin_menu_does_not_show_view_courses_label(self):
        request = self.factory.get(reverse("portal:admin-dashboard"))
        request.user = self.admin
        request.resolver_match = None

        labels = [item["label"] for item in sidebar_links(request)]

        self.assertNotIn("View Courses", labels)

    def test_admin_menu_does_not_show_course_api_label(self):
        request = self.factory.get(reverse("portal:admin-dashboard"))
        request.user = self.admin
        request.resolver_match = None

        labels = [item["label"] for item in sidebar_links(request)]

        self.assertNotIn("Course API", labels)
        self.assertIn("Departmental", labels)
        departmental_link = next(item for item in sidebar_links(request) if item["label"] == "Departmental")
        self.assertEqual(
            [child["label"] for child in departmental_link["children"]],
            ["View APIs", "View Fees", "Students"],
        )

    def test_admin_can_manage_programmes_while_hod_has_its_department_workspace(self):
        self.department.head_of_department = self.lecturer
        self.department.save(update_fields=["head_of_department", "updated_at"])

        admin_request = self.factory.get(reverse("portal:admin-dashboard"))
        admin_request.user = self.admin
        admin_request.resolver_match = None
        self.assertIn("Programmes", [item["label"] for item in sidebar_links(admin_request)])

        self.client.force_login(self.admin)
        created = self.client.post(
            reverse("portal:admin-programmes"),
            {
                "programme-faculty": self.department.faculty_id,
                "programme-department": self.department.id,
                "programme-name": "Computer Science",
                "programme-code": "BSC-CS",
                "programme-award": "BSc",
                "programme-duration_years": 4,
                "programme-is_active": "on",
            },
        )
        self.assertRedirects(created, reverse("portal:admin-programmes"))
        programme = Programme.objects.get(code="BSC-CS")
        self.assertEqual(programme.department, self.department)
        self.assertTrue(AuditLog.objects.filter(action="programme_created", user=self.admin).exists())

        institution = Institution.objects.create(
            name="Programme University",
            institution_code="PROGRAMMES",
            email="admin@programmes.example.test",
            subdomain="programmes",
            status=Institution.Status.ACTIVE,
        )
        self.admin.institution = institution
        self.admin.save(update_fields=["institution"])
        billing = self.client.get(
            reverse("portal:billing-overview"),
            HTTP_HOST=f"{institution.subdomain}.{settings.CORE_DOMAIN}",
        )
        self.assertEqual(billing.status_code, 200)
        self.assertContains(billing, reverse("portal:admin-programmes"))
        self.assertContains(billing, "Subscription &amp; Billing", html=False)

        self.client.force_login(self.lecturer)
        workspace = self.client.get(reverse("portal:hod-programmes"))
        self.assertEqual(workspace.status_code, 200)
        self.assertContains(workspace, programme.name)
        detail = self.client.get(reverse("portal:hod-programme-detail", args=[programme.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Upload programme handbook")

        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("portal:hod-programmes")).status_code, 403)

    def test_hod_login_keeps_the_departmental_submenu_visible(self):
        self.department.head_of_department = self.lecturer
        self.department.save(update_fields=["head_of_department", "updated_at"])
        RoleAssignment.objects.create(
            institution=self.lecturer.institution,
            user=self.lecturer,
            role=User.Role.HOD,
            department=self.department,
        )

        response = self.client.post(
            reverse("portal:role-login", kwargs={"role": User.Role.HOD}),
            {"role": User.Role.HOD, "username": self.lecturer.username, "password": "pass1234"},
        )

        self.assertRedirects(response, reverse("portal:dashboard"), fetch_redirect_response=False)
        page = self.client.get(reverse("portal:hod-api-and-document"))
        self.assertContains(page, "API and Document")
        self.assertContains(page, "Set Fee")

    def test_student_must_select_a_programme_when_the_department_offers_one(self):
        programme = Programme.objects.create(
            department=self.department,
            name="Computer Science",
            code="BSC-CS",
            award="BSc",
        )
        payload = {
            "username": "programme-student",
            "first_name": "Programme",
            "last_name": "Student",
            "email": "programme-student@example.com",
            "id_number": "STU-PROGRAMME",
            "department": self.department.id,
            "level": "100",
            "phone_number": "08000000000",
            "password1": "pass1234",
            "password2": "pass1234",
        }

        missing_programme = self.client.post(reverse("portal:student-signup"), payload)
        self.assertEqual(missing_programme.status_code, 200)
        self.assertContains(missing_programme, "Choose your programme")

        created = self.client.post(
            reverse("portal:student-signup"), {**payload, "programme": programme.id},
        )
        self.assertRedirects(created, reverse("portal:student-dashboard"))
        self.assertEqual(User.objects.get(username="programme-student").programme, programme)

    def test_admin_can_search_departments_and_assign_an_hod(self):
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("portal:admin-hods"),
            {"search": "computer", "department": self.department.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.department.name)
        self.assertContains(response, self.lecturer.full_name)

        response = self.client.post(
            reverse("portal:admin-hods"),
            {
                "action": "assign-hod",
                "department_id": self.department.id,
                "lecturer_id": self.lecturer.id,
            },
        )

        self.assertRedirects(response, f'{reverse("portal:admin-hods")}?department={self.department.id}')
        self.department.refresh_from_db()
        self.assertEqual(self.department.head_of_department, self.lecturer)
        self.assertTrue(self.lecturer.has_role(User.Role.HOD))
        self.assertTrue(AuditLog.objects.filter(action="hod_assigned", user=self.admin).exists())

        request = self.factory.get(reverse("portal:lecturer-dashboard"))
        request.user = self.lecturer
        request.resolver_match = None
        request.session = {"active_role": User.Role.HOD}
        labels = [item["label"] for item in sidebar_links(request)]
        self.assertIn("Exam Officer", labels)
        self.assertIn("Course Allocation", labels)
        departmental_menu = next(item for item in sidebar_links(request) if item["label"] == "Departmental")
        self.assertEqual(
            [item["label"] for item in departmental_menu["children"]],
            ["API and Document", "Set Fee"],
        )
        self.assertNotIn("Timetable and Handbook", labels)

    def test_only_assigned_hod_can_update_its_departmental_api_and_fees(self):
        self.department.head_of_department = self.lecturer
        self.department.save(update_fields=["head_of_department", "updated_at"])
        first_fee = DepartmentalFee.objects.filter(
            session=self.session,
            department=self.department,
        ).select_related("association").first()
        self.client.force_login(self.lecturer)

        api_response = self.client.post(
            reverse("portal:hod-api-and-document"),
            {"paystack_public_key": "pk_hod", "paystack_secret_key": "sk_hod"},
        )
        self.assertRedirects(api_response, reverse("portal:hod-api-and-document"))
        gateway = DepartmentPaymentGateway.objects.get(department=self.department)
        self.assertEqual(gateway.paystack_public_key, "pk_hod")
        self.assertEqual(gateway.paystack_secret_key, "sk_hod")

        api_and_document_response = self.client.get(reverse("portal:hod-api-and-document"))
        self.assertContains(api_and_document_response, "Course Allocation Document")
        self.assertContains(api_and_document_response, "Add Course Allocation")
        self.assertContains(api_and_document_response, "Department Handbook")
        self.assertContains(api_and_document_response, "Upload Handbook")
        self.assertNotContains(api_and_document_response, "value=\"sk_hod\"", html=False)

        course_api_response = self.client.post(
            reverse("portal:hod-api-and-document"),
            {
                "action": "save-course-gateway",
                "paystack_public_key": "pk_course_hod",
                "paystack_secret_key": "sk_course_hod",
            },
        )
        self.assertRedirects(course_api_response, reverse("portal:hod-api-and-document"))
        course_gateway = DepartmentCoursePaymentGateway.objects.get(department=self.department)
        self.assertEqual(course_gateway.paystack_public_key, "pk_course_hod")
        self.assertEqual(course_gateway.paystack_secret_key, "sk_course_hod")

        public_key_only_response = self.client.post(
            reverse("portal:hod-api-and-document"),
            {"paystack_public_key": "pk_hod_updated"},
        )
        self.assertRedirects(public_key_only_response, reverse("portal:hod-api-and-document"))
        gateway.refresh_from_db()
        self.assertEqual(gateway.paystack_public_key, "pk_hod_updated")
        self.assertEqual(gateway.paystack_secret_key, "sk_hod")

        allocation_response = self.client.post(
            reverse("portal:hod-api-and-document"),
            {
                "action": "upload-allocations",
                "file": SimpleUploadedFile(
                    "allocation.csv",
                    b"lecturer_name,course_code\nAda Lecturer,CSC101\n",
                    content_type="text/csv",
                ),
            },
        )
        self.assertRedirects(allocation_response, reverse("portal:hod-api-and-document"))
        self.assertTrue(
            CourseAllocationUpload.objects.filter(
                department=self.department,
                uploaded_by=self.lecturer,
            ).exists()
        )

        handbook_response = self.client.post(
            reverse("portal:hod-api-and-document"),
            {
                "action": "upload-handbook",
                "file": SimpleUploadedFile(
                    "handbook.txt",
                    b"CSC101 Introduction to Computing 100 2 units",
                    content_type="text/plain",
                ),
            },
        )
        self.assertRedirects(handbook_response, reverse("portal:hod-api-and-document"))
        self.assertTrue(Handbook.objects.filter(department=self.department).exists())

        fee_response = self.client.post(
            reverse("portal:hod-departmental-fees"),
            {
                f"fee_{fee.association_id}": "5500.00" if fee.pk == first_fee.pk else str(fee.amount)
                for fee in DepartmentalFee.objects.filter(session=self.session, department=self.department)
            },
        )
        self.assertRedirects(fee_response, reverse("portal:hod-departmental-fees"))
        first_fee.refresh_from_db()
        self.assertEqual(first_fee.amount, Decimal("5500.00"))

        non_hod = User.objects.create_user(
            username="non-hod",
            password="pass1234",
            role=User.Role.LECTURER,
            department=self.department,
            is_approved=True,
        )
        self.client.force_login(non_hod)
        self.assertEqual(self.client.get(reverse("portal:hod-api-and-document")).status_code, 403)
        self.assertEqual(self.client.get(reverse("portal:hod-departmental-fees")).status_code, 403)

    def test_hod_can_create_a_department_specific_association_from_set_fees(self):
        self.department.head_of_department = self.lecturer
        self.department.save(update_fields=["head_of_department", "updated_at"])
        self.client.force_login(self.lecturer)

        response = self.client.post(
            reverse("portal:hod-departmental-fees"),
            {"action": "create-association", "name": "Computer Society Levy"},
        )

        self.assertRedirects(response, reverse("portal:hod-departmental-fees"))
        association = DepartmentalAssociation.objects.get(
            department=self.department,
            name="Computer Society Levy",
        )
        self.assertFalse(association.is_constant)
        self.assertTrue(
            DepartmentalFee.objects.filter(
                session=self.session,
                department=self.department,
                association=association,
            ).exists()
        )

        delete_response = self.client.post(
            reverse("portal:hod-departmental-fees"),
            {"action": "delete-association", "association_id": association.id},
        )
        self.assertRedirects(delete_response, reverse("portal:hod-departmental-fees"))
        self.assertFalse(DepartmentalAssociation.objects.filter(pk=association.id).exists())

    def test_lecturers_and_hods_can_download_handbooks(self):
        handbook = Handbook.objects.create(
            department=self.department,
            title="Computer Science Handbook",
            academic_session="2026/2027",
            file=SimpleUploadedFile("handbook.pdf", b"handbook", content_type="application/pdf"),
        )

        self.client.force_login(self.lecturer)
        lecturer_response = self.client.get(reverse("portal:lecturer-documents"))
        handbook_url = reverse("portal:download-document", args=["handbook", handbook.id])
        self.assertContains(lecturer_response, handbook_url)
        self.assertEqual(self.client.get(f"{handbook_url}?download=1").status_code, 200)

        self.department.head_of_department = self.lecturer
        self.department.save(update_fields=["head_of_department", "updated_at"])
        hod_response = self.client.get(reverse("portal:lecturer-documents"))
        self.assertContains(hod_response, handbook.title)

    def test_admin_departmental_fee_post_is_read_only(self):
        fee = DepartmentalFee.objects.filter(session=self.session, department=self.department).first()
        self.client.force_login(self.admin)

        response = self.client.post(
            f'{reverse("portal:admin-departmental-fees")}?department={self.department.id}',
            {"action": "update-session-fees", f"fee_{fee.association_id}": "9000.00"},
        )

        self.assertRedirects(response, f'{reverse("portal:admin-departmental-fees")}?department={self.department.id}')
        fee.refresh_from_db()
        self.assertNotEqual(fee.amount, Decimal("9000.00"))

    def test_admin_dashboard_shows_pending_lecturer_id(self):
        pending_lecturer = User.objects.create_user(
            username="pending-lecturer",
            password="pass1234",
            role=User.Role.LECTURER,
            department=self.department,
            id_number="LEC-001",
            is_approved=False,
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse("portal:admin-dashboard"))

        self.assertContains(response, pending_lecturer.id_number)

    def test_lecturer_documents_page_is_timetable_only(self):
        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:lecturer-documents"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload Timetable")
        self.assertNotContains(response, "Upload Handbook")
        self.assertContains(response, "Filter Handbooks")

    def test_lecturer_users_page_lists_departmental_students_only(self):
        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:lecturer-users"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paid Courses Students")
        self.assertContains(response, "Filter")

    def test_admin_departmental_users_page_is_available(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("portal:admin-departmental-users"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paid Departmental Students")

    def test_lecturer_can_download_paid_student_ids_pdf(self):
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )

        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:lecturer-paid-course-students-pdf", args=[self.paid_course.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("paid-students.pdf", response["Content-Disposition"])

    def test_lecturer_can_search_paid_course_student_by_id(self):
        StudentCourseRegistration.objects.create(
            student=self.student,
            course=self.paid_course,
            session=self.session,
            registered_by=self.student,
        )
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            session=self.session,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )

        self.client.force_login(self.lecturer)
        response = self.client.get(
            reverse("portal:lecturer-paid-course-student-search", args=[self.paid_course.id]),
            {"student_id": self.student.id_number},
        )

        self.assertContains(response, self.student.full_name)
        self.assertContains(response, self.student.id_number)
        self.assertContains(response, str(self.paid_course.amount))

    def test_paid_course_search_starts_empty_for_a_new_session(self):
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            session=self.session,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )
        new_session = AcademicSession.objects.create(name="2027/2028", is_current=True)

        self.client.force_login(self.lecturer)
        response = self.client.get(
            reverse("portal:lecturer-paid-course-student-search", args=[self.paid_course.id]),
            {"student_id": self.student.id_number},
        )

        self.assertContains(response, new_session.name)
        self.assertContains(response, "No paid student was found")
        self.assertNotContains(response, self.student.full_name)

    def test_updating_student_level_clears_course_registrations(self):
        self.student.level = "100"
        self.student.save(update_fields=["level"])
        StudentCourseRegistration.objects.create(student=self.student, course=self.free_course, registered_by=self.student)
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:profile"),
            {
                "username": self.student.username,
                "first_name": self.student.first_name,
                "last_name": self.student.last_name,
                "email": self.student.email,
                "id_number": self.student.id_number,
                "department": self.department.id,
                "level": "200",
                "phone_number": self.student.phone_number,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(StudentCourseRegistration.objects.filter(student=self.student).exists())

    def test_level_change_requires_a_new_payment_before_paid_course_reregistration(self):
        self.student.level = "100"
        self.student.save(update_fields=["level"])
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        original_payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            session=self.session,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )

        self.client.force_login(self.student)
        self.client.post(
            reverse("portal:profile"),
            {
                "username": self.student.username,
                "first_name": self.student.first_name,
                "last_name": self.student.last_name,
                "email": self.student.email,
                "id_number": self.student.id_number,
                "department": self.department.id,
                "level": "200",
                "phone_number": self.student.phone_number,
            },
        )

        original_payment.refresh_from_db()
        self.assertFalse(original_payment.is_active_for_registration)
        page_response = self.client.get(
            reverse("portal:student-courses"), {"search": self.paid_course.code}
        )
        self.assertContains(page_response, 'name="action" value="pay-course"', html=False)

    def test_new_academic_session_requires_payment_before_paid_course_reregistration(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            session=self.session,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )
        AcademicSession.objects.create(name="2027/2028", is_current=True)

        self.client.force_login(self.student)
        page_response = self.client.get(
            reverse("portal:student-courses"), {"search": self.paid_course.code}
        )

        self.assertContains(page_response, 'name="action" value="pay-course"', html=False)
        self.assertNotContains(page_response, "Already registered")

    def test_lecturer_can_create_random_paid_student_groups_and_download_one_group(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            session=self.session,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )

        self.client.force_login(self.lecturer)
        response = self.client.post(
            reverse("portal:lecturer-course-groups", args=[self.paid_course.id]),
            {"enable_grouping": "on", "grouping_method": "random", "group_names": "A1, A2"},
        )

        self.assertEqual(response.status_code, 302)
        groups = CourseStudentGroup.objects.filter(course=self.paid_course, session=self.session)
        self.assertEqual(set(groups.values_list("name", flat=True)), {"A1", "A2"})
        membership = CourseStudentGroupMembership.objects.get(student=self.student)
        self.client.force_login(self.student)
        student_page = self.client.get(reverse("portal:student-courses"))
        self.assertContains(student_page, membership.group.name)

        later_student = User.objects.create_user(
            username="later-student",
            password="pass1234",
            role=User.Role.STUDENT,
            department=self.department,
            id_number="STU-002",
        )
        CoursePayment.objects.create(
            student=later_student,
            course=self.paid_course,
            session=self.session,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )
        self.client.force_login(later_student)
        self.client.post(
            reverse("portal:student-courses"),
            {"action": "register-course", "course_id": self.paid_course.id},
        )
        later_membership = CourseStudentGroupMembership.objects.get(student=later_student)
        later_student_page = self.client.get(reverse("portal:student-courses"))
        self.assertContains(later_student_page, later_membership.group.name)

        self.client.force_login(self.lecturer)
        pdf_response = self.client.get(
            reverse("portal:lecturer-paid-course-students-pdf", args=[self.paid_course.id]),
            {"group": membership.group_id},
        )
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")

    def test_lecturer_can_leave_grouping_off_and_still_download_paid_student_ids(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            session=self.session,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )

        self.client.force_login(self.lecturer)
        response = self.client.post(reverse("portal:lecturer-course-groups", args=[self.paid_course.id]), {})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CourseStudentGroup.objects.filter(course=self.paid_course, session=self.session).exists())
        pdf_response = self.client.get(reverse("portal:lecturer-paid-course-students-pdf", args=[self.paid_course.id]))
        self.assertEqual(pdf_response.status_code, 200)

    def test_lecturer_can_delete_a_saved_course_group(self):
        group = CourseStudentGroup.objects.create(
            course=self.paid_course,
            lecturer=self.lecturer,
            session=self.session,
            name="A1",
            grouping_method=CourseStudentGroup.GroupingMethod.RANDOM,
        )
        CourseStudentGroupMembership.objects.create(group=group, student=self.student)

        self.client.force_login(self.lecturer)
        response = self.client.post(
            reverse("portal:lecturer-course-groups", args=[self.paid_course.id]),
            {"action": "delete-group", "group_id": group.id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CourseStudentGroup.objects.filter(pk=group.id).exists())
        self.assertFalse(CourseStudentGroupMembership.objects.filter(student=self.student).exists())

    @patch("portal.views.initialize_paystack_transaction")
    def test_course_payment_redirects_to_paystack_authorization_url(self, initialize_paystack_transaction):
        initialize_paystack_transaction.return_value = {"authorization_url": "https://checkout.paystack.com/mock-course"}
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:student-courses"),
            {"action": "pay-course", "course_id": self.paid_course.id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.paystack.com/mock-course")
        initialize_paystack_transaction.assert_called_once()
        self.assertEqual(initialize_paystack_transaction.call_args.kwargs["secret_key"], "sk_test_course")
        self.assertEqual(
            initialize_paystack_transaction.call_args.kwargs["metadata"]["student_id"],
            self.student.id_number,
        )

    @patch("portal.views.initialize_paystack_transaction")
    def test_paid_course_payment_prefers_its_department_course_api(self, initialize_paystack_transaction):
        initialize_paystack_transaction.return_value = {"authorization_url": "https://checkout.paystack.com/department-course"}
        DepartmentCoursePaymentGateway.objects.update_or_create(
            department=self.department,
            defaults={
                "paystack_public_key": "pk_department_course",
                "paystack_secret_key": "sk_department_course",
            },
        )
        self.client.force_login(self.student)

        response = self.client.post(
            reverse("portal:student-courses"),
            {"action": "pay-course", "course_id": self.paid_course.id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.paystack.com/department-course")
        self.assertEqual(initialize_paystack_transaction.call_args.kwargs["secret_key"], "sk_department_course")

    def test_paid_course_payment_requires_its_department_hod_api(self):
        DepartmentCoursePaymentGateway.objects.filter(department=self.department).delete()
        self.client.force_login(self.student)

        response = self.client.post(
            reverse("portal:student-courses"),
            {"action": "pay-course", "course_id": self.paid_course.id},
            follow=True,
        )

        self.assertContains(response, "The HOD has not configured a paid-course API")

    @patch("portal.views.initialize_paystack_transaction")
    def test_course_payment_starts_without_registration(self, initialize_paystack_transaction):
        self.client.force_login(self.student)
        initialize_paystack_transaction.return_value = {"authorization_url": "https://checkout.paystack.com/mock-course"}
        response = self.client.post(
            reverse("portal:student-courses"),
            {"action": "pay-course", "course_id": self.paid_course.id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.paystack.com/mock-course")
        initialize_paystack_transaction.assert_called_once()
        self.assertEqual(
            initialize_paystack_transaction.call_args.kwargs["metadata"]["student_id"],
            self.student.id_number,
        )
        self.assertTrue(
            CoursePayment.objects.filter(student=self.student, course=self.paid_course).exists()
        )

    @patch("portal.views.initialize_paystack_transaction")
    def test_course_payment_retry_rotates_reference(self, initialize_paystack_transaction):
        initialize_paystack_transaction.return_value = {"authorization_url": "https://checkout.paystack.com/mock-course"}
        payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PENDING,
            paystack_public_key_used="pk_test_course",
            paystack_secret_key_used="sk_test_course",
        )
        first_reference = payment.paystack_reference

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:student-courses"),
            {"action": "pay-course", "course_id": self.paid_course.id},
        )

        payment.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.paystack.com/mock-course")
        self.assertNotEqual(payment.paystack_reference, first_reference)
        self.assertEqual(
            initialize_paystack_transaction.call_args.kwargs["reference"],
            payment.paystack_reference,
        )

    @patch("portal.views.initialize_paystack_transaction")
    def test_course_payment_falls_back_to_browser_checkout_when_paystack_is_unreachable(self, initialize_paystack_transaction):
        initialize_paystack_transaction.side_effect = ValueError("Unable to reach Paystack right now. Please try again shortly.")
        self.client.force_login(self.student)

        response = self.client.post(
            reverse("portal:student-courses"),
            {"action": "pay-course", "course_id": self.paid_course.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open Paystack")
        self.assertContains(response, "Pay for CSC201")
        self.assertContains(response, "Paystack could not be reached from the server")

    @patch("portal.views.initialize_paystack_transaction")
    def test_departmental_payment_redirects_to_paystack_authorization_url(self, initialize_paystack_transaction):
        initialize_paystack_transaction.return_value = {"authorization_url": "https://checkout.paystack.com/mock-department"}
        DepartmentPaymentGateway.objects.filter(department=self.department).update(
            paystack_public_key="pk_test_department",
            paystack_secret_key="sk_test_department",
        )
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:student-departmental"),
            {
                "white_form": SimpleUploadedFile("white.pdf", b"white", content_type="application/pdf"),
                "school_receipt": SimpleUploadedFile("receipt.pdf", b"receipt", content_type="application/pdf"),
                "association_choice": "acf",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.paystack.com/mock-department")
        initialize_paystack_transaction.assert_called_once()
        self.assertEqual(initialize_paystack_transaction.call_args.kwargs["secret_key"], "sk_test_department")

    @patch("portal.views.initialize_paystack_transaction")
    def test_departmental_payment_falls_back_to_browser_checkout_when_paystack_is_unreachable(self, initialize_paystack_transaction):
        initialize_paystack_transaction.side_effect = ValueError("Unable to reach Paystack right now. Please try again shortly.")

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:student-departmental"),
            {
                "white_form": SimpleUploadedFile("white.pdf", b"white", content_type="application/pdf"),
                "school_receipt": SimpleUploadedFile("receipt.pdf", b"receipt", content_type="application/pdf"),
                "association_choice": "acf",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open Paystack")
        self.assertContains(response, "Pay Departmental Fees")
        self.assertContains(response, "Paystack could not be reached from the server")

    @patch("portal.views.initialize_paystack_transaction")
    def test_departmental_payment_requires_document_uploads(self, initialize_paystack_transaction):
        initialize_paystack_transaction.return_value = {"authorization_url": "https://checkout.paystack.com/mock-department"}

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:student-departmental"),
            {
                "association_choice": "acf",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please complete these departmental payment fields before starting payment")
        self.assertContains(response, "White Form")
        self.assertContains(response, "School Receipt")
        initialize_paystack_transaction.assert_not_called()

    @patch("portal.views.initialize_paystack_transaction")
    def test_departmental_payment_defaults_association_when_missing(self, initialize_paystack_transaction):
        initialize_paystack_transaction.return_value = {"authorization_url": "https://checkout.paystack.com/mock-department"}

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:student-departmental"),
            {
                "white_form": SimpleUploadedFile("white.pdf", b"white", content_type="application/pdf"),
                "school_receipt": SimpleUploadedFile("receipt.pdf", b"receipt", content_type="application/pdf"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.paystack.com/mock-department")
        initialize_paystack_transaction.assert_called_once()

    @patch("portal.views.initialize_paystack_transaction")
    def test_departmental_payment_accepts_supporting_documents(self, initialize_paystack_transaction):
        initialize_paystack_transaction.return_value = {"authorization_url": "https://checkout.paystack.com/mock-department"}

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("portal:student-departmental"),
            {
                "white_form": SimpleUploadedFile("white.pdf", b"white", content_type="application/pdf"),
                "school_receipt": SimpleUploadedFile("receipt.pdf", b"receipt", content_type="application/pdf"),
                "supporting_documents": [
                    SimpleUploadedFile("support-1.pdf", b"support-1", content_type="application/pdf"),
                    SimpleUploadedFile("support-2.pdf", b"support-2", content_type="application/pdf"),
                ],
                "association_choice": "acf",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.paystack.com/mock-department")
        initialize_paystack_transaction.assert_called_once()

    @patch("portal.services.urlopen")
    def test_paystack_error_surfaces_api_message(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://api.paystack.co/transaction/initialize",
            400,
            "Bad Request",
            hdrs=None,
            fp=BytesIO(b'{"message":"Invalid secret key"}'),
        )

        from .services import initialize_paystack_transaction

        with self.assertRaisesMessage(ValueError, "Paystack rejected the request: Invalid secret key"):
            initialize_paystack_transaction(
                secret_key="sk_test_bad",
                email="student@example.com",
                amount=Decimal("1000.00"),
                reference="ref-123",
                callback_url="https://example.com/callback",
            )

    @patch("portal.services.urlopen")
    def test_paystack_error_surfaces_http_body_when_not_json(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://api.paystack.co/transaction/initialize",
            400,
            "Bad Request",
            hdrs=None,
            fp=BytesIO(b"Invalid request body"),
        )

        from .services import initialize_paystack_transaction

        with self.assertRaisesMessage(ValueError, "Paystack rejected the request (HTTP 400): Invalid request body"):
            initialize_paystack_transaction(
                secret_key="sk_test_bad",
                email="student@example.com",
                amount=Decimal("1000.00"),
                reference="ref-123",
                callback_url="https://example.com/callback",
            )

    @patch("portal.services.urlopen")
    def test_paystack_request_sends_browser_like_user_agent(self, urlopen):
        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"status":true,"data":{"authorization_url":"https://checkout.paystack.com/mock"}}'

        captured_headers = {}

        def fake_urlopen(request, timeout=20):
            captured_headers.update(dict(request.header_items()))
            return DummyResponse()

        urlopen.side_effect = fake_urlopen

        from .services import initialize_paystack_transaction, PAYSTACK_USER_AGENT

        initialize_paystack_transaction(
            secret_key="sk_test_ok",
            email="student@example.com",
            amount=Decimal("1000.00"),
            reference="ref-123",
            callback_url="https://example.com/callback",
        )

        self.assertEqual(captured_headers.get("User-agent"), PAYSTACK_USER_AGENT)

    @patch("portal.views.verify_paystack_transaction")
    def test_course_payment_callback_verifies_transaction_before_marking_paid(self, verify_paystack_transaction):
        payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PENDING,
            paystack_public_key_used="pk_test_course",
            paystack_secret_key_used="sk_test_course",
        )
        verify_paystack_transaction.return_value = {
            "reference": payment.paystack_reference,
            "status": "success",
            "amount": 250000,
        }

        self.client.force_login(self.student)
        response = self.client.get(
            reverse("portal:course-payment-callback", args=[payment.id]),
            {"reference": payment.paystack_reference},
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(payment.status, CoursePayment.Status.PAID)

    @patch("portal.views.verify_paystack_transaction")
    def test_course_payment_callback_unlocks_course_material_access(self, verify_paystack_transaction):
        registration = StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        material_one = CourseMaterial.objects.create(
            course=self.paid_course,
            lecturer=self.lecturer,
            title="Week 1 Notes",
            file=SimpleUploadedFile("week1.pdf", b"week1", content_type="application/pdf"),
            is_free=False,
            amount=Decimal("500.00"),
        )
        material_two = CourseMaterial.objects.create(
            course=self.paid_course,
            lecturer=self.lecturer,
            title="Week 2 Slides",
            file=SimpleUploadedFile("week2.pdf", b"week2", content_type="application/pdf"),
            is_free=False,
            amount=Decimal("500.00"),
        )
        MaterialAccess.objects.create(material=material_one, student=self.student, status=MaterialAccess.Status.PENDING)
        MaterialAccess.objects.create(material=material_two, student=self.student, status=MaterialAccess.Status.PENDING)
        payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PENDING,
            paystack_public_key_used="pk_test_course",
            paystack_secret_key_used="sk_test_course",
        )
        verify_paystack_transaction.return_value = {
            "reference": payment.paystack_reference,
            "status": "success",
            "amount": 250000,
        }

        self.client.force_login(self.student)
        response = self.client.get(
            reverse("portal:course-payment-callback", args=[payment.id]),
            {"reference": payment.paystack_reference},
        )

        self.assertEqual(response.status_code, 302)
        for material in (material_one, material_two):
            access = MaterialAccess.objects.get(material=material, student=self.student)
            self.assertEqual(access.status, MaterialAccess.Status.PAID)

    def test_departmental_exam_card_download_is_pdf(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.free_course, registered_by=self.student)
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        InstitutionProfile.objects.create(name="Example University")
        payment = DepartmentalPayment.objects.create(
            student=self.student,
            department=self.department,
            session=self.session,
            total_amount=Decimal("4000.00"),
            status=DepartmentalPayment.Status.PAID,
            association_summary="Departmental Fee",
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:download-exam-card", args=[payment.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(".pdf", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn(b"Example University", response.content)
        self.assertIn(self.student.full_name.encode("utf-8"), response.content)
        self.assertIn(self.free_course.code.encode("utf-8"), response.content)
        self.assertIn(self.paid_course.code.encode("utf-8"), response.content)
        self.assertIn(b"SESSION", response.content)
        self.assertIn(self.session.name.encode("utf-8"), response.content)

    def test_paid_departmental_payment_shows_exam_card_download_to_student(self):
        payment = DepartmentalPayment.objects.create(
            student=self.student,
            department=self.department,
            session=self.session,
            total_amount=Decimal("4000.00"),
            status=DepartmentalPayment.Status.PAID,
            association_summary="Departmental Fee, ACF",
        )

        self.client.force_login(self.student)
        departmental_page = self.client.get(reverse("portal:student-departmental"))
        exam_card_url = reverse("portal:download-exam-card", args=[payment.id])

        self.assertEqual(departmental_page.status_code, 200)
        self.assertContains(departmental_page, "Payment completed.")
        self.assertContains(departmental_page, "Download Exam Card")
        self.assertContains(departmental_page, exam_card_url)

        download = self.client.get(exam_card_url)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "application/pdf")

    def test_admin_cannot_create_an_academic_session_from_departmental_fees(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.free_course, registered_by=self.student)
        LecturerCourseRegistration.objects.create(lecturer=self.lecturer, course=self.paid_course)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("portal:admin-departmental-fees"),
            {
                "action": "create-session",
                "name": "2027/2028",
                "is_current": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AcademicSession.objects.filter(name="2027/2028").exists())
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_current)
        self.assertTrue(StudentCourseRegistration.objects.exists())
        self.assertTrue(LecturerCourseRegistration.objects.exists())
        self.free_course.refresh_from_db()
        self.paid_course.refresh_from_db()
        self.assertEqual(self.free_course.lecturer, self.lecturer)
        self.assertEqual(self.paid_course.lecturer, self.lecturer)

    def test_course_material_batch_form_creates_multiple_files(self):
        from .forms import CourseMaterialBatchForm
        from .views import _create_course_materials_from_upload

        form = CourseMaterialBatchForm(
            data={
                "course": self.paid_course.id,
                "title": "Lecture Pack",
                "is_free": "on",
                "is_download_enabled": "on",
            },
            files=MultiValueDict(
                {
                    "files": [
                        SimpleUploadedFile("pack-1.pdf", b"pack-1", content_type="application/pdf"),
                        SimpleUploadedFile("pack-2.pdf", b"pack-2", content_type="application/pdf"),
                    ]
                }
            ),
            user=self.lecturer,
        )

        self.assertTrue(form.is_valid(), form.errors)
        created_materials = _create_course_materials_from_upload(form, lecturer=self.lecturer)

        self.assertEqual(len(created_materials), 2)
        self.assertEqual(CourseMaterial.objects.filter(course=self.paid_course).count(), 2)

    def test_student_materials_page_lists_all_course_files(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PAID,
        )
        material_one = CourseMaterial.objects.create(
            course=self.paid_course,
            lecturer=self.lecturer,
            title="Week 1 Notes",
            file=SimpleUploadedFile("week1.pdf", b"week1", content_type="application/pdf"),
            is_free=False,
            amount=Decimal("500.00"),
        )
        material_two = CourseMaterial.objects.create(
            course=self.paid_course,
            lecturer=self.lecturer,
            title="Week 2 Slides",
            file=SimpleUploadedFile("week2.pdf", b"week2", content_type="application/pdf"),
            is_free=False,
            amount=Decimal("500.00"),
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-materials"))

        self.assertContains(response, material_one.title)
        self.assertContains(response, material_two.title)
        self.assertContains(response, reverse("portal:download-material", args=[material_one.id]))
        self.assertContains(response, reverse("portal:download-material", args=[material_two.id]))

    @patch("portal.views.verify_paystack_transaction")
    def test_departmental_payment_callback_verifies_transaction_before_marking_paid(self, verify_paystack_transaction):
        from .models import AcademicSession, DepartmentalAssociation, DepartmentalFee, DepartmentalPayment

        session, _ = AcademicSession.objects.get_or_create(name="2030/2031", defaults={"is_current": True})
        association, _ = DepartmentalAssociation.objects.get_or_create(
            code="departmental_fee",
            defaults={"name": "Departmental Fee", "is_constant": True},
        )
        DepartmentalFee.objects.get_or_create(
            session=session,
            department=self.department,
            association=association,
            defaults={"amount": Decimal("4000.00")},
        )
        payment = DepartmentalPayment.objects.create(
            student=self.student,
            department=self.department,
            session=session,
            total_amount=Decimal("4000.00"),
            paystack_public_key_used="pk_test_department",
            paystack_secret_key_used="sk_test_department",
        )
        verify_paystack_transaction.return_value = {
            "reference": payment.paystack_reference,
            "status": "success",
            "amount": 400000,
        }

        self.client.force_login(self.student)
        response = self.client.get(
            reverse("portal:departmental-payment-callback", args=[payment.id]),
            {"reference": payment.paystack_reference},
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(payment.status, DepartmentalPayment.Status.PAID)

    def test_due_course_reminder_sends_email_30_minutes_before_class(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        now = timezone.localtime().replace(second=0, microsecond=0)
        if now.weekday() == 6:
            now = now - timedelta(days=1)
        weekday_labels = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        class_time = (now + timedelta(minutes=30)).time().replace(second=0, microsecond=0)
        self.paid_course.schedule_day = weekday_labels[now.weekday()]
        self.paid_course.schedule_time = class_time
        self.paid_course.save()

        dispatch_due_course_reminders(now=now)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(set(mail.outbox[0].bcc), {"student@example.com", "lecturer@example.com"})
        self.assertEqual(mail.outbox[0].to, [])
        self.assertIn(self.paid_course.title, mail.outbox[0].body)
        self.assertEqual(
            CourseReminderDispatch.objects.filter(channel=CourseReminderDispatch.Channel.EMAIL).count(),
            2,
        )
        self.assertFalse(
            CourseReminderDispatch.objects.filter(
                channel=CourseReminderDispatch.Channel.EMAIL,
                delivered_at__isnull=True,
            ).exists()
        )

    @patch("portal.services.get_connection")
    def test_failed_course_reminder_email_is_recorded_for_retry(self, get_connection):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        now = timezone.localtime().replace(second=0, microsecond=0)
        if now.weekday() == 6:
            now = now - timedelta(days=1)
        weekday_labels = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        self.paid_course.schedule_day = weekday_labels[now.weekday()]
        self.paid_course.schedule_time = (now + timedelta(minutes=30)).time().replace(second=0, microsecond=0)
        self.paid_course.save()
        get_connection.return_value.send_messages.return_value = 0

        dispatch_due_course_reminders(now=now)

        failed_dispatches = CourseReminderDispatch.objects.filter(channel=CourseReminderDispatch.Channel.EMAIL)
        self.assertEqual(failed_dispatches.count(), 2)
        self.assertTrue(failed_dispatches.filter(delivered_at__isnull=True, attempt_count=1).exists())
        self.assertTrue(failed_dispatches.exclude(last_error="").exists())

    def test_due_course_reminder_creates_browser_alert_for_opted_in_user(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        self.student.browser_alerts_enabled = True
        self.student.class_reminder_alerts_enabled = True
        self.student.save(update_fields=["browser_alerts_enabled", "class_reminder_alerts_enabled"])
        now = timezone.localtime().replace(second=0, microsecond=0)
        if now.weekday() == 6:
            now = now - timedelta(days=1)
        weekday_labels = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        self.paid_course.schedule_day = weekday_labels[now.weekday()]
        self.paid_course.schedule_time = (now + timedelta(minutes=30)).time().replace(second=0, microsecond=0)
        self.paid_course.save()

        dispatch_due_course_reminders(now=now)

        self.assertTrue(
            UserAlert.objects.filter(
                recipient=self.student,
                alert_type=UserAlert.AlertType.CLASS_REMINDER,
            ).exists()
        )

    def test_lecturer_message_creates_student_browser_alert(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        self.student.browser_alerts_enabled = True
        self.student.save(update_fields=["browser_alerts_enabled"])
        notification = Notification.objects.create(sender=self.lecturer, subject="Class update", body="Bring your lab manual.")

        from .services import deliver_notification

        deliver_notification(notification)

        self.assertTrue(NotificationRecipient.objects.filter(notification=notification, student=self.student).exists())
        self.assertTrue(
            UserAlert.objects.filter(
                recipient=self.student,
                alert_type=UserAlert.AlertType.MESSAGE,
                title="Class update",
            ).exists()
        )

    def test_course_catalogs_are_hidden_until_a_filter_or_search_is_supplied(self):
        unassigned_course = Course.objects.create(
            department=self.department,
            code="CSC303",
            title="Operating Systems",
            level="300",
        )

        self.client.force_login(self.student)
        student_response = self.client.get(reverse("portal:student-courses"))
        self.assertEqual(student_response.context["available_courses"].count(), 0)
        student_filtered_response = self.client.get(reverse("portal:student-courses"), {"search": self.free_course.code})
        self.assertContains(student_filtered_response, self.free_course.code)

        self.client.force_login(self.lecturer)
        lecturer_response = self.client.get(reverse("portal:lecturer-courses"))
        self.assertEqual(lecturer_response.context["available_courses"].count(), 0)
        lecturer_filtered_response = self.client.get(reverse("portal:lecturer-courses"), {"search": unassigned_course.code})
        self.assertContains(lecturer_filtered_response, unassigned_course.code)

    def test_lecturer_can_send_multiple_attachments_that_a_recipient_can_download(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        first_attachment = SimpleUploadedFile("week-one.pdf", b"lecture handout", content_type="application/pdf")
        second_attachment = SimpleUploadedFile("reading-list.pdf", b"reading list", content_type="application/pdf")

        self.client.force_login(self.lecturer)
        response = self.client.post(
            reverse("portal:lecturer-messages"),
            {
                "subject": "Week one handout",
                "body": "Please read the attached handout.",
                "attachments": [first_attachment, second_attachment],
            },
        )

        self.assertEqual(response.status_code, 302)
        notification = Notification.objects.get(sender=self.lecturer, subject="Week one handout")
        attachments = list(NotificationAttachment.objects.filter(notification=notification))
        self.assertEqual(len(attachments), 2)
        self.assertTrue(attachments[0].file.name.endswith(".pdf"))
        self.assertTrue(attachments[1].file.name.endswith(".pdf"))
        self.assertTrue(NotificationRecipient.objects.filter(notification=notification, student=self.student).exists())

        self.client.force_login(self.student)
        inbox_response = self.client.get(reverse("portal:student-messages"), {"open": notification.recipients.get(student=self.student).id})
        attachment_url = reverse("portal:download-notification-attachment", args=[attachments[0].id])
        self.assertContains(inbox_response, attachment_url)

        view_response = self.client.get(attachment_url)
        download_response = self.client.get(f"{attachment_url}?download=1")
        self.assertTrue(view_response["Content-Disposition"].startswith("inline"))
        self.assertTrue(download_response["Content-Disposition"].startswith("attachment"))

    def test_admin_academic_sessions_page_does_not_allow_identity_changes(self):
        self.client.force_login(self.admin)

        page_response = self.client.get(reverse("portal:admin-about"))

        self.assertContains(page_response, "Academic Sessions")
        self.assertContains(page_response, "Create Academic Session")
        self.assertNotContains(page_response, "Institution Details")
        self.assertNotContains(page_response, "University or institution name")

    def test_admin_can_create_academic_session_from_about_and_see_past_sessions(self):
        LecturerCourseRegistration.objects.create(lecturer=self.lecturer, course=self.paid_course)
        self.paid_course.lecturer = self.lecturer
        self.paid_course.save(update_fields=["lecturer", "updated_at"])
        StudentCourseRegistration.objects.create(
            student=self.student,
            course=self.paid_course,
            session=self.session,
            registered_by=self.student,
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("portal:admin-about"),
            {"action": "create-academic-session", "name": "2027/2028", "is_current": "on"},
        )

        self.assertEqual(response.status_code, 302)
        new_session = AcademicSession.objects.get(name="2027/2028")
        self.assertTrue(new_session.is_current)
        self.session.refresh_from_db()
        self.assertFalse(self.session.is_current)
        self.assertTrue(DepartmentalFee.objects.filter(session=new_session, department=self.department).exists())
        self.assertFalse(LecturerCourseRegistration.objects.filter(lecturer=self.lecturer).exists())
        self.paid_course.refresh_from_db()
        self.assertIsNone(self.paid_course.lecturer)
        self.assertFalse(
            StudentCourseRegistration.objects.filter(student=self.student, session=new_session).exists()
        )

        page_response = self.client.get(reverse("portal:admin-about"))
        self.assertContains(page_response, "Create Academic Session")
        self.assertContains(page_response, "Past Sessions")
        self.assertContains(page_response, self.session.name)

    def test_admin_departments_does_not_offer_course_allocation_uploads(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("portal:admin-departments"))

        self.assertNotContains(response, "Add Course Allocation")
        self.assertNotContains(response, 'value="upload-allocations"', html=False)

    def test_hod_allocation_replaces_the_department_teaching_assignments(self):
        self.department.head_of_department = self.lecturer
        self.department.save(update_fields=["head_of_department", "updated_at"])
        LecturerCourseRegistration.objects.create(lecturer=self.lecturer, course=self.free_course)
        self.free_course.lecturer = self.lecturer
        self.free_course.save(update_fields=["lecturer", "updated_at"])
        self.client.force_login(self.lecturer)

        response = self.client.post(
            reverse("portal:hod-api-and-document"),
            {
                "action": "upload-allocations",
                "file": SimpleUploadedFile(
                    "allocation.csv",
                    b"lecturer_name,course_code\nAda Lecturer,CSC201\n",
                    content_type="text/csv",
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.free_course.refresh_from_db()
        self.paid_course.refresh_from_db()
        self.assertIsNone(self.free_course.lecturer)
        self.assertEqual(self.paid_course.lecturer, self.lecturer)
        self.assertEqual(
            list(LecturerCourseRegistration.objects.filter(lecturer=self.lecturer).values_list("course_id", flat=True)),
            [self.paid_course.id],
        )

    def test_current_handbook_creates_courses_only_for_its_session(self):
        old_course = Course.objects.create(
            department=self.department,
            academic_session=self.session,
            code="CSC901",
            title="Previous Curriculum",
            level="100",
        )
        new_session = AcademicSession.objects.create(name="2027/2028", is_current=True)
        handbook = Handbook.objects.create(
            department=self.department,
            title="New Curriculum Handbook",
            academic_session=new_session.name,
            file=SimpleUploadedFile(
                "handbook.txt",
                b"CSC901 New Curriculum Course 100 2 units",
                content_type="text/plain",
            ),
        )

        from .automation import import_handbook_courses

        import_handbook_courses(handbook)
        new_course = Course.objects.get(
            department=self.department,
            academic_session=new_session,
            code="CSC901",
        )
        self.assertNotEqual(new_course.pk, old_course.pk)

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-courses"), {"search": "CSC901"})
        self.assertContains(response, "New Curriculum Course")
        self.assertNotContains(response, "Previous Curriculum")

    def test_student_message_menu_counter_clears_when_message_is_opened(self):
        notification = Notification.objects.create(
            sender=self.lecturer,
            subject="New announcement",
            body="Please check the course page.",
        )
        recipient = NotificationRecipient.objects.create(notification=notification, student=self.student)

        self.client.force_login(self.student)
        dashboard_response = self.client.get(reverse("portal:student-dashboard"))
        self.assertContains(dashboard_response, 'aria-label="1 unread message"', html=False)

        message_response = self.client.get(reverse("portal:student-messages"), {"open": recipient.id})
        recipient.refresh_from_db()
        self.assertTrue(recipient.is_read)
        self.assertNotContains(message_response, 'aria-label="1 unread message"', html=False)

    def test_lecturer_can_view_all_of_their_sent_messages_with_content(self):
        first_message = Notification.objects.create(
            sender=self.lecturer,
            subject="First class update",
            body="Bring your notebook to class.",
            level="100",
        )
        second_message = Notification.objects.create(
            sender=self.lecturer,
            subject="Second class update",
            body="The assignment deadline is Friday.",
        )
        other_lecturer = User.objects.create_user(
            username="lecturer2",
            password="pass1234",
            role=User.Role.LECTURER,
            department=self.department,
            is_approved=True,
            email="lecturer2@example.com",
        )
        Notification.objects.create(
            sender=other_lecturer,
            subject="Another lecturer message",
            body="This must not appear in the first lecturer's history.",
        )
        NotificationRecipient.objects.create(notification=first_message, student=self.student)
        NotificationRecipient.objects.create(notification=second_message, student=self.student)

        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:lecturer-messages"))

        self.assertContains(response, first_message.subject)
        self.assertContains(response, first_message.body)
        self.assertContains(response, second_message.subject)
        self.assertContains(response, second_message.body)
        self.assertContains(response, "Recipients:</strong> 1", html=False)
        self.assertNotContains(response, "Another lecturer message")

    def test_lecturer_can_delete_their_sent_message(self):
        notification = Notification.objects.create(
            sender=self.lecturer,
            subject="Withdrawn announcement",
            body="This message should be removed.",
        )
        NotificationRecipient.objects.create(notification=notification, student=self.student)
        UserAlert.objects.create(
            recipient=self.student,
            alert_type=UserAlert.AlertType.MESSAGE,
            title=notification.subject,
            body=notification.body,
            dedupe_key=f"message:{notification.id}:{self.student.id}",
        )

        self.client.force_login(self.lecturer)
        response = self.client.post(reverse("portal:lecturer-message-delete", args=[notification.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Notification.objects.filter(pk=notification.id).exists())
        self.assertFalse(NotificationRecipient.objects.filter(notification_id=notification.id).exists())
        self.assertFalse(UserAlert.objects.filter(dedupe_key=f"message:{notification.id}:{self.student.id}").exists())

    def test_message_alerts_are_not_delivered_to_lecturer_feed(self):
        UserAlert.objects.create(
            recipient=self.lecturer,
            alert_type=UserAlert.AlertType.MESSAGE,
            title="Student-only message",
            body="This should not be delivered to lecturers.",
            dedupe_key="message:manual:lecturer",
        )
        UserAlert.objects.create(
            recipient=self.lecturer,
            alert_type=UserAlert.AlertType.CLASS_REMINDER,
            title="Class reminder",
            body="Class starts soon.",
            dedupe_key="class-reminder:manual:lecturer",
        )

        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:alerts-feed"))

        alert_titles = [alert["title"] for alert in response.json()["alerts"]]
        self.assertEqual(alert_titles, ["Class reminder"])

    def test_admin_cannot_receive_or_access_browser_reminders(self):
        from .services import create_alert

        created_alert = create_alert(
            recipient=self.admin,
            alert_type=UserAlert.AlertType.CLASS_REMINDER,
            title="Admin should not receive this",
            body="Class starts soon.",
            target_url=reverse("portal:admin-dashboard"),
            dedupe_key="class-reminder:admin-test",
        )

        self.assertIsNone(created_alert)
        self.assertFalse(UserAlert.objects.filter(recipient=self.admin).exists())

        self.client.force_login(self.admin)
        dashboard = self.client.get(reverse("portal:admin-dashboard"))
        self.assertNotContains(dashboard, "alert-runtime")
        self.assertEqual(self.client.get(reverse("portal:alerts-feed")).status_code, 403)
        self.assertEqual(self.client.post(reverse("portal:alert-preferences")).status_code, 403)

    def test_lecturer_message_sends_student_email(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.paid_course, registered_by=self.student)
        notification = Notification.objects.create(
            sender=self.lecturer,
            subject="Class update",
            body="Bring your lab manual.",
        )

        from .services import deliver_notification

        with self.captureOnCommitCallbacks(execute=True):
            deliver_notification(notification)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].bcc, [self.student.email])
        self.assertIn(notification.subject, mail.outbox[0].subject)
        self.assertIn(notification.body, mail.outbox[0].body)

    def test_paystack_webhook_marks_course_payment_paid(self):
        payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PENDING,
            paystack_public_key_used="pk_test_course",
            paystack_secret_key_used="sk_test_course",
        )
        payload = {
            "event": "charge.success",
            "data": {
                "reference": payment.paystack_reference,
                "amount": 250000,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(b"sk_test_course", body, hashlib.sha512).hexdigest()

        response = self.client.post(
            reverse("portal:paystack-webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, CoursePayment.Status.PAID)

    def test_paystack_webhook_marks_course_payment_paid_with_course_api_secret(self):
        gateway = CoursePaymentGateway.objects.get(slug="courses")
        payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PENDING,
            paystack_public_key_used=gateway.paystack_public_key,
            paystack_secret_key_used=gateway.paystack_secret_key,
        )
        payload = {
            "event": "charge.success",
            "data": {
                "reference": payment.paystack_reference,
                "amount": 250000,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(gateway.paystack_secret_key.encode("utf-8"), body, hashlib.sha512).hexdigest()

        response = self.client.post(
            reverse("portal:paystack-webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, CoursePayment.Status.PAID)

    def test_paystack_webhook_requires_the_secret_used_for_the_payment(self):
        payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PENDING,
            paystack_public_key_used="pk_test_course",
            paystack_secret_key_used="sk_test_course",
        )
        payload = {
            "event": "charge.success",
            "data": {
                "reference": payment.paystack_reference,
                "amount": 250000,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(b"sk_test_department", body, hashlib.sha512).hexdigest()

        response = self.client.post(
            reverse("portal:paystack-webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["processed"])
        self.assertEqual(payment.status, CoursePayment.Status.PENDING)

    def test_paystack_webhook_creates_course_registration_and_access(self):
        gateway = CoursePaymentGateway.objects.get(slug="courses")
        payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PENDING,
            paystack_public_key_used=gateway.paystack_public_key,
            paystack_secret_key_used=gateway.paystack_secret_key,
        )
        payload = {
            "event": "charge.success",
            "data": {
                "reference": payment.paystack_reference,
                "amount": 250000,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(gateway.paystack_secret_key.encode("utf-8"), body, hashlib.sha512).hexdigest()

        response = self.client.post(
            reverse("portal:paystack-webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, CoursePayment.Status.PAID)
        self.assertTrue(StudentCourseRegistration.objects.filter(student=self.student, course=self.paid_course).exists())

    def test_paystack_webhook_accepts_non_payment_test_events(self):
        payload = {
            "event": "customer.create",
            "data": {"customer_code": "CUS_test"},
        }
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(b"sk_test_course", body, hashlib.sha512).hexdigest()

        response = self.client.post(
            reverse("portal:paystack-webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["event"], "customer.create")

    def test_profile_password_reset_sends_email_verification_link(self):
        self.client.force_login(self.student)
        response = self.client.post(reverse("portal:profile-password-reset"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["student@example.com"])
        self.assertIn("password-reset/confirm", mail.outbox[0].body)


@override_settings(
    BOOTSTRAP_ADMIN_USERNAME="test-bootstrap-admin",
    BOOTSTRAP_ADMIN_PASSWORD="test-bootstrap-password",
)
class DefaultAdminBootstrapTests(TestCase):
    username = "test-bootstrap-admin"
    password = "test-bootstrap-password"

    @override_settings(BOOTSTRAP_ADMIN_USERNAME="", BOOTSTRAP_ADMIN_PASSWORD="")
    def test_bootstrap_is_disabled_without_explicit_credentials(self):
        self.assertIsNone(ensure_default_admin_user())
        self.assertFalse(User.objects.filter(role=User.Role.ADMIN).exists())

    def test_bootstrap_admin_is_created_with_default_password(self):
        admin_user = ensure_default_admin_user()

        self.assertEqual(admin_user.username, self.username)
        self.assertTrue(admin_user.check_password(self.password))
        self.assertEqual(admin_user.role, User.Role.ADMIN)
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_superuser)

    def test_bootstrap_does_not_reset_existing_admin_password(self):
        admin_user = ensure_default_admin_user()
        admin_user.set_password("updated-secret-123")
        admin_user.save(update_fields=["password"])

        ensure_default_admin_user()
        admin_user.refresh_from_db()

        self.assertTrue(admin_user.check_password("updated-secret-123"))
        self.assertFalse(admin_user.check_password(self.password))

    def test_super_admin_login_uses_current_password_after_bootstrap(self):
        admin_user = ensure_default_admin_user()
        admin_user.set_password("updated-secret-123")
        admin_user.save(update_fields=["password"])

        response = self.client.post(
            reverse("portal:super-admin-login"),
            {
                "role": User.Role.ADMIN,
                "username": self.username,
                "password": "updated-secret-123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("portal:super-admin-dashboard"))

    def test_super_admin_login_page_sets_csrf_cookie_for_post(self):
        admin_user = ensure_default_admin_user()
        admin_user.set_password("updated-secret-123")
        admin_user.save(update_fields=["password"])

        client = Client(enforce_csrf_checks=True)
        get_response = client.get(reverse("portal:super-admin-login"))

        self.assertEqual(get_response.status_code, 200)
        self.assertIn("csrftoken", client.cookies)

        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("portal:super-admin-login"),
            {
                "csrfmiddlewaretoken": csrf_token,
                "role": User.Role.ADMIN,
                "username": self.username,
                "password": "updated-secret-123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("portal:super-admin-dashboard"))

    def test_logout_clears_authenticated_session(self):
        admin_user = ensure_default_admin_user()
        admin_user.set_password("updated-secret-123")
        admin_user.save(update_fields=["password"])

        login_response = self.client.post(
            reverse("portal:super-admin-login"),
            {
                "role": User.Role.ADMIN,
                "username": self.username,
                "password": "updated-secret-123",
            },
        )

        self.assertEqual(login_response.status_code, 302)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(admin_user.pk))

        logout_response = self.client.post(reverse("portal:logout"))

        self.assertEqual(logout_response.status_code, 302)
        self.assertEqual(logout_response.url, reverse("portal:home"))
        self.assertIsNone(self.client.session.get("_auth_user_id"))
