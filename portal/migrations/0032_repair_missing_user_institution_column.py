from django.db import migrations


def ensure_user_institution_column(apps, schema_editor):
    """Repair a database whose migration record exists but column is absent."""
    User = apps.get_model("portal", "User")
    table_name = User._meta.db_table
    field = User._meta.get_field("institution")

    with schema_editor.connection.cursor() as cursor:
        columns = {
            description.name
            for description in schema_editor.connection.introspection.get_table_description(
                cursor,
                table_name,
            )
        }

    if field.column not in columns:
        # schema_editor.add_field() derives the nullable bigint column, PROTECT
        # foreign key, and normal ForeignKey index from the historical model
        # state instead of duplicating backend-specific SQL here.
        schema_editor.add_field(User, field)


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0031_subscription_notification_delivery_state"),
    ]

    operations = [
        migrations.RunPython(ensure_user_institution_column, migrations.RunPython.noop),
    ]
