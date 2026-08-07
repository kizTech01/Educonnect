from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0008_user_passport_photo"),
    ]

    operations = [
        migrations.AddField(
            model_name="coursereminderdispatch",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="coursereminderdispatch",
            name="delivered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="coursereminderdispatch",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="coursereminderdispatch",
            name="last_error",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
