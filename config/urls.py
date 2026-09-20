from django.contrib import admin
from django.urls import include, path
from django.contrib.sitemaps.views import sitemap
from portal.sitemaps import StaticViewSitemap

sitemaps = {
    "static": StaticViewSitemap,
}
urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("", include("portal.urls")),
    path(
    "sitemap.xml",
    sitemap,
    {"sitemaps": sitemaps},
    name="django.contrib.sitemaps.views.sitemap",
   ),
]
