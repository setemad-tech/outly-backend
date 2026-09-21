"""
Django settings for core project.
"""
from dotenv import load_dotenv
import os
from pathlib import Path
import dj_database_url

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------
# Core / security
# ---------------------------------------------------------------------
# SECRET_KEY: was hardcoded as 'key' — that's fine for a throwaway local
# test but must NEVER be used in production (it's how Django signs
# sessions, password reset tokens, and the QR ticket signatures — a
# guessable key means forgeable tickets). Falls back to a dev-only value
# so nothing breaks locally if you haven't set one yet, but Render MUST
# have a real SECRET_KEY set as an env var before going live.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key-do-not-deploy-with-this")

# DEBUG: was hardcoded True. Defaults to True so local dev needs zero
# changes, but Render's env vars MUST set DEBUG=False explicitly.
DEBUG = os.environ.get("DEBUG", "True") == "True"

# ALLOWED_HOSTS: was hardcoded []. Empty is harmless locally (Django
# allows localhost automatically when DEBUG=True regardless of this
# list) but required once DEBUG=False in production.
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()
]
ALLOWED_HOSTS.append(".onrender.com")  # leading dot = matches any subdomain


# Application definition

INSTALLED_APPS = [
    "daphne",
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    "accounts",
    "events",
    "bookings",
    "payments",
    "chat",
    "corsheaders",

    # allauth
    # "allauth",
    # "allauth.account",
    # "allauth.socialaccount",
    # "allauth.socialaccount.providers.google",

    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",

    "anymail",
    "storages",
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise: MUST sit directly after SecurityMiddleware (its own
    # documented requirement) — serves static files (admin CSS, etc.)
    # in production, since Render's web service has no separate static
    # file server the way some hosts do.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # "allauth.account.middleware.AccountMiddleware",
]

CORS_ALLOW_CREDENTIALS = True

CORS_ALLOWED_ORIGINS = [
    "https://outly.ae",
    "https://www.outly.ae",
    "https://outly-frontend.pages.dev",
    "http://localhost:5173",
    "http://localhost:8080",
    "http://localhost:8081",
]

# Was missing entirely — needed for Django's CSRF protection (admin login,
# etc.) to trust your real domain once behind Cloudflare/a custom host.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "CSRF_TRUSTED_ORIGINS", "https://outly.ae"
    ).split(",") if o.strip()
]

# Cloudflare terminates the "real" HTTPS connection and forwards to
# Render over plain HTTP internally — this tells Django to trust that
# forwarded header rather than seeing every request as insecure.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = "core.asgi.application"


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------
# Render auto-injects a single DATABASE_URL when you link a Postgres
# instance — falls back to your existing separate DB_* vars if
# DATABASE_URL isn't set, so local dev needs ZERO changes.
_database_url = os.environ.get("DATABASE_URL")

if _database_url:
    DATABASES = {
        "default": dj_database_url.parse(
            _database_url, conn_max_age=600, ssl_require=not DEBUG
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME"),
            "USER": os.getenv("DB_USER"),
            "PASSWORD": os.getenv("DB_PASSWORD"),
            "HOST": os.getenv("DB_HOST"),
            "PORT": os.getenv("DB_PORT"),
        }
    }


AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------
# Static & media files
# ---------------------------------------------------------------------
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / "staticfiles"  # was missing — required for collectstatic to have somewhere to put files

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

USE_R2 = bool(os.environ.get("R2_ACCESS_KEY_ID"))

if USE_R2:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "access_key": os.environ.get("R2_ACCESS_KEY_ID"),
            "secret_key": os.environ.get("R2_SECRET_ACCESS_KEY"),
            "bucket_name": os.environ.get("R2_BUCKET_NAME"),
            "endpoint_url": os.environ.get("R2_ENDPOINT_URL"),
            "region_name": "auto",
            "signature_version": "s3v4",
            "custom_domain": os.environ.get("R2_PUBLIC_DOMAIN") or None,
            "querystring_auth": not bool(os.environ.get("R2_PUBLIC_DOMAIN")),
            "default_acl": None,
        },
    }
else:
    # Fixed: was "/events/" — that's what caused the duplicated
    # /events/events/covers/... 404s from earlier. cover_image's own
    # upload_to="events/covers/" already includes "events/", so MEDIA_URL
    # must NOT also include it, or the path doubles up.
    MEDIA_URL = "/media/"
    MEDIA_ROOT = BASE_DIR / "media"


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}


EMAIL_BACKEND = "anymail.backends.resend.EmailBackend"

ANYMAIL = {
    "RESEND_API_KEY": os.environ.get("RESEND_API_KEY"),
}

# onboarding@resend.dev is Resend's shared test sender — works instantly with
# no domain verification but can only email your own Resend account address.
# Once a domain is verified in Resend, both of these can point at real
# addresses on it (they don't need to be the same address, or receivable
# mailboxes — domain verification authorizes the whole domain to send).
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "onboarding@resend.dev")

# Ticket confirmation emails (bookings/emails.py) use this instead of
# DEFAULT_FROM_EMAIL, so account/OTP mail and ticket mail can come from
# different addresses. Falls back to DEFAULT_FROM_EMAIL if unset.
TICKETS_FROM_EMAIL = os.environ.get("TICKETS_FROM_EMAIL", DEFAULT_FROM_EMAIL)

# Used to build the QR ticket image URL embedded in confirmation emails
# (bookings/emails.py) — must be the real, publicly-reachable API domain,
# not localhost, or the QR image in the email is just unreachable/broken
# for every recipient. Set SITE_URL on Render explicitly rather than relying
# on this fallback.
SITE_URL = os.environ.get("SITE_URL", "https://api.outly.ae")


# ---------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

PLATFORM_COMMISSION_PERCENT = int(os.environ.get("PLATFORM_COMMISSION_PERCENT", 0))


# ---------------------------------------------------------------------
# Chat (Channels)
# ---------------------------------------------------------------------
# Render's managed Redis gives a single REDIS_URL — falls back to local
# 127.0.0.1:6379 (your Docker container) if REDIS_URL isn't set, so
# local dev needs zero changes here either.
_redis_url = os.environ.get("REDIS_URL")

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [_redis_url] if _redis_url else [("127.0.0.1", 6379)]},
    },
}