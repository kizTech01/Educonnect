from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("portal", "0016_notification_attachment")]

    operations = [
        migrations.AddField(
            model_name="department",
            name="head_of_department",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="headed_department",
                to="portal.user",
            ),
        ),
    ]
