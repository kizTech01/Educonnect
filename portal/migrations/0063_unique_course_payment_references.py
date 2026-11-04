from uuid import uuid4

from django.db import migrations, models


def repair_course_payment_references(apps, schema_editor):
    course_payment = apps.get_model("portal", "CoursePayment")
    database = schema_editor.connection.alias
    seen = set()
    rows = course_payment.objects.using(database).order_by("pk").values_list(
        "pk", "paystack_reference"
    )
    for payment_id, reference in rows.iterator():
        if reference and reference not in seen:
            seen.add(reference)
            continue
        new_reference = f"COURSE-{uuid4().hex.upper()}"
        while new_reference in seen:
            new_reference = f"COURSE-{uuid4().hex.upper()}"
        course_payment.objects.using(database).filter(pk=payment_id).update(
            paystack_reference=new_reference
        )
        seen.add(new_reference)


class Migration(migrations.Migration):
    dependencies = [("portal", "0062_user_email_verification_otp")]

    operations = [
        migrations.RunPython(repair_course_payment_references, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="coursepayment",
            name="paystack_reference",
            field=models.CharField(blank=True, default="", max_length=64, unique=True),
        ),
    ]
