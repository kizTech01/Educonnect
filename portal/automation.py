"""Document automation for department setup.

When an OPENAI_API_KEY is configured, the uploaded text is sent to the Responses
API for structured extraction.  The local parser remains available so imports
from CSV, TXT and DOCX keep working without a third-party AI account.
"""
import base64
import csv
import json
import os
import re
import urllib.request
import zipfile
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from xml.etree import ElementTree

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from .models import AcademicSession, Course, CourseResult, Curriculum, Department, LecturerCourseRegistration, Programme, RoleAssignment, StudentCourseRegistration, User
from .services import TEMPORARY_ACCOUNT_PASSWORD


COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,6}\s?-?\d{3,4})\b", re.I)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def extract_text(uploaded_file):
    """Read an uploaded file while leaving it usable by Django storage.

    Automation analysis happens before ``AIAutomationJob.file`` is saved.  An
    ``InMemoryUploadedFile`` is the same object Django later passes to the
    storage backend, so closing it here makes ``FileField.save()`` fail while
    iterating over ``chunks()``.  Resetting its cursor is sufficient for both
    in-memory and temporary upload handlers.
    """
    uploaded_file.open("rb")
    data = uploaded_file.read()
    uploaded_file.seek(0)
    suffix = os.path.splitext(uploaded_file.name)[1].lower()
    if suffix in {".txt", ".csv"}:
        return data.decode("utf-8", errors="replace")
    if suffix == ".docx":
        with zipfile.ZipFile(BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        table_rows = []
        for row in root.iter(f"{namespace}tr"):
            cells = []
            for cell in row.findall(f"{namespace}tc"):
                cells.append(" ".join(text.strip() for text in cell.itertext() if text.strip()))
            if any(cells):
                table_rows.append("\t".join(cells))
        if table_rows:
            return "\n".join(table_rows)
        return "\n".join(node.text for node in root.iter() if node.text)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
        except ImportError as exc:
            raise ValueError("PDF extraction needs pypdf. Install the project requirements and try again.") from exc
    if suffix == ".xlsx":
        return _xlsx_text(data)
    raise ValueError("Use a CSV, TXT, DOCX, XLSX, text-based PDF, or image file.")


def _xlsx_text(data):
    with zipfile.ZipFile(BytesIO(data)) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in root]
        sheets = [name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")]
        lines = []
        for sheet in sheets:
            root = ElementTree.fromstring(archive.read(sheet))
            for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                cells = []
                for cell in row:
                    value = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                    if value is None or not value.text:
                        continue
                    text = value.text
                    if cell.attrib.get("t") == "s" and text.isdigit() and int(text) < len(shared):
                        text = shared[int(text)]
                    cells.append(text)
                if cells:
                    lines.append(",".join(cells))
        return "\n".join(lines)


def _ai_extract(text, task):
    key = getattr(settings, "OPENAI_API_KEY", "")
    if not key:
        return None
    prompt = (
        f"Extract {task} from this university document. Return JSON only, an array of objects. "
        "Never invent information; use empty strings for unknown values.\n\n" + text[:60000]
    )
    body = json.dumps({
        "model": getattr(settings, "OPENAI_AUTOMATION_MODEL", "gpt-4.1-mini"),
        "input": prompt,
        "text": {"format": {"type": "json_object"}},
    }).encode()
    return _openai_extract(body, key)


def _openai_extract(body, key):
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
        output = payload.get("output_text", "")
        if not output:
            output = "".join(
                content.get("text", "")
                for message in payload.get("output", [])
                for content in message.get("content", [])
                if content.get("type") == "output_text"
            )
        parsed = json.loads(output)
        return parsed.get("items", parsed if isinstance(parsed, list) else [])
    except Exception:
        return None


def _ai_extract_image(uploaded_file, task):
    """Use the configured vision model for scanned departmental documents."""
    key = getattr(settings, "OPENAI_API_KEY", "")
    if not key:
        return None
    uploaded_file.open("rb")
    data = uploaded_file.read()
    # Keep the upload open and rewound: callers may persist it after vision
    # analysis (as the AI automation workflow does).
    uploaded_file.seek(0)
    suffix = os.path.splitext(uploaded_file.name)[1].lower()
    media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[suffix]
    prompt = (
        f"Extract {task} from this scanned university document. Return JSON only, an array of objects. "
        "Never invent information; use empty strings for unknown values."
    )
    body = json.dumps({
        "model": getattr(settings, "OPENAI_AUTOMATION_MODEL", "gpt-4.1-mini"),
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": f"data:{media_type};base64,{base64.b64encode(data).decode()}"},
        ]}],
        "text": {"format": {"type": "json_object"}},
    }).encode()
    return _openai_extract(body, key)


