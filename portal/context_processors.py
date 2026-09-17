from django.conf import settings

from .models import InstitutionProfile


def institution(request):
    if getattr(request, "institution", None) is not None:
        return {"institution": request.institution, "PLATFORM_BASE_DOMAIN": settings.PLATFORM_BASE_DOMAIN}
    profile = InstitutionProfile.objects.first()
    if profile is None:
        profile = InstitutionProfile.objects.create(name="Educonnect")
    return {"institution": profile, "PLATFORM_BASE_DOMAIN": settings.PLATFORM_BASE_DOMAIN}
