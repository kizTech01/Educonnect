from django.core.management.base import BaseCommand

from portal.services import dispatch_due_course_reminders


class Command(BaseCommand):
    help = "Send due class reminder emails and create browser alerts for courses starting in 30 minutes."

    def handle(self, *args, **options):
        dispatch_due_course_reminders()
        self.stdout.write(self.style.SUCCESS("Processed due course reminders."))
