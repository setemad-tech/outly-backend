from django.urls import path

from . import views

urlpatterns = [
    path("admin/overview/", views.OverviewView.as_view()),
    path("admin/organizers/", views.OrganizerListView.as_view()),
    path("admin/organizers/<int:pk>/review/", views.OrganizerReviewView.as_view()),
    path("admin/events/", views.EventListView.as_view()),
    path("admin/events/<uuid:pk>/", views.EventDetailView.as_view()),
    path("admin/users/", views.UserListView.as_view()),
    path("admin/users/<int:pk>/", views.UserDetailView.as_view()),
    path("admin/bookings/", views.BookingListView.as_view()),
    path("admin/activity/", views.ActivityListView.as_view()),
]
