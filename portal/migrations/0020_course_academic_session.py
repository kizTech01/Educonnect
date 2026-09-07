from django.db import migrations, models
import django.db.models.deletion


def assign_existing_courses_to_current_session(apps, schema_editor):
    AcademicSession = apps.get_model("portal", "AcademicSession")
    Course = apps.get_model("portal", "Course")
    current_session = AcademicSession.objects.filter(is_current=True).first()
    if current_session:
        Course.objects.filter(academic_session__isnull=True).update(academic_session=current_session)


class Migration(migrations.Migration):
    dependencies = [("portal", "0019_departmentcoursepaymentgateway")]

    operations = [
        migrations.AddField(
            model_name="course",
            name="academic_session",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="courses",
                to="portal.academicsession",
            ),
        ),
        migrations.AlterField(
            model_name="course",
            name="code",
            field=models.CharField(max_length=20),
        ),
        migrations.AddConstraint(
            model_name="course",
            constraint=models.UniqueConstraint(
                fields=("department", "code", "academic_session"),
                name="unique_department_session_course",
            ),
        ),
        migrations.RunPython(assign_existing_courses_to_current_session, migrations.RunPython.noop),
    ]