def course_records(text):
    ai = _ai_extract(text, "courses with code, title, level, semester, and credit_units")
    if ai:
        return ai
    records = []
    for line in text.splitlines():
        match = COURSE_CODE_RE.search(line)
        if not match:
            continue
        code = match.group(1).upper().replace(" ", "").replace("-", "")
        title = line[match.end():].strip(" :-\t")
        # Handbooks commonly place level and credit units after the title.
        title = re.sub(r"\s+\b[1-5]00\b.*$", "", title).strip()
        if len(title) < 3:
            continue
        level_match = re.search(r"\b([1-5]00)\b", line)
        units_match = re.search(r"\b(\d+)\s*(?:units?|credits?)\b", line, re.I)
        records.append({
            "code": code, "title": title[:200],
            "level": level_match.group(1) if level_match else f"{code[-3]}00" if code[-3:].isdigit() else "100",
            "semester": "second" if re.search(r"second|harmattan", line, re.I) else "first",
            "credit_units": int(units_match.group(1)) if units_match else 2,
        })
    return records


def import_handbook_courses(handbook):
    records = course_records(extract_text(handbook.file))
    created = updated = skipped = 0
    session = AcademicSession.objects.filter(name=handbook.academic_session).first()
    if session is None:
        raise ValueError("The handbook must belong to an existing academic session.")
    if not session.is_current:
        raise ValueError("Upload a handbook for the current academic session.")
    with transaction.atomic():
        # Uploading a handbook creates a new version only for this department.
        # Students already assigned to an earlier version keep it; students who
        # join from this session onwards receive this one at signup.
        # Each upload is a curriculum snapshot. Existing students retain the
        # snapshot on their account; students created after this upload get
        # the newest version only.
        curriculum_scope = Curriculum.objects.filter(
            department=handbook.department,
            programme=handbook.programme,
            effective_session=session,
        )
        latest_version = curriculum_scope.aggregate(max_version=Max("version"))["max_version"] or 0
        # A programme can receive its first student before its first handbook.
        # That account gets an empty placeholder curriculum; populate it on
        # the first handbook rather than immediately making those students
        # stale. Every later handbook remains a new immutable version.
        curriculum = None
        if handbook.programme_id:
            latest = curriculum_scope.order_by("-version", "-created_at").first()
            if latest and not Course.objects.filter(curriculum=latest).exists():
                curriculum = latest
        if curriculum is None:
            curriculum = Curriculum.objects.create(
                department=handbook.department,
                programme=handbook.programme,
                effective_session=session,
                version=latest_version + 1,
            )
        for record in records:
            code = re.sub(r"[^A-Z0-9]", "", str(record.get("code", "")).upper())[:20]
            title = str(record.get("title", "")).strip()[:200]
            if not code or not title:
                skipped += 1
                continue
            existing = Course.objects.filter(
                department=handbook.department,
                code__iexact=code,
                curriculum=curriculum,
            ).first()
            values = {
                "title": title,
                "level": str(record.get("level", "100")) if str(record.get("level", "100")) in {"100", "200", "300", "400", "500"} else "100",
                "semester": "second" if str(record.get("semester", "")).lower().startswith("second") else "first",
                "credit_units": max(1, int(record.get("credit_units") or 2)),
            }
            if existing:
                Course.objects.filter(pk=existing.pk).update(**values)
                updated += 1
            else:
                course = Course.objects.create(
                    department=handbook.department,
                    academic_session=session,
                    curriculum=curriculum,
                    code=code,
                    **values,
                )
                if handbook.programme_id:
                    course.programmes.add(handbook.programme)
                created += 1
    return f"Course automation finished: {created} created, {updated} updated, {skipped} skipped."


