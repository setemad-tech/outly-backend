"""
backoffice/views.py
---------------------
Staff-only API for the admin portal. Every endpoint requires is_staff
(IsAdminUser), and every change is written to the AdminAction log.

  GET    /api/admin/overview/?range=30          company analytics
  GET    /api/admin/organizers/?status=&q=      applications + organizers
  POST   /api/admin/organizers/<id>/review/     {status, note}
  GET    /api/admin/events/?scope=&status=&q=   all events
  PATCH  /api/admin/events/<id>/                {status}
  DELETE /api/admin/events/<id>/
  GET    /api/admin/users/?q=
  GET    /api/admin/users/<id>/
  PATCH  /api/admin/users/<id>/                 profile, email_verified, is_staff, is_active
  DELETE /api/admin/users/<id>/
  GET    /api/admin/bookings/?status=&q=
  GET    /api/admin/activity/
"""

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.emails import send_organizer_review_email
from accounts.models import OrganizerProfile
from bookings.models import Booking
from events.models import Event

from . import analytics
from .models import AdminAction, log_action
from .serializers import (
    AdminUserUpdateSerializer,
    AdminActionSerializer,
    AdminBookingSerializer,
    AdminEventSerializer,
    AdminOrganizerSerializer,
    AdminUserDetailSerializer,
    AdminUserSerializer,
)

User = get_user_model()
SOLD = analytics.SOLD


class Page(LimitOffsetPagination):
    default_limit = 25
    max_limit = 100


class StaffOnly:
    permission_classes = [permissions.IsAdminUser]


class OverviewView(StaffOnly, APIView):
    def get(self, request):
        key, start, end = analytics.parse_range(request)
        return Response({"range": key, **analytics.company_overview(start, end)})


# ---------- Organizers ----------

class OrganizerListView(StaffOnly, generics.ListAPIView):
    serializer_class = AdminOrganizerSerializer
    pagination_class = Page

    def get_queryset(self):
        qs = (
            OrganizerProfile.objects.select_related("user", "reviewed_by")
            .annotate(events_count=Count("events"))
            .order_by("-created_at")
        )
        status_ = self.request.query_params.get("status")
        if status_ in OrganizerProfile.Status.values:
            qs = qs.filter(status=status_)
        q = self.request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(display_name__icontains=q) | Q(user__email__icontains=q)
                | Q(instagram__icontains=q) | Q(website__icontains=q)
            )
        return qs


class OrganizerReviewView(StaffOnly, APIView):
    """Approve or reject. The applicant is emailed (with the note, if rejected)."""

    def post(self, request, pk):
        profile = get_object_or_404(OrganizerProfile.objects.select_related("user"), pk=pk)
        new_status = request.data.get("status")
        if new_status not in (OrganizerProfile.Status.APPROVED, OrganizerProfile.Status.REJECTED):
            return Response({"detail": "status must be 'approved' or 'rejected'."}, status=400)
        note = str(request.data.get("note", "")).strip()

        profile.status = new_status
        profile.review_note = note if new_status == OrganizerProfile.Status.REJECTED else ""
        profile.reviewed_at = timezone.now()
        profile.reviewed_by = request.user
        profile.save(update_fields=["status", "review_note", "reviewed_at", "reviewed_by"])
        send_organizer_review_email(profile)
        log_action(request.user, f"organizer.{new_status}", "organizer", profile.pk,
                   profile.display_name, note=note)

        profile.events_count = profile.events.count()
        return Response(AdminOrganizerSerializer(profile).data)


# ---------- Events ----------

def _events_with_sales():
    return Event.objects.select_related("organizer", "category").annotate(
        tickets_sold=Coalesce(Sum("bookings__quantity", filter=Q(bookings__status__in=SOLD)), 0),
        revenue_minor=Coalesce(Sum("bookings__price_paid_minor", filter=Q(bookings__status__in=SOLD)), 0),
    )


class EventListView(StaffOnly, generics.ListAPIView):
    serializer_class = AdminEventSerializer
    pagination_class = Page

    def get_queryset(self):
        qs = _events_with_sales()
        scope = self.request.query_params.get("scope", "upcoming")
        if scope == "upcoming":
            qs = qs.filter(start_at__gte=timezone.now()).order_by("start_at")
        elif scope == "past":
            qs = qs.filter(start_at__lt=timezone.now()).order_by("-start_at")
        else:
            qs = qs.order_by("-start_at")
        status_ = self.request.query_params.get("status")
        if status_ in Event.Status.values:
            qs = qs.filter(status=status_)
        q = self.request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q) | Q(venue_name__icontains=q)
                | Q(organizer__display_name__icontains=q)
            )
        return qs


class EventDetailView(StaffOnly, APIView):
    def patch(self, request, pk):
        event = get_object_or_404(Event, pk=pk)
        new_status = request.data.get("status")
        if new_status not in Event.Status.values:
            return Response({"detail": "Unknown status."}, status=400)
        old = event.status
        event.status = new_status
        event.save(update_fields=["status", "updated_at"])
        log_action(request.user, "event.status", "event", event.pk, event.title, old=old, new=new_status)
        return Response(AdminEventSerializer(_events_with_sales().get(pk=pk)).data)

    def delete(self, request, pk):
        event = get_object_or_404(Event, pk=pk)
        sold = event.bookings.filter(status__in=SOLD).count()
        if sold and request.query_params.get("force") != "true":
            return Response(
                {"detail": f"{sold} booking(s) exist for this event — cancel it instead, "
                           "or confirm deletion.", "bookings": sold},
                status=status.HTTP_409_CONFLICT,
            )
        log_action(request.user, "event.deleted", "event", event.pk, event.title, bookings=sold)
        event.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------- Users ----------

