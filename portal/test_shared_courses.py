from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .automation import _create_course_from_allocation, import_handbook_courses
from .models import (
    AcademicSession,
    Course,
    CourseMaterial,
    Curriculum,
    CurriculumCourse,
    Department,
    Faculty,
    Handbook,
    Institution,
    LecturerCourseRegistration,
    MaterialAccess,
    Programme,
    StudentCourseRegistration,
    User,
)
from .services import sync_material_access_for_material


class SharedCourseCurriculumTests(TestCase):
    def setUp(self):
        self.session = AcademicSession.objects.create(name="2045/2046", is_current=True)
        self.department = Department.objects.create(name="Computing", code="CMP")
        self.computer_science = Programme.objects.create(
            institution=self.department.institution,
            department=self.department, name="Computer Science", code="BCSC",
        )
        self.cyber_security = Programme.objects.create(
            institution=self.department.institution,
            department=self.department, name="Cyber Security", code="BCYS",
        )

    def _upload(self, programme, text):
        handbook = Handbook.objects.create(
            institution=self.department.institution,
            department=self.department,
            programme=programme,
            title=f"{programme.name} handbook",
            academic_session=self.session.name,
            file=SimpleUploadedFile("handbook.txt", text),
        )
        import_handbook_courses(handbook)

    def test_programmes_share_one_course_with_independent_curriculum_metadata(self):
        self._upload(self.computer_science, b"CSC201 Data Structures 200 3 units")
        self._upload(self.cyber_security, b"CSC201 Data Structures 300 2 units")

        course = Course.objects.get(department=self.department, code="CSC201")
        self.assertEqual(Course.objects.filter(department=self.department, code="CSC201").count(), 1)
        entries = CurriculumCourse.objects.filter(course=course).select_related("curriculum__programme")
        metadata = {
            entry.curriculum.programme.code: (entry.level, entry.credit_units)
            for entry in entries
        }
        self.assertEqual(metadata, {"BCSC": ("200", 3), "BCYS": ("300", 2)})

    def test_one_allocation_and_material_are_shared_by_registered_programme_students(self):
        self._upload(self.computer_science, b"CSC201 Data Structures 200 3 units")
        self._upload(self.cyber_security, b"CSC201 Data Structures 200 3 units")
        course = Course.objects.get(department=self.department, code="CSC201")
        lecturer = User.objects.create_user(
            username="dr-ada", password="pass1234", role=User.Role.LECTURER,
            department=self.department, email="ada@example.com",
        )
        course.lecturer = lecturer
        course.schedule_day = Course.Day.TUESDAY
        course.venue = "Lecture Theatre 2"
        course.save()
        allocation = LecturerCourseRegistration.objects.create(lecturer=lecturer, course=course)

        students = []
        for index, programme in enumerate((self.computer_science, self.cyber_security), start=1):
            student = User.objects.create_user(
                username=f"student{index}", password="pass1234", role=User.Role.STUDENT,
                department=self.department, programme=programme, level="200",
                id_number=f"STU-{index}", email=f"student{index}@example.com",
            )
            students.append(student)
            StudentCourseRegistration.objects.create(student=student, course=course, session=self.session)

        material = CourseMaterial.objects.create(
            course=course, lecturer=lecturer, title="Lecture 4", file=SimpleUploadedFile("lecture4.pdf", b"pdf"),
        )
        sync_material_access_for_material(material)

        self.assertEqual(LecturerCourseRegistration.objects.filter(course=course, is_active=True).count(), 1)
        self.assertEqual(all(registration.course_id == allocation.course_id for registration in StudentCourseRegistration.objects.all()), True)
        self.assertEqual(MaterialAccess.objects.filter(material=material, student__in=students).count(), 2)
        course.refresh_from_db()
        self.assertEqual(course.schedule_day, Course.Day.TUESDAY)

    def test_student_catalogue_uses_their_curriculum_metadata_for_a_shared_course(self):
        self._upload(self.computer_science, b"CSC201 Data Structures 200 3 units")
        self._upload(self.cyber_security, b"CSC201 Data Structures 300 2 units")
        student = User.objects.create_user(
            username="cyber-student", password="pass1234", role=User.Role.STUDENT,
            department=self.department, programme=self.cyber_security, level="300",
            id_number="CYB-1", email="cyber@example.com",
        )

        self.client.force_login(student)
        response = self.client.get(reverse("portal:student-courses"), {"search": "CSC201"})

        self.assertContains(response, "CSC201 - Data Structures")
        self.assertContains(response, ">300<", html=False)

    def test_course_codes_are_isolated_by_institution(self):
        other = Institution.objects.create(
            name="Other University", institution_code="OTHER", email="other@example.com", subdomain="other",
        )
        faculty = Faculty.objects.create(institution=other, name="Science", code="SCI")
        other_department = Department.objects.create(institution=other, faculty=faculty, name="Computing", code="CMP")
        other_session = AcademicSession.objects.create(institution=other, name="2045/2046", is_current=True)
        other_curriculum = Curriculum.objects.create(
            institution=other, department=other_department, effective_session=other_session,
        )
        Course.objects.create(
            institution=other, department=other_department, academic_session=other_session,
            curriculum=other_curriculum, code="CSC201", title="Data Structures", level="200",
        )
        self._upload(self.computer_science, b"CSC201 Data Structures 200 3 units")

        self.assertEqual(Course.all_objects.filter(code="CSC201").count(), 2)
        self.assertEqual(Course.objects.get(department=self.department, code="CSC201").institution_id, self.department.institution_id)

    def test_catalogue_constraint_rejects_a_second_course_code_in_one_department(self):
        self._upload(self.computer_science, b"CSC201 Data Structures 200 3 units")
        curriculum = self.computer_science.curricula.get(effective_session=self.session)

        # bulk_create bypasses Course.save/full_clean, proving the PostgreSQL-
        # compatible database constraint, not just form validation, protects
        # the shared catalogue identity.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.bulk_create([
                Course(
                    institution=self.department.institution,
                    department=self.department,
                    academic_session=self.session,
                    curriculum=curriculum,
                    code="CSC201",
                    title="Duplicate Data Structures",
                    level="200",
                )
            ])

    def test_shared_offering_schedule_and_availability_apply_to_every_programme(self):
        self._upload(self.computer_science, b"CSC201 Data Structures 200 3 units")
        self._upload(self.cyber_security, b"CSC201 Data Structures 200 3 units")
        course = Course.objects.get(department=self.department, code="CSC201")

        students = []
        for index, programme in enumerate((self.computer_science, self.cyber_security), start=1):
            student = User.objects.create_user(
                username=f"registered{index}", password="pass1234", role=User.Role.STUDENT,
                department=self.department, programme=programme, level="200",
                id_number=f"REG-{index}", email=f"registered{index}@example.com",
            )
            self.client.force_login(student)
            response = self.client.post(reverse("portal:student-courses"), {
                "action": "register-course", "course_id": course.pk,
            })
            self.assertEqual(response.status_code, 302)
            self.assertTrue(StudentCourseRegistration.objects.filter(student=student, course=course).exists())
            students.append(student)

        course.schedule_day = Course.Day.TUESDAY
        course.venue = "Lecture Theatre 2"
        course.is_available = False
        course.save()
        for student in students:
            self.client.force_login(student)
            response = self.client.get(reverse("portal:student-courses"))
            self.assertContains(response, "Tuesday")
            self.assertContains(response, "Unavailable")

        blocked_student = User.objects.create_user(
            username="blocked", password="pass1234", role=User.Role.STUDENT,
            department=self.department, programme=self.computer_science, level="200",
            id_number="BLOCKED-1", email="blocked@example.com",
        )
        self.client.force_login(blocked_student)
        self.client.post(reverse("portal:student-courses"), {
            "action": "register-course", "course_id": course.pk,
        })
        self.assertFalse(StudentCourseRegistration.objects.filter(student=blocked_student, course=course).exists())

    def test_allocation_created_course_has_an_authoritative_curriculum_entry(self):
        course = _create_course_from_allocation(
            self.department,
            {"course_title": "Algorithms", "unit": "3", "semester": "First"},
            "CSC302",
            self.session,
        )

        self.assertIsNotNone(course)
        entry = CurriculumCourse.objects.get(course=course)
        self.assertEqual(entry.curriculum, course.curriculum)
        self.assertEqual(entry.level, "300")
        self.assertEqual(entry.credit_units, 3)
