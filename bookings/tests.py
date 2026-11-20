"""
Ticket types, packages, promo codes and points at checkout (Stage 3, BU-06
to BU-10 + BU-30). Run: python manage.py test bookings
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import OrganizerProfile, User
from events.models import Event, TicketType
from loyalty.models import LoyaltyEntry
from loyalty.services import balance
from promos.models import PromoCode

from .models import Booking
from .quote import QuoteError, SoldOutError, build_quote
from .services import cancel_pending_booking, create_pending_booking


def make_user(email, **extra):
    return User.objects.create_user(email=email, password="x-Pass-1234", email_verified=True, **extra)


@override_settings(ALLOWED_HOSTS=["*"])
class TicketTypeOrderTests(TestCase):
    def setUp(self):
        self.org_user = make_user("org@test.ae")
        self.org = OrganizerProfile.objects.create(
            user=self.org_user, display_name="Org", status=OrganizerProfile.Status.APPROVED
        )
        self.event = Event.objects.create(
            organizer=self.org, title="Party", slug="party-1", venue_name="Marina",
            latitude=25.1, longitude=55.1, start_at=timezone.now() + timedelta(days=3),
            capacity=20, status=Event.Status.PUBLISHED, price_minor=10000,
        )
        self.ga = TicketType.objects.create(event=self.event, name="General", price_minor=10000, position=0)
        self.vip = TicketType.objects.create(event=self.event, name="VIP", price_minor=20000, position=1, quantity=2)
        self.drink = TicketType.objects.create(event=self.event, name="Entry + drink", price_minor=12000, position=2)
        self.table = TicketType.objects.create(
            event=self.event, name="Table for 6", price_minor=60000, guests_per_unit=6,
            quantity=2, max_per_order=2, position=3,
        )
        self.buyer = make_user("buyer@test.ae")

    def items(self, *pairs):
        return [{"ticket_type": str(t.id), "quantity": q} for t, q in pairs]

    def test_four_types_mixed_order_is_itemised(self):
        q = build_quote(event=self.event, items=self.items((self.ga, 2), (self.vip, 1), (self.table, 1)))
        self.assertEqual([l.ticket_type.name for l in q.lines], ["General", "VIP", "Table for 6"])
        # 100 -> 115, 200 -> 230, 600 -> 690 (fee included, whole AED)
        self.assertEqual(q.subtotal_minor, 2 * 11500 + 23000 + 69000)
        self.assertEqual(q.guests, 2 + 1 + 6)
        self.assertEqual(q.units, 4)
        self.assertEqual(q.total_minor, q.subtotal_minor)

    def test_booking_counts_people_and_keeps_snapshot(self):
        b = create_pending_booking(user=self.buyer, event_id=self.event.id, items=self.items((self.table, 1), (self.ga, 1)))
        self.assertEqual(b.quantity, 7)  # people admitted
        self.assertEqual(b.items.count(), 2)
        self.assertEqual(self.event.spots_left, 13)
        # A later price change doesn't touch what was sold.
        self.table.price_minor = 99900
        self.table.save()
        self.assertEqual(b.items.get(name="Table for 6").unit_price_minor, 69000)

    def test_ticket_email_and_preview_show_what_was_bought(self):
        from .emails import build_ticket_email_content

        self.table.description = "Reserved table with a bottle <included>"
        self.table.save()
        b = create_pending_booking(user=self.buyer, event_id=self.event.id, items=self.items((self.table, 1)))
        b.status = Booking.Status.CONFIRMED
        b.save()
        self.table.description = "changed later"
        self.table.save()
        _, text, html = build_ticket_email_content(b)
        self.assertIn("1 × Table for 6 · 6 guests", html)
        self.assertIn("Reserved table with a bottle &lt;included&gt;", html)  # snapshot, escaped
        self.assertNotIn("changed later", html)
        self.assertLess(html.index("qr.png"), html.index("YOUR TICKETS"))  # right under the QR
        self.assertIn("Your tickets:\n  1 × Table for 6 · 6 guests\n    Reserved table", text)
        # Staff can preview it (QR inlined); nobody else can.
        url = f"/api/admin/bookings/{b.id}/ticket-email/"
        c = APIClient()
        c.force_authenticate(self.buyer)
        self.assertEqual(c.get(url).status_code, 403)
        c.force_authenticate(make_user("staff@test.ae", is_staff=True))
        with self.settings(ALLOWED_HOSTS=["*"]):
            r = c.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn("data:image/png;base64,", r.data["html"])
        self.assertIn("Table for 6", r.data["html"])

    def test_package_stock_and_capacity(self):
        other = make_user("o@test.ae")
        create_pending_booking(user=other, event_id=self.event.id, items=self.items((self.table, 2)))
        with self.assertRaises(SoldOutError):
            build_quote(event=self.event, items=self.items((self.table, 1)))
        # 12 of 20 people taken: 8 left -> a 9th person can't buy.
        with self.assertRaises(SoldOutError):
            build_quote(event=self.event, items=self.items((self.ga, 9)))
        self.assertEqual(build_quote(event=self.event, items=self.items((self.ga, 8))).guests, 8)

    def test_last_unit_cant_be_sold_twice(self):
        a, b = make_user("a@test.ae"), make_user("b@test.ae")
        create_pending_booking(user=a, event_id=self.event.id, items=self.items((self.vip, 2)))
        with self.assertRaises(SoldOutError):
            create_pending_booking(user=b, event_id=self.event.id, items=self.items((self.vip, 1)))

    def test_expired_hold_frees_stock(self):
        a = make_user("a@test.ae")
        booking = create_pending_booking(user=a, event_id=self.event.id, items=self.items((self.vip, 2)))
        Booking.objects.filter(pk=booking.pk).update(booked_at=timezone.now() - timedelta(minutes=30))
        self.assertEqual(build_quote(event=self.event, items=self.items((self.vip, 2))).units, 2)

    def test_sale_windows_and_pause(self):
        now = timezone.now()
        self.vip.sales_start_at = now + timedelta(hours=1)
        self.vip.save()
        with self.assertRaisesMessage(QuoteError, "isn't on sale yet"):
            build_quote(event=self.event, items=self.items((self.vip, 1)))
        self.vip.sales_start_at, self.vip.sales_end_at = now - timedelta(hours=2), now - timedelta(seconds=1)
        self.vip.save()
        with self.assertRaisesMessage(QuoteError, "have ended"):
            build_quote(event=self.event, items=self.items((self.vip, 1)))
        self.ga.status = TicketType.Status.PAUSED
        self.ga.save()
        with self.assertRaisesMessage(QuoteError, "isn't on sale right now"):
            build_quote(event=self.event, items=self.items((self.ga, 1)))

    def test_limits(self):
        with self.assertRaisesMessage(QuoteError, "up to 2"):
            build_quote(event=self.event, items=self.items((self.table, 3)))
        with self.assertRaises(QuoteError):
            build_quote(event=self.event, items=[])
        with self.assertRaises(QuoteError):
            build_quote(event=self.event, items=[{"ticket_type": "nope", "quantity": 1}])

    def test_cancelled_event_doesnt_sell(self):
        self.event.status = Event.Status.CANCELLED
        self.event.save()
        with self.assertRaisesMessage(ValidationError, "isn't open"):
            create_pending_booking(user=self.buyer, event_id=self.event.id, items=self.items((self.ga, 1)))

    def test_old_clients_sending_quantity_still_work(self):
        b = create_pending_booking(user=self.buyer, event_id=self.event.id, quantity=3)
        self.assertEqual(b.quantity, 3)
        self.assertEqual(b.items.get().name, "General")

    # ---- promo codes ----

    def promo(self, **kw):
        defaults = dict(
            code="party10", kind=PromoCode.Kind.PERCENT, value=10, organizer=self.org,
            funded_by=PromoCode.FundedBy.ORGANIZER,
        )
        defaults.update(kw)
        return PromoCode.objects.create(**defaults)

    def test_promo_case_insensitive_and_whole_aed(self):
        self.promo()
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), promo_code=" Party10 ")
        self.assertEqual(q.discount_minor, 1100)  # 10% of 115 = 11.5 -> 11 AED
        self.assertEqual(q.total_minor, 10400)
        self.assertEqual(q.as_dict()["promo_code"], "PARTY10")

    def test_promo_errors_dont_block_quote_but_block_booking(self):
        self.promo(ends_at=timezone.now() - timedelta(minutes=1))
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), promo_code="PARTY10")
        self.assertEqual(q.promo_error, "That promo code has expired.")
        self.assertEqual(q.total_minor, 11500)
        with self.assertRaisesMessage(ValidationError, "expired"):
            create_pending_booking(user=self.buyer, event_id=self.event.id,
                                   items=self.items((self.ga, 1)), promo_code="PARTY10")
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), promo_code="NOPE")
        self.assertIn("doesn't exist", q.promo_error)

    def test_promo_scope_type_and_minimum(self):
        self.promo(code="VIPONLY", event=self.event).ticket_types.set([self.vip])
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), promo_code="VIPONLY")
        self.assertIn("doesn't apply", q.promo_error)
        q = build_quote(event=self.event, items=self.items((self.ga, 1), (self.vip, 1)), promo_code="VIPONLY")
        self.assertEqual(q.discount_minor, 2300)
        self.assertEqual([l.discount_minor for l in q.lines], [0, 2300])
        self.promo(code="MIN500", min_subtotal_minor=50000)
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), promo_code="MIN500")
        self.assertIn("Spend at least 500 AED", q.promo_error)
        # Another organizer's code doesn't work here.
        other = OrganizerProfile.objects.create(user=make_user("x@test.ae"), display_name="X")
        self.promo(code="OTHER", organizer=other)
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), promo_code="OTHER")
        self.assertIn("isn't valid for this event", q.promo_error)

    def test_promo_redemption_limits_release_on_cancel(self):
        self.promo(max_redemptions=1)
        a, b = make_user("a@test.ae"), make_user("b@test.ae")
        first = create_pending_booking(user=a, event_id=self.event.id, items=self.items((self.ga, 1)), promo_code="party10")
        with self.assertRaisesMessage(ValidationError, "fully used"):
            create_pending_booking(user=b, event_id=self.event.id, items=self.items((self.ga, 1)), promo_code="party10")
        cancel_pending_booking(first)  # abandoned checkout gives the use back
        create_pending_booking(user=b, event_id=self.event.id, items=self.items((self.ga, 1)), promo_code="party10")

    def test_promo_per_user_limit(self):
        self.promo(code="ONCE")
        create_pending_booking(user=self.buyer, event_id=self.event.id, items=self.items((self.ga, 1)), promo_code="once")
        second = Event.objects.create(
            organizer=self.org, title="Party 2", slug="party-2", venue_name="Marina",
            latitude=25.1, longitude=55.1, start_at=timezone.now() + timedelta(days=3),
            capacity=20, status=Event.Status.PUBLISHED,
        )
        tt = TicketType.objects.create(event=second, name="GA", price_minor=10000)
        q = build_quote(event=second, items=[{"ticket_type": str(tt.id), "quantity": 1}],
                        user=self.buyer, promo_code="ONCE")
        self.assertIn("already used", q.promo_error)

    def test_fixed_promo_free_order_and_stripe_minimum(self):
        self.promo(code="FREE", kind=PromoCode.Kind.FIXED, value=11500)
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), promo_code="FREE")
        self.assertEqual(q.total_minor, 0)
        self.promo(code="ALMOST", kind=PromoCode.Kind.FIXED, value=11400)
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), promo_code="ALMOST")
        self.assertEqual(q.total_minor, 200)  # never a 1 AED card charge

    def test_funding_organizer_vs_outly(self):
        from backoffice.analytics import organizer_share
        from django.db.models import Sum

        self.promo(code="ORG20", kind=PromoCode.Kind.FIXED, value=2000)
        PromoCode.objects.create(code="OUTLY20", kind=PromoCode.Kind.FIXED, value=2000,
                                 funded_by=PromoCode.FundedBy.OUTLY)
        a, b = make_user("a@test.ae"), make_user("b@test.ae")
        org_b = create_pending_booking(user=a, event_id=self.event.id, items=self.items((self.ga, 1)), promo_code="org20")
        outly_b = create_pending_booking(user=b, event_id=self.event.id, items=self.items((self.ga, 1)), promo_code="outly20")
        share = lambda bk: Booking.objects.filter(pk=bk.pk).aggregate(s=Sum(organizer_share()))["s"]
        self.assertEqual(outly_b.price_paid_minor, 9500)
        self.assertEqual(share(outly_b), 10000)        # organizer untouched, OUTLY pays
        self.assertEqual(org_b.price_paid_minor, 9500)
        self.assertLess(share(org_b), 10000)           # organizer pays...
        self.assertGreaterEqual(share(org_b), 0)
        self.assertLess(org_b.service_fee_minor, 1500)  # ...and the fee shrinks in proportion

    def test_points_stack_after_promo(self):
        self.promo()
        LoyaltyEntry.objects.create(user=self.buyer, points=50, kind=LoyaltyEntry.Kind.ADJUSTED)
        b = create_pending_booking(user=self.buyer, event_id=self.event.id, items=self.items((self.ga, 1)),
                                   promo_code="party10", use_points=True)
        self.assertEqual(b.discount_minor, 1100)
        self.assertEqual(b.points_redeemed, 50)
        self.assertEqual(b.price_paid_minor, 11500 - 1100 - 5000)
        self.assertEqual(balance(self.buyer), 0)
        cancel_pending_booking(b)
        self.assertEqual(balance(self.buyer), 50)

    def test_points_earned_only_on_card_payment(self):
        from unittest import mock

        # Level 0 (under 25 events attended): no points at all.
        q = build_quote(event=self.event, items=self.items((self.table, 1)), user=self.buyer)
        self.assertEqual(q.level, "")
        self.assertEqual(q.loyalty_points, 0)
        with mock.patch("loyalty.levels.events_attended", return_value=25):  # Explorer
            # Full price by card: 2% of the organizer's 600 AED = 12 points.
            q = build_quote(event=self.event, items=self.items((self.table, 1)), user=self.buyer)
            self.assertEqual((q.level, q.loyalty_points), ("explorer", 12))
            # Part paid with points: points only on the card-paid part.
            LoyaltyEntry.objects.create(user=self.buyer, points=300, kind=LoyaltyEntry.Kind.ADJUSTED)
            q = build_quote(event=self.event, items=self.items((self.table, 1)), user=self.buyer, use_points=True)
            self.assertEqual(q.points_redeemed, 300)
            self.assertEqual(q.total_minor, 69000 - 30000)
            self.assertEqual(q.loyalty_points, 12 * 39000 // 69000)  # = 6
            # Entirely with points: nothing earned, and the booking agrees.
            LoyaltyEntry.objects.create(user=self.buyer, points=500, kind=LoyaltyEntry.Kind.ADJUSTED)
            b = create_pending_booking(user=self.buyer, event_id=self.event.id,
                                       items=self.items((self.table, 1)), use_points=True)
            self.assertEqual(b.price_paid_minor, 0)
            self.assertEqual(b.loyalty_points, 0)
            self.assertEqual(sum(i.loyalty_points for i in b.items.all()), 0)

    def test_levels_follow_events_attended(self):
        from loyalty.levels import events_attended, level_for, meets, summary

        self.assertEqual([level_for(n) for n in (0, 24, 25, 74, 75, 149, 150)],
                         ["", "", "explorer", "explorer", "insider", "insider", "icon"])
        self.assertTrue(meets("icon", "insider"))
        self.assertFalse(meets("explorer", "insider"))
        self.assertTrue(meets("", ""))
        # Only checked-in events count.
        other = Event.objects.create(
            organizer=self.org, title="Other", slug="other-1", venue_name="Marina", latitude=25.1, longitude=55.1,
            start_at=timezone.now() + timedelta(days=4), capacity=20, status=Event.Status.PUBLISHED,
        )
        Booking.objects.create(user=self.buyer, event=self.event, status=Booking.Status.ATTENDED, quantity=1)
        Booking.objects.create(user=self.buyer, event=other, status=Booking.Status.CONFIRMED, quantity=1)
        self.assertEqual(events_attended(self.buyer), 1)
        info = summary(self.buyer)
        self.assertEqual((info["level"], info["next_level"], info["events_to_next"]), ("", "explorer", 24))

    def test_early_access_by_level(self):
        from unittest import mock

        self.event.early_access_level = "insider"
        self.event.early_access_hours = 24
        self.event.save()
        self.vip.sales_start_at = timezone.now() + timedelta(hours=10)
        self.vip.save()
        with self.assertRaisesMessage(QuoteError, "isn't on sale yet"):
            build_quote(event=self.event, items=self.items((self.vip, 1)), user=self.buyer)
        with mock.patch("loyalty.levels.events_attended", return_value=75):  # Insider
            q = build_quote(event=self.event, items=self.items((self.vip, 1)), user=self.buyer)
            self.assertEqual(q.level, "insider")
            b = create_pending_booking(user=self.buyer, event_id=self.event.id, items=self.items((self.vip, 1)))
            self.assertEqual(b.items.get().name, "VIP")
        with mock.patch("loyalty.levels.events_attended", return_value=25):  # Explorer isn't enough
            with self.assertRaises(QuoteError):
                build_quote(event=self.event, items=self.items((self.vip, 1)), user=make_user("s@test.ae"))
        # Further out than the early window: still closed for Insider.
        self.vip.sales_start_at = timezone.now() + timedelta(hours=30)
        self.vip.save()
        with mock.patch("loyalty.levels.events_attended", return_value=75):
            with self.assertRaises(QuoteError):
                build_quote(event=self.event, items=self.items((self.vip, 1)), user=make_user("g@test.ae"))

    def test_members_only_promo(self):
        from unittest import mock

        self.promo(min_level="insider")
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), user=self.buyer, promo_code="party10")
        self.assertEqual(q.promo_error, "That promo code is for Insider members and above.")
        with mock.patch("loyalty.levels.events_attended", return_value=150):
            q = build_quote(event=self.event, items=self.items((self.ga, 1)), user=self.buyer, promo_code="party10")
        self.assertEqual(q.discount_minor, 1100)

    def test_points_not_allowed_on_type(self):
        self.ga.allow_points = False
        self.ga.save()
        LoyaltyEntry.objects.create(user=self.buyer, points=50, kind=LoyaltyEntry.Kind.ADJUSTED)
        q = build_quote(event=self.event, items=self.items((self.ga, 1)), user=self.buyer, use_points=True)
        self.assertEqual(q.points_redeemed, 0)


@override_settings(ALLOWED_HOSTS=["*"])
class TicketTypeApiTests(TestCase):
    def setUp(self):
        self.org_user = make_user("org@test.ae")
        self.org = OrganizerProfile.objects.create(
            user=self.org_user, display_name="Org", status=OrganizerProfile.Status.APPROVED
        )
        self.client = APIClient()
        self.client.force_authenticate(self.org_user)

    def create_event(self, **extra):
        payload = dict(
            title="Gala", slug="gala-1", venue_name="DIFC", latitude="25.2", longitude="55.3",
            start_at=(timezone.now() + timedelta(days=5)).isoformat(), capacity=30, status="published",
            category_label="", **extra,
        )
        return self.client.post("/api/events/", payload, format="json")

    def test_create_with_types_then_edit_and_archive(self):
        r = self.create_event(ticket_types=[
            {"name": "GA", "price_minor": 5000},
            {"name": "Table", "price_minor": 50000, "guests_per_unit": 5, "quantity": 3},
        ])
        self.assertEqual(r.status_code, 201, r.content)
        event_id = r.data["id"]
        types = r.data["ticket_types"]
        self.assertEqual([t["name"] for t in types], ["GA", "Table"])
        self.assertEqual(types[1]["units_sold"], 0)
        self.assertEqual(r.data["price_minor"], 5000)
        self.assertEqual(r.data["from_price_minor"], 5800)

        buyer = make_user("b@test.ae")
        create_pending_booking(user=buyer, event_id=event_id,
                               items=[{"ticket_type": types[1]["id"], "quantity": 1}])
        # Guests per unit can't change once sold.
        bad = [{"id": types[0]["id"], "name": "GA", "price_minor": 5000},
               {"id": types[1]["id"], "name": "Table", "price_minor": 50000, "guests_per_unit": 6}]
        r = self.client.patch(f"/api/events/{event_id}/", {"ticket_types": bad}, format="json")
        self.assertEqual(r.status_code, 400)
        # Removing a sold type archives it; the sale stays.
        r = self.client.patch(f"/api/events/{event_id}/", {"ticket_types": [
            {"id": types[0]["id"], "name": "General", "price_minor": 6000},
            {"name": "VIP", "price_minor": 15000},
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([t["name"] for t in r.data["ticket_types"]], ["General", "VIP"])
        self.assertEqual(TicketType.objects.get(pk=types[1]["id"]).status, "archived")
        # Reordering sticks (the list query aggregates, so ordering must be explicit).
        current = r.data["ticket_types"]
        r = self.client.patch(f"/api/events/{event_id}/", {"ticket_types": [
            {"id": current[1]["id"], "name": "VIP", "price_minor": 15000},
            {"id": current[0]["id"], "name": "General", "price_minor": 6000},
        ]}, format="json")
        self.assertEqual([t["name"] for t in r.data["ticket_types"]], ["VIP", "General"])
        listed = APIClient().get("/api/events/").data
        listed = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual([t["name"] for t in listed[0]["ticket_types"]], ["VIP", "General"])

        # The public sees on-sale types only, without stock internals.
        anon = APIClient().get(f"/api/events/{event_id}/").data
        self.assertNotIn("units_sold", anon["ticket_types"][0])

    def test_create_without_types_gets_general_admission(self):
        r = self.create_event(price_minor=7500)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["ticket_types"][0]["name"], "General admission")
        self.assertEqual(r.data["ticket_types"][0]["ticket_price_minor"], 8700)

    def test_validation(self):
        r = self.create_event(ticket_types=[{"name": "Cheap", "price_minor": 50}])
        self.assertEqual(r.status_code, 400)
        r = self.create_event(ticket_types=[{"name": "Huge table", "price_minor": 0, "guests_per_unit": 40}])
        self.assertEqual(r.status_code, 400)

    def test_quote_endpoint(self):
        r = self.create_event(ticket_types=[{"name": "GA", "price_minor": 5000}])
        tt = r.data["ticket_types"][0]["id"]
        r = APIClient().post(f"/api/events/{r.data['id']}/quote/",
                             {"items": [{"ticket_type": tt, "quantity": 2}], "promo_code": "zzz"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["total_minor"], 11600)
        self.assertTrue(r.data["promo_error"])

    def test_service_fee_set_per_organizer_by_admin(self):
        r = self.create_event(ticket_types=[{"name": "GA", "price_minor": 10000}])
        event_id, tt = r.data["id"], r.data["ticket_types"][0]["id"]
        url = f"/api/admin/organizers/{self.org.id}/service-fee/"
        # Only staff can change it.
        self.assertEqual(self.client.patch(url, {"service_fee_percent": 5}, format="json").status_code, 403)
        staff = make_user("staff@test.ae", is_staff=True)
        admin = APIClient()
        admin.force_authenticate(staff)
        for bad in (-1, 101, "abc", 7.5, ""):
            self.assertEqual(admin.patch(url, {"service_fee_percent": bad}, format="json").status_code, 400, bad)
        r = admin.patch(url, {"service_fee_percent": 10}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["service_fee_percent"], 10)
        # The event page and checkout follow it: 100 + 10% = 110.
        r = APIClient().get(f"/api/events/{event_id}/")
        self.assertEqual(r.data["service_fee_percent"], 10)
        self.assertEqual(r.data["from_price_minor"], 11000)
        self.assertEqual(r.data["ticket_types"][0]["ticket_price_minor"], 11000)
        r = APIClient().post(f"/api/events/{event_id}/quote/",
                             {"items": [{"ticket_type": tt, "quantity": 2}]}, format="json")
        self.assertEqual(r.data["total_minor"], 22000)
        # 0% = the organizer's own price.
        admin.patch(url, {"service_fee_percent": 0}, format="json")
        r = APIClient().get(f"/api/events/{event_id}/")
        self.assertEqual(r.data["from_price_minor"], 10000)

    def test_promo_api_staff_only(self):
        r = self.create_event(ticket_types=[{"name": "GA", "price_minor": 5000}])
        event_id = r.data["id"]
        # Organizers can't make codes any more — only OUTLY staff.
        r = self.client.post("/api/me/organizer/promos/", {
            "code": "early-bird", "kind": "percent", "value": 20, "event": event_id,
        }, format="json")
        self.assertEqual(r.status_code, 405)
        r = self.client.post("/api/admin/promos/", {"code": "x1", "kind": "percent", "value": 5}, format="json")
        self.assertEqual(r.status_code, 403)

        staff = APIClient()
        staff.force_authenticate(make_user("staff@test.ae", is_staff=True))
        r = staff.post("/api/admin/promos/", {
            "code": "early!bird", "kind": "percent", "value": 20, "event": event_id,
        }, format="json")
        self.assertEqual(r.status_code, 400)  # only letters, numbers, - and _
        # Organizer-paid without saying which organizer is refused.
        r = staff.post("/api/admin/promos/", {
            "code": "nobody", "kind": "percent", "value": 20, "funded_by": "organizer",
        }, format="json")
        self.assertEqual(r.status_code, 400)
        r = staff.post("/api/admin/promos/", {
            "code": "early-bird", "kind": "percent", "value": 20, "event": event_id, "funded_by": "organizer",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["code"], "EARLY-BIRD")
        self.assertEqual(r.data["funded_by"], "organizer")
        self.assertEqual(r.data["organizer"], self.org.id)  # taken from the event
        pid = r.data["id"]
        r = staff.patch(f"/api/admin/promos/{pid}/", {"value": 90}, format="json")
        self.assertEqual(r.status_code, 400)  # discount is fixed once made
        r = staff.patch(f"/api/admin/promos/{pid}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200)
        # An event can't be paired with a different organizer.
        other_org = OrganizerProfile.objects.create(
            user=make_user("o2@test.ae"), display_name="O2", status=OrganizerProfile.Status.APPROVED
        )
        r = staff.post("/api/admin/promos/", {
            "code": "MIXUP", "kind": "percent", "value": 5, "event": event_id, "organizer": other_org.id,
        }, format="json")
        self.assertEqual(r.status_code, 400)
        # The event's organizer sees it (read-only); another organizer doesn't.
        mine = self.client.get("/api/me/organizer/promos/").data
        self.assertEqual([p["code"] for p in mine], ["EARLY-BIRD"])
        self.assertEqual(self.client.patch(f"/api/me/organizer/promos/{pid}/", {"is_active": True}, format="json").status_code, 404)
        c2 = APIClient()
        c2.force_authenticate(other_org.user)
        self.assertEqual(c2.get("/api/me/organizer/promos/").data, [])
        # The picker data lists the organizer with the event.
        targets = staff.get("/api/admin/promos/targets/").data
        org_row = next(o for o in targets if o["id"] == self.org.id)
        self.assertEqual([e["id"] for e in org_row["events"]], [event_id])
