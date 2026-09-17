from django.core.management.base import BaseCommand

from portal.services import send_subscription_expiry_notifications


class Command(BaseCommand):
    help = "Send idempotent subscription expiry emails due today."

    def handle(self, *args, **options):
        sent = send_subscription_expiry_notifications()
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} subscription notification(s)."))
