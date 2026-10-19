"""Document automation for department setup.

When an OPENAI_API_KEY is configured, the uploaded text is sent to the Responses
API for structured extraction.  The local parser remains available so imports
from CSV, TXT and DOCX keep working without a third-party AI account.
"""
import base64
import csv
import hashlib
import json
import os
import re
import unicodedata
import urllib.request
import zipfile
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from xml.etree import ElementTree

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from .models import AcademicSession, AuditLog, Course, CourseAllocationUpload, CourseResult, Curriculum, CurriculumCourse, Department, LecturerCourseRegistration, Programme, RoleAssignment, StudentCourseRegistration, User, curriculum_for_department, curriculum_for_programme
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
                # A Word cell commonly contains one paragraph per allocated
                # course.  Preserve a separator between those paragraphs;
                # concatenating their XML text turns "Science" + "GST 108"
                # into "ScienceGST 108", which prevents course-code parsing.
                paragraphs = [
                    "".join(paragraph.itertext()).strip()
                    for paragraph in cell.iter(f"{namespace}p")
                ]
                cells.append(" ".join(paragraph for paragraph in paragraphs if paragraph))
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
    ai = _ai_extract(text, "courses with code, title, level, semester, credit_units, and requirement (compulsory or elective)")
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
            "requirement": "elective" if re.search(r"\belective\b", line, re.I) else "compulsory",
        })
    return records


def import_handbook_courses(handbook):
    records = course_records(extract_text(handbook.file))
    created = linked = updated = skipped = 0
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
        # Every handbook is an immutable curriculum snapshot.  Even an empty
        # placeholder from an earlier enrolment is not reused: students who
        # already existed keep their assigned curriculum, while students who
        # join after this upload receive this new version.
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
            # The catalogue identity is the safely normalised course code in
            # this department/institution, never a handbook version or a
            # programme.  This is what prevents one CSC201 row per programme.
            existing = Course.objects.filter(
                department=handbook.department,
                code__iexact=code,
            ).order_by("pk").first()
            course_values = {
                "title": title,
                "level": str(record.get("level", "100")) if str(record.get("level", "100")) in {"100", "200", "300", "400", "500"} else "100",
                "semester": "second" if str(record.get("semester", "")).lower().startswith("second") else "first",
                "credit_units": max(1, int(record.get("credit_units") or 2)),
            }
            curriculum_values = {
                field: course_values[field] for field in ("level", "semester", "credit_units")
            }
            curriculum_values["requirement"] = (
                CurriculumCourse.Requirement.ELECTIVE
                if str(record.get("requirement", "")).lower() == CurriculumCourse.Requirement.ELECTIVE
                else CurriculumCourse.Requirement.COMPULSORY
            )
            if existing:
                course = existing
                # Do not silently overwrite a shared catalogue title or an
                # active offering from a later handbook.  Curriculum-specific
                # values live on CurriculumCourse below.
                updated += 1
            else:
                course = Course.objects.create(
                    institution=handbook.institution,
                    department=handbook.department,
                    academic_session=session,
                    curriculum=curriculum,
                    code=code,
                    **course_values,
                )
                created += 1
            entry, entry_created = CurriculumCourse.objects.get_or_create(
                institution=handbook.institution,
                curriculum=curriculum,
                course=course,
                defaults=curriculum_values,
            )
            if not entry_created:
                for field, value in curriculum_values.items():
                    setattr(entry, field, value)
                entry.save(update_fields=[*curriculum_values, "updated_at"])
            else:
                linked += 1
            # Keep the previous M2M in sync for integrations still reading it
            # during the staged production upgrade.
            course.curricula.add(curriculum)
            if handbook.programme_id:
                course.programmes.add(handbook.programme)
    return f"Course automation finished: {created} catalogue courses created, {linked} curriculum entries linked, {updated} existing courses reused, {skipped} skipped."


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