def _rows(text):
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    return list(csv.DictReader(StringIO(text), dialect=dialect))


def _field(row, *names):
    """Read CSV/XLSX/DOCX headers regardless of punctuation or casing."""
    normalised = {
        re.sub(r"[^a-z0-9]", "", str(key).lower()): value
        for key, value in row.items() if key is not None
    }
    for name in names:
        value = normalised.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _records_from_upload(upload, task):
    suffix = os.path.splitext(upload.file.name)[1].lower()
    if suffix in IMAGE_SUFFIXES:
        return _ai_extract_image(upload.file, task) or []
    text = extract_text(upload.file)
    local_rows = _rows(text)
    if "lecturers with" in task:
        usable_rows = [row for row in local_rows if _field(row, "lecturer_id", "staff_id", "id") and _field(row, "full_name", "lecturer_name", "name")]
    else:
        # Word tables often merge the lecturer cell across several course rows.
        # Keep those continuation rows; import_course_allocations carries the
        # preceding lecturer name forward to each of them.
        usable_rows = [row for row in local_rows if _field(row, "course_code", "course", "code")]
    return usable_rows or _ai_extract(text, task) or local_rows


def _programme_records(uploaded_file):
    """Extract programme rows without allowing an AI response to choose a department."""
    suffix = os.path.splitext(uploaded_file.name)[1].lower()
    task = "programmes with programme_code, programme_name, award, duration_years, and is_active"
    if suffix in IMAGE_SUFFIXES:
        return _ai_extract_image(uploaded_file, task) or []

    text = extract_text(uploaded_file)
    # Prefer structured AI extraction when configured. Creation below still
    # validates every row and binds it to the Institution Admin's selection.
    ai_records = _ai_extract(text, task)
    if ai_records:
        return ai_records
    return _rows(text)


def _programme_duration(value):
    match = re.search(r"\d+", str(value or ""))
    duration = int(match.group()) if match else 4
    return duration if 1 <= duration <= 15 else 4


def import_programmes(uploaded_file, department):
    """Create or update programmes only within the selected department."""
    if not department or not department.institution_id:
        raise ValueError("Select a valid department before importing programmes.")
    records = _programme_records(uploaded_file)
    if not records:
        raise ValueError("No programme records were found in the uploaded file.")

    created = updated = skipped = 0
    with transaction.atomic():
        for record in records:
            code = _field(record, "programme_code", "program_code", "code").upper().replace(" ", "")[:30]
            name = _field(record, "programme_name", "program_name", "programme", "program", "name")[:180]
            if not code or not name:
                skipped += 1
                continue
            award = _field(record, "award", "degree", "qualification")[:80]
            duration = _programme_duration(_field(record, "duration_years", "duration", "years", "programme_duration"))
            raw_active = _field(record, "is_active", "active", "status").casefold()
            is_active = raw_active not in {"false", "0", "no", "inactive", "disabled"}
            existing = Programme.all_objects.filter(institution=department.institution, code__iexact=code).first()
            if existing and existing.department_id != department.id:
                skipped += 1
                continue
            if existing:
                existing.name = name
                existing.award = award
                existing.duration_years = duration
                existing.is_active = is_active
                existing.full_clean()
                existing.save(update_fields=["name", "award", "duration_years", "is_active", "updated_at"])
                updated += 1
                continue
            programme = Programme(
                institution=department.institution,
                department=department,
                code=code,
                name=name,
                award=award,
                duration_years=duration,
                is_active=is_active,
            )
            programme.full_clean()
            programme.save()
            created += 1
    if not created and not updated:
        raise ValueError("No valid new programmes could be imported. Check the programme code and name columns.")
    return f"Programme import finished: {created} created, {updated} updated, {skipped} skipped."


