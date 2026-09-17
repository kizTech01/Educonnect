from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from .models import Institution, reset_current_institution, set_current_institution


class TenantResolutionMiddleware:
    """Resolve an institution from its subdomain/custom domain for each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Resolve the session user before establishing a tenant filter.  This
        # lets us compare the authenticated account with the requested host
        # instead of silently turning a foreign-tenant session into anonymous.
        request.user.is_authenticated
        host = request.get_host().split(":", 1)[0].lower().rstrip(".")
        domain = settings.PLATFORM_BASE_DOMAIN.lower().strip(".")
        institution = None
        if host and domain and host != domain and host.endswith(f".{domain}"):
            subdomain = host[: -(len(domain) + 1)]
            institution = Institution.objects.filter(subdomain__iexact=subdomain).first()
        if institution is None and host:
            institution = Institution.objects.filter(custom_domain__iexact=host).first()

        request.tenant_resolved_from_host = institution is not None

        # The shared platform host is used for login and Super Admin pages.  A
        # tenant account that remains signed in there must still be scoped to
        # its own institution; otherwise tenant-aware managers would run
        # without a tenant filter and could expose cross-tenant data.
        if institution is None and request.user.is_authenticated and not request.user.is_superuser:
            institution = request.user.institution

        request.institution = institution
        request.tenant_token = set_current_institution(institution)
        try:
            return self.get_response(request)
        finally:
            reset_current_institution(request.tenant_token)


class TenantAccessMiddleware:
    """Enforce host/user ownership and subscription restrictions server-side."""

    billing_prefixes = ("/billing/", "/subscription/", "/paystack/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        institution = getattr(request, "institution", None)
        if user.is_authenticated:
            if user.is_superuser:
                # Super administrators belong to the central platform, never a tenant.
                if institution:
                    logout(request)
                    return HttpResponseForbidden("Super administrators must use the central Educonnect portal.")
            elif institution and user.institution_id != institution.id:
                logout(request)
                return HttpResponseForbidden("This account does not belong to this institution portal.")
            elif institution:
                subscription = institution.current_subscription
                path_is_billing = request.path.startswith(self.billing_prefixes)
                is_restricted = (
                    institution.status != Institution.Status.ACTIVE
                    or not subscription
                    or not subscription.allows_access
                )
                # The pre-SaaS dataset is attached to one compatibility tenant
                # during migration.  It has no subscription record until the
                # platform operator provisions one, so retain its central-host
                # behaviour without creating a bypass for expired or suspended
                # institutions.
                legacy_without_subscription = (
                    not getattr(request, "tenant_resolved_from_host", False)
                    and institution.institution_code == "EDUCONNECT-LEGACY"
                    and subscription is None
                )
                if (
                    is_restricted
                    and not legacy_without_subscription
                    and not path_is_billing
                    and request.path not in {"/logout/", "/"}
                ):
                    messages.warning(
                        request,
                        "Your Educonnect subscription is unavailable. Please renew your subscription.",
                    )
                    return redirect("portal:billing-overview")
        return self.get_response(request)
