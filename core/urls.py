"""
outly/urls.py  (your project's root URL config — the file Django actually
reads first, at the path set by ROOT_URLCONF in settings.py)

This is what was missing. Every views.py we built had the right endpoints
and permissions, but nothing told Django these apps' urls.py files existed
at all — hence every request 404ing at the Django level, before it even
reached your views.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),

    path("api/", include("accounts.urls")),
    path("api/", include("events.urls")),
    path("api/", include("bookings.urls")),
    path("api/", include("payments.urls")),
    path("api/", include("chat.urls")),
]

if not settings.USE_R2:
    # Uploaded files on local disk (no R2 configured) — served by Django so
    # cover images at least load. With R2, files are served by Cloudflare.
    urlpatterns += [
        re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
    ]