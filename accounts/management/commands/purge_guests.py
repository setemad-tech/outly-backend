from django.core.management.base import BaseCommand

from accounts.guests import GUEST_RETENTION_DAYS, purge_expired_guests


class Command(BaseCommand):
    help = f"Remove guest details {GUEST_RETENTION_DAYS} days after their last event (accounts/guests.py)."

    def handle(self, *args, **options):
        self.stdout.write(f"Removed {purge_expired_guests()} guest(s).")
