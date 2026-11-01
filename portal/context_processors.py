from types import SimpleNamespace

from django.conf import settings


def institution(request):
    if getattr(request, "institution", None) is not None:
        return {"institution": request.institution, "PLATFORM_BASE_DOMAIN": settings.PLATFORM_BASE_DOMAIN}
    user = getattr(request, "user", None)
    if getattr(user, "is_authenticated", False) and not user.is_superuser and user.institution_id:
        return {"institution": user.institution, "PLATFORM_BASE_DOMAIN": settings.PLATFORM_BASE_DOMAIN}
    # The central platform has no tenant branding. A neutral object keeps the
    # base templates stable without falling back to another institution's
    # legacy profile/logo.
    return {
        "institution": SimpleNamespace(name="EduConnect", logo=None, has_logo=False, subdomain="", updated_at=None),
        "PLATFORM_BASE_DOMAIN": settings.PLATFORM_BASE_DOMAIN,
    }
