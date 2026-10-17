from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import LoyaltyEntry
from .serializers import LoyaltyEntrySerializer
from .services import balance


class MyLoyaltyView(APIView):
    """GET /api/loyalty/ — your points balance and the latest 50 changes."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        entries = LoyaltyEntry.objects.filter(user=request.user).select_related("booking")[:50]
        return Response({
            "balance": balance(request.user),
            "entries": LoyaltyEntrySerializer(entries, many=True).data,
        })
