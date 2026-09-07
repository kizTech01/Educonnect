from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("portal", "0018_notificationattachment")]

    operations = [
        migrations.CreateModel(
            name="DepartmentCoursePaymentGateway",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("paystack_public_key", models.CharField(blank=True, max_length=255)),
                ("paystack_secret_key", models.CharField(blank=True, max_length=255)),
                ("department", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="course_payment_gateway", to="portal.department")),
            ],
            options={"ordering": ["department__name"]},
        ),
    ]
