# EduConnect Security Remediation Report

Audit baseline: `EDUCONNECT_FULL_PLATFORM_SECURITY_AUDIT.md` (1 November 2026).
This remediation did not access production, run migrations against production,
perform payments, commit, push, or alter the pre-existing local databases.

## Fixed findings

| Finding | Root cause | Remediation |
|---|---|---|
| EC-001 shared temporary password | Provisioning and staff resets used a reusable credential. | Provisioned accounts receive a random, 256-bit one-time activation token, stored only as an HMAC hash. Passwords are unusable until activation; links expire after 24 hours and are consumed atomically. Administrator reset, institution provisioning, automation import, and screening-created students use this flow. |
| EC-002 lecturer file IDOR | Download views checked the lecturer role but not their allocation/department relationship. | Course and material downloads require the lecturer's registered-course queryset. Timetable/handbook downloads require staff department assignment. Existing tenant-scoped querysets and student registration/payment checks remain in force. |
| EC-003 screening replay | The original cache replay key was per-process. Signatures were not bound to endpoint/method. | A database replay ledger has an institution/signature unique constraint, expiry index, atomic reservation, secure 503 failure behaviour, and expiry cleanup. The HMAC payload is now `METHOD.PATH.TIMESTAMP.BODY`, preventing cross-endpoint replay. |
| EC-004 Paystack secret exposure | Gateway and historical transaction secrets were database plaintext. | Secret fields use Fernet encryption with plaintext-read migration compatibility, password widgets, and no frontend exposure. The primary key is environment-managed; prior keys allow staged rotation and re-encryption on save. |
| EC-005 SVG logo upload | SVG was accepted and served under the app origin. | New logos only accept validated PNG/JPEG/WebP bytes. SVG/SVGZ are blocked by middleware and form validation; legacy SVGs are refused by the logo delivery endpoint until replaced with a safe raster logo. |
| EC-006 currency validation | Subscription finalization did not compare provider currency. | Subscription finalization requires the verified currency to exactly match the payment currency. |
| EC-007 forwarded IP spoofing | Audit attribution trusted arbitrary `X-Forwarded-For`. | The forwarded value is trusted only when `REMOTE_ADDR` is in `TRUSTED_PROXY_IPS`; otherwise `REMOTE_ADDR` is used. |
| EC-008 weak HSTS default | The default was one hour. | The production default is now one year. |

## Changed files

Security-related changes are in `.env.example`, `config/settings.py`, `render.yaml`,
`portal/fields.py`, `portal/models.py`, `portal/forms.py`, `portal/middleware.py`,
`portal/services.py`, `portal/automation.py`, `portal/views.py`,
`portal/migrations/0060_security_remediation.py`, `portal/test_saas.py`, and
`portal/test_security_remediation.py`.

The repository was already dirty before this work. Unrelated existing changes and
local SQLite files were left intact.

## Migration and deployment

Migration `0060_security_remediation` adds account activation and shared replay
tables, indexes/constraints, expands encrypted secret columns, and encrypts
existing nonempty Paystack values in place. It is additive and does not delete
payment configuration or payment history.

Before production deployment:

1. Back up the database and verify a restore in a non-production environment.
2. Generate a Fernet key and store it only in the deployment secret manager as
   `PAYSTACK_CREDENTIALS_ENCRYPTION_KEY`; never put it in source or the database.
3. Deploy the code and run the migration once. The migration converts legacy
   plaintext credential rows while retaining their usable logical values.
4. Configure all screening clients to sign `METHOD.PATH.TIMESTAMP.BODY` exactly.
   Requests using the prior timestamp/body-only signature are deliberately rejected.
5. Set `TRUSTED_PROXY_IPS` only to proxy addresses that remove incoming
   forwarding headers. Confirm HTTPS coverage for all subdomains before relying
   on the one-year HSTS/preload configuration.
6. Rotate/reset every account ever provisioned with the former shared password,
   notify affected users through normal institutional channels, and replace any
   legacy SVG logo with PNG, JPEG, or WebP.
7. Rotate Paystack keys after migration where operationally appropriate.

For a staged Paystack key rotation, deploy the new Fernet key as
`PAYSTACK_CREDENTIALS_ENCRYPTION_KEY` and put the former key in
`PAYSTACK_CREDENTIALS_PREVIOUS_ENCRYPTION_KEYS`. Existing values remain readable;
resaving a credential re-encrypts it with the primary key. Remove previous keys
only after all secret rows have been re-saved or explicitly migrated and verified.

Rollback: restore the verified database backup and the preceding application
release. Do not remove the new primary key before rollback, because migrated
credentials are encrypted with it. If rolling back only application code while
retaining the database, the preceding code will not understand encrypted values;
use database restore plus release rollback as the safe pair.

## Security regression coverage

Added coverage verifies unique unusable-password account activation, unsafe SVG
rejection and valid raster uploads, ciphertext-at-rest plus staged key rotation
and form masking, and duplicate signed screening requests from independent
clients. Existing suite coverage exercises lecturer/student/document access,
cross-tenant boundaries, payment callbacks/webhooks, and currency checks.

## Verification results

- `python manage.py check`: passed.
- `python manage.py check --deploy` with production-like HTTPS/R2/Fernet values:
  passed with no issues.
- `python manage.py makemigrations --check --dry-run`: passed (`No changes detected`).
- `git diff --check`: passed.
- `python manage.py test portal.test_security_remediation`: passed (4 tests).
- `python manage.py test portal.test_saas`: passed (67 tests).
- Full suite: 269 tests discovered; 267 passed and 2 failed before/independent of
  these fixes. Both failures are the previously audited cron response-contract
  mismatch: `internal_lecture_reminders` returns `status: "ok"`, while two
  pre-existing modified tests expect `completed` and `completed_with_failures`.
  The handler remains correctly authenticated, fails closed, and redacts
  processing exceptions. This unrelated contract was not silently changed.

## Remaining risks and provisional rating

The code-level findings above are remediated. Production controls that still
require operational verification include Render proxy/header behaviour,
database TLS/at-rest encryption and backups, R2 private-bucket policy,
malware scanning for general document uploads, SMTP controls, Paystack webhook
configuration, dependency CVE scanning, monitoring/alerting, and external
penetration testing. Tenant isolation still depends partly on disciplined use of
tenant-aware ORM managers rather than database row-level security.

Provisional code-level rating: **8.2 / 10**, up from 5.8/10. The remaining
operational and assurance gaps prevent a higher rating.
