from django.contrib import admin

from .models import PromoCode


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = ["code", "kind", "value", "organizer", "event", "funded_by", "is_active", "ends_at"]
    list_filter = ["kind", "funded_by", "is_active"]
    search_fields = ["code"]
    raw_id_fields = ["organizer", "event", "created_by"]
    filter_horizontal = ["ticket_types"]
