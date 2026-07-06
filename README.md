# EduConnect

EduConnect is a Django-based university portal for managing students, lecturers, materials, departmental documents, and course communication.

## Included modules

- Landing page with role-based login for admin, student, and lecturer
- First-time student and lecturer registration from the landing page
- Student dashboard for course registration, materials, handbook/timetable download, and lecturer messages
- Lecturer dashboard for course management, free or paid materials, access control, student visibility, and targeted messaging
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

The portal will bootstrap the default admin login automatically:

- Username: `admin`
- Password: `mauyola`

You can override those bootstrap credentials with `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` in `.env`. The bootstrap password is only applied when the admin account is first created, so later password changes are preserved.

## Database notes

- Leave `DB_ENGINE` unset to use SQLite locally.
- Set `DB_ENGINE=mysql` to use MySQL.
- `PyMySQL` is wired in through `config/__init__.py` so the project can connect to MySQL without extra adapter code.

## Demo data used during smoke testing

The local SQLite database now includes:

- `student_demo` / `demo12345`
- `lecturer_demo` / `demo12345`

These can be removed later if you want a clean database.
