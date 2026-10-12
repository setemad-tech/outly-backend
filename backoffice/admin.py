from django.contrib import admin

from .models import AdminAction


@admin.register(AdminAction)
class AdminActionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target_label")
    list_filter = ("action", "target_type")
    search_fields = ("target_label", "actor__email")
    readonly_fields = [f.name for f in AdminAction._meta.fields]

    def has_add_permission(self, request):
        return False  # the log is written by the portal, never by hand
