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