def _person_key(value):
    """Match staff names while ignoring title and punctuation differences."""
    tokens = re.findall(r"[a-z0-9]+", str(value).lower())
    return " ".join(token for token in tokens if token not in {"prof", "professor", "dr", "mr", "mrs", "ms"})


def _find_department_lecturer(department, lecturer_key):
    direct_match = User.objects.filter(
        role=User.Role.LECTURER,
        department=department,
    ).filter(
        Q(username__iexact=lecturer_key) | Q(id_number__iexact=lecturer_key)
    ).first()
    if direct_match:
        return direct_match
    requested_name = _person_key(lecturer_key)
    if not requested_name:
        return None
    lecturers = list(User.objects.filter(role=User.Role.LECTURER, department=department))
    exact_matches = [lecturer for lecturer in lecturers if _person_key(lecturer.full_name) == requested_name]
    if exact_matches:
        return exact_matches[0]
    requested_tokens = set(requested_name.split())
    partial_matches = [
        lecturer for lecturer in lecturers
        if requested_tokens and requested_tokens.issubset(set(_person_key(lecturer.full_name).split()))
    ]
    return partial_matches[0] if len(partial_matches) == 1 else None


def _find_department_course(department, course_code, session):
    courses = Course.objects.filter(department=department).filter(
        Q(academic_session=session) | Q(academic_session__isnull=True)
    )
    course = courses.filter(code__iexact=course_code).first()
    if course:
        return course
    # Allocation sheets often use a suffix such as "(E)" or "(A)" for a
    # programme stream, while the course catalogue stores the base code.
    base_code = re.sub(r"(?<=\d)[A-Z]+$", "", course_code)
    if base_code != course_code:
        return courses.filter(code__iexact=base_code).first()
    return None


def _allocation_course_code(course_code):
    return re.sub(r"(?<=\d)[A-Z]+$", "", course_code)


def _create_course_from_allocation(department, row, course_code, session):
    """Create a catalogue entry only when a valid allocation supplies its details."""
    code = _allocation_course_code(course_code)
    title = _field(row, "course_title", "title").strip()[:200]
    if not code or not title or Course.objects.filter(
        department=department,
        code__iexact=code,
        academic_session=session,
    ).exists():
        return None
    number_match = re.search(r"(\d{3,4})", code)
    level = f"{number_match.group(1)[0]}00" if number_match else "100"
    if level not in {"100", "200", "300", "400", "500"}:
        level = "100"
    unit_match = re.search(r"\d+", _field(row, "unit", "credit_units", "credits"))
    return Course.objects.create(
        department=department,
        academic_session=session,
        code=code,
        title=title,
        level=level,
        credit_units=int(unit_match.group()) if unit_match else 2,
    )


def import_department_lecturers(upload):
    rows = _records_from_upload(upload, "lecturers with full_name, lecturer_id, email, and phone_number")
    created = existing = skipped = 0
    with transaction.atomic():
        for row in rows:
            lecturer_id = _field(row, "lecturer_id", "staff_id", "id")
            name = _field(row, "full_name", "lecturer_name", "name")
            if not lecturer_id or not name:
                skipped += 1
                continue
            user = User.objects.filter(username=lecturer_id).first() or User.objects.filter(id_number=lecturer_id).first()
            if user:
                if user.role == User.Role.LECTURER and not user.department_id:
                    user.department = upload.department
                    user.save(update_fields=["department"])
                existing += 1
                continue
            name_parts = name.split(maxsplit=1)
            User.objects.create_user(
                username=lecturer_id, password=TEMPORARY_ACCOUNT_PASSWORD, role=User.Role.LECTURER,
                id_number=lecturer_id, first_name=name_parts[0], last_name=name_parts[1] if len(name_parts) > 1 else "",
                email=_field(row, "email"), phone_number=_field(row, "phone", "phone_number"),
                department=upload.department, is_approved=True,
            )
            created += 1
    return f"Lecturer automation finished: {created} accounts created, {existing} already existed, {skipped} skipped. New accounts use the configured temporary password."


