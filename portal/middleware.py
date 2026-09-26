from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect

from .models import Institution, reset_current_institution, set_current_institution


class TenantResolutionMiddleware:
    """Resolve an institution from its subdomain/custom domain for each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Resolve the session user before applying a tenant queryset filter.
        # Otherwise the authentication backend may look up a valid user only
        # within the host's tenant and turn a cross-tenant session anonymous
        # before TenantAccessMiddleware can reject it.
        request.user.is_authenticated
        host = request.get_host().split(":", 1)[0].lower().rstrip(".")
        domain = settings.PLATFORM_BASE_DOMAIN.lower().strip(".")

        institution = None
        tenant_subdomain = False
        # With no configured platform domain (the local-development default),
        # retain the legacy single-tenant behaviour.  Once a platform domain
        # is configured, only its bare host is allowed to infer a tenant from
        # the authenticated session.
        platform_host = not domain or host == domain

        # Example:
        # mau.educonnect.devs.surf
        # -> subdomain = mau
        if host and domain and host != domain and host.endswith(f".{domain}"):
            tenant_subdomain = True
            subdomain = host[: -(len(domain) + 1)].strip(".")

            if subdomain and "." not in subdomain:
                institution = (
                    Institution.objects
                    .filter(subdomain__iexact=subdomain)
                    .first()
                )

        # Support institution custom domains.
        if institution is None and host:
            institution = (
                Institution.objects
                .filter(custom_domain__iexact=host)
                .first()
            )

        request.tenant_resolved_from_host = institution is not None

        # A tenant user visiting the central platform domain should still
        # have their own institution available.  Do not make that inference
        # for an unknown subdomain or custom domain: doing so would make a
        # mistyped address appear to be a valid portal for the signed-in user.
        if (
            institution is None
            and platform_host
            and request.user.is_authenticated
            and not request.user.is_superuser
        ):
            institution = request.user.institution

        # Do not let an unrecognised institution subdomain reach
        # tenant-dependent views.  Custom domains must additionally be listed
        # in DJANGO_ALLOWED_HOSTS, so Django rejects an unknown custom host
        # before it gets this far.
        if tenant_subdomain and institution is None:
            return HttpResponse(
                """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Institution Not Found</title>
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    <style>
                        body {
                            font-family: Arial, sans-serif;
                            background: #f5f7fa;
                            margin: 0;
                            padding: 60px 20px;
                            text-align: center;
                        }

                        .container {
                            max-width: 600px;
                            margin: auto;
                            background: white;
                            padding: 40px;
                            border-radius: 12px;
                            box-shadow: 0 4px 20px rgba(0,0,0,.08);
                        }

                        h1 {
                            margin-bottom: 10px;
                        }

                        p {
                            color: #666;
                            line-height: 1.6;
                        }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h1>Institution Not Found</h1>
                        <p>
                            This EduConnect portal address is not connected
                            to an institution.
                        </p>
                        <p>
                            Please contact the EduConnect platform administrator.
                        </p>
                    </div>
                </body>
                </html>
                """,
                status=404,
            )

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
