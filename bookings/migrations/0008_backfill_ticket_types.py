"""
Every event made before ticket types existed gets one "General admission"
type at its old price, and every existing booking gets a matching item
copied from its own snapshot — so old tickets, sales figures and check-in
keep working exactly as before. Nothing existing is changed or deleted.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    TicketType = apps.get_model("events", "TicketType")
    Booking = apps.get_model("bookings", "Booking")
    BookingItem = apps.get_model("bookings", "BookingItem")

    for event in Event.objects.all().iterator():
        tt = TicketType.objects.filter(event=event).order_by("position").first()
        if tt is None:
            tt = TicketType.objects.create(
                event=event, name="General admission", price_minor=event.price_minor,
                guests_per_unit=1, max_per_order=10, status="active", position=0,
            )
        for b in Booking.objects.filter(event=event, items__isnull=True).iterator():
            qty = max(b.quantity, 1)
            full = b.price_paid_minor + b.points_redeemed * 100  # before points
            unit_price = full // qty
            unit_base = max(unit_price - b.service_fee_minor // qty, 0)
            BookingItem.objects.create(
                booking=b, ticket_type=tt, name=tt.name, quantity=qty, guests_per_unit=1,
                unit_base_minor=unit_base, unit_price_minor=unit_price,
                service_fee_minor=b.service_fee_minor, discount_minor=0,
                loyalty_points=b.loyalty_points,
            )


def backwards(apps, schema_editor):
    pass  # the tables themselves are dropped by the earlier migrations


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0007_booking_promo_code_booking_promo_code_text_and_more"),
        ("events", "0003_tickettype"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
