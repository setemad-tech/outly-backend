from django.contrib import admin

from .models import EmailOTP, OrganizerProfile, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    # Not django.contrib.auth.admin.UserAdmin — its fieldsets/forms assume
    # `username` is the login field, but ours is `email` (USERNAME_FIELD in
    # models.py), so the stock version doesn't fit this model. Plain
    # ModelAdmin auto-generates a form from the model instead, which just
    # works here; this is only for visibility (who's verified), not for
    # creating users through admin.
    list_display = ("email", "is_staff", "email_verified", "date_joined")
    list_filter = ("email_verified", "is_staff", "is_superuser")
    search_fields = ("email",)
    ordering = ("-date_joined",)
    # The auto-generated form would otherwise show the raw password hash
    # as an editable text field — visible is fine for a superuser, editable
    # risks someone fat-fingering it and locking the account out.
    readonly_fields = ("password",)


@admin.register(OrganizerProfile)
class OrganizerProfileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "user", "is_verified", "stripe_onboarding_complete")
    search_fields = ("display_name", "user__email")


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    # code_hash is never shown — it's a password hash, not something to
    # display even in admin.
    list_display = ("user", "attempts", "created_at", "expires_at")
    readonly_fields = ("code_hash",)
    search_fields = ("user__email",)
