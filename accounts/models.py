"""
accounts app
------------
Owns identity: who someone is and, optionally, their organizer profile.
Nothing here knows about events, bookings, payments, or chat — those apps
point back to accounts.User, never the other way around.
"""

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models

import uuid


class UserManager(BaseUserManager):
    """
    Django's default UserManager hardcodes `username` as create_user()/
    create_superuser()'s first positional argument, regardless of what
    USERNAME_FIELD is set to — that mismatch is exactly what caused
    `create_superuser() missing 1 required positional argument: 'username'`.
    This manager actually matches the email-based setup: email is the
    real identifier everywhere, username is just an internal filler value
    (see User.save() below).
    """

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    # Login is by email throughout the frontend (auth.login(email, password)),
    # so email needs to be the actual USERNAME_FIELD, not just a regular
    # field checked in application code. This changes what SimpleJWT's
    # TokenObtainPairView expects in the request body too — it reads
    # USERNAME_FIELD automatically, so no custom login view is needed for
    # this to work, just this model change.
    email = models.EmailField(unique=True)
    username = models.CharField(max_length=150, unique=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # username no longer required at signup/createsuperuser

    objects = UserManager()  # replaces the default username-first manager

    phone_number = models.CharField(max_length=20, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    preferred_language = models.CharField(max_length=10, default="en")
    city = models.CharField(max_length=80, blank=True)  # "Dubai" today

    # False for every account, including ones that existed before this
    # field was added — QR tickets go out by email, so an unconfirmed
    # address (typo'd at signup) must never be trusted just because the
    # account is old. Everyone proves their email once via the OTP flow
    # (see accounts/otp.py) before they can book.
    email_verified = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        # AbstractUser still requires a unique username internally even
        # though we don't collect one at signup — derive a stable one from
        # a uuid so it's never a collision source. Simple pragmatic fix;
        # a cleaner long-term option is subclassing AbstractBaseUser
        # directly and dropping username entirely.
        if not self.username:
            self.username = f"user_{uuid.uuid4().hex[:12]}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.get_full_name() or self.email


class OrganizerProfile(models.Model):
    """
    Any user can apply to become an organizer without needing a separate
    account type — this just attaches organizer-only fields to an existing
    user. Applying only creates a PENDING profile: a person reviews it in
    Django admin and approves (or rejects) it there. Nothing organizer-only
    (creating events, check-in, payouts...) works until it's APPROVED — use
    approved_organizer(user) below rather than hasattr(user, "organizer_profile").
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.OneToOneField(
        "accounts.User", on_delete=models.CASCADE, related_name="organizer_profile"
    )
    display_name = models.CharField(max_length=80)
    bio = models.TextField(blank=True)

    # --- Application details (what the reviewer checks) -------------------
    city = models.CharField(max_length=80, blank=True)
    instagram = models.CharField(max_length=100, blank=True)
    website = models.CharField(max_length=200, blank=True)
    event_types = models.JSONField(default=list, blank=True)

    # --- Manual review ----------------------------------------------------
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    review_note = models.TextField(
        blank=True,
        help_text="Shown to the applicant when their application is rejected.",
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, blank=True, null=True,
        related_name="+",
    )

    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    events_hosted = models.PositiveIntegerField(default=0)

    # --- Payouts (Stripe Connect) ---------------------------------------
    # We deliberately never store real bank account numbers/IBANs
    # ourselves — that's a serious compliance and security liability.
    # Instead the organizer completes onboarding through Stripe Connect
    # (Stripe hosts the KYC + bank-linking flow), and we only ever keep a
    # *reference* to their Connect account. Stripe holds and moves the
    # actual money to their real bank account, not us.
    stripe_account_id = models.CharField(
        max_length=64, blank=True,
        help_text="Stripe Connect account id (acct_...). Blank until onboarding starts.",
    )
    stripe_onboarding_complete = models.BooleanField(
        default=False,
        help_text="True once Stripe confirms this account can receive transfers.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.display_name

    @property
    def is_verified(self):
        """Kept for API compatibility — "verified" now means "approved by admin"."""
        return self.status == self.Status.APPROVED


def approved_organizer(user):
    """The user's OrganizerProfile if an admin has approved it, else None."""
    if not getattr(user, "is_authenticated", False):
        return None
    profile = getattr(user, "organizer_profile", None)
    if profile is None or profile.status != OrganizerProfile.Status.APPROVED:
        return None
    return profile


class EmailOTP(models.Model):
    """
    One active verification code per user. A new send overwrites the
    previous row (OneToOne + update_or_create in accounts/otp.py) rather
    than accumulating history — only the latest code should ever work, so
    there's nothing worth keeping once it's replaced or used.
    """
    user = models.OneToOneField(
        "accounts.User", on_delete=models.CASCADE, related_name="email_otp"
    )
    # Hashed with Django's password hasher (same as User.password), never
    # stored in plaintext — a DB read alone shouldn't be enough to verify
    # someone else's email.
    code_hash = models.CharField(max_length=128)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def __str__(self):
        return f"OTP for {self.user.email}"