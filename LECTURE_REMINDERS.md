# Scheduled lecture reminders

EduConnect uses the existing `Course` schedule fields as its source of truth:
course code, title, recurring `schedule_day`, `schedule_time`, venue, current
academic session, active lecturer allocation, and active student course
registration. `Timetable` is an uploaded departmental document and does not
contain machine-readable lecture occurrences.

`dispatch_due_course_reminders` combines a course's local recurring weekday
and start time with today's date in that institution's configured timezone
(normally `Africa/Lagos`). A complete, available course in the current session
is eligible only from 25 to 35 minutes before it starts. This five-minute
tolerance on either side of the 30-minute target works with the supported
five-minute scheduler cadence and absorbs small runner delays, never sends
after the start time, and does not use an exact timestamp comparison. Courses
without a code, title, day, time, or venue are skipped.
There are no separate dated, cancellation, or rescheduling models in the
current schema; when such data is added, it must be incorporated before a
reminder is sent.

Lecturers are selected only from the course's primary lecturer and active
`LecturerCourseRegistration` allocations in the same institution. Students
are selected only from active registrations for that same course and its
current academic session in the same institution. Inactive users and
unapproved lecturers are excluded. The shared-table tenant boundary is applied
explicitly to course, user, allocation, registration, session, and dispatch
queries; course codes are never global identifiers.

Email and browser alerts retain each recipient's existing preferences. Email
is delivered individually so the lecturer and student formal templates remain
personalized and recipient addresses are private. Browser alerts use the same
formal content for users who enabled them.

Each recipient, channel, course occurrence, and course has a unique persistent
`CourseReminderDispatch`. Before delivery, a worker claims that row under a
database lock. Successful deliveries are recorded; failed deliveries release
the claim for a later run, so successful recipients are not retried. A stale
claim can be recovered after five minutes if a worker crashes. As with any SMTP
provider, a crash after the provider accepts an email but before the database
write can still result in a retry; the system does not claim exactly-once email
delivery.

## Schedulers and delivery capacity

The application has three supported ways to invoke the same idempotent
processor:

- Render's `educonnect-course-reminders` cron service invokes the management
  command every minute.
- The Docker production `reminders` service invokes the management command
  every minute.
- An external caller can POST to the authenticated endpoint described below.

The endpoint processes due recipients synchronously. It is appropriate only
when the number of recipients due in one run can reliably complete within the
caller's request timeout. In particular, it opens an SMTP connection and sends
one personalised message for each recipient; it is **not** suitable as an HTTP
worker for a large simultaneous lecture population (for example, 90,000 users
across three institutions). cron-job.org documents a 30-second default request
limit. A timeout is not an indication that all emails failed or all emails
succeeded: work completed before the connection ends may be recorded, and the
normal persistent claims make the next run retry only undelivered recipients.

Before using an external HTTP scheduler at that scale, move delivery to a
durable background worker/queue with independent worker capacity and monitor
its backlog. Do not start a second scheduler merely to compensate for slow
runs. The existing Render cron service is already a direct command runner and
does not have the external HTTP connection limit.

## cron-job.org trigger

cron-job.org supports one-minute jobs, POST requests, custom headers, test
runs, and per-run execution history. Create **one** job with the following
values:

- **URL:** `https://educonnect.devs.surf/internal/cron/lecture-reminders/`
- **Request method:** `POST`
- **Schedule:** the service's *every minute* interval (all hours, days, and
  minutes). The scheduler timezone does not affect course timing because
  EduConnect evaluates each course in its institution timezone.
- **Header name:** `X-EduConnect-Cron-Secret`
- **Header value:** the production value of `EDUCONNECT_CRON_SECRET`.
- **Request body:** leave empty. The endpoint does not read or require JSON,
  form data, or any other body.

Enter the secret only in cron-job.org's protected custom-request-header field.
Never put it in the URL, query string, job name, source control, screenshots,
or a public runbook. The same non-empty value must exist as the Render web
service's `EDUCONNECT_CRON_SECRET`; a missing server value deliberately returns
`503`.

