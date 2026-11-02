# Generated manually to make authentication throttling shared across workers.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0060_security_remediation"),
    ]

    operations = [
        migrations.CreateModel(
            name="LoginThrottle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key_digest", models.CharField(max_length=64, unique=True)),
                ("failure_count", models.PositiveSmallIntegerField(default=0)),
                ("locked_until", models.DateTimeField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [models.Index(fields=["locked_until"], name="portal_log_locked__6ed9e7_idx")],
            },
        ),
    ]
