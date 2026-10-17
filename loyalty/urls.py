from django.urls import path

from .views import MyLoyaltyView

urlpatterns = [
    path("loyalty/", MyLoyaltyView.as_view(), name="my-loyalty"),
]
