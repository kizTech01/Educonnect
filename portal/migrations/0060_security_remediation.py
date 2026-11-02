# Generated manually for the security remediation.  This migration is
# intentionally additive/non-destructive and preserves existing credentials.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import portal.fields


PAYSTACK_SECRET_FIELDS = (
    ("SubscriptionPaymentGateway", "paystack_secret_key"),
    ("CoursePayment", "paystack_secret_key_used"),
    ("CoursePaymentGateway", "paystack_secret_key"),
    ("DepartmentPaymentGateway", "paystack_secret_key"),
    ("DepartmentCoursePaymentGateway", "paystack_secret_key"),
    ("DepartmentalPayment", "paystack_secret_key_used"),
)


def encrypt_existing_payment_credentials(apps, schema_editor):
    """Convert legacy plaintext values using the configured primary Fernet key.

    The custom field deliberately reads plaintext too, so a failed deployment
    can be retried safely.  Updating each value through the field prepares it
    as encrypted text without changing the key's application-visible value.
    """
    for model_name, field_name in PAYSTACK_SECRET_FIELDS:
        model = apps.get_model("portal", model_name)
        for row in model._base_manager.exclude(**{field_name: ""}).iterator():
            value = getattr(row, field_name)
            if value:
                model._base_manager.filter(pk=row.pk).update(**{field_name: value})


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0059_academicsession_unique_current_session"),
    ]

    operations = [
        migrations.CreateModel(
            name="AccountActivation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="activation", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="ScreeningRequestReplay",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("signature_digest", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("institution", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="screening_request_replays", to="portal.institution")),
            ],
            options={
                "indexes": [models.Index(fields=["expires_at"], name="portal_scre_expires_10914b_idx")],
                "constraints": [models.UniqueConstraint(fields=("institution", "signature_digest"), name="unique_screening_request_signature")],
            },
        ),
        migrations.AlterField(
            model_name="subscriptionpaymentgateway",
            name="paystack_secret_key",
            field=portal.fields.EncryptedTextField(blank=True, max_length=2048),
        ),
        migrations.AlterField(
            model_name="coursepayment",
            name="paystack_secret_key_used",
            field=portal.fields.EncryptedTextField(blank=True, max_length=2048),
        ),
        migrations.AlterField(
            model_name="coursepaymentgateway",
            name="paystack_secret_key",
            field=portal.fields.EncryptedTextField(blank=True, max_length=2048),
        ),
        migrations.AlterField(
            model_name="departmentpaymentgateway",
            name="paystack_secret_key",
            field=portal.fields.EncryptedTextField(blank=True, max_length=2048),
        ),
        migrations.AlterField(
            model_name="departmentcoursepaymentgateway",
            name="paystack_secret_key",
            field=portal.fields.EncryptedTextField(blank=True, max_length=2048),
        ),
        migrations.AlterField(
            model_name="departmentalpayment",
            name="paystack_secret_key_used",
            field=portal.fields.EncryptedTextField(blank=True, max_length=2048),
        ),
        migrations.RunPython(encrypt_existing_payment_credentials, migrations.RunPython.noop),
    ]
