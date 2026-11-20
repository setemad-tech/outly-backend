from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import LoyaltyEntry
from .levels import summary
from .serializers import LoyaltyEntrySerializer
from .services import balance


class MyLoyaltyView(APIView):
    """GET /api/loyalty/ — your points balance, level, and the latest 50 changes."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        entries = LoyaltyEntry.objects.filter(user=request.user).select_related("booking__event")[:50]
        return Response({
            "balance": balance(request.user),
            **summary(request.user),
            "entries": LoyaltyEntrySerializer(entries, many=True).data,
        })
