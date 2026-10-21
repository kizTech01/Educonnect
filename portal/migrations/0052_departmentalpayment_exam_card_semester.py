from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0051_remove_legacy_institution_profile_logo"),
    ]

    operations = [
        migrations.AddField(
            model_name="departmentalpayment",
            name="exam_card_semester",
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
