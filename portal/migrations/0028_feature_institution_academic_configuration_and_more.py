# Generated manually for the multi-institution feature and screening integration layer.

import decimal
import portal.models
from django.db import migrations, models
import django.db.models.deletion


def create_platform_features(apps, schema_editor):
    Feature = apps.get_model("portal", "Feature")
    defaults = (
        ("new-educonnect-features", "New EduConnect Features", "Enables institution access to progressively released EduConnect capabilities."),
        ("online-screening", "Online Screening", "Connects this institution to its separately deployed admission screening application."),
        ("educonnect-ai", "EduConnect AI", "Provides role-aware, institution-scoped information and assistance."),
        ("payments", "Payments", "Enables institution payment capabilities."),
        ("communication", "Communication", "Enables institution messaging and notification capabilities."),
    )
    for code, name, description in defaults:
        Feature.objects.get_or_create(code=code, defaults={"name": name, "description": description})


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0027_subscription_durations_and_gateway"),
    ]

    operations = [
        migrations.CreateModel(
            name="Feature",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("requires_subscription", models.BooleanField(default=True)),
                ("dependencies", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="institution",
            name="academic_configuration",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="institution",
            name="phone",
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AddField(
            model_name="institution",
            name="timezone",
            field=models.CharField(blank=True, default="Africa/Lagos", max_length=64),
        ),
        migrations.CreateModel(
            name="ScreeningIntegration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_open", models.BooleanField(default=False)),
                ("opens_at", models.DateTimeField(blank=True, null=True)),
                ("closes_at", models.DateTimeField(blank=True, null=True)),
                ("application_fee", models.DecimalField(decimal_places=2, default=decimal.Decimal("0.00"), max_digits=12)),
                ("available_programmes", models.JSONField(blank=True, default=list)),
                ("admission_requirements", models.JSONField(blank=True, default=dict)),
                ("required_documents", models.JSONField(blank=True, default=list)),
                ("applicant_categories", models.JSONField(blank=True, default=list)),
                ("utme_de_settings", models.JSONField(blank=True, default=dict)),
                ("workflow_settings", models.JSONField(blank=True, default=dict)),
                ("notification_settings", models.JSONField(blank=True, default=dict)),
                ("api_secret", models.CharField(default=portal.models.generate_api_secret, editable=False, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("admission_session", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="portal.academicsession")),
                ("institution", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="screening_integration", to="portal.institution")),
            ],
            options={"verbose_name": "screening integration"},
        ),
        migrations.CreateModel(
            name="InstitutionFeature",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enabled", models.BooleanField(default=False)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("feature", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="institution_settings", to="portal.feature")),
                ("institution", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="feature_settings", to="portal.institution")),
            ],
        ),
        migrations.CreateModel(
            name="ScreeningApplication",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_application_id", models.CharField(max_length=100)),
                ("applicant_reference", models.CharField(blank=True, max_length=100)),
                ("jamb_number", models.CharField(blank=True, max_length=30)),
                ("first_name", models.CharField(max_length=150)),
                ("last_name", models.CharField(max_length=150)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("programme", models.CharField(blank=True, max_length=200)),
                ("status", models.CharField(choices=[("received", "Received"), ("approved", "Approved"), ("admitted", "Admitted"), ("transferred", "Transferred"), ("rejected", "Rejected")], default="received", max_length=20)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("admitted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("academic_session", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="portal.academicsession")),
                ("department", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="portal.department")),
                ("institution", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="screening_applications", to="portal.institution")),
                ("student", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="screening_admissions", to="portal.user")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="institutionfeature",
            constraint=models.UniqueConstraint(fields=("institution", "feature"), name="unique_institution_feature"),
        ),
        migrations.AddConstraint(
            model_name="screeningapplication",
            constraint=models.UniqueConstraint(fields=("institution", "external_application_id"), name="unique_screening_application_per_institution"),
        ),
        migrations.AddIndex(
            model_name="screeningapplication",
            index=models.Index(fields=["institution", "status", "external_application_id"], name="portal_scre_institu_094588_idx"),
        ),
        migrations.RunPython(create_platform_features, migrations.RunPython.noop),
    ]
