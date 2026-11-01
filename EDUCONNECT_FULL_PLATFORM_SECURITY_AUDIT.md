# EduConnect Full Platform Security Audit

**Audit date:** 1 November 2026
**Scope:** Repository source, migrations, templates, deployment configuration, and isolated local checks. No production service, database, object store, Render account, real payment, or live tenant was accessed. No application source, migration, configuration, or data was changed during this audit.

## Executive summary

EduConnect has a meaningful multi-tenant security foundation: request-scoped tenant managers, host-to-tenant resolution, host/user mismatch rejection, tenant-aware writes, backend role decorators, private-media delivery endpoints, Paystack server-side verification, and signed screening APIs. The code also includes useful regression tests for several tenant and payment boundaries.

Two verified weaknesses require prompt remediation. First, a universal, hard-coded temporary password is used for institution administrators, imported lecturers, and administrator-initiated resets. It is disclosed in UI messages and there is no enforced first-login password change. Second, any approved lecturer in a tenant can directly request course files, course materials, timetables, and handbooks belonging to other lecturers/departments in that same tenant; the download views treat every lecturer as an institution-wide reader.

The production Blueprint enables `DEBUG=False`, generated Django secrets, HTTPS cookies, SSL redirect, and private R2 storage. A production-like Django deployment check passed. Production environment values, Render service state, R2 bucket policy, database transport/encryption/backups, network access, and actual monitoring were not available and therefore are **not verified**.

**Provisional overall rating: 5.8 / 10 (needs remediation before handling broadly sensitive institutional data).**

## Assessment method and boundaries

- Read all application Python modules, routes, relevant templates/forms/models/middleware, migrations, requirements, Docker/Render settings, and tests.
- Traced sensitive paths from URL to view, permission logic, queryset/model ownership, upload handling, payment finalization, and file delivery.
- Ran `python manage.py check --deploy` with the local development environment: it correctly reported development-only `DEBUG`/HTTPS warnings.
- Ran the same deployment check with production-like non-secret placeholder storage settings and `DEBUG=False`: **passed with no issues**.
- Ran `python -m pip check`: **passed**. `pip-audit` is not installed, so a CVE database scan was not performed and no dependency is claimed clean of known vulnerabilities.
- Ran the isolated Django test suite. It discovered 265 tests. Two reminder-endpoint tests fail because they expect status values `completed` / `completed_with_failures`, while the view returns `ok`; the failure is an implementation/test-contract mismatch, not an authorization bypass. The test database was in-memory and destroyed after execution.
- No exploitation was attempted against a live system. “Reproduced” below means deterministically demonstrated from the route and authorization code, not against production.

## Architecture and attack surface

| Surface | Observed implementation | Main residual risk |
|---|---|---|
| Tenant routing | Host/subdomain/custom-domain resolution and `ContextVar` tenant scoping | Cross-tenant query safety still relies on using tenant managers outside requests/jobs |
| Web sessions | Django auth, CSRF middleware, secure production cookies, 8-hour session age | Universal temporary password compromises account controls |
| RBAC | `role_required`, `super_admin_required`, role assignments, department checks on many workflows | Some download endpoints use coarse role checks instead of ownership/assignment |
| Payments | Server initialization/verification, webhook HMAC, row locks/idempotent finalizers | Secrets stored as database plaintext; subscription currency is not validated |
| File handling | Request size/filename denial list, forms, private R2 intent, authenticated streams | SVG logo is accepted and rendered inline; type checking is mostly extension-based |
| External screening API | HMAC, timestamp, constant-time compare, replay key | Default local-memory cache is per Gunicorn worker, weakening replay detection |

## Findings

### Critical findings

No Critical finding was verified from the available repository. In particular, I did not verify cross-institution record retrieval through the reviewed web routes, a payment-forgery path, SQL injection, command injection, or an unauthenticated administrative endpoint.

### High findings

#### EC-001 — Universal hard-coded temporary password

- **Severity:** HIGH
- **Affected module:** Authentication/account lifecycle
- **Affected files:** `portal/services.py:56`; `portal/views.py:2378-2402`, `2545-2558`, `6252-6257`; `portal/automation.py:599`, `874`
- **Status:** Verified by code inspection

