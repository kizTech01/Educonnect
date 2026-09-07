from django.core.validators import FileExtensionValidator
from django.db import migrations, models
import django.db.models.deletion


def assign_existing_departments(apps, schema_editor):
    Faculty = apps.get_model("portal", "Faculty")
    Department = apps.get_model("portal", "Department")
    faculty, _ = Faculty.objects.get_or_create(
        code="UNASSIGNED",
        defaults={"name": "Unassigned Faculty", "description": "Created for existing departments during the faculty upgrade."},
    )
    Department.objects.filter(faculty__isnull=True).update(faculty=faculty)


class Migration(migrations.Migration):
    dependencies = [("portal", "0013_coursestudentgroup_coursestudentgroupmembership_and_more")]

    operations = [
        migrations.CreateModel(
            name="Faculty",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=150, unique=True)),
                ("code", models.CharField(max_length=20, unique=True)),
                ("description", models.TextField(blank=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="InstitutionProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(default="Educonnect", max_length=200)),
                ("logo", models.FileField(blank=True, upload_to="institution/%Y/%m/", validators=[FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "svg", "webp"])])),
                ("website", models.URLField(blank=True)),
                ("email", models.EmailField(blank=True, max_length=254)),
            ],
        ),
        migrations.AddField(
            model_name="department",
            name="faculty",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="departments", to="portal.faculty"),
        ),
        migrations.RunPython(assign_existing_departments, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="department",
            name="faculty",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="departments", to="portal.faculty"),
        ),
        migrations.CreateModel(
            name="DepartmentLecturerUpload",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("file", models.FileField(upload_to="department_uploads/lecturers/%Y/%m/")),
                ("processing_summary", models.TextField(blank=True)),
                ("department", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lecturer_uploads", to="portal.department")),
                ("uploaded_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="lecturer_uploads", to="portal.user")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="CourseAllocationUpload",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("file", models.FileField(upload_to="department_uploads/course_allocations/%Y/%m/")),
                ("processing_summary", models.TextField(blank=True)),
                ("department", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="course_allocation_uploads", to="portal.department")),
                ("uploaded_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="course_allocation_uploads", to="portal.user")),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
