from django.core.management.base import BaseCommand

from portal.services import dispatch_due_course_reminders


class Command(BaseCommand):
    help = "Send due class reminder emails and create browser alerts for courses starting in 30 minutes."

    def handle(self, *args, **options):
        summary = dispatch_due_course_reminders()
        self.stdout.write(self.style.SUCCESS(
            "Processed due course reminders: "
            f"courses={summary['courses_due']}, "
            f"email_delivered={summary['email_delivered']}, "
            f"email_failed={summary['email_failed']}, "
            f"alerts_delivered={summary['alerts_delivered']}."
        ))
