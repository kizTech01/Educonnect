# Generated manually to keep the programme curriculum upgrade deployable on
# existing institutions with department-wide course records.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0039_course_result_assessment_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="curriculum",
            name="programme",
            field=models.ForeignKey(
                blank=True,
                help_text="Blank for a department-wide legacy curriculum.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="curricula",
                to="portal.programme",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="curriculum",
            name="unique_department_curriculum_session_version",
        ),
        migrations.AddConstraint(
            model_name="curriculum",
            constraint=models.UniqueConstraint(
                fields=("department", "programme", "effective_session", "version"),
                name="unique_programme_curriculum_session_version",
            ),
        ),
        migrations.AddField(
            model_name="course",
            name="curricula",
            field=models.ManyToManyField(blank=True, related_name="catalogue_courses", to="portal.curriculum"),
        ),
        migrations.AddField(
            model_name="course",
            name="programmes",
            field=models.ManyToManyField(blank=True, related_name="courses", to="portal.programme"),
        ),
        migrations.AddField(
            model_name="handbook",
            name="programme",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="handbooks",
                to="portal.programme",
            ),
        ),
        migrations.AlterField(
            model_name="course",
            name="schedule_day",
            field=models.CharField(
                blank=True,
                choices=[
                    ("Monday", "Monday"), ("Tuesday", "Tuesday"), ("Wednesday", "Wednesday"),
                    ("Thursday", "Thursday"), ("Friday", "Friday"), ("Saturday", "Saturday"),
                    ("Sunday", "Sunday"),
                ],
                max_length=15,
            ),
        ),
    ]
