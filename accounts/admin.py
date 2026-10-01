from django.contrib import admin, messages
from django.utils import timezone

from .emails import send_organizer_review_email
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
    """
    Where organizer applications get reviewed. Filter by "Pending review",
    open an application, check the details/links, then either use the
    Approve/Reject actions on the list, or set Status on the record itself
    (that's how to reject WITH a note — the note is emailed to the applicant).
    """
    list_display = (
        "display_name", "user", "status", "city", "created_at", "reviewed_at",
        "stripe_onboarding_complete",
    )
    list_filter = ("status", "stripe_onboarding_complete")
    search_fields = ("display_name", "user__email", "instagram", "website")
    ordering = ("status", "-created_at")
    actions = ("approve_selected", "reject_selected")
    readonly_fields = (
        "user", "created_at", "reviewed_at", "reviewed_by",
        "stripe_account_id", "stripe_onboarding_complete",
    )
    fieldsets = (
        ("Review", {"fields": ("status", "review_note", "reviewed_at", "reviewed_by")}),
        ("Application", {
            "fields": ("user", "display_name", "bio", "city", "instagram", "website",
                       "event_types", "created_at"),
        }),
        ("Organizer stats", {"fields": ("rating", "events_hosted")}),
        ("Payouts", {"fields": ("stripe_account_id", "stripe_onboarding_complete")}),
    )

    def _mark_reviewed(self, request, profile):
        profile.reviewed_at = timezone.now()
        profile.reviewed_by = request.user

    def save_model(self, request, obj, form, change):
        status_changed = "status" in form.changed_data
        if status_changed:
            self._mark_reviewed(request, obj)
        super().save_model(request, obj, form, change)
        if status_changed:
            send_organizer_review_email(obj)

    def _set_status(self, request, queryset, new_status):
        updated = 0
        for profile in queryset.exclude(status=new_status).select_related("user"):
            profile.status = new_status
            self._mark_reviewed(request, profile)
            profile.save(update_fields=["status", "reviewed_at", "reviewed_by"])
            send_organizer_review_email(profile)
            updated += 1
        return updated

    @admin.action(description="Approve selected organizer applications")
    def approve_selected(self, request, queryset):
        n = self._set_status(request, queryset, OrganizerProfile.Status.APPROVED)
        self.message_user(request, f"Approved {n} organizer(s); they've been emailed.", messages.SUCCESS)

    @admin.action(description="Reject selected organizer applications (no note)")
    def reject_selected(self, request, queryset):
        n = self._set_status(request, queryset, OrganizerProfile.Status.REJECTED)
        self.message_user(
            request,
            f"Rejected {n} application(s). To include a reason, open the record and set a review note.",
            messages.WARNING,
        )


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    # code_hash is never shown — it's a password hash, not something to
    # display even in admin.
    list_display = ("user", "attempts", "created_at", "expires_at")
    readonly_fields = ("code_hash",)
    search_fields = ("user__email",)
