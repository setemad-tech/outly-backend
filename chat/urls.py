from django.urls import path
from .views import ChatRoomListView, MessageHistoryView

urlpatterns = [
    path("chat/rooms/", ChatRoomListView.as_view()),
    path("chat/rooms/<uuid:room_id>/messages/", MessageHistoryView.as_view()),
]