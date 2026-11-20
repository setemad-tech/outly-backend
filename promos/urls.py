from django.urls import path

from . import views

urlpatterns = [
    path("me/organizer/promos/", views.OrganizerPromoListView.as_view()),
    path("admin/promos/targets/", views.AdminPromoTargetsView.as_view()),
    path("admin/promos/", views.AdminPromoListView.as_view()),
    path("admin/promos/<int:pk>/", views.AdminPromoDetailView.as_view()),
]