def _course_allocation_records(text):
    """Normalise both row-based sheets and Word allocation-table layouts.

    A typical spreadsheet has one ``Lecturer ID`` / ``Course Code`` pair per
    row.  Department allocation documents often instead have one lecturer per
    row and an ``Allocated Courses`` cell containing several course codes and
    titles.  Convert the latter to the same one-course-per-row representation
    before matching it against the department catalogue.
    """
    records = []
    carried_lecturer_id = ""
    carried_lecturer_name = ""
    for row in _rows(text):
        lecturer_id = _field(row, "lecturer_id", "staff_id", "staff_no", "lecturer id")
        lecturer_name = _field(row, "lecturer_name", "full_name", "lecturer name", "name")
        if lecturer_id:
            carried_lecturer_id = lecturer_id
        if lecturer_name:
            carried_lecturer_name = lecturer_name

        # Support vertically merged lecturer cells in Word tables by carrying
        # their identifier down to the course rows that follow.
        normalised_row = dict(row)
        if not lecturer_id and carried_lecturer_id:
            normalised_row["lecturer_id"] = carried_lecturer_id
        if not lecturer_name and carried_lecturer_name:
            normalised_row["lecturer_name"] = carried_lecturer_name

        course_code = _field(normalised_row, "course_code", "course", "code")
        if course_code:
            records.append(normalised_row)
            continue

        allocated_courses = _field(
            normalised_row,
            "allocated_courses", "allocated courses", "course allocations", "allocated course list",
        )
        if not allocated_courses:
            # Word templates often label this column "Allocated Courses
            # (Course Code - Course Title)".  The explanatory suffix varies,
            # so accept any heading that begins with "Allocated Courses".
            allocated_courses = next(
                (
                    str(value).strip()
                    for key, value in normalised_row.items()
                    if re.sub(r"[^a-z0-9]", "", str(key).lower()).startswith("allocatedcourses")
                    and value not in (None, "")
                ),
                "",
            )
        matches = list(COURSE_CODE_RE.finditer(allocated_courses))
        for index, match in enumerate(matches):
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(allocated_courses)
            title = allocated_courses[match.end():next_start].strip(" \t:-–—")
            allocation_row = dict(normalised_row)
            allocation_row["course_code"] = match.group(1)
            if title:
                allocation_row["course_title"] = title
            records.append(allocation_row)
    return records


def _records_from_upload(upload, task):
    suffix = os.path.splitext(upload.file.name)[1].lower()
    if suffix in IMAGE_SUFFIXES:
        return _ai_extract_image(upload.file, task) or []
    text = extract_text(upload.file)
    local_rows = _rows(text)
    if "lecturers with" in task:
        usable_rows = [row for row in local_rows if _field(row, "lecturer_id", "staff_id", "id") and _field(row, "full_name", "lecturer_name", "name")]
    else:
        usable_rows = _course_allocation_records(text)
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


def _allocation_key(value):
    """Normalise IDs/codes across Unicode, dash, whitespace and case variants."""
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _find_department_lecturer(department, lecturer_key):
    requested_id = _allocation_key(lecturer_key)
    if not requested_id:
        return None
    lecturers = list(User.all_objects.filter(
        institution=department.institution,
        department=department,
    ).filter(Q(role=User.Role.LECTURER) | Q(role_assignments__role=User.Role.LECTURER, role_assignments__is_active=True)).distinct())
    direct_matches = [
        lecturer for lecturer in lecturers
        if _allocation_key(lecturer.id_number) == requested_id or _allocation_key(lecturer.username) == requested_id
    ]
    return direct_matches[0] if len(direct_matches) == 1 else None


def _find_department_course(department, course_code, session=None):
    # Department ownership is the durable tenant boundary for catalogue rows.
    # Some legacy courses predate the tenant FK backfill, so filtering by that
    # nullable copied field makes an otherwise valid course invisible.
    courses = Course.all_objects.filter(department=department)
    normalised = _allocation_key(course_code)
    matching_courses = [course for course in courses if _allocation_key(course.code) == normalised]
    if matching_courses:
        if session:
            return next(
                (course for course in matching_courses if course.academic_session_id == session.pk),
                matching_courses[0],
            )
        return matching_courses[0]
    # Allocation sheets often use a suffix such as "(E)" or "(A)" for a
    # programme stream, while the course catalogue stores the base code.
    base_code = re.sub(r"(?<=\d)[A-Z]+$", "", course_code)
    if base_code != course_code:
        return courses.filter(code__iexact=base_code).first()
    return None


