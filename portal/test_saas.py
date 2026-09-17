import hashlib
import hmac
import json
import tempfile
from unittest.mock import patch
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AcademicSession,
    Course,
    CoursePaymentGateway,
    Department,
    Faculty,
    Institution,
    Payment,
    Subscription,
    SubscriptionPlan,
    SubscriptionPlanDuration,
    SubscriptionPaymentGateway,
    SubscriptionNotification,
    StudentCourseRegistration,
    Feature,
    InstitutionFeature,
    ScreeningIntegration,
    ScreeningApplication,
    AuditLog,
    InstitutionProfile,
    DepartmentLecturerUpload,
    User,
    reset_current_institution,
    set_current_institution,
)
from .forms import InstitutionCreateForm, SubscriptionRenewalForm
from .services import (
    TEMPORARY_ACCOUNT_PASSWORD,
    finalize_subscription_payment,
    paystack_amount_in_kobo,
    send_subscription_expiry_notifications,
)
from .views import delete_institution_data


class MultiInstitutionSaaSTests(TestCase):
    def make_institution(self, code, subdomain):
        return Institution.objects.create(
            name=f"{code} University", institution_code=code, institution_type=Institution.Type.UNIVERSITY,
            email=f"{subdomain}@example.test", subdomain=subdomain, status=Institution.Status.ACTIVE,
        )

    def test_tenant_manager_never_returns_another_institutions_records(self):
        first = self.make_institution("FIRST", "first")
        second = self.make_institution("SECOND", "second")
        token = set_current_institution(first)
        try:
            Department.objects.create(name="First department", code="FIRST-DEPT")
        finally:
            reset_current_institution(token)
        token = set_current_institution(second)
        try:
            Department.objects.create(name="Second department", code="SECOND-DEPT")
            self.assertEqual(list(Department.objects.values_list("code", flat=True)), ["SECOND-DEPT"])
        finally:
            reset_current_institution(token)

    def test_authenticated_user_cannot_use_another_institution_host(self):
        first = self.make_institution("FIRST", "first")
        second = self.make_institution("SECOND", "second")
        admin = User.all_objects.create_user("first-admin", password="safe-password-123", role=User.Role.ADMIN, institution=first)
        self.client.force_login(admin)
        response = self.client.get("/admin-portal/", HTTP_HOST="second.educonnect.com")
        self.assertEqual(response.status_code, 403)

    def test_institutions_can_use_the_same_session_and_gateway_names(self):
        first = self.make_institution("FIRST", "first")
        second = self.make_institution("SECOND", "second")
        for institution in (first, second):
            token = set_current_institution(institution)
            try:
                AcademicSession.objects.create(name="2026/2027", is_current=True)
                CoursePaymentGateway.objects.create(slug="courses")
            finally:
                reset_current_institution(token)

        self.assertEqual(
            AcademicSession.all_objects.filter(institution__in=[first, second], name="2026/2027").count(),
            2,
        )
        self.assertEqual(
            CoursePaymentGateway.all_objects.filter(institution__in=[first, second], slug="courses").count(),
            2,
        )

    def test_verified_subscription_payment_extends_once_from_existing_expiry(self):
        institution = self.make_institution("FIRST", "first")
        plan = SubscriptionPlan.objects.create(name="Annual", price=Decimal("25000"), billing_period=SubscriptionPlan.BillingPeriod.YEARLY)
        previous_expiry = timezone.localdate() + timedelta(days=20)
        subscription = Subscription.objects.create(
            institution=institution, plan=plan, start_date=timezone.localdate() - timedelta(days=30),
            end_date=previous_expiry, status=Subscription.Status.ACTIVE, amount=plan.price,
        )
        payment = Payment.objects.create(institution=institution, subscription=subscription, plan=plan, reference="SUB-TEST", amount=plan.price)
        verification = {"status": "success", "reference": payment.reference, "amount": paystack_amount_in_kobo(plan.price)}
        _, changed = finalize_subscription_payment(reference=payment.reference, verification=verification)
        subscription.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(subscription.end_date, previous_expiry + timedelta(days=365))
        _, changed = finalize_subscription_payment(reference=payment.reference, verification=verification)
        subscription.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual(subscription.end_date, previous_expiry + timedelta(days=365))

    def test_subscription_enforcement_applies_on_tenant_and_central_hosts(self):
        plan = SubscriptionPlan.objects.create(
            name="Host enforcement plan",
            price=Decimal("10000.00"),
            billing_period=SubscriptionPlan.BillingPeriod.YEARLY,
        )
        active = self.make_institution("ACTIVE", "active")
        expired = self.make_institution("EXPIRED", "expired")
        suspended = self.make_institution("SUSPENDED", "suspended")
        for institution, end_date, status in (
            (active, timezone.localdate() + timedelta(days=30), Subscription.Status.ACTIVE),
            (expired, timezone.localdate() - timedelta(days=1), Subscription.Status.ACTIVE),
            (suspended, timezone.localdate() + timedelta(days=30), Subscription.Status.SUSPENDED),
        ):
            Subscription.objects.create(
                institution=institution,
                plan=plan,
                start_date=timezone.localdate() - timedelta(days=30),
                end_date=end_date,
                status=status,
                amount=plan.price,
            )
        active_user = User.all_objects.create_user("active-user", password="safe-password-123", role=User.Role.STUDENT, institution=active)
        expired_user = User.all_objects.create_user("expired-user", password="safe-password-123", role=User.Role.STUDENT, institution=expired)
        suspended_user = User.all_objects.create_user("suspended-user", password="safe-password-123", role=User.Role.STUDENT, institution=suspended)

        self.client.force_login(active_user)
        self.assertRedirects(self.client.get("/dashboard/", HTTP_HOST="active.educonnect.com"), "/student/")
        self.assertRedirects(self.client.get("/dashboard/", HTTP_HOST="educonnect.com"), "/student/")
        self.client.force_login(expired_user)
        self.assertRedirects(self.client.get("/dashboard/", HTTP_HOST="expired.educonnect.com"), "/billing/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/dashboard/", HTTP_HOST="educonnect.com"), "/billing/", fetch_redirect_response=False)
        self.client.force_login(suspended_user)
        self.assertRedirects(self.client.get("/dashboard/", HTTP_HOST="suspended.educonnect.com"), "/billing/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/dashboard/", HTTP_HOST="educonnect.com"), "/billing/", fetch_redirect_response=False)


