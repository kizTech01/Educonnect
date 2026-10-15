from django.db import migrations, models
from django.utils import timezone


def seed_global_policies(apps, schema_editor):
    """Install the initial public drafts during deployment, never on a page view."""
    from portal.policies import POLICY_CATALOG, POLICY_PUBLICATION_DATE, POLICY_VERSION

    GlobalPolicy = apps.get_model("portal", "GlobalPolicy")
    for slug, definition in POLICY_CATALOG.items():
        if GlobalPolicy.objects.filter(slug=slug, status="published").exists():
            continue
        GlobalPolicy.objects.get_or_create(
            slug=slug,
            version=POLICY_VERSION,
            defaults={
                **definition,
                "effective_date": POLICY_PUBLICATION_DATE,
                "last_updated": POLICY_PUBLICATION_DATE,
                "status": "published",
                "published_at": timezone.now(),
            },
        )


class Migration(migrations.Migration):
    dependencies = [("portal", "0044_globalpolicy_policyacceptance")]
    # PostgreSQL cannot create the partial unique index used by this constraint
    # while the seed inserts still have deferred trigger events in the current
    # transaction.  Keep the data and schema operations in separate
    # transactions so an existing deployment can apply this migration safely.
    atomic = False

    operations = [
        migrations.RunPython(seed_global_policies, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="globalpolicy",
            constraint=models.UniqueConstraint(
                fields=("slug",),
                condition=models.Q(status="published"),
                name="one_published_global_policy_per_slug",
            ),
        ),
    ]
