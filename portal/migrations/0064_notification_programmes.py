from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portal", "0063_unique_course_payment_references")]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="programmes",
            field=models.ManyToManyField(
                blank=True,
                related_name="notifications",
                to="portal.programme",
            ),
        ),
    ]
