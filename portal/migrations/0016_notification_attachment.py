from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portal", "0015_allow_shared_lecturer_course_registrations")]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="attachment",
            field=models.FileField(blank=True, upload_to="message_attachments/%Y/%m/"),
        ),
    ]
