import mimetypes

from django.db import migrations, models


def copy_existing_logo_files_to_database(apps, schema_editor):
    """Preserve logos already present on Render's mounted media disk."""
    Institution = apps.get_model("portal", "Institution")
    for institution in Institution.objects.exclude(logo="").iterator():
        try:
            with institution.logo.storage.open(institution.logo.name, "rb") as logo_file:
                logo_data = logo_file.read()
        except OSError:
            # A file lost on an earlier ephemeral deployment cannot be
            # reconstructed here. The next logo upload will create its durable
            # database copy through Institution.save().
            continue
        if logo_data:
            Institution.objects.filter(pk=institution.pk).update(
                logo_data=logo_data,
                logo_content_type=mimetypes.guess_type(institution.logo.name)[0]
                or "application/octet-stream",
            )


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0052_departmentalpayment_exam_card_semester"),
    ]

    operations = [
        migrations.AddField(
            model_name="institution",
            name="logo_content_type",
            field=models.CharField(blank=True, editable=False, max_length=100),
        ),
        migrations.AddField(
            model_name="institution",
            name="logo_data",
            field=models.BinaryField(blank=True, default=b"", editable=False),
        ),
        migrations.RunPython(
            copy_existing_logo_files_to_database,
            migrations.RunPython.noop,
        ),
    ]
