from django.contrib import admin

from .models import LoyaltyEntry


@admin.register(LoyaltyEntry)
class LoyaltyEntryAdmin(admin.ModelAdmin):
    list_display = ["user", "points", "kind", "note", "created_at"]
    list_filter = ["kind"]
    search_fields = ["user__email", "note"]
    raw_id_fields = ["user", "booking", "created_by"]