class ProtectedTenantMediaTests(TestCase):
    def make_institution(self, code, subdomain, *, status=Institution.Status.ACTIVE):
        return Institution.objects.create(
            name=f"{code} University",
            institution_code=code,
            institution_type=Institution.Type.UNIVERSITY,
            email=f"{subdomain}@example.test",
            subdomain=subdomain,
            status=status,
        )

    def provision_course(self, institution, *, code):
        token = set_current_institution(institution)
        try:
            session = AcademicSession.objects.create(name=f"{code} session", is_current=True)
            faculty = Faculty.objects.create(name=f"{code} Faculty", code=f"{code}FAC")
            department = Department.objects.create(name=f"{code} Department", code=f"{code}DEP", faculty=faculty)
            lecturer = User.all_objects.create_user(
                username=f"{code.lower()}-lecturer",
                password="safe-password-123",
                role=User.Role.LECTURER,
                institution=institution,
                department=department,
                is_approved=True,
            )
            student = User.all_objects.create_user(
                username=f"{code.lower()}-student",
                password="safe-password-123",
                role=User.Role.STUDENT,
                institution=institution,
                department=department,
            )
            course = Course.objects.create(
                department=department,
                lecturer=lecturer,
                academic_session=session,
                code=f"{code}101",
                title=f"{code} private material",
                level="100",
                file=SimpleUploadedFile(f"{code.lower()}-private.pdf", b"private file", content_type="application/pdf"),
                is_free=True,
            )
            StudentCourseRegistration.objects.create(student=student, course=course, session=session, registered_by=student)
            return student, course
        finally:
            reset_current_institution(token)

    def test_course_files_require_authorization_and_expired_tenants_are_blocked_on_central_host(self):
        plan = SubscriptionPlan.objects.create(
            name="Protected media plan",
            price=Decimal("10000.00"),
            billing_period=SubscriptionPlan.BillingPeriod.YEARLY,
        )
        active = self.make_institution("ACTIVE-MEDIA", "active-media")
        expired = self.make_institution("EXPIRED-MEDIA", "expired-media")
        Subscription.objects.create(
            institution=active,
            plan=plan,
            start_date=timezone.localdate(),
            end_date=timezone.localdate() + timedelta(days=30),
            status=Subscription.Status.ACTIVE,
            amount=plan.price,
        )
        Subscription.objects.create(
            institution=expired,
            plan=plan,
            start_date=timezone.localdate() - timedelta(days=31),
            end_date=timezone.localdate() - timedelta(days=1),
            status=Subscription.Status.ACTIVE,
            amount=plan.price,
        )

        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            active_student, active_course = self.provision_course(active, code="ACTIVEMEDIA")
            expired_student, expired_course = self.provision_course(expired, code="EXPIREDMEDIA")
            active_url = reverse("portal:download-course-file", args=[active_course.id])
            expired_url = reverse("portal:download-course-file", args=[expired_course.id])

            anonymous_response = self.client.get(active_url, HTTP_HOST="active-media.educonnect.com")
            self.assertRedirects(anonymous_response, reverse("portal:home"), fetch_redirect_response=False)

            self.client.force_login(active_student)
            authorized_response = self.client.get(active_url, HTTP_HOST="active-media.educonnect.com")
            self.assertEqual(authorized_response.status_code, 200)
            self.assertEqual(b"".join(authorized_response.streaming_content), b"private file")
            direct_media_response = self.client.get(f"/media/{active_course.file.name}", HTTP_HOST="active-media.educonnect.com")
            self.assertEqual(direct_media_response.status_code, 404)

            self.client.force_login(expired_student)
            expired_response = self.client.get(expired_url, HTTP_HOST="educonnect.com")
            self.assertRedirects(expired_response, reverse("portal:billing-overview"), fetch_redirect_response=False)