def import_course_allocations(upload):
    rows = _records_from_upload(upload, "course allocations with lecturer_name or lecturer_id and course_code")
    if not rows:
        raise ValueError("No course allocations were found in the uploaded file.")
    session = AcademicSession.objects.filter(is_current=True).first()
    if session is None:
        year = timezone.now().year
        session, _ = AcademicSession.objects.get_or_create(
            name=f"{year}/{year + 1}",
            defaults={"is_current": True},
        )
    assigned = created_courses = skipped = 0
    with transaction.atomic():
        LecturerCourseRegistration.objects.filter(
            course__department=upload.department,
        ).delete()
        Course.objects.filter(department=upload.department).exclude(lecturer__isnull=True).update(lecturer=None)
        lecturer_key = ""
        for row in rows:
            lecturer_key = _field(row, "lecturer_id", "staff_id", "lecturer_name", "lecturer", "name") or lecturer_key
            course_code = re.sub(r"[^A-Z0-9]", "", _field(row, "course_code", "course", "code").upper())
            lecturer = _find_department_lecturer(upload.department, lecturer_key)
            course = _find_department_course(upload.department, course_code, session)
            if not course and lecturer:
                course = _create_course_from_allocation(upload.department, row, course_code, session)
                created_courses += bool(course)
            if not lecturer or not course:
                skipped += 1
                continue
            update_fields = []
            if course.academic_session_id is None:
                course.academic_session = session
                update_fields.append("academic_session")
            if course.lecturer_id is None:
                course.lecturer = lecturer
                update_fields.append("lecturer")
            if update_fields:
                course.save(update_fields=[*update_fields, "updated_at"])
            LecturerCourseRegistration.objects.get_or_create(lecturer=lecturer, course=course)
            assigned += 1
    return f"Course-allocation automation finished: {assigned} allocations assigned, {created_courses} courses created, {skipped} rows could not be matched."


def import_course_results(uploaded_file, *, course, session, semester, lecturer, institution):
    """Read a result sheet into editable drafts for one lecturer-owned course.

    The selected course/session/semester stay authoritative; file data can only
    identify registered students and their marks, never redirect records to a
    different department or course.
    """
    text = extract_text(uploaded_file)
    rows = _ai_extract(text, "student result rows with student_id, CA, test, other assessment, exam and total score") or _rows(text)
    if not rows:
        raise ValueError("No result rows were found in the uploaded file.")
    created = updated = skipped = protected = 0
    with transaction.atomic():
        for row in rows:
            student_id = _field(row, "student_id", "matric_no", "matric_number", "id_number", "student number", "id")
            if not student_id:
                skipped += 1
                continue
            student = User.objects.filter(
                role=User.Role.STUDENT, department=course.department,
            ).filter(Q(id_number__iexact=student_id) | Q(username__iexact=student_id)).first()
            if not student or not StudentCourseRegistration.objects.filter(student=student, course=course, session=session).exists():
                skipped += 1
                continue
            def mark(*names):
                value = _field(row, *names).replace("%", "").strip()
                try:
                    return Decimal(value) if value else Decimal("0.00")
                except InvalidOperation:
                    return Decimal("0.00")
            ca = mark("ca", "continuous assessment")
            test = mark("test", "tests")
            other = mark("other", "assignment", "practical", "other assessment")
            exam = mark("exam", "exam score", "examination")
            total = mark("score", "total", "total score")
            total = total if total else ca + test + other + exam
            if not Decimal("0") <= total <= Decimal("100"):
                skipped += 1
                continue
            existing = CourseResult.objects.filter(student=student, course=course, session=session, semester=semester).first()
            if existing and existing.status not in {CourseResult.Status.DRAFT, CourseResult.Status.RETURNED}:
                protected += 1
                continue
            result = existing or CourseResult(
                institution=institution, student=student, course=course, session=session, semester=semester,
            )
            result.ca_score, result.test_score = ca, test
            result.other_assessment_score, result.exam_score, result.score = other, exam, total
            result.assessment_breakdown = {}
            result.status = CourseResult.Status.DRAFT
            result.submitted_by = lecturer
            result.save()
            if existing:
                updated += 1
            else:
                created += 1
    return f"Result import finished: {created} drafts created, {updated} drafts updated, {protected} locked result(s) left unchanged, {skipped} row(s) skipped."


