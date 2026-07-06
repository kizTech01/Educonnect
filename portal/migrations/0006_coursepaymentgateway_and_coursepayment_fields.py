from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0005_alter_departmentalfee_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="coursepayment",
            name="paystack_public_key_used",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="coursepayment",
            name="paystack_reference",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="coursepayment",
            name="paystack_secret_key_used",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.CreateModel(
            name="CoursePaymentGateway",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("slug", models.CharField(default="courses", max_length=30, unique=True)),
                ("paystack_public_key", models.CharField(blank=True, max_length=255)),
                ("paystack_secret_key", models.CharField(blank=True, max_length=255)),
            ],
            options={
                "ordering": ["slug"],
            },
        ),
    ]
