"""Regression coverage for the platform-security remediation."""

import hashlib
import hmac
import json
from io import BytesIO

from cryptography.fernet import Fernet
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .forms import InstitutionCreateForm, SubscriptionPaymentGatewayForm
from .models import (
    AccountActivation,
    Institution,
    Subscription,
    SubscriptionPaymentGateway,
    SubscriptionPlan,
    User,
)
from .services import issue_account_activation


def raster_png():
    image = BytesIO()
    Image.new("RGB", (1, 1), "white").save(image, format="PNG")
    return image.getvalue()


class AccountActivationSecurityTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            name="Activation University", institution_code="ACTIVATION", email="admin@activation.test",
            subdomain="activation", status=Institution.Status.ACTIVE,
        )

    def test_provisioned_users_receive_distinct_unusable_password_activations(self):
        first = User.all_objects.create_user("first-activation", role=User.Role.LECTURER, institution=self.institution)
        second = User.all_objects.create_user("second-activation", role=User.Role.LECTURER, institution=self.institution)

        first_token = issue_account_activation(first)
        second_token = issue_account_activation(second)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first_token, second_token)
        self.assertFalse(first.has_usable_password())
        self.assertFalse(second.has_usable_password())
        self.assertNotEqual(
            AccountActivation.objects.get(user=first).token_hash,
            AccountActivation.objects.get(user=second).token_hash,
        )


class LogoUploadSecurityTests(TestCase):
    def form_data(self):
        return {
            "name": "Logo University", "institution_code": "LOGO", "institution_type": Institution.Type.UNIVERSITY,
            "email": "admin@logo.test", "country": "Nigeria", "subdomain": "logo", "status": Institution.Status.ACTIVE,
        }

    def test_active_svg_is_rejected_but_valid_raster_logo_is_accepted(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        dangerous = InstitutionCreateForm(
            data=self.form_data(),
            files={"logo": SimpleUploadedFile("logo.svg", b"<svg onload='alert(1)'></svg>", content_type="image/svg+xml")},
        )
        self.assertFalse(dangerous.is_valid())
        self.assertIn("logo", dangerous.errors)

        safe = InstitutionCreateForm(
            data=self.form_data(),
            files={"logo": SimpleUploadedFile("logo.png", raster_png(), content_type="image/png")},
        )
        self.assertTrue(safe.is_valid(), safe.errors)


class PaymentCredentialSecurityTests(TestCase):
    def test_database_value_is_encrypted_and_can_be_rotated_without_losing_the_secret(self):
        old_key = Fernet.generate_key().decode("ascii")
        new_key = Fernet.generate_key().decode("ascii")
        with override_settings(PAYSTACK_CREDENTIALS_ENCRYPTION_KEY=old_key, TESTING=False):
            gateway = SubscriptionPaymentGateway.objects.create(
                slug="subscription", paystack_public_key="pk_test", paystack_secret_key="sk_secret_value",
            )
            with connection.cursor() as cursor:
                cursor.execute("SELECT paystack_secret_key FROM portal_subscriptionpaymentgateway WHERE id = %s", [gateway.pk])
                encrypted_before = cursor.fetchone()[0]
            self.assertTrue(encrypted_before.startswith("enc:v1:"))
            self.assertNotIn("sk_secret_value", encrypted_before)
            self.assertNotIn("sk_secret_value", SubscriptionPaymentGatewayForm(instance=gateway).as_p())

        with override_settings(
            PAYSTACK_CREDENTIALS_ENCRYPTION_KEY=new_key,
            PAYSTACK_CREDENTIALS_PREVIOUS_ENCRYPTION_KEYS=old_key,
            TESTING=False,
        ):
            gateway = SubscriptionPaymentGateway.objects.get(pk=gateway.pk)
            self.assertEqual(gateway.paystack_secret_key, "sk_secret_value")
            gateway.save()

        with override_settings(PAYSTACK_CREDENTIALS_ENCRYPTION_KEY=new_key, TESTING=False):
            self.assertEqual(
                SubscriptionPaymentGateway.objects.get(pk=gateway.pk).paystack_secret_key,
                "sk_secret_value",
            )


class ScreeningReplaySecurityTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            name="Screening Security University", institution_code="SCREEN-SEC", email="admin@screen-sec.test",
            subdomain="screen-sec", status=Institution.Status.ACTIVE,
        )
        plan = SubscriptionPlan.objects.create(name="Screening Security Plan", price="1.00")
        Subscription.objects.create(
            institution=self.institution, plan=plan, start_date=timezone.localdate(),
            end_date=timezone.localdate().replace(year=timezone.localdate().year + 1),
            status=Subscription.Status.ACTIVE,
        )
        from .models import ScreeningIntegration
        self.integration = ScreeningIntegration.objects.create(institution=self.institution)

    def signed_headers(self, path, body):
        timestamp = str(int(timezone.now().timestamp()))
        signed = b"POST." + path.encode("utf-8") + b"." + timestamp.encode("ascii") + b"." + body
        return {
            "HTTP_X_EDUCONNECT_INSTITUTION": self.institution.institution_code,
            "HTTP_X_EDUCONNECT_TIMESTAMP": timestamp,
            "HTTP_X_EDUCONNECT_SIGNATURE": hmac.new(
                self.integration.api_secret.encode("utf-8"), signed, hashlib.sha256
            ).hexdigest(),
        }

    def test_duplicate_signed_request_is_rejected_across_independent_clients(self):
        path = reverse("portal:api-screening-applications")
        body = json.dumps({"application_id": "REPLAY-1", "first_name": "Ada", "last_name": "Okafor"}).encode()
        headers = self.signed_headers(path, body)

        first = Client().post(path, data=body, content_type="application/json", **headers)
        second = Client().post(path, data=body, content_type="application/json", **headers)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
