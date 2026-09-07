from .models import InstitutionProfile


def institution(request):
    profile = InstitutionProfile.objects.first()
    if profile is None:
        profile = InstitutionProfile.objects.create(name="Educonnect")
    return {"institution": profile}
