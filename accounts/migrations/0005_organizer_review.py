from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def verified_to_status(apps, schema_editor):
    # Profiles an admin had already marked verified stay usable; everyone
    # else (self-activated with one click) now waits for manual review.
    OrganizerProfile = apps.get_model("accounts", "OrganizerProfile")
    OrganizerProfile.objects.filter(is_verified=True).update(status="approved")


def status_to_verified(apps, schema_editor):
    OrganizerProfile = apps.get_model("accounts", "OrganizerProfile")
    OrganizerProfile.objects.filter(status="approved").update(is_verified=True)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_alter_user_managers_user_email_verified_emailotp"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="organizerprofile",
            name="city",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="organizerprofile",
            name="instagram",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="organizerprofile",
            name="website",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="organizerprofile",
            name="event_types",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="organizerprofile",
            name="status",
            field=models.CharField(
                choices=[("pending", "Pending review"), ("approved", "Approved"), ("rejected", "Rejected")],
                db_index=True, default="pending", max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="organizerprofile",
            name="review_note",
            field=models.TextField(blank=True, help_text="Shown to the applicant when their application is rejected."),
        ),
        migrations.AddField(
            model_name="organizerprofile",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="organizerprofile",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(verified_to_status, status_to_verified),
        migrations.RemoveField(model_name="organizerprofile", name="is_verified"),
    ]