def analyze_institution_automation(upload, command):
    """Produce a non-mutating account-import proposal from a supplied file.

    This is intentionally deterministic at the execution boundary: AI/document
    extraction can suggest rows, but account creation uses only the reviewed
    proposal stored on the job.  That prevents a later model response from
    changing the approved operation.
    """
    command = (command or "").strip()
    lowered = command.casefold()
    if not any(token in lowered for token in ("account", "staff", "lecturer", "student", "import")):
        raise ValueError("The initial automation release supports reviewed account imports. State whether to create staff, lecturers, or students.")
    if "student" in lowered:
        role = User.Role.STUDENT
    elif "lecturer" in lowered:
        role = User.Role.LECTURER
    else:
        role = User.Role.LECTURER if "lecturer" in lowered else User.Role.MIS
    text = extract_text(upload)
    rows = _rows(text)
    proposed, errors, duplicates = [], [], []
    seen = set()
    for row_number, row in enumerate(rows, start=2):
        email = _field(row, "email")
        staff_id = _field(row, "staff_id", "lecturer_id", "student_id", "id_number", "id")
        name = _field(row, "full_name", "name", "lecturer_name", "student_name")
        department_code = _field(row, "department", "department_code")
        username = _field(row, "username") or staff_id or email
        key = (email.casefold() or username.casefold())
        if not name or not username:
            errors.append({"row": row_number, "error": "Name and a username, ID, or email are required."})
            continue
        if key in seen or User.all_objects.filter(username__iexact=username).exists() or (email and User.all_objects.filter(email__iexact=email).exists()):
            duplicates.append({"row": row_number, "identifier": email or username})
            continue
        seen.add(key)
        department = Department.objects.filter(code__iexact=department_code).first() if department_code else None
        if department_code and not department:
            errors.append({"row": row_number, "error": f"Department '{department_code}' does not exist in this institution."})
            continue
        proposed.append({
            "row": row_number, "username": username[:150], "email": email[:254],
            "id_number": staff_id[:30], "name": name[:300], "department_id": department.id if department else None,
            "role": role,
        })
    return {
        "action": "create_accounts",
        "analysis": {"records_found": len(rows), "valid_records": len(proposed), "duplicates": duplicates, "errors": errors},
        "proposed_changes": proposed,
    }


@transaction.atomic
def execute_institution_automation(job, approver):
    """Execute exactly one reviewed proposal, once, under a transaction."""
    if job.status == job.Status.EXECUTED:
        return job.execution_summary
    if job.status != job.Status.APPROVED or job.approved_by_id != approver.id:
        raise ValueError("Only the approving Institution Admin can execute this approved operation.")
    if job.action != "create_accounts":
        raise ValueError("This approved operation has no registered executor.")
    created = skipped = 0
    for record in job.proposed_changes:
        if User.all_objects.filter(username__iexact=record["username"]).exists():
            skipped += 1
            continue
        name_parts = record["name"].split(maxsplit=1)
        user = User.objects.create_user(
            username=record["username"], email=record.get("email", ""), id_number=record.get("id_number") or None,
            first_name=name_parts[0], last_name=name_parts[1] if len(name_parts) > 1 else "",
            department_id=record.get("department_id"), role=record["role"],
            password=TEMPORARY_ACCOUNT_PASSWORD, is_approved=True,
        )
        RoleAssignment.objects.get_or_create(
            institution=job.institution, user=user, role=record["role"], department_id=record.get("department_id"),
            defaults={"assigned_by": approver},
        )
        created += 1
    job.status = job.Status.EXECUTED
    job.executed_at = timezone.now()
    job.execution_summary = {"created": created, "skipped": skipped, "temporary_password_required": True}
    job.save(update_fields=["status", "executed_at", "execution_summary", "updated_at"])
    return job.execution_summary
