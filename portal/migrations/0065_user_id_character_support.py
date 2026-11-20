from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0064_notification_programmes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="username",
            field=models.CharField(
                error_messages={"unique": "A user with that username already exists."},
                help_text="Required. 150 characters or fewer.",
                max_length=150,
                unique=True,
                verbose_name="username",
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="id_number",
            field=models.CharField(blank=True, max_length=150, null=True),
        ),
    ]
