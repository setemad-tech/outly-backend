"""Quick checkout: email code sign-in and temporary guests (accounts/email_login.py, guests.py)."""

import re
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking
from events.models import Event, TicketType

from .guests import purge_expired_guests
from .models import EmailLoginCode, OrganizerProfile, User


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    ALLOWED_HOSTS=["*"],
)
class QuickCheckoutTests(TestCase):
    def setUp(self):
        cache.clear()  # throttle counters + daily purge flag
        self.client = APIClient()
        org_user = User.objects.create_user(email="org@example.com", password="x-Pass-1234")
        org = OrganizerProfile.objects.create(user=org_user, display_name="Org", status=OrganizerProfile.Status.APPROVED)
        self.event = Event.objects.create(
            organizer=org, title="Party", slug="party", venue_name="Marina", latitude=25.1, longitude=55.1,
            start_at=timezone.now() + timedelta(days=3), capacity=20, status=Event.Status.PUBLISHED, price_minor=10000,
        )
        self.ga = TicketType.objects.create(event=self.event, name="General", price_minor=10000)

    # helpers
    def ask(self, email):
        return self.client.post("/api/auth/email-code/", {"email": email}, format="json")

    def last_code(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def verify(self, email, code):
        return self.client.post("/api/auth/email-code/verify/", {"email": email, "code": code}, format="json")

    def sign_in(self, email):
        self.ask(email)
        return self.verify(email, self.last_code())

    def api(self, access):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return c

    # tests
    def test_new_email_becomes_one_hour_guest_only_after_code(self):
        self.assertEqual(self.ask("New.Person@Example.com").status_code, 200)
        self.assertFalse(User.objects.filter(email__iexact="new.person@example.com").exists())
        self.assertEqual(mail.outbox[-1].to, ["new.person@example.com"])
        code = self.last_code()
        self.assertEqual(self.verify("new.person@example.com", "000000" if code != "000000" else "111111").status_code, 400)
        r = self.verify("NEW.person@example.com", code)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["created"] and r.data["guest"])
        user = User.objects.get(email="new.person@example.com")
        self.assertTrue(user.is_guest and user.email_verified)
        self.assertFalse(user.has_usable_password())
        left = user.guest_session_ends_at - timezone.now()
        self.assertTrue(timedelta(minutes=59) < left <= timedelta(minutes=60))
        me = self.api(r.data["access"]).get("/api/me/")
        self.assertEqual((me.data["email"], me.data["is_guest"]), ("new.person@example.com", True))
        self.assertEqual(self.verify("new.person@example.com", code).status_code, 400)  # used up

        # An hour later the session is over: API and refresh both refuse.
        later = timezone.now() + timedelta(minutes=61)
        with mock.patch("accounts.authentication.timezone.now", return_value=later):
            self.assertEqual(self.api(r.data["access"]).get("/api/me/").status_code, 401)
            self.assertEqual(
                self.client.post("/api/auth/refresh/", {"refresh": r.data["refresh"]}, format="json").status_code, 401
            )
        # Coming back later starts a new session on the same guest.
        cache.clear()
        EmailLoginCode.objects.all().delete()
        r2 = self.sign_in("new.person@example.com")
        self.assertFalse(r2.data["created"])
        self.assertEqual(User.objects.filter(email="new.person@example.com").count(), 1)

    def test_existing_account_signs_in_as_itself(self):
        user = User.objects.create_user(email="old@example.com", password="x-Pass-1234")
        r = self.sign_in("old@example.com")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["created"] or r.data["guest"])
        user.refresh_from_db()
        self.assertFalse(user.is_guest)
        self.assertTrue(user.email_verified and user.has_usable_password())

    def test_guest_books_earns_nothing_and_gets_view_link(self):
        from bookings.quote import build_quote
        from bookings.services import create_pending_booking
        from bookings.emails import build_ticket_email_content

        self.sign_in("guest@example.com")
        guest = User.objects.get(email="guest@example.com")
        with mock.patch("loyalty.levels.events_attended", return_value=200):  # would be Icon
            q = build_quote(event=self.event, items=[{"ticket_type": str(self.ga.id), "quantity": 1}], user=guest)
        self.assertEqual((q.level, q.loyalty_points), ("", 0))
        b = create_pending_booking(user=guest, event_id=self.event.id, items=[{"ticket_type": str(self.ga.id), "quantity": 1}])
        b.status = Booking.Status.CONFIRMED
        b.save()
        _, text, html = build_ticket_email_content(b)
        link = re.search(r"View your ticket online: (\S+)", text).group(1)
        self.assertIn(link, html)
        token = link.rsplit("/", 1)[1]
        r = APIClient().get(f"/api/bookings/view/{token}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["holder"], r.data["event"]["title"]), ("guest@example.com", "Party"))
        self.assertEqual(APIClient().get(f"/api/bookings/view/{token}x/").status_code, 404)

    def test_guest_keeps_tickets_by_creating_account_from_session(self):
        r = self.sign_in("keep@example.com")
        guest = User.objects.get(email="keep@example.com")
        Booking.objects.create(user=guest, event=self.event, status=Booking.Status.CONFIRMED, quantity=1)
        # Plain signup with a guest's email is refused (would hand over their tickets unproven).
        r_reg = self.client.post("/api/auth/register/", {
            "email": "keep@example.com", "password": "x-Pass-1234", "name": "Keep Me", "gender": "female",
        }, format="json")
        self.assertEqual(r_reg.status_code, 400)
        self.assertIn("as a guest", str(r_reg.data))
        # From inside the guest session it works and keeps the booking.
        up = self.api(r.data["access"]).post("/api/me/upgrade/", {
            "name": "Keep Me", "password": "x-Pass-1234", "gender": "female",
        }, format="json")
        self.assertEqual(up.status_code, 200)
        guest.refresh_from_db()
        self.assertFalse(guest.is_guest)
        self.assertEqual(guest.get_full_name(), "Keep Me")
        self.assertEqual(guest.bookings.count(), 1)
        later = timezone.now() + timedelta(hours=3)
        with mock.patch("accounts.authentication.timezone.now", return_value=later):
            self.assertEqual(self.api(up.data["access"]).get("/api/me/").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/login/", {"email": "keep@example.com", "password": "x-Pass-1234"},
                                          format="json").status_code, 200)

    def test_guest_details_removed_45_days_after_last_event(self):
        old = User.objects.create(email="old-guest@example.com", is_guest=True, email_verified=True, first_name="Old",
                                  date_joined=timezone.now() - timedelta(days=100))
        recent = User.objects.create(email="recent-guest@example.com", is_guest=True, email_verified=True,
                                     date_joined=timezone.now() - timedelta(days=100))
        member = User.objects.create(email="member@example.com", date_joined=timezone.now() - timedelta(days=100))
        past = Event.objects.create(
            organizer=self.event.organizer, title="Past", slug="past", venue_name="X", latitude=25, longitude=55,
            start_at=timezone.now() - timedelta(days=50), capacity=5, status=Event.Status.PUBLISHED,
        )
        soon = Event.objects.create(
            organizer=self.event.organizer, title="Soon", slug="soon", venue_name="X", latitude=25, longitude=55,
            start_at=timezone.now() - timedelta(days=10), capacity=5, status=Event.Status.PUBLISHED,
        )
        Booking.objects.create(user=old, event=past, status=Booking.Status.ATTENDED, quantity=1)
        Booking.objects.create(user=recent, event=soon, status=Booking.Status.ATTENDED, quantity=1)
        self.assertEqual(purge_expired_guests(), 1)
        old.refresh_from_db()
        self.assertEqual((old.email, old.first_name, old.is_active), (f"removed-guest-{old.pk}@guest.invalid", "", False))
        self.assertEqual(old.bookings.count(), 1)  # the record stays
        recent.refresh_from_db()
        member.refresh_from_db()
        self.assertTrue(recent.is_active and member.is_active)
        self.assertEqual(purge_expired_guests(), 0)

    def test_cooldown_attempts_and_bad_email(self):
        self.assertEqual(self.ask("not-an-email").status_code, 400)
        self.ask("a@example.com")
        self.assertEqual(self.ask("a@example.com").status_code, 429)
        for _ in range(5):
            self.verify("a@example.com", "999999")
        self.assertEqual(self.verify("a@example.com", self.last_code()).status_code, 400)  # locked

    def test_staff_cannot_use_codes(self):
        User.objects.create_user(email="staff@example.com", password="x-Pass-1234", is_staff=True)
        sent = len(mail.outbox)
        self.assertEqual(self.ask("staff@example.com").status_code, 200)  # looks the same
        self.assertEqual(len(mail.outbox), sent)  # but nothing is sent
        self.assertEqual(self.verify("staff@example.com", "123456").status_code, 400)

    def test_requests_are_rate_limited_per_ip(self):
        statuses = [self.ask(f"p{i}@example.com").status_code for i in range(11)]
        self.assertEqual(statuses[-1], 429)
