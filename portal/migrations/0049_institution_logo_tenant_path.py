from django.db import migrations, models

import portal.models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0048_exam_card_ai_configuration_semester"),
    ]

    operations = [
        migrations.AlterField(
            model_name="institution",
            name="logo",
            field=models.FileField(blank=True, upload_to=portal.models.institution_logo_upload_path),
        ),
    ]