`TEMPORARY_ACCOUNT_PASSWORD` is a fixed, guessable value. Institution creation uses it for the tenant administrator; reset functions restore it for administrators and ordinary tenant users; import/automation paths use it for created accounts. UI success messages disclose that same password. Its literal value is intentionally omitted from this report. The code sends a reset/verification email in one flow, but does not make password change a precondition for access; other creation/reset paths do not require it at all.

Safe illustrative scenario: someone who knows an institution administrator’s email address can attempt the fixed temporary password after the account is provisioned or reset, before the intended recipient changes it.

**Impact:** account takeover, followed by institution-wide administrative access, records changes, payment-key changes, and data disclosure. The shared value also makes accidental disclosure of one account materially useful against others.

**Recommended fix:** generate a cryptographically random, single-use activation token per account; set an unusable password until activation or force password change before any workspace access; never display a password in a message; invalidate prior sessions on reset; add rate-limit/audit tests for activation and reset flows. Immediately rotate/reset all accounts that may have received this password.

#### EC-002 — Lecturer file-download IDOR within a tenant

- **Severity:** HIGH
- **Affected module:** Course/material/document delivery and RBAC
- **Affected files:** `portal/views.py:6664-6691`, `6723-6769`, `6814-6831`
- **Status:** Verified by code inspection

The three download views scope the object through tenant-aware managers, so this is not currently a cross-tenant bypass. However, once a requester has the `lecturer` role, `download_course_file` and `download_material` return any tenant course/material without checking that the lecturer is assigned to the course. `download_document` similarly grants every lecturer any tenant timetable or handbook without a department/programme assignment check. Object primary keys are sufficient to request the protected content directly.

Safe illustrative scenario: Lecturer A changes the numeric `course_id` or `material_id` in a download URL to Lecturer B’s resource. The view loads it and immediately returns the file because the lecturer role satisfies the coarse branch.

**Impact:** unauthorized access to other departments’ teaching materials, course files, timetables, and handbooks. These may include copyrighted material, assessment guidance, or internal academic documents.

**Recommended fix:** for lecturers, require `lecturer_registered_courses_queryset(request.user)` (or an explicit course/department permission) before returning a course/material. Require departmental assignment for timetable/handbook access. Add direct-URL tests for an unrelated lecturer, an HOD, and an institution administrator, both on a tenant host and the central-host compatibility flow.

### Medium findings

#### EC-003 — Screening API replay prevention is not shared across web workers

- **Severity:** MEDIUM
- **Affected module:** Screening API
- **Affected files:** `portal/views.py:714-742`; `render.yaml:11` (four Gunicorn workers)
- **Status:** Verified design weakness; live replay not attempted

The HMAC and timestamp validation are good controls. Replay prevention uses Django’s default cache (`cache.add`) but settings do not define a shared cache. Django’s default is process-local memory; Render starts four Gunicorn workers. A captured valid request can therefore be accepted once by each worker during its valid time window. Some endpoints are made mostly idempotent by database lookups, but admission status changes and audit events can be repeated.

**Impact:** repeated authenticated screening actions and unreliable replay protection if a signed request is intercepted or the integration is compromised.

**Recommended fix:** configure a shared Redis/managed cache and use an atomic, namespaced nonce/signature key with a TTL at least equal to allowed clock skew. Add multi-process/integration tests and log rejected replays without logging signatures.

#### EC-004 — Payment gateway secret keys are stored as recoverable database text

- **Severity:** MEDIUM
- **Affected module:** Payment configuration and webhook verification
- **Affected files:** `portal/models.py:741-752`, `1561-1616`, `2144`; `portal/views.py:1155-1180`
- **Status:** Verified by code inspection

Subscription, departmental, and course Paystack secret keys are `CharField` values and historical payment rows retain the secret used. Password widgets avoid returning these values to browsers, which is good, but a database dump, ORM admin access, verbose model serialization, or log accident would expose active and historical merchant secrets in plaintext.

**Impact:** an attacker with read access to the database can forge Paystack webhook signatures for affected gateways and inspect/abuse payment integrations.

**Recommended fix:** use envelope encryption backed by a managed KMS/secret manager, limit decryption to the payment service, avoid retaining historical full keys (retain a key ID/version instead), rotate keys after migration, and restrict production database read access.

#### EC-005 — SVG logos are accepted and served inline from the application origin

