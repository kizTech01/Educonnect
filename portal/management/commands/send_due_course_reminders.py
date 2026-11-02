import json

from django.core.management.base import BaseCommand

from portal.services import dispatch_due_course_reminders


class Command(BaseCommand):
    help = "Send due class reminder emails and create browser alerts for courses starting in 30 minutes."

    def handle(self, *args, **options):
        summary = dispatch_due_course_reminders()
        self.stdout.write(self.style.SUCCESS(
            f"Lecture reminder execution: {json.dumps(summary, sort_keys=True)}"
        ))
