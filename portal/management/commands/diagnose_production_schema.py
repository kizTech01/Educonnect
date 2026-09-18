"""Read-only PostgreSQL schema diagnostics for a Render deployment.

The command deliberately reports structural metadata only.  It never prints
connection settings, credentials, application rows, or row counts.
"""

from django.core.management.base import BaseCommand
from django.db import DatabaseError, connection


PORTAL_TABLES = (
    "portal_institution",
    "portal_user",
    "portal_department",
    "portal_faculty",
    "portal_course",
    "portal_subscriptionplan",
    "portal_subscription",
    "portal_payment",
)
INSTITUTION_MIGRATION = "0022_multi_tenant_saas"


class Command(BaseCommand):
    help = "Report read-only portal migration and schema metadata without secrets."

    def handle(self, *args, **options):
        self.stdout.write("=== EduConnect schema diagnostic (metadata only) ===")
        self.stdout.write(f"database_backend: {connection.vendor}")

        with connection.cursor() as cursor:
            table_names = {
                table.name for table in connection.introspection.get_table_list(cursor)
            }

        if connection.vendor == "postgresql":
            self._report_postgresql_context()
            self._report_postgresql_table_schemas()
        else:
            self.stdout.write("postgresql_context: unavailable (non-PostgreSQL database)")

        self._report_tables(table_names)
        self._report_user_institution_column(table_names)
        self._report_migration_history(table_names)

    def _report_postgresql_context(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_schema(), current_setting('search_path')"
            )
            database, schema, search_path = cursor.fetchone()
        # Database and schema identifiers are structural metadata; hostnames,
        # users, and connection strings are intentionally never displayed.
        self.stdout.write(f"database_name: {database}")
        self.stdout.write(f"current_schema: {schema}")
        self.stdout.write(f"search_path: {search_path}")

    def _report_postgresql_table_schemas(self):
        placeholders = ", ".join(["%s"] * len(PORTAL_TABLES))
        query = (
            "SELECT table_schema, table_name "
            "FROM information_schema.tables "
            f"WHERE table_name IN ({placeholders}) "
            "ORDER BY table_name, table_schema"
        )
        with connection.cursor() as cursor:
            cursor.execute(query, PORTAL_TABLES)
            locations = cursor.fetchall()

        found = {name: [] for name in PORTAL_TABLES}
        for schema, name in locations:
            found[name].append(schema)
        for name in PORTAL_TABLES:
            schemas = ", ".join(found[name]) or "absent"
            self.stdout.write(f"table_schemas.{name}: {schemas}")

    def _report_tables(self, table_names):
        self.stdout.write("table_existence:")
        for name in ("django_migrations", *PORTAL_TABLES):
            self.stdout.write(f"  {name}: {'present' if name in table_names else 'absent'}")

    def _report_user_institution_column(self, table_names):
        if "portal_user" not in table_names:
            self.stdout.write("column.portal_user.institution_id: table absent")
            return

        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(cursor, "portal_user")
            }
        state = "present" if "institution_id" in columns else "absent"
        self.stdout.write(f"column.portal_user.institution_id: {state}")

    def _report_migration_history(self, table_names):
        if "django_migrations" not in table_names:
            self.stdout.write("portal_migration_history: django_migrations table absent")
            return

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT name, applied FROM django_migrations "
                    "WHERE app = %s ORDER BY applied, name",
                    ["portal"],
                )
                rows = cursor.fetchall()
        except DatabaseError as error:
            self.stdout.write(
                "portal_migration_history: unavailable "
                f"({error.__class__.__name__})"
            )
            return

        applied = {name for name, _ in rows}
        self.stdout.write(f"portal_migration_count: {len(rows)}")
        self.stdout.write(
            f"migration.{INSTITUTION_MIGRATION}: "
            f"{'applied' if INSTITUTION_MIGRATION in applied else 'not applied'}"
        )
        if rows:
            latest_name, latest_applied = rows[-1]
            self.stdout.write(f"latest_portal_migration: {latest_name} ({latest_applied.isoformat()})")
        else:
            self.stdout.write("latest_portal_migration: none")

        self.stdout.write("portal_migrations:")
        for name, applied_at in rows:
            self.stdout.write(f"  {name} ({applied_at.isoformat()})")
