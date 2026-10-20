from django.db import migrations


def retain_legacy_logo_when_needed(apps, schema_editor):
    """Move the only legacy logo reference to the institution record.

    Institution.logo is the sole branding source rendered by the portal.  A
    few older tenants may have uploaded only an InstitutionProfile logo, so
    retain that file reference if and only if their Institution has no logo.
    Files are not copied: the same storage object remains available under its
    existing name.
    """
    Institution = apps.get_model("portal", "Institution")
    InstitutionProfile = apps.get_model("portal", "InstitutionProfile")

    for profile in InstitutionProfile.objects.exclude(logo="").iterator():
        institution = Institution.objects.filter(pk=profile.institution_id).first()
        if institution and not institution.logo:
            institution.logo = profile.logo
            institution.save(update_fields=["logo"])


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0050_shared_course_curricula"),
    ]

    operations = [
        migrations.RunPython(retain_legacy_logo_when_needed, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="institutionprofile",
            name="logo",
        ),
    ]
