# EduConnect software documentation

This is the documentation map for EduConnect. Keep the README short enough for local setup; place durable operational, architectural, and product knowledge in the sections below.

## 1. Product and scope

- **Purpose:** a multi-tenant university portal for academic administration, teaching, student services, payments, and communication.
- **Users:** platform super administrators, institution administrators, admissions officers, lecturers, HODs, exam officers, and students.
- **Core capabilities:** tenant onboarding, role-based access, course and programme management, registration, payments, materials, departmental documents, results, accommodation, notifications, and EduConnect AI.
- **Out of scope:** state the workflows the platform deliberately does not support, and link to approved product decisions.

## 2. Getting started

- Prerequisites: supported Python version, virtual environment, database, and Docker (optional).
- Local setup: copy `.env.example`, install `requirements.txt`, run migrations, and start `manage.py runserver`.
- Seed or demo data: describe only non-production accounts and how to remove them.
- Common commands: migrations, tests, static files, reminder dispatch, and production checks.

The top-level [README](../README.md) is the source for the shortest working local setup.

## 3. Architecture

- **Application:** Django project configuration in `config/` and domain application in `portal/`.
- **Tenant model:** explain institution, subdomain, domain routing, current-institution context, and query scoping.
- **Data model:** maintain an entity-relationship diagram for Institution, User, role assignments, academics, billing, and notifications. Reference migrations as the authoritative schema history.
- **Integrations:** email/SMTP, object storage, payment gateways, web push, and any AI service. Document owner, credentials, failure mode, and test/sandbox details for each.

See [SaaS architecture](../SAAS_ARCHITECTURE.md) for the existing multi-tenant design.

## 4. Roles, access, and account lifecycle

Document every role, its permissions, approval steps, and tenant boundary. Include these account states:

1. Institution is created and its administrator is provisioned.
2. A temporary password is generated as `<lowercase-subdomain>educonnect` (for example, `maueduconnect`).
3. The administrator can use the dashboard immediately; an email OTP is sent only when they choose to replace the temporary password.
4. Lecturer accounts created by institution workflows use the same tenant-specific temporary-password policy.
5. Self-service applicants and activation-link users set their own password; they do not share a tenant password.
6. Password reset, account activation, approval, suspension, and deactivation are recorded in the audit trail.

The former shared `educonnect` credential has a limited migration path for eligible legacy administrator and lecturer accounts only. On successful use of the tenant-specific replacement it is immediately invalidated. Do not expose temporary credentials in logs, tickets, or public documentation.

## 5. Feature guides

Use one page per feature area. Each page should state the user goal, authorized roles, prerequisites, workflow, validation rules, notifications, audit events, error handling, and acceptance tests.

- Institution onboarding and subscriptions
- Academic structure: faculties, departments, programmes, sessions, curriculum, and courses
- Student admission, registration, payments, materials, and results
- Lecturer onboarding, allocations, materials, attendance, and messaging
- HOD and examination workflows, result approval, policies, and exam cards
- Accommodation
- Notifications, email, push, and scheduled lecture reminders
- EduConnect AI: permitted questions, data boundary, and unavailable actions

## 6. Configuration and deployment

- Environment-variable reference: name, required status, safe development value, production source, and rotation owner. Never store secret values in documentation.
- Local Docker and production container instructions.
- Database migration, static/media, object storage, backup, restore, and rollback procedures.
- Domain, TLS, Caddy/reverse-proxy, allowed-host, and CSRF configuration.
- Health checks, logging, alerting, capacity, and scheduled jobs.

Existing runbooks: [deployment](../DEPLOYMENT.md), [Oracle deployment](../ORACLE_DEPLOYMENT.md), and [lecture reminders](../LECTURE_REMINDERS.md).

## 7. Security and privacy

- Authentication, password, OTP, session, and rate-limit policy.
- Tenant isolation and authorization testing requirements.
- Data classification, retention, backups, encryption, and incident response.
- Dependency updates, vulnerability handling, audit logs, and release approval.

Use the [security audit](../EDUCONNECT_FULL_PLATFORM_SECURITY_AUDIT.md) and [remediation report](../EDUCONNECT_SECURITY_REMEDIATION_REPORT.md) as the historical record; maintain current controls here.

## 8. Testing and quality

- Test layers: unit, model/form, view/authorization, tenant isolation, integration, regression, and smoke tests.
- Required commands and expected outcomes for a pull request and release.
- Test data rules: no production data or real credentials; use isolated tenants for boundary tests.
- Release checklist: migrations reviewed, automated tests pass, deployment verified, monitoring checked, and rollback documented.

## 9. API and operations reference

- External endpoints/webhooks: authentication, request/response contracts, idempotency, rate limits, and error codes.
- Management commands: purpose, arguments, schedule, owner, and safe manual invocation.
- Support playbooks: common access, billing, notification, and data-repair issues; include escalation path and audit requirements.

## 10. Change log and ownership

Each substantial change should record its date, owner, affected modules, migrations, configuration changes, security impact, tests, rollout plan, and rollback plan. Assign an owner and review date to every operational runbook.