- **Severity:** MEDIUM
- **Affected module:** Institution branding/upload handling
- **Affected files:** `portal/forms.py:221-225`; `portal/views.py:416-449`; `portal/middleware.py:13-18`
- **Status:** Verified upload/rendering condition; browser execution not attempted

Institution creation accepts `.svg`, while the global upload deny list blocks `.svgz` but not `.svg`. The logo endpoint serves the stored file inline and preserves its SVG MIME type. SVG is an active document format and can contain script/event content. The site CSP permits inline scripts, making same-origin SVG rendering an avoidable stored-XSS exposure if a malicious file is ever accepted by a privileged logo-management flow.

**Impact:** a compromised super-admin/logo-upload workflow could persist browser-active content under the trusted application origin.

**Recommended fix:** accept only raster logo formats (PNG/JPEG/WebP) or sanitize/rasterize SVG server-side before storing; use a separate media origin with a restrictive CSP for any retained SVG; make protected downloads attachments unless inline rendering is necessary.

#### EC-006 — Subscription finalization does not validate currency

- **Severity:** MEDIUM
- **Affected module:** Subscription payments
- **Affected files:** `portal/services.py:228-299`
- **Status:** Identified by code inspection; exploit requires a payment-provider scenario that was not tested

Course and departmental webhook handling requires NGN, but `finalize_subscription_payment` checks status, reference, and amount only. It does not compare the provider-returned currency to `Payment.currency` (default NGN). Current initialization does not submit another currency, so this is conditional rather than a demonstrated payment bypass.

**Impact:** if a provider/account configuration or future integration permits a same-reference success in another currency, subscription entitlement could be granted for an economically different payment.

**Recommended fix:** require an exact currency match in the finalizer and add tests for foreign-currency success responses, as well as webhook/callback reference-conflict tests.

### Low and informational findings

#### EC-007 — Audit IP addresses can be spoofed when direct-origin traffic is possible

- **Severity:** LOW
- **Affected files:** `portal/views.py:702-703`
- **Status:** Verified by code inspection

`_client_ip` trusts the first `X-Forwarded-For` value without confirming that the sender is Render’s trusted proxy. This affects audit attribution, not request authorization.

**Fix:** have the proxy remove incoming forwarding headers and set a trusted one; only honor it from known proxy ranges, otherwise use `REMOTE_ADDR`.

#### EC-008 — HSTS protection is shorter than a robust production baseline

- **Severity:** LOW
- **Affected files:** `config/settings.py:372-376`
- **Status:** Verified by code inspection

Production defaults to an HSTS lifetime of one hour while also enabling include-subdomains and preload. This is not consistent with the usual one-year minimum expected before preload enrollment and provides weak persistence against HTTPS downgrade after the cache expires.

**Fix:** after confirming every current and future subdomain is HTTPS-capable, raise to at least 31,536,000 seconds and only retain preload when the public preload requirements are intentionally met.

#### EC-009 — Security test contract is failing

- **Severity:** INFORMATIONAL
- **Affected files:** `portal/views.py:275-291`; `portal/test_lecture_reminders.py:182`, `205`
- **Status:** Reproduced in isolated tests

Two tests expect legacy status strings, while the endpoint returns `ok`. It does not create an immediate security bypass, but failing tests reduce confidence in CI signals for the protected cron trigger.

**Fix:** reconcile the intended response contract and test it; include authentication, secret absence, secret mismatch, method, processing-error, and redaction assertions.

#### EC-010 — Dependency vulnerability status is not verified

- **Severity:** INFORMATIONAL
- **Affected files:** `requirements.txt`
- **Status:** Tooling gap

Dependencies are pinned and `pip check` passes, but no installed dependency vulnerability scanner was available (`pip-audit` is absent). No conclusion about known CVEs can be drawn from `pip check`.

**Fix:** add an allowlisted dependency-vulnerability scan to CI and a scheduled review process; do not treat a successful installation consistency check as a CVE scan.

## Multi-tenant security assessment

### Controls verified

