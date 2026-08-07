# EduConnect

EduConnect is a Django-based university portal for managing students, lecturers, materials, departmental documents, and course communication.

## Included modules

- Landing page with role-based login for admin, student, and lecturer
- First-time student and lecturer registration from the landing page
- Student dashboard for course registration, materials, handbook/timetable download, and lecturer messages
- Lecturer dashboard for course management, free or paid materials, access control, student visibility, and targeted messaging
- Lecturer messages are delivered to the student's inbox, browser alerts (when enabled), and email address
- Admin dashboard for departments, users, approvals, courses, timetables, handbooks, and material management
- SQLite by default for quick local testing, with optional MySQL support

## Quick start

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Configure environment variables:

```bash
copy .env.example .env
```

3. Run migrations:

```bash
python manage.py migrate
```

4. Start the server:

```bash
python manage.py runserver
```

To create the first administrator, set `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` to strong, unique values before the first request. The portal never creates an administrator when either value is missing. The bootstrap password is only applied when that account is first created, so later password changes are preserved.

## Database notes

- Leave `DB_ENGINE` unset to use SQLite locally.
- Set `DB_ENGINE=mysql` to use MySQL.
- `PyMySQL` is wired in through `config/__init__.py` so the project can connect to MySQL without extra adapter code.

## Email and scale

Set the SMTP environment variables in production. Lecturer announcements are delivered in private BCC batches of 100 recipients, so student addresses are never disclosed to other recipients. Class reminders continue to respect each user's email reminder preference.

For up to 2,000 concurrent users, use PostgreSQL or MySQL (not SQLite), set `DJANGO_DEBUG=False`, and run multiple application workers behind a reverse proxy. If you later enable automatic class reminders, schedule this command once per minute:

```bash
python manage.py send_due_course_reminders
```

The reminder job is deliberately not run on dashboard requests; this keeps normal student and lecturer page loads independent of reminder-email work. Browser alert polling remains lightweight (one indexed request every 30 seconds per active browser).

## Demo data used during smoke testing

The local SQLite database now includes:

- `student_demo` / `demo12345`
- `lecturer_demo` / `demo12345`

These can be removed later if you want a clean database.
