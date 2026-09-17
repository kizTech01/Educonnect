# EduConnect SaaS architecture

EduConnect remains a single Django application with tenant-scoped institutional data. `Institution` is the tenant root; existing users, faculties, departments, courses, sessions, payments, materials, notifications, and related records are associated with that tenant through the existing `TimeStampedModel` base.

Tenant resolution is performed from the requested subdomain or custom domain. Authenticated tenant users who reach the central host are still query-scoped to their own institution for backward compatibility. A user authenticated to one institution is rejected from another institution's host.

Platform Super Admins are Django superusers without a tenant. They manage institutions, plans, subscriptions, audit records, feature settings, and screening configuration. Institution administrators remain ordinary tenant `admin` users.

## Feature controls

`Feature` and `InstitutionFeature` provide data-driven feature flags. Access checks require an active institution, a valid subscription, the feature setting, and (when a plan lists feature codes) plan entitlement. The current standard feature codes are:

- `new-educonnect-features`
- `online-screening`
- `educonnect-ai`
- `payments`
- `communication`

Navigation only shows enabled features; server-side decorators enforce the same checks for AI and Screening routes.

## External Online Screening integration

Online Screening remains independently deployable. Enabling it creates a tenant `ScreeningIntegration` configuration and a per-institution HMAC secret. The external application signs each request as `HMAC-SHA256(secret, "<unix timestamp>.<raw request body>")` and sends:

- `X-Educonnect-Institution`: institution code
- `X-Educonnect-Timestamp`: Unix timestamp
- `X-Educonnect-Signature`: signature

Signed API endpoints are versioned under `/api/v1/`. Requests expire after five minutes by default and signatures cannot be replayed during that window. The integration ledger is tenant-scoped and admission transfer creates or links exactly one student using the application/admission reference, without crossing institution boundaries.

Configure `SCREENING_APPLICATION_URL_TEMPLATE` for the independently hosted applicant portal and `SCREENING_API_MAX_CLOCK_SKEW_SECONDS` only if the deployment needs a different signing window.