- `TenantResolutionMiddleware` resolves a single-label platform subdomain or custom domain, sets a request tenant, returns 404 for an unknown platform subdomain, and clears the context token in `finally` (`portal/middleware.py:71-184`).
- `TenantAccessMiddleware` rejects authenticated host/user institution mismatches and prevents super-admin use on tenant hosts (`portal/middleware.py:198-220`).
- `TenantManager`, `TenantUserManager`, and `TimeStampedModel.save()` automatically filter/read and prevent a tenant request from writing a different tenant (`portal/models.py:268-326`).
- The department-payment-document route deliberately uses its payment’s institution as authoritative and applies role/department/ownership checks (`portal/views.py:6451-6537`).
- Tests cover host mismatch, unknown subdomains, custom-domain resolution, tenant managers, private media, and tenant logos (`portal/test_saas.py`, `portal/test_accommodation.py`).

### Residual concerns

Tenant isolation depends on the request context and developer use of tenant managers. `all_objects` intentionally bypasses the manager for jobs and repair paths. Most reviewed uses apply an explicit institution filter, but this is a high-consequence convention rather than database row-level security. Database foreign keys also do not universally enforce that related models share the same institution; many models rely on `clean()`/form logic.

No cross-tenant IDOR was reproduced in the reviewed tenant-host routes. The verified lecturer download issue is cross-resource **within** a tenant.

## Role and permission assessment

Backend role checks are widely applied using `role_required` and `super_admin_required`; controls are not limited to template button hiding. Student payment/download flows validate ownership and registrations. Lecturer result entry selects only assigned courses, and the test suite covers rejected unassigned-course submission. Super-admin routes are consistently decorated in the reviewed URL/view set.

The weak point is object-level authorization after the role gate (EC-002). A role establishes broad eligibility but must not replace a check of the resource’s course/department/recipient relationship.

## Paystack and financial security assessment

### Controls verified

- Browser callbacks re-verify against Paystack server-side; they do not trust callback query parameters alone.
- Webhooks require an HMAC SHA-512 signature and associate the matching signing key with the payment’s recorded gateway key (`portal/views.py:1155-1226`, `2178-2216`).
- Amount checks convert to kobo deterministically; course/department payment finalizers use row locks and idempotent status handling.
- Subscription finalization uses `select_for_update`, unique references, and audit events (`portal/services.py:228-299`).

### Remaining risks

EC-004 and EC-006 apply. Live Paystack configuration, callback URLs, webhook subscriptions, event retry behavior, and production key rotation were not verifiable without the live account.

## Authentication and account security assessment

### Controls verified

- Django password validators are configured; secure production cookies, an eight-hour server session age, browser-close expiry, CSRF, and `never_cache` on role wrappers are present.
- Login responses use generic credential errors and a cache-backed failed-login throttle. Password reset gives the same response to unknown emails and uses a digest plus attempt limit in session state.
- Unapproved lecturers are rejected at login and in the decorator.

### Remaining risks

EC-001 overrides the value of these otherwise useful controls. Also, login failures are throttled but not visibly added to `AuditLog`; account/role/financial audit coverage is present in many flows but is not complete enough to claim full security-event monitoring.

## File and document security assessment

### Controls verified

- Multipart size, path separators, NULs, traversal patterns, dangerous extensions, and per-file sizes are checked before upload (`portal/middleware.py`).
- Production storage refuses to start with absent/partial R2 settings and intends private objects; application views stream protected files rather than exposing `FieldFile.url` (`config/storage.py`).
- Protected downloads are no-store and `nosniff`; student message attachments check recipient ownership.

### Remaining risks

EC-002 and EC-005 apply. Validation is primarily filename-extension and size based; MIME/content inspection is not generally present. R2 bucket ACL/policy, public access block, retention, malware scanning, and production media origin could not be verified.

## Django production configuration assessment

The Render Blueprint sets `DJANGO_DEBUG=False`, creates `DJANGO_SECRET_KEY` at deployment, uses a Render PostgreSQL `DATABASE_URL`, and configures the core domain. With production-like non-secret placeholders, Django’s deployment check passed. `SecurityMiddleware`, SSL redirect, secure session/CSRF cookies, HSTS, CSP, `X-Frame-Options: DENY`, referrer policy, and permissions policy are configured.

The local `.env` intentionally has `DEBUG=True`; its deployment-check warnings must not be read as production evidence. Production Render environment values and reverse-proxy behavior remain unverified. EC-008 applies to the HSTS default.

## Dependency and infrastructure assessment