def _allocation_course_code(course_code):
    return re.sub(r"(?<=\d)[A-Z]+$", "", _allocation_key(course_code))


def _create_course_from_allocation(department, row, course_code, session):
    """Create a catalogue entry only when a valid allocation supplies its details."""
    code = _allocation_course_code(course_code)
    title = _field(row, "course_title", "title").strip()[:200]
    if not code or not title or _find_department_course(department, code, session):
        return None
    number_match = re.search(r"(\d{3,4})", code)
    inferred_level = f"{number_match.group(1)[0]}00" if number_match else "100"
    if inferred_level not in {"100", "200", "300", "400", "500"}:
        inferred_level = "100"
    unit_match = re.search(r"\d+", _field(row, "unit", "credit_units", "credits"))
    curriculum = curriculum_for_department(department, session)
    # ``Course.save`` assigns a curriculum to legacy catalogue rows.  Therefore
    # a course can already be in the requested curriculum even when an older
    # catalogue/session lookup did not return it. Reuse that exact catalogue
    # row rather than attempting to create another catalogue record.
    existing = Course.all_objects.filter(
        department=department,
        curriculum=curriculum,
    )
    for candidate in existing:
        if _allocation_key(candidate.code) == _allocation_key(code):
            return candidate
    course = Course.objects.create(
        institution=department.institution,
        department=department,
        academic_session=session,
        curriculum=curriculum,
        code=code,
        title=title,
        level=inferred_level,
        semester="second" if _field(row, "semester").lower().startswith("second") else Course.Semester.FIRST,
        credit_units=int(unit_match.group()) if unit_match else 2,
    )
    # Allocation imports can introduce a course before a handbook does. Make
    # its department-wide placement explicit immediately, so the shared-course
    # relationship remains authoritative rather than relying only on the
    # legacy Course.curriculum foreign key.
    if curriculum:
        CurriculumCourse.objects.get_or_create(
            institution=department.institution,
            curriculum=curriculum,
            course=course,
            defaults={
                "level": course.level,
                "semester": course.semester,
                "credit_units": course.credit_units,
            },
        )
        course.curricula.add(curriculum)
    return course


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
    # Allocation is department-wide.  The current session is needed only if a
    # previously unknown course must be added to the catalogue; it never
    # limits which existing departmental course may be assigned.
    session = AcademicSession.all_objects.filter(
        institution=upload.institution, is_current=True
    ).first()
    if session is None:
        year = timezone.now().year
        session, _ = AcademicSession.all_objects.get_or_create(
            institution=upload.institution,
            name=f"{year}/{year + 1}",
            defaults={"is_current": True},
        )
    upload.file.open("rb")
    content_hash = hashlib.sha256(upload.file.read()).hexdigest()
    upload.file.seek(0)
    scope = {
        "institution": upload.institution,
        "department": upload.department,
    }
    duplicate = CourseAllocationUpload.all_objects.filter(**scope, content_hash=content_hash, is_active=True).exclude(pk=upload.pk).first()
    if duplicate:
        upload.content_hash = content_hash
        upload.reconciliation = {"duplicate_of": duplicate.pk, "assigned": 0, "created_courses": 0, "skipped": 0}
        upload.processing_summary = "Course-allocation upload is unchanged; the existing active allocation was retained."
        upload.save(update_fields=["content_hash", "reconciliation", "processing_summary", "updated_at"])
        AuditLog.objects.create(
            institution=upload.institution, user=upload.uploaded_by,
            action="course_allocation_duplicate", object_type="CourseAllocationUpload",
            object_id=str(upload.pk), description=upload.processing_summary,
            new_value={"duplicate_of": duplicate.pk, "department_id": upload.department_id},
        )
        return upload.processing_summary

    assigned = created_courses = skipped = reassigned = removed = preserved = superseded = 0
    with transaction.atomic():
        incoming = {}
        for row in rows:
            # A departmental lecturer ID, not a display name, is the stable
            # allocation key.  It prevents a similarly named lecturer from
            # receiving a course by mistake.
            lecturer_key = _field(row, "lecturer_id", "staff_id", "staff_no", "lecturer id")
            course_code = _allocation_key(_field(row, "course_code", "course", "code"))
            lecturer = _find_department_lecturer(upload.department, lecturer_key)
            course = _find_department_course(upload.department, course_code, session)
            if not course and lecturer:
                course = _create_course_from_allocation(upload.department, row, course_code, session)
                created_courses += bool(course)
            if not lecturer or not course:
                skipped += 1
                continue
            allocation = incoming.setdefault(course.pk, {"course": course, "lecturers": []})
            if lecturer not in allocation["lecturers"]:
                allocation["lecturers"].append(lecturer)

        # A bad or wrong-department file must not silently remove every
        # lecturer allocation in the department.  Leave the previous active
        # allocation untouched until at least one ID/course pair is valid.
        if not incoming:
            raise ValueError(
                "No valid lecturer ID and course-code pairs were found for this department; the existing allocation was kept."
            )

        scoped_courses = Course.all_objects.filter(department=upload.department)
        previous = LecturerCourseRegistration.all_objects.filter(course__in=scoped_courses, is_active=True)
        for record in previous.select_related("course", "lecturer"):
            expected = incoming.get(record.course_id)
            expected_ids = {lecturer.pk for lecturer in expected["lecturers"]} if expected else set()
            if record.lecturer_id not in expected_ids:
                record.is_active = False
                record.deactivated_at = timezone.now()
                record.save(update_fields=["is_active", "deactivated_at", "updated_at"])
                if not expected:
                    removed += 1
                    if record.course.lecturer_id == record.lecturer_id:
                        record.course.lecturer = None
                        record.course.save(update_fields=["lecturer", "updated_at"])
        for allocation in incoming.values():
            course = allocation["course"]
            lecturers = allocation["lecturers"]
            lecturer_ids = {lecturer.pk for lecturer in lecturers}
            # Keep an already-valid lead lecturer.  Otherwise select the first
            # lecturer in the sheet as the catalogue's lead without losing any
            # co-lecturer rows below.
            if course.lecturer_id not in lecturer_ids:
                if course.lecturer_id:
                    reassigned += 1
                course.lecturer = lecturers[0]
                course.save(update_fields=["lecturer", "updated_at"])
            for lecturer in lecturers:
                record, _ = LecturerCourseRegistration.all_objects.get_or_create(lecturer=lecturer, course=course)
                record.is_active = True
                record.deactivated_at = None
                record.allocation_upload = upload
                record.save(update_fields=["is_active", "deactivated_at", "allocation_upload", "updated_at"])
                assigned += 1

        # Lecturer allocation changes must never alter a student's registered
        # courses, payments, or results.
        CourseAllocationUpload.all_objects.filter(**scope, is_active=True).exclude(pk=upload.pk).update(is_active=False)
        upload.content_hash = content_hash
        upload.is_active = True
        upload.reconciliation = {"assigned": assigned, "created_courses": created_courses, "reassigned": reassigned, "removed": removed, "skipped": skipped}
        upload.processing_summary = (f"Course-allocation reconciliation finished: {assigned} allocations assigned, {created_courses} courses created, "
                                     f"{reassigned} lecturer changes, {removed} removed allocations, {skipped} unmatched rows.")
        upload.save(update_fields=["content_hash", "is_active", "reconciliation", "processing_summary", "updated_at"])
        AuditLog.objects.create(
            institution=upload.institution, user=upload.uploaded_by,
            action="course_allocation_reconciled", object_type="CourseAllocationUpload",
            object_id=str(upload.pk), description=upload.processing_summary,
            new_value={**upload.reconciliation, "department_id": upload.department_id,
                       "catalogue_session_id": session.pk},
        )
    return upload.processing_summary


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