def _users_with_stats():
    return (
        User.objects.select_related("organizer_profile")
        .annotate(
            bookings_count=Count("bookings", filter=Q(bookings__status__in=SOLD)),
            spent_minor=Coalesce(Sum("bookings__price_paid_minor", filter=Q(bookings__status__in=SOLD)), 0),
        )
        .order_by("-date_joined")
    )


class UserListView(StaffOnly, generics.ListAPIView):
    serializer_class = AdminUserSerializer
    pagination_class = Page

    def get_queryset(self):
        qs = _users_with_stats()
        q = self.request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
        return qs


class UserDetailView(StaffOnly, APIView):
    """
    GET    one account (plus what deleting it would remove)
    PATCH  edit profile fields, verify the email manually, grant/remove
           staff, activate/deactivate — see AdminUserUpdateSerializer
    DELETE remove the account for good (bookings and organizer events go with it)

    Guard rails: nobody can lock themselves out (deactivate, un-staff or
    delete their own account), and only a superuser can change a superuser.
    """

    def _guard(self, request, user):
        if user.is_superuser and not request.user.is_superuser:
            return Response({"detail": "Only a superuser can change a superuser account."}, status=403)
        return None

    def _detail(self, pk):
        row = _users_with_stats().get(pk=pk)
        return AdminUserDetailSerializer(row).data

    def get(self, request, pk):
        get_object_or_404(User, pk=pk)
        return Response(self._detail(pk))

    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        if denied := self._guard(request, user):
            return denied
        serializer = AdminUserUpdateSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if user.pk == request.user.pk:
            if serializer.validated_data.get("is_active") is False:
                return Response({"detail": "You can't deactivate your own account."}, status=400)
            if serializer.validated_data.get("is_staff") is False:
                return Response({"detail": "You can't remove your own staff access."}, status=400)
        before = {f: getattr(user, f) for f in serializer.validated_data}
        serializer.save()
        changed = {f: [before[f], getattr(user, f)] for f in before if before[f] != getattr(user, f)}

        if changed:
            # One log line per meaningful change, so the activity log reads clearly.
            if "is_active" in changed:
                log_action(request.user, "user.activated" if user.is_active else "user.deactivated",
                           "user", user.pk, user.email)
            if "is_staff" in changed:
                log_action(request.user, "user.staff_granted" if user.is_staff else "user.staff_removed",
                           "user", user.pk, user.email)
            if "email_verified" in changed:
                log_action(request.user, "user.email_verified" if user.email_verified else "user.email_unverified",
                           "user", user.pk, user.email)
            profile = {k: v for k, v in changed.items() if k not in ("is_active", "is_staff", "email_verified")}
            if profile:
                log_action(request.user, "user.updated", "user", user.pk, user.email,
                           fields=sorted(profile), **({"old_email": profile["email"][0]} if "email" in profile else {}))
        return Response(self._detail(pk))

    def delete(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        if denied := self._guard(request, user):
            return denied
        if user.pk == request.user.pk:
            return Response({"detail": "You can't delete your own account here."}, status=400)
        profile = getattr(user, "organizer_profile", None)
        log_action(
            request.user, "user.deleted", "user", user.pk, user.email,
            bookings=user.bookings.count(),
            organizer_events=profile.events.count() if profile else 0,
        )
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserLoyaltyView(StaffOnly, APIView):
    """POST {points, note} — add (positive) or remove (negative) loyalty points."""

    def post(self, request, pk):
        from loyalty.services import adjust

        user = get_object_or_404(User, pk=pk)
        try:
            points = int(request.data.get("points"))
        except (TypeError, ValueError):
            return Response({"detail": "Points must be a whole number."}, status=400)
        note = str(request.data.get("note") or "").strip()
        if not note:
            return Response({"detail": "Add a short reason — the user sees it in their points history."}, status=400)
        try:
            adjust(user=user, points=points, note=note, by=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        log_action(request.user, "user.loyalty_adjusted", "user", user.pk, user.email, points=points, note=note)
        return Response(AdminUserDetailSerializer(_users_with_stats().get(pk=pk)).data)


# ---------- Bookings ----------

class BookingListView(StaffOnly, generics.ListAPIView):
    serializer_class = AdminBookingSerializer
    pagination_class = Page

    def get_queryset(self):
        qs = Booking.objects.select_related("user", "event", "payment").order_by("-booked_at")
        status_ = self.request.query_params.get("status")
        if status_ in Booking.Status.values:
            qs = qs.filter(status=status_)
        q = self.request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(user__email__icontains=q) | Q(event__title__icontains=q))
        event_id = self.request.query_params.get("event")
        if event_id:
            qs = qs.filter(event_id=event_id)
        return qs


# ---------- Activity log ----------

class ActivityListView(StaffOnly, generics.ListAPIView):
    serializer_class = AdminActionSerializer
    pagination_class = Page
    queryset = AdminAction.objects.select_related("actor")
