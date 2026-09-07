from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("portal", "0017_department_head_of_department")]

    operations = [
        migrations.CreateModel(
            name="NotificationAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("file", models.FileField(upload_to="message_attachments/%Y/%m/")),
                ("notification", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attachments", to="portal.notification")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
    ]