`requirements.txt` pins Django 6.0.1 and the application dependencies. `pip check` returned no broken requirements. A vulnerability database scan was not available, so dependency CVE status is **NOT VERIFIED**. Docker starts migrations and static collection; Render runs a schema diagnostic before migrations. The repo was already a dirty working tree and contained local SQLite artifacts; none were altered. Ensure generated local databases remain ignored and are never added to source control.

PostgreSQL TLS mode, at-rest encryption, network policy, backups, restore tests, Render access controls, secret rotation, R2 bucket policy, and centralized log retention are **NOT VERIFIED** from repository contents.

## Existing security controls that passed verification

- Tenant host/user mismatch rejection and reset of request tenant context.
- Model-level tenant write protection for `TimeStampedModel` and user tenant assignment.
- Super-admin/tenant-host separation.
- CSRF on ordinary browser state changes; CSRF-exempt endpoints are authenticated by cron secret, Paystack signature, or screening HMAC.
- Paystack amount/reference/signature checks for course and departmental payments.
- Atomic/idempotent payment and allocation operations in reviewed paths.
- Private authenticated file streaming, no-store responses, and message-recipient attachment checks.
- SQL injection indicators were not found: reviewed query construction uses Django ORM; no application `raw`, `extra`, or direct cursor execution was found.
- No application use of shell execution, deserialization primitives, user-supplied outbound URLs, or open redirect based on a user-provided `next` parameter was found in the reviewed code.

## Areas that could not be verified

- Actual Render variables, allowed custom domains, proxy forwarding-header sanitization, TLS certificate coverage, WAF/rate limits, and log sink access.
- PostgreSQL TLS, encryption, users/roles, backups, point-in-time restore, and schema state.
- R2 public access controls, encryption, retention/versioning, object lifecycle, and malware scanning.
- Paystack account setup, webhook endpoint registration, event delivery/retry logs, and live currency/provider behavior.
- SMTP provider controls, email-delivery logs, and whether reset links/OTPs are delivered only to intended accounts.
- Full dependency CVE status and external penetration testing.

## Prioritized remediation roadmap

1. **Immediately:** remove the fixed temporary password; reset potentially affected accounts; replace creation/reset/import with random activation tokens and forced password change (EC-001).
2. **Immediately:** add object-level course/department authorization to all lecturer download endpoints and direct-URL regression tests (EC-002).
3. **Next sprint:** move Paystack secrets to managed/envelope-encrypted storage; stop retaining historical full secrets; rotate existing keys (EC-004).
4. **Next sprint:** configure shared Redis-backed replay storage for screening requests and test multi-worker behavior (EC-003).
5. **Next sprint:** reject or sanitize/rasterize SVG and add content-based upload validation/AV scanning appropriate to deployment (EC-005).
6. **Next sprint:** add exact subscription currency validation and full payment negative-path tests (EC-006).
7. **Hardening:** increase HSTS deliberately, protect audit IP provenance, log authentication failures safely, and repair the failing cron contract tests (EC-007–009).
8. **Assurance:** add `pip-audit`/equivalent in CI, code-scanning, tests for every tenant/role/download matrix, production configuration checks, and scheduled backup/restore plus secret-rotation exercises.

## Ratings

| Category | Rating | Basis |
|---|---:|---|
| Overall | **5.8 / 10** | Good foundations undermined by verified account and object-authorization weaknesses |
| Multi-tenant isolation | **6.5 / 10** | Strong host/context safeguards and tests; isolation remains convention-based outside request scope |
| Authentication | **4.5 / 10** | Django controls present, but fixed temporary password is a major flaw |
| Role-based access control | **5.5 / 10** | Decorators and role models are good; lecturer object authorization is too broad |
| Payment security | **6.5 / 10** | Strong signature/amount/idempotency design; at-rest secrets and currency gap remain |
| Database security | **NOT VERIFIED** | Schema reviewed; operational database controls unavailable |
| File/document security | **5.0 / 10** | Private-stream design is sound; lecturer IDOR and active SVG intake reduce confidence |
| API/web security | **7.0 / 10** | CSRF/HMAC/CSP/ORM controls are strong; replay cache is not multi-worker safe |
| Production configuration | **7.0 / 10** | Blueprint and production-like check are good; live environment/infrastructure not verified |
| Logging and monitoring | **5.0 / 10** | Useful audit records exist; coverage, IP provenance, sink/retention, and alerting unverified |
