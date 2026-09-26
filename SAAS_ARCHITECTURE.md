# EduConnect SaaS architecture

EduConnect remains a single Django application with tenant-scoped institutional data. `Institution` is the tenant root; existing users, faculties, departments, courses, sessions, payments, materials, notifications, and related records are associated with that tenant through the existing `TimeStampedModel` base.

Tenant resolution is performed from the requested subdomain or custom domain. Authenticated tenant users who reach the central host are still query-scoped to their own institution for backward compatibility. A user authenticated to one institution is rejected from another institution's host.

Platform Super Admins are Django superusers without a tenant. They manage institutions, plans, subscriptions, audit records, and screening configuration. Institution administrators remain ordinary tenant `admin` users.

## Institutional RBAC and role-scoped AI

The legacy `User.role` remains the primary, backwards-compatible role for existing logins and integrations. `RoleAssignment` adds auditable multiple duties without changing or duplicating user accounts. The supported institutional roles are Institution Admin, MIS, Bursary, Admission Officer, Academic Planning, HOD, Exam Officer, Lecturer, Senate Member, Student, and Parent/Guardian; Super Admin is represented exclusively by Django superuser accounts with no institution. Migration `0033` mirrors each existing primary role into an active assignment.

All assignment rows are tenant-owned and validate that their user and optional department belong to the same institution. Server-side role checks use `User.has_role()`; this supports Lecturer + HOD and Lecturer + Exam Officer without allowing a client to select an unassigned role.

HODs manage only their assigned department. The **Exam Officer** workflow lists only lecturers in that department, grants an additional `exam_officer` assignment, retains the Lecturer role, and writes an `exam_officer_assigned` audit record. Institution Admin navigation intentionally excludes timetable and handbook operations; existing timetable and handbook records remain intact and operational ownership is retained in the academic/HOD workspace.

Every dashboard role receives EduConnect AI according to its server-side role assignment. The AI uses the active role selected in the secure role switcher, never the union of a user's roles. Each role has an explicit tenant-scoped query allow-list; for example, a Lecturer only receives their own courses and materials, an HOD receives their department data, Bursary receives finance aggregates, and a Parent/Guardian receives only information permitted by an active guardian link. The assistant cannot make administrative changes.

`GuardianRelationship` is an explicit, revocable, same-tenant link between a guardian and a student, with per-link result, finance, and attendance visibility switches. The role-specific AI layer uses the existing tenant-aware managers and only queries the request tenant; guardian answers are limited to explicitly linked students.

## Module access

EduConnect has no per-institution module activation or feature-switch layer. Modules are available through server-side role and object permissions. An institution's subscription and lifecycle status may restrict account access, but never selectively activate a module. Online Screening has a tenant-owned integration configuration and application window; those are operational configuration, not entitlement switches.

## Reviewable automation

Institution Admins and MIS users can submit supported account-import files for analysis. The proposed rows, duplicate findings, and validation errors are persisted as an `AIAutomationJob`; analysis never changes account records. Only an Institution Admin can approve a proposal, and only that approving administrator can execute it. Each analysis, approval, and execution is tenant-scoped and audited.

## External Online Screening integration

Online Screening remains independently deployable. A platform administrator configures its tenant `ScreeningIntegration` and per-institution HMAC secret. The external application signs each request as `HMAC-SHA256(secret, "<unix timestamp>.<raw request body>")` and sends:

- `X-Educonnect-Institution`: institution code
- `X-Educonnect-Timestamp`: Unix timestamp
- `X-Educonnect-Signature`: signature

Signed API endpoints are versioned under `/api/v1/`. Requests expire after five minutes by default and signatures cannot be replayed during that window. The integration ledger is tenant-scoped and admission transfer creates or links exactly one student using the application/admission reference, without crossing institution boundaries.

Configure `SCREENING_APPLICATION_URL_TEMPLATE` for the independently hosted applicant portal and `SCREENING_API_MAX_CLOCK_SKEW_SECONDS` only if the deployment needs a different signing window.
