from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portal", "0014_institutionprofile_faculty_and_department_automation")]

    operations = [
        migrations.RemoveConstraint(
            model_name="lecturercourseregistration",
            name="unique_lecturer_course_registration",
        ),
        migrations.AddConstraint(
            model_name="lecturercourseregistration",
            constraint=models.UniqueConstraint(
                fields=("lecturer", "course"),
                name="unique_lecturer_course_registration",
            ),
        ),
    ]
