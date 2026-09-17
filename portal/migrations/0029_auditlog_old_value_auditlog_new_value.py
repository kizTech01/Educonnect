# Add structured before/after values to the platform audit trail.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0028_feature_institution_academic_configuration_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="old_value",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="new_value",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