class InstitutionAdministratorAuthenticationTests(TestCase):
    def setUp(self):
        self.super_admin = User.all_objects.create_user(
            username="platform-admin",
            email="platform-admin@example.test",
            password="platform-admin-password",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.institution = Institution.objects.create(
            name="Meridian University",
            institution_code="MERIDIAN",
            institution_type=Institution.Type.UNIVERSITY,
            email="hello@meridian.example.test",
            subdomain="meridian",
            status=Institution.Status.ACTIVE,
        )
        self.institution_admin = User.all_objects.create_user(
            username="admin@meridian.example.test",
            email="admin@meridian.example.test",
            password="initial-password",
            role=User.Role.ADMIN,
            institution=self.institution,
        )

    def test_institution_admin_uses_email_as_their_username_and_standard_admin_login(self):
        response = self.client.post(
            reverse("portal:role-login", kwargs={"role": User.Role.ADMIN}),
            {
                "role": User.Role.ADMIN,
                "username": self.institution_admin.email,
                "password": "initial-password",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("portal:dashboard"))

    def test_superuser_is_rejected_by_standard_admin_login_and_can_use_super_admin_login(self):
        standard_response = self.client.post(
            reverse("portal:role-login", kwargs={"role": User.Role.ADMIN}),
            {
                "role": User.Role.ADMIN,
                "username": self.super_admin.username,
                "password": "platform-admin-password",
            },
        )
        self.assertEqual(standard_response.status_code, 200)
        self.assertContains(standard_response, "Super administrator accounts must use the super administrator login.")

        super_response = self.client.post(
            reverse("portal:super-admin-login"),
            {
                "role": User.Role.ADMIN,
                "username": self.super_admin.username,
                "password": "platform-admin-password",
            },
        )
        self.assertRedirects(super_response, reverse("portal:super-admin-dashboard"))


class InstitutionFeatureAndScreeningTests(TestCase):
    def setUp(self):
        self.super_admin = User.all_objects.create_user(
            username="platform-admin", email="platform-admin@example.test", password="platform-admin-password",
            role=User.Role.ADMIN, is_staff=True, is_superuser=True,
        )
        self.institution = Institution.objects.create(
            name="Screening University", institution_code="SCREEN", institution_type=Institution.Type.UNIVERSITY,
            email="screen@example.test", subdomain="screen", status=Institution.Status.ACTIVE,
        )
        self.institution_admin = User.all_objects.create_user(
            username="admin@screen.example.test", email="admin@screen.example.test", password="initial-password",
            role=User.Role.ADMIN, institution=self.institution,
        )
        self.plan = SubscriptionPlan.objects.create(
            name="Screening plan", price=Decimal("10000.00"), billing_period=SubscriptionPlan.BillingPeriod.YEARLY,
        )
        Subscription.objects.create(
            institution=self.institution, plan=self.plan, start_date=timezone.localdate(),
            end_date=timezone.localdate() + timedelta(days=365), status=Subscription.Status.ACTIVE, amount=self.plan.price,
        )
        self.department = Faculty.all_objects.create(name="Science", code="SCI", institution=self.institution)
        self.department = Department.all_objects.create(
            name="Computer Science", code="CSC", faculty=self.department, institution=self.institution,
        )
        self.session = AcademicSession.all_objects.create(name="2026/2027", is_current=True, institution=self.institution)

    def enable(self, code):
        feature = Feature.objects.get(code=code)
        setting, _ = InstitutionFeature.objects.get_or_create(institution=self.institution, feature=feature)
        setting.enabled = True
        setting.save()
        return setting

    def signed_headers(self, integration, body):
        timestamp = str(int(timezone.now().timestamp()))
        signature = hmac.new(
            integration.api_secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256,
        ).hexdigest()
        return {
            "HTTP_X_EDUCONNECT_INSTITUTION": self.institution.institution_code,
            "HTTP_X_EDUCONNECT_TIMESTAMP": timestamp,
            "HTTP_X_EDUCONNECT_SIGNATURE": signature,
        }

    def test_disabled_feature_is_enforced_even_when_a_user_enters_the_url(self):
        user = User.all_objects.create_user("student", password="safe-password-123", role=User.Role.STUDENT, institution=self.institution)
        self.client.force_login(user)
        response = self.client.get(reverse("portal:ai-assistant"), HTTP_HOST="screen.educonnect.com")
        self.assertEqual(response.status_code, 403)

    def test_screening_api_transfers_one_admitted_applicant_into_its_own_tenant(self):
        self.enable("online-screening")
        integration = ScreeningIntegration.objects.create(institution=self.institution, admission_session=self.session)
        payload = json.dumps({
            "application_id": "APP-100", "first_name": "Ada", "last_name": "Okafor", "email": "ada@example.test",
            "jamb_number": "12345678AA", "department_code": "CSC", "academic_session": "2026/2027",
        }).encode("utf-8")
        headers = self.signed_headers(integration, payload)
        response = self.client.post(reverse("portal:api-screening-applications"), data=payload, content_type="application/json", **headers)
        self.assertEqual(response.status_code, 201)

        empty_body = b""
        admit_headers = self.signed_headers(integration, empty_body)
        response = self.client.post(reverse("portal:api-screening-admit", args=[self.institution.institution_code, "APP-100"]), data=empty_body, content_type="application/json", **admit_headers)
        self.assertEqual(response.status_code, 200)

        transfer_payload = json.dumps({"application_id": "APP-100", "department_code": "CSC", "academic_session": "2026/2027", "level": "100"}).encode("utf-8")
        transfer_headers = self.signed_headers(integration, transfer_payload)
        response = self.client.post(reverse("portal:api-student-from-screening"), data=transfer_payload, content_type="application/json", **transfer_headers)
        self.assertEqual(response.status_code, 201)
        application = ScreeningApplication.objects.get(institution=self.institution, external_application_id="APP-100")
        self.assertEqual(application.status, ScreeningApplication.Status.TRANSFERRED)
        self.assertEqual(application.student.institution, self.institution)
        self.assertEqual(application.student.department, self.department)

    def test_screening_api_rejects_a_signature_from_another_institution(self):
        self.enable("online-screening")
        integration = ScreeningIntegration.objects.create(institution=self.institution)
        other = Institution.objects.create(
            name="Other University", institution_code="OTHER", institution_type=Institution.Type.UNIVERSITY,
            email="other@example.test", subdomain="other", status=Institution.Status.ACTIVE,
        )
        body = b"{}"
        headers = self.signed_headers(integration, body)
        headers["HTTP_X_EDUCONNECT_INSTITUTION"] = other.institution_code
        response = self.client.post(reverse("portal:api-screening-applications"), data=body, content_type="application/json", **headers)
        self.assertEqual(response.status_code, 403)

    def test_disabling_a_feature_required_by_another_feature_is_rejected(self):
        prerequisite = Feature.objects.get(code="new-educonnect-features")
        dependent = Feature.objects.get(code="online-screening")
        dependent.dependencies = [prerequisite.code]
        dependent.save()
        self.enable(prerequisite.code)
        self.enable(dependent.code)

        prerequisite_setting = InstitutionFeature.objects.get(
            institution=self.institution, feature=prerequisite
        )
        prerequisite_setting.enabled = False

        with self.assertRaisesMessage(ValidationError, "Disable dependent feature"):
            prerequisite_setting.save()
        self.assertTrue(self.institution.feature_enabled(dependent.code))

    def test_screening_configuration_cannot_use_another_institutions_session(self):
        self.enable("online-screening")
        other = Institution.objects.create(
            name="Other University", institution_code="OTHER", institution_type=Institution.Type.UNIVERSITY,
            email="other@example.test", subdomain="other", status=Institution.Status.ACTIVE,
        )
        foreign_session = AcademicSession.all_objects.create(
            name="2027/2028", is_current=True, institution=other,
        )
        self.assertTrue(Institution.objects.filter(pk=self.institution.pk).exists())
        self.client.force_login(self.super_admin)

        response = self.client.post(
            reverse("portal:super-admin-screening-configuration", args=[self.institution.id]),
            {"is_open": "on", "admission_session": foreign_session.id, "application_fee": "0"},
            HTTP_HOST="educonnect.com",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")

    def test_creating_an_institution_uses_its_email_for_the_admin_username_and_temporary_password(self):
        self.client.force_login(self.super_admin)

        response = self.client.post(
            reverse("portal:super-admin-institutions"),
            {
                "name": "Coastal Polytechnic",
                "institution_code": "COASTAL",
                "institution_type": Institution.Type.POLYTECHNIC,
                "email": "hello@coastal.example.test",
                "country": "Nigeria",
                "subdomain": "coastal",
                "status": Institution.Status.ACTIVE,
                "logo": SimpleUploadedFile("coastal-logo.png", b"coastal-logo", content_type="image/png"),
            },
        )

        self.assertRedirects(response, reverse("portal:super-admin-institutions"))
        institution = Institution.objects.get(institution_code="COASTAL")
        administrator = User.all_objects.get(email="hello@coastal.example.test")
        self.assertEqual(administrator.username, "hello@coastal.example.test")
        self.assertEqual(administrator.role, User.Role.ADMIN)
        self.assertTrue(administrator.check_password(TEMPORARY_ACCOUNT_PASSWORD))
        self.assertEqual(Path(institution.logo.name).suffix, ".png")
        trial = institution.current_subscription
        self.assertEqual(trial.status, Subscription.Status.TRIAL)
        self.assertEqual(trial.amount, Decimal("0.00"))
        self.assertEqual((trial.end_date - trial.start_date).days, 183)
        self.assertTrue(trial.plan.is_trial)

    def test_renewal_form_offers_only_the_requested_paid_plan_durations(self):
        trial = SubscriptionPlan.objects.create(
            name="Six-month free trial", price=Decimal("0.00"),
            billing_period=SubscriptionPlan.BillingPeriod.CUSTOM,
            custom_duration_days=183, is_trial=True,
        )
        plans = [
            SubscriptionPlan.objects.create(
                name=label,
                price=Decimal("10000.00"),
                billing_period=SubscriptionPlan.BillingPeriod.CUSTOM,
                custom_duration_days=duration,
            )
            for label, duration in (("Standard", 183), ("Premium", 365))
        ]
        durations = [
            SubscriptionPlanDuration.objects.create(plan=plan, duration_days=duration, price=Decimal("10000.00"))
            for plan, duration in zip(plans, (183, 365))
        ]
        SubscriptionPlan.objects.create(
            name="Monthly", price=Decimal("1000.00"),
            billing_period=SubscriptionPlan.BillingPeriod.MONTHLY,
        )

        form = SubscriptionRenewalForm()

        self.assertEqual(set(form.fields["plan"].queryset), set(plans))
        self.assertEqual(set(form.fields["plan_duration"].queryset), set(durations))
        self.assertNotIn(trial, form.fields["plan"].queryset)

    def test_super_admin_can_save_the_subscription_payment_gateway(self):
        self.client.force_login(self.super_admin)

        response = self.client.post(
            reverse("portal:super-admin-settings"),
            {
                "paystack_public_key": "pk_test_subscription",
                "paystack_secret_key": "sk_test_subscription",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("portal:super-admin-settings"))
        gateway = SubscriptionPaymentGateway.objects.get(slug="subscription")
        self.assertEqual(gateway.paystack_public_key, "pk_test_subscription")
        self.assertEqual(gateway.paystack_secret_key, "sk_test_subscription")
        self.assertTrue(gateway.is_configured)

    def test_institution_form_does_not_show_separate_administrator_name_or_email_fields(self):
        self.client.force_login(self.super_admin)

        response = self.client.get(reverse("portal:super-admin-institutions"))

        self.assertContains(response, 'name="email"')
        self.assertNotContains(response, 'name="admin_name"')
        self.assertNotContains(response, 'name="admin_email"')

    def test_institution_form_normalizes_a_portal_url_to_a_subdomain(self):
        form = InstitutionCreateForm(
            data={
                "name": "Northern Coast College",
                "institution_code": "NCC",
                "institution_type": Institution.Type.COLLEGE,
                "email": "hello@ncc.example.test",
                "country": "Nigeria",
                "subdomain": "https://Northern Coast College.educonnect.com/",
                "status": Institution.Status.ACTIVE,
            },
            files={"logo": SimpleUploadedFile("ncc-logo.png", b"ncc-logo", content_type="image/png")},
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["subdomain"], "northern-coast-college")

    def test_super_admin_sees_each_institutions_people_counts_and_current_plan(self):
        User.all_objects.create_user(
            username="meridian-student",
            password="safe-password-123",
            role=User.Role.STUDENT,
            institution=self.institution,
        )
        User.all_objects.create_user(
            username="meridian-lecturer",
            password="safe-password-123",
            role=User.Role.LECTURER,
            institution=self.institution,
        )
        plan = SubscriptionPlan.objects.create(
            name="Meridian annual", price=Decimal("50000.00"),
            billing_period=SubscriptionPlan.BillingPeriod.YEARLY,
        )
        Subscription.objects.create(
            institution=self.institution,
            plan=plan,
            start_date=timezone.localdate(),
            end_date=timezone.localdate() + timedelta(days=365),
            status=Subscription.Status.ACTIVE,
            amount=plan.price,
        )
        self.client.force_login(self.super_admin)

        response = self.client.get(reverse("portal:super-admin-institutions"))
        row = response.context["institutions"].get(pk=self.institution.pk)

        self.assertEqual(row.student_count, 1)
        self.assertEqual(row.staff_count, 2)  # One lecturer and one institution administrator.
        self.assertEqual(row.plan_name, plan.name)

    def test_super_admin_can_edit_an_institution_and_sync_its_admin_login(self):
        self.client.force_login(self.super_admin)

        response = self.client.post(
            reverse("portal:super-admin-institution-edit", args=[self.institution.id]),
            {
                "name": "Meridian Technical University",
                "institution_code": "MERIDIAN",
                "institution_type": Institution.Type.UNIVERSITY,
                "email": "office@meridian.example.test",
                "country": "Nigeria",
                "subdomain": "meridian-tech",
                "custom_domain": "",
                "status": Institution.Status.ACTIVE,
            },
        )

        self.assertRedirects(response, reverse("portal:super-admin-institutions"))
        self.institution.refresh_from_db()
        self.institution_admin.refresh_from_db()
        self.assertEqual(self.institution.name, "Meridian Technical University")
        self.assertEqual(self.institution.subdomain, "meridian-tech")
        self.assertEqual(self.institution_admin.email, "office@meridian.example.test")
        self.assertEqual(self.institution_admin.username, "office@meridian.example.test")

    def test_super_admin_can_permanently_delete_an_institution_without_deleting_its_plan(self):
        institution = Institution.objects.create(
            name="Disposable College",
            institution_code="DISPOSABLE",
            institution_type=Institution.Type.COLLEGE,
            email="admin@disposable.example.test",
            subdomain="disposable",
            status=Institution.Status.ACTIVE,
        )
        administrator = User.all_objects.create_user(
            username="admin@disposable.example.test",
            email="admin@disposable.example.test",
            password="safe-password-123",
            role=User.Role.ADMIN,
            institution=institution,
        )
        plan = SubscriptionPlan.objects.create(
            name="Disposable annual", price=Decimal("50000.00"),
            billing_period=SubscriptionPlan.BillingPeriod.YEARLY,
        )
        Subscription.objects.create(
            institution=institution,
            plan=plan,
            start_date=timezone.localdate(),
            end_date=timezone.localdate() + timedelta(days=365),
            status=Subscription.Status.ACTIVE,
            amount=plan.price,
        )
        subscription = institution.current_subscription
        payment = Payment.objects.create(
            institution=institution,
            subscription=subscription,
            plan=plan,
            reference="DISPOSABLE-PAYMENT",
            amount=plan.price,
        )
        token = set_current_institution(institution)
        try:
            faculty = Faculty.objects.create(name="School of Arts", code="ARTS")
            department = Department.objects.create(
                faculty=faculty,
                name="Fine Arts",
                code="FART",
            )
            gateway = CoursePaymentGateway.objects.create(slug="courses")
        finally:
            reset_current_institution(token)
        screening = ScreeningApplication.objects.create(
            institution=institution,
            external_application_id="DISPOSABLE-APPLICATION",
            first_name="Delete",
            last_name="Me",
            department=department,
        )
        audit_log = AuditLog.objects.create(
            institution=institution,
            action="disposable_tenant_event",
        )
        self.client.force_login(self.super_admin)

        response = self.client.post(
            reverse("portal:super-admin-institution-delete", args=[institution.id]),
            {"confirmation": institution.name},
        )

        self.assertRedirects(response, reverse("portal:super-admin-institutions"))
        self.assertFalse(Institution.objects.filter(pk=institution.pk).exists())
        self.assertFalse(User.all_objects.filter(pk=administrator.pk).exists())
        self.assertFalse(Faculty.all_objects.filter(pk=faculty.pk).exists())
        self.assertFalse(Department.all_objects.filter(pk=department.pk).exists())
        self.assertFalse(CoursePaymentGateway.all_objects.filter(pk=gateway.pk).exists())
        self.assertFalse(ScreeningApplication.objects.filter(pk=screening.pk).exists())
        self.assertFalse(Payment.objects.filter(pk=payment.pk).exists())
        self.assertFalse(AuditLog.objects.filter(pk=audit_log.pk).exists())
        self.assertTrue(SubscriptionPlan.objects.filter(pk=plan.pk).exists())

    def test_institution_deletion_requires_the_exact_institution_name(self):
        self.client.force_login(self.super_admin)

        response = self.client.post(
            reverse("portal:super-admin-institution-delete", args=[self.institution.id]),
            {"confirmation": self.institution.name.lower()},
        )

        self.assertRedirects(
            response,
            reverse("portal:super-admin-institution-edit", args=[self.institution.id]),
        )
        self.assertTrue(Institution.objects.filter(pk=self.institution.pk).exists())
        self.assertTrue(User.all_objects.filter(pk=self.institution_admin.pk).exists())

    def test_institution_administrator_cannot_delete_an_institution(self):
        self.client.force_login(self.institution_admin)

        response = self.client.post(
            reverse("portal:super-admin-institution-delete", args=[self.institution.id]),
            {"confirmation": self.institution.name},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Institution.objects.filter(pk=self.institution.pk).exists())

    def test_institution_action_buttons_are_grouped_and_delete_requires_confirmation(self):
        self.client.force_login(self.super_admin)

        response = self.client.get(reverse("portal:super-admin-institutions"))

        self.assertContains(response, 'class="institution-actions"')
        self.assertContains(
            response,
            f'{reverse("portal:super-admin-institution-edit", args=[self.institution.id])}#delete-institution',
        )

    def test_institution_admin_can_request_an_email_verified_password_change(self):
        self.client.force_login(self.institution_admin)

        response = self.client.post(reverse("portal:profile-password-reset"))

        self.assertRedirects(response, reverse("portal:profile"))
        self.assertEqual(mail.outbox[0].to, [self.institution_admin.email])
        self.assertIn("password-reset/confirm", mail.outbox[0].body)

    def test_institution_admin_can_change_their_password_from_their_profile(self):
        self.client.force_login(self.institution_admin)

        response = self.client.post(
            reverse("portal:institution-admin-change-password"),
            {
                "old_password": "initial-password",
                "new_password1": "new-institution-admin-password-123",
                "new_password2": "new-institution-admin-password-123",
            },
        )

        self.assertRedirects(response, reverse("portal:profile"))
        self.institution_admin.refresh_from_db()
        self.assertTrue(self.institution_admin.check_password("new-institution-admin-password-123"))

    def test_super_admin_dashboard_uses_the_shared_fixed_sidebar_layout(self):
        self.client.force_login(self.super_admin)

        response = self.client.get(reverse("portal:super-admin-dashboard"))

        self.assertContains(response, 'data-dashboard-layout')
        self.assertContains(response, 'data-dashboard-sidebar')
        self.assertContains(response, 'data-sidebar-toggle')

    def test_every_super_admin_sidebar_page_has_a_template(self):
        self.client.force_login(self.super_admin)

        for url_name in (
            "portal:super-admin-users",
            "portal:super-admin-reports",
            "portal:super-admin-settings",
            "portal:super-admin-profile",
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)

    def test_super_admin_resets_an_institution_admin_password_and_sends_a_verification_link(self):
        self.client.force_login(self.super_admin)

        response = self.client.post(
            reverse("portal:super-admin-reset-institution-admin-password", args=[self.institution_admin.id]),
        )

        self.assertRedirects(response, reverse("portal:super-admin-institutions"))
        self.institution_admin.refresh_from_db()
        self.assertTrue(self.institution_admin.check_password(TEMPORARY_ACCOUNT_PASSWORD))
        self.assertEqual(mail.outbox[0].to, [self.institution_admin.email])
        self.assertIn("password-reset/confirm", mail.outbox[0].body)

    def test_institution_list_stacks_sections_and_shows_reset_for_every_institution_admin(self):
        second_institution = Institution.objects.create(
            name="Second Institution",
            institution_code="SECOND",
            institution_type=Institution.Type.UNIVERSITY,
            email="hello@second.example.test",
            subdomain="second",
            status=Institution.Status.ACTIVE,
        )
        second_admin = User.all_objects.create_user(
            username="admin@second.example.test",
            email="admin@second.example.test",
            password="initial-password",
            role=User.Role.ADMIN,
            institution=second_institution,
        )
        self.client.force_login(self.super_admin)

        response = self.client.get(reverse("portal:super-admin-institutions"))

        self.assertContains(response, 'class="institution-management-layout"')
        self.assertContains(response, self.institution_admin.email)
        self.assertContains(response, second_admin.email)
        self.assertContains(
            response,
            reverse("portal:super-admin-reset-institution-admin-password", args=[second_admin.id]),
        )

        reset_response = self.client.post(
            reverse("portal:super-admin-reset-institution-admin-password", args=[second_admin.id]),
        )

        self.assertRedirects(reset_response, reverse("portal:super-admin-institutions"))
        second_admin.refresh_from_db()
        self.assertTrue(second_admin.check_password(TEMPORARY_ACCOUNT_PASSWORD))


class InstitutionDeletionStorageTests(TransactionTestCase):
    """Exercise physical file cleanup outside TestCase's rollback-only wrapper."""

    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.plan = SubscriptionPlan.objects.create(
            name="Shared deletion plan",
            price=Decimal("50000.00"),
            billing_period=SubscriptionPlan.BillingPeriod.YEARLY,
        )
        self.institution = self.create_institution("DELETE", "delete")
        self.other_institution = self.create_institution("KEEP", "keep")

    def tearDown(self):
        self.media_override.disable()
        self.media_directory.cleanup()
        super().tearDown()

    def create_institution(self, code, subdomain):
        return Institution.objects.create(
            name=f"{code} University",
            institution_code=code,
            institution_type=Institution.Type.UNIVERSITY,
            email=f"{subdomain}@example.test",
            subdomain=subdomain,
            status=Institution.Status.ACTIVE,
        )

    def create_tenant_resources(self, institution, prefix):
        institution.logo.save(f"{prefix}-institution.png", SimpleUploadedFile("institution.png", b"institution"))
        institution.save(update_fields=["logo"])
        user = User.all_objects.create_user(
            username=f"{prefix}-student",
            password="safe-password-123",
            role=User.Role.STUDENT,
            institution=institution,
        )
        user.passport_photo.save(f"{prefix}-passport.png", SimpleUploadedFile("passport.png", b"passport"))
        user.save(update_fields=["passport_photo"])
        faculty = Faculty.all_objects.create(name=f"{prefix} Faculty", code=f"{prefix}FAC", institution=institution)
        department = Department.all_objects.create(
            faculty=faculty,
            name=f"{prefix} Department",
            code=f"{prefix}DEP",
            institution=institution,
        )
        upload = DepartmentLecturerUpload.all_objects.create(
            institution=institution,
            department=department,
            file=SimpleUploadedFile(f"{prefix}-lecturers.csv", b"name,email"),
        )
        profile = InstitutionProfile.all_objects.create(
            institution=institution,
            name=f"{prefix} profile",
            logo=SimpleUploadedFile(f"{prefix}-profile.png", b"profile"),
        )
        subscription = Subscription.objects.create(
            institution=institution,
            plan=self.plan,
            start_date=timezone.localdate(),
            end_date=timezone.localdate() + timedelta(days=365),
            status=Subscription.Status.ACTIVE,
            amount=self.plan.price,
        )
        Payment.objects.create(
            institution=institution,
            subscription=subscription,
            plan=self.plan,
            reference=f"{prefix}-PAYMENT",
            amount=self.plan.price,
        )
        feature, _ = Feature.objects.get_or_create(
            code="online-screening",
            defaults={"name": "Online Screening"},
        )
        InstitutionFeature.objects.create(institution=institution, feature=feature, enabled=False)
        integration = ScreeningIntegration.objects.create(institution=institution)
        screening = ScreeningApplication.objects.create(
            institution=institution,
            external_application_id=f"{prefix}-APPLICATION",
            first_name="Tenant",
            last_name="Applicant",
            department=department,
        )
        audit_log = AuditLog.objects.create(institution=institution, action=f"{prefix}_audit")
        return {
            "user": user,
            "faculty": faculty,
            "department": department,
            "upload": upload,
            "profile": profile,
            "integration": integration,
            "screening": screening,
            "audit_log": audit_log,
            "files": [institution.logo.name, user.passport_photo.name, upload.file.name, profile.logo.name],
        }

    def assert_no_direct_tenant_records(self, institution_id):
        for model in apps.get_models():
            institution_field = next(
                (field for field in model._meta.fields if field.name == "institution"),
                None,
            )
            if institution_field is None:
                continue
            manager = getattr(model, "all_objects", model._default_manager)
            self.assertFalse(
                manager.filter(institution_id=institution_id).exists(),
                f"{model._meta.label} still contains a deleted tenant record.",
            )

    def test_complete_tenant_deletion_removes_records_and_its_files_only_after_commit(self):
        deleted = self.create_tenant_resources(self.institution, "delete")
        kept = self.create_tenant_resources(self.other_institution, "keep")
        deleted_id = self.institution.id

        delete_institution_data(self.institution)

        self.assertFalse(Institution.objects.filter(pk=deleted_id).exists())
        self.assert_no_direct_tenant_records(deleted_id)
        self.assertTrue(SubscriptionPlan.objects.filter(pk=self.plan.pk).exists())
        self.assertTrue(Institution.objects.filter(pk=self.other_institution.pk).exists())
        self.assertTrue(User.all_objects.filter(pk=kept["user"].pk).exists())
        for file_name in deleted["files"]:
            self.assertFalse(deleted["user"].passport_photo.storage.exists(file_name))
        for file_name in kept["files"]:
            self.assertTrue(kept["user"].passport_photo.storage.exists(file_name))

    def test_rollback_preserves_tenant_records_and_uploaded_files(self):
        resources = self.create_tenant_resources(self.institution, "rollback")
        institution_id = self.institution.id

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                delete_institution_data(self.institution)
                raise RuntimeError("Simulate a failure after deletion work.")

        self.assertTrue(Institution.objects.filter(pk=institution_id).exists())
        self.assertTrue(User.all_objects.filter(pk=resources["user"].pk).exists())
        self.assertTrue(ScreeningApplication.objects.filter(pk=resources["screening"].pk).exists())
        for file_name in resources["files"]:
            self.assertTrue(resources["user"].passport_photo.storage.exists(file_name))


class SubscriptionWebhookAndExpiryNotificationTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            name="Billing University",
            institution_code="BILLING",
            institution_type=Institution.Type.UNIVERSITY,
            email="billing@example.test",
            subdomain="billing",
            status=Institution.Status.ACTIVE,
        )
        self.plan = SubscriptionPlan.objects.create(
            name="Billing annual plan",
            price=Decimal("12000.00"),
            billing_period=SubscriptionPlan.BillingPeriod.YEARLY,
        )
        self.subscription = Subscription.objects.create(
            institution=self.institution,
            plan=self.plan,
            start_date=timezone.localdate(),
            end_date=timezone.localdate() + timedelta(days=30),
            status=Subscription.Status.ACTIVE,
            amount=self.plan.price,
        )
        self.payment = Payment.objects.create(
            institution=self.institution,
            subscription=self.subscription,
            plan=self.plan,
            duration_days=365,
            reference="SUBSCRIPTION-WEBHOOK-TEST",
            amount=self.plan.price,
        )
        self.gateway = SubscriptionPaymentGateway.objects.create(
            slug="subscription",
            paystack_public_key="pk_test_subscription",
            paystack_secret_key="sk_test_subscription",
            is_active=True,
        )

    def webhook_response(self, secret):
        body = json.dumps({
            "event": "charge.success",
            "data": {
                "reference": self.payment.reference,
                "amount": paystack_amount_in_kobo(self.payment.amount),
            },
        }).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
        return self.client.post(
            reverse("portal:paystack-webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

    @patch("portal.views.verify_and_finalize_subscription_payment")
    def test_subscription_webhook_uses_the_configured_subscription_gateway(self, finalize):
        finalize.return_value = (self.payment, True)

        response = self.webhook_response(self.gateway.paystack_secret_key)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["processed"])
        finalize.assert_called_once_with(self.payment.reference)

    @patch("portal.views.verify_and_finalize_subscription_payment")
    def test_subscription_webhook_rejects_invalid_or_wrong_gateway_signatures(self, finalize):
        for secret in ("invalid-signature", "sk_wrong_subscription"):
            with self.subTest(secret=secret):
                response = self.webhook_response(secret)
                self.assertEqual(response.status_code, 400)
        finalize.assert_not_called()

    @override_settings(PAYSTACK_SECRET_KEY="")
    @patch("portal.views.verify_and_finalize_subscription_payment")
    def test_subscription_webhook_requires_a_configured_gateway(self, finalize):
        self.gateway.delete()

        response = self.webhook_response("sk_missing_gateway")

        self.assertEqual(response.status_code, 400)
        finalize.assert_not_called()

    @patch("portal.views.verify_and_finalize_subscription_payment")
    def test_subscription_webhook_is_idempotent_when_the_finalizer_reports_processed_once(self, finalize):
        finalize.side_effect = [(self.payment, True), (self.payment, False)]

        first = self.webhook_response(self.gateway.paystack_secret_key)
        second = self.webhook_response(self.gateway.paystack_secret_key)

        self.assertTrue(first.json()["processed"])
        self.assertFalse(second.json()["processed"])
        self.assertEqual(finalize.call_count, 2)

    @patch("portal.services.verify_paystack_transaction")
    def test_valid_subscription_webhook_finalizes_a_payment_only_once(self, verify):
        verify.return_value = {
            "status": "success",
            "reference": self.payment.reference,
            "amount": paystack_amount_in_kobo(self.payment.amount),
        }

        first = self.webhook_response(self.gateway.paystack_secret_key)
        second = self.webhook_response(self.gateway.paystack_secret_key)

        self.payment.refresh_from_db()
        self.subscription.refresh_from_db()
        self.assertTrue(first.json()["processed"])
        self.assertFalse(second.json()["processed"])
        self.assertEqual(self.payment.status, Payment.Status.SUCCESS)
        self.assertEqual(self.subscription.status, Subscription.Status.ACTIVE)
        self.assertEqual(self.subscription.end_date, timezone.localdate() + timedelta(days=395))
        self.assertEqual(self.subscription.events.filter(event_type="renewed").count(), 1)

    @override_settings(PAYSTACK_SECRET_KEY="sk_different_platform_gateway")
    @patch("portal.views.verify_and_finalize_subscription_payment")
    def test_subscription_payment_is_not_finalized_with_another_configured_gateway_secret(self, finalize):
        response = self.webhook_response("sk_different_platform_gateway")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["processed"])
        finalize.assert_not_called()

    def test_expiry_notification_is_recorded_only_after_successful_delivery(self):
        sent = send_subscription_expiry_notifications(today=timezone.localdate())

        notification = SubscriptionNotification.objects.get(subscription=self.subscription, days_before_expiry=30)
        self.assertEqual(sent, 1)
        self.assertEqual(notification.status, SubscriptionNotification.Status.SENT)
        self.assertEqual(notification.attempt_count, 1)
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(send_subscription_expiry_notifications(today=timezone.localdate()), 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_failed_expiry_notification_is_retryable(self):
        with patch("portal.services.EmailMessage.send", side_effect=RuntimeError("SMTP unavailable")):
            self.assertEqual(send_subscription_expiry_notifications(today=timezone.localdate()), 0)
        notification = SubscriptionNotification.objects.get(subscription=self.subscription, days_before_expiry=30)
        self.assertEqual(notification.status, SubscriptionNotification.Status.FAILED)
        self.assertEqual(notification.attempt_count, 1)
        self.assertIsNone(notification.sent_at)

        self.assertEqual(send_subscription_expiry_notifications(today=timezone.localdate()), 1)
        notification.refresh_from_db()
        self.assertEqual(notification.status, SubscriptionNotification.Status.SENT)
        self.assertEqual(notification.attempt_count, 2)
        self.assertEqual(len(mail.outbox), 1)


class ProductionMediaConfigurationTests(TestCase):
    def test_direct_media_urls_are_not_served_by_django_or_caddy(self):
        response = self.client.get("/media/private-document.pdf")
        caddyfile = (settings.BASE_DIR / "Caddyfile").read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("handle_path /media/*", caddyfile)

    def test_production_schedulers_include_subscription_expiry_notifications(self):
        compose_file = (settings.BASE_DIR / "compose.production.yaml").read_text(encoding="utf-8")
        render_file = (settings.BASE_DIR / "render.yaml").read_text(encoding="utf-8")

        self.assertIn("subscription-notifications", compose_file)
        self.assertIn("send_subscription_expiry_notifications", compose_file)
        self.assertIn("educonnect-subscription-notifications", render_file)
        self.assertIn("send_subscription_expiry_notifications", render_file)
