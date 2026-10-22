from django.db import migrations


def align_document_institutions_with_payments(apps, schema_editor):
    """Repair legacy child rows using their payment's tenant ownership.

    DepartmentalPaymentDocument belongs to DepartmentalPayment.  The child
    tenant key is denormalised for ordinary tenant querysets, so it must agree
    with the parent or a valid document can be hidden by the tenant manager.
    """
    DepartmentalPaymentDocument = apps.get_model("portal", "DepartmentalPaymentDocument")

    for document in DepartmentalPaymentDocument.objects.select_related("payment").iterator():
        payment_institution_id = document.payment.institution_id
        if payment_institution_id and document.institution_id != payment_institution_id:
            DepartmentalPaymentDocument.objects.filter(pk=document.pk).update(
                institution_id=payment_institution_id
            )


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0053_institution_logo_database_copy"),
    ]

    operations = [
        migrations.RunPython(
            align_document_institutions_with_payments,
            migrations.RunPython.noop,
        ),
    ]
