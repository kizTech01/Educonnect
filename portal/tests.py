from io import BytesIO
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.datastructures import MultiValueDict

from .models import (
    AcademicSession,
    Course,
    CoursePayment,
    CoursePaymentGateway,
    CourseReminderDispatch,
    CourseMaterial,
    Department,
    DepartmentPaymentGateway,
    DepartmentalAssociation,
    DepartmentalFee,
    DepartmentalPayment,
    MaterialAccess,
    Notification,
    NotificationRecipient,
    StudentCourseRegistration,
    User,
    UserAlert,
)
from .services import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    DEFAULT_PAYSTACK_PUBLIC_KEY,
    DEFAULT_PAYSTACK_SECRET_KEY,
    dispatch_due_course_reminders,
    ensure_default_admin_user,
)
from .views import sidebar_links


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

    def test_student_courses_shows_download_button_for_free_registered_course(self):
        StudentCourseRegistration.objects.create(student=self.student, course=self.free_course, registered_by=self.student)

        self.client.force_login(self.student)
        response = self.client.get(reverse("portal:student-courses"))

        self.assertContains(response, reverse("portal:download-course-file", args=[self.free_course.id]))

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

    def test_lecturer_paid_courses_menu_shows_only_paid_registered_courses(self):
        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("portal:lecturer-courses"), {"fee_type": "paid"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paid Courses")
        self.assertContains(response, self.paid_course.code)
        self.assertNotContains(response, self.free_course.code)
        self.assertContains(response, reverse("portal:lecturer-paid-course-students-pdf", args=[self.paid_course.id]))

    def test_blank_course_gateway_stays_unconfigured_until_admin_saves_keys(self):
        CoursePaymentGateway.objects.all().delete()

        from .views import course_payment_gateway

        gateway = course_payment_gateway()

        self.assertEqual(gateway.paystack_public_key, "")
        self.assertEqual(gateway.paystack_secret_key, "")
        self.assertFalse(gateway.is_configured)

    def test_blank_department_gateway_uses_default_paystack_test_keys(self):
        DepartmentPaymentGateway.objects.filter(department=self.department).update(
            paystack_public_key="",
            paystack_secret_key="",
        )

        from .views import ensure_department_gateway_credentials

        gateway = ensure_department_gateway_credentials(self.department)

        self.assertEqual(gateway.paystack_public_key, DEFAULT_PAYSTACK_PUBLIC_KEY)
        self.assertEqual(gateway.paystack_secret_key, DEFAULT_PAYSTACK_SECRET_KEY)

    def test_admin_menu_does_not_show_view_courses_label(self):
        request = self.factory.get(reverse("portal:admin-dashboard"))
        request.user = self.admin
        request.resolver_match = None

        labels = [item["label"] for item in sidebar_links(request)]

        self.assertNotIn("View Courses", labels)

    def test_admin_menu_shows_course_api_label(self):
        request = self.factory.get(reverse("portal:admin-dashboard"))
        request.user = self.admin
        request.resolver_match = None

        labels = [item["label"] for item in sidebar_links(request)]

        self.assertIn("Course API", labels)
        self.assertIn("Departmental", labels)
        departmental_link = next(item for item in sidebar_links(request) if item["label"] == "Departmental")
        self.assertFalse(any(child["label"] == "Users" for child in departmental_link["children"]))

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
            paystack_public_key=DEFAULT_PAYSTACK_PUBLIC_KEY,
            paystack_secret_key=DEFAULT_PAYSTACK_SECRET_KEY,
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
        self.assertEqual(initialize_paystack_transaction.call_args.kwargs["secret_key"], DEFAULT_PAYSTACK_SECRET_KEY)

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
        self.assertIn("Modibbo Adama University Yola".encode("utf-8"), response.content)
        self.assertIn(self.student.full_name.encode("utf-8"), response.content)
        self.assertIn(self.free_course.code.encode("utf-8"), response.content)
        self.assertIn(self.paid_course.code.encode("utf-8"), response.content)
        self.assertIn(b"SESSION", response.content)
        self.assertIn(self.session.name.encode("utf-8"), response.content)

    def test_admin_creating_session_makes_it_current(self):
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
        new_session = AcademicSession.objects.get(name="2027/2028")
        self.assertTrue(new_session.is_current)
        self.session.refresh_from_db()
        self.assertFalse(self.session.is_current)

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
        self.assertEqual(set(mail.outbox[0].to), {"student@example.com", "lecturer@example.com"})
        self.assertIn(self.paid_course.title, mail.outbox[0].body)
        self.assertEqual(
            CourseReminderDispatch.objects.filter(channel=CourseReminderDispatch.Channel.EMAIL).count(),
            2,
        )

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

    def test_paystack_webhook_marks_course_payment_paid(self):
        payment = CoursePayment.objects.create(
            student=self.student,
            course=self.paid_course,
            amount=self.paid_course.amount,
            status=CoursePayment.Status.PENDING,
            paystack_public_key_used=DEFAULT_PAYSTACK_PUBLIC_KEY,
            paystack_secret_key_used=DEFAULT_PAYSTACK_SECRET_KEY,
        )
        payload = {
            "event": "charge.success",
            "data": {
                "reference": payment.paystack_reference,
                "amount": 250000,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(DEFAULT_PAYSTACK_SECRET_KEY.encode("utf-8"), body, hashlib.sha512).hexdigest()

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
        signature = hmac.new(DEFAULT_PAYSTACK_SECRET_KEY.encode("utf-8"), body, hashlib.sha512).hexdigest()

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


class DefaultAdminBootstrapTests(TestCase):
    def test_bootstrap_admin_is_created_with_default_password(self):
        admin_user = ensure_default_admin_user()

        self.assertEqual(admin_user.username, DEFAULT_ADMIN_USERNAME)
        self.assertTrue(admin_user.check_password(DEFAULT_ADMIN_PASSWORD))
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
        self.assertFalse(admin_user.check_password(DEFAULT_ADMIN_PASSWORD))

    def test_admin_login_uses_current_password_after_bootstrap(self):
        admin_user = ensure_default_admin_user()
        admin_user.set_password("updated-secret-123")
        admin_user.save(update_fields=["password"])

        response = self.client.post(
            reverse("portal:role-login", kwargs={"role": User.Role.ADMIN}),
            {
                "role": User.Role.ADMIN,
                "username": DEFAULT_ADMIN_USERNAME,
                "password": "updated-secret-123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("portal:dashboard"))

    def test_direct_role_login_page_sets_csrf_cookie_for_post(self):
        admin_user = ensure_default_admin_user()
        admin_user.set_password("updated-secret-123")
        admin_user.save(update_fields=["password"])

        client = Client(enforce_csrf_checks=True)
        get_response = client.get(reverse("portal:role-login", kwargs={"role": User.Role.ADMIN}))

        self.assertEqual(get_response.status_code, 200)
        self.assertIn("csrftoken", client.cookies)

        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("portal:role-login", kwargs={"role": User.Role.ADMIN}),
            {
                "csrfmiddlewaretoken": csrf_token,
                "role": User.Role.ADMIN,
                "username": DEFAULT_ADMIN_USERNAME,
                "password": "updated-secret-123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("portal:dashboard"))

    def test_logout_clears_authenticated_session(self):
        admin_user = ensure_default_admin_user()
        admin_user.set_password("updated-secret-123")
        admin_user.save(update_fields=["password"])

        login_response = self.client.post(
            reverse("portal:role-login", kwargs={"role": User.Role.ADMIN}),
            {
                "role": User.Role.ADMIN,
                "username": DEFAULT_ADMIN_USERNAME,
                "password": "updated-secret-123",
            },
        )

        self.assertEqual(login_response.status_code, 302)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(admin_user.pk))

        logout_response = self.client.post(reverse("portal:logout"))

        self.assertEqual(logout_response.status_code, 302)
        self.assertEqual(logout_response.url, reverse("portal:home"))
        self.assertIsNone(self.client.session.get("_auth_user_id"))
