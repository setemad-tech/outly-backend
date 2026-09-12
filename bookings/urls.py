from django.urls import path
from .views import (
    MyTicketsListView,
    CreateFreeBookingView,
    BookingDetailView,
    CheckInView,
    ParticipantsListView,
    OrganizerAnalyticsView,
    TicketQRImageView,
)

urlpatterns = [
    path("bookings/mine/", MyTicketsListView.as_view()),
    path("bookings/<uuid:event_id>/free/", CreateFreeBookingView.as_view()),
    # Literal routes before <uuid:pk> — the uuid converter already rejects
    # non-UUID strings, so ordering isn't currently load-bearing, but it's
    # cheap insurance against that changing later.
    path("bookings/checkin/", CheckInView.as_view()),
    path("bookings/<uuid:pk>/qr.png/", TicketQRImageView.as_view()),
    path("bookings/<uuid:pk>/", BookingDetailView.as_view()),
    path("events/<uuid:event_id>/participants/", ParticipantsListView.as_view()),
    path("me/organizer/analytics/", OrganizerAnalyticsView.as_view()),
]