Use cron-job.org's **Test run** once after deployment. It is a real production
invocation, so run it when no unintended courses are within the 25–35 minute
window, or use a controlled test institution/course. Check its history rather
than copying its request configuration into tickets. Expected results are:

- `200` and `{ "status": "ok", ... }`: authentication and processing worked.
- `401`: the header is absent or does not exactly match the Render value.
- `405`: the job was configured with a method other than POST.
- `503`: the Render web service does not have `EDUCONNECT_CRON_SECRET` set.
- `500`, timeout, or connection failure: inspect Render logs before retrying;
  do not lower the reminder window or create a duplicate job.

The execution-history duration and status are useful for detecting a slow
endpoint, but they are not proof that email was accepted. In Render, inspect
the web-service logs for `Lecture reminder cron execution failed` and the
cron/worker logs for `Unable to send lecture reminder email`. Do not log the
request-header value while diagnosing either system.

### Current diagnostic position

Code inspection confirms that the route, POST-only guard, constant-time secret
comparison, multi-tenant recipient queries, timezone conversion, unique
delivery records, stale-claim recovery, and retry-on-failure path already
exist. The repository also contains both the five-minute GitHub Action caller
and the every-minute Render command cron definition. There is no production
cron-job.org execution history, Render log, deployment state, or secret-store
access in this repository, so the cause of a past production failure cannot be
proved from source alone.

Check in this order: (1) cron-job.org or GitHub execution history reached the
production URL, (2) the response status was `200` rather than `401`/`503`, (3)
the deployed Render web service has the same configured cron secret, (4) a
course is available, current-session, and completely scheduled 25–35 minutes
ahead in its institution timezone, (5) the recipient has an active current
course registration (and an email address plus email preference for email),
and (6) SMTP/worker logs show provider acceptance. The local `.env` is not
evidence of the Render production environment.

### Migration and recovery

Keep the GitHub Actions schedule enabled until all of these are true: the
cron-job.org Test run returned `200`, the execution history shows successful
minute runs, Render logs are clean, and a controlled due lecture confirms the
lecturer and a registered student each receive exactly one reminder. Only then
disable the `schedule:` section in
`.github/workflows/lecture-reminders.yml`; retain `workflow_dispatch` for a
manual diagnostic run. This repository intentionally leaves that workflow
unchanged until that operator-verified cutover happens.

If cron-job.org stops running, pause or disable its job first, then re-enable
the GitHub Actions `schedule:` section (or rely on the already configured
Render/Docker command scheduler). Never enable two external HTTP schedulers as
a routine fallback. Overlap is protected by delivery claims, but it adds load
and a provider-acceptance-before-database-write crash can still produce a rare
email retry.

## GitHub Actions trigger

`.github/workflows/lecture-reminders.yml` calls the authenticated production
endpoint every five minutes (`*/5 * * * *`, interpreted in UTC) and supports
**Run workflow** for manual execution. Although a one-minute expression
(`* * * * *`) was requested, GitHub Actions currently supports scheduled
workflows no more frequently than every five minutes, so it cannot provide a
valid one-minute trigger. GitHub also does not guarantee exact start times; the
delivery window above handles small delays. Use a different external scheduler
if a true one-minute trigger is mandatory. The workflow has no retry loop and
fails on a non-2xx response.

Set the following names only, with the same strong random value, in GitHub
repository secrets and Render environment variables:

- `EDUCONNECT_CRON_SECRET`

Generate a value locally without printing it into source control, for example:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The endpoint accepts only `POST /internal/cron/lecture-reminders/` with the
secret in `X-EduConnect-Cron-Secret`. It fails closed with 503 if the server
secret is missing, and returns no student or institutional data. To manually
test, configure both secrets, open the workflow in GitHub, select **Run
workflow**, then inspect the successful job and the non-sensitive summary.
For local service testing, run `python manage.py send_due_course_reminders`.
