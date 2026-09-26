from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import (
    AcademicSession,
    AccommodationAllocation,
    AccommodationApplication,
    AccommodationSession,
    Department,
    Faculty,
    Hostel,
    HostelBed,
    HostelBlock,
    HostelRoom,
    Institution,
    User,
    reset_current_institution,
    set_current_institution,
)


class AccommodationIntegrityTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            name="Accommodation University", institution_code="ACCOMMODATION",
            email="accommodation@example.test", subdomain="accommodation",
            status=Institution.Status.ACTIVE,
        )
        self.token = set_current_institution(self.institution)
        self.session = AcademicSession.objects.create(name="2030/2031", is_current=True)
        faculty = Faculty.objects.create(name="Science", code="SCI")
        department = Department.objects.create(name="Computing", code="CSC", faculty=faculty)
        self.student = User.all_objects.create_user(
            username="accommodation-student", password="safe-password-123",
            role=User.Role.STUDENT, institution=self.institution, department=department,
        )
        self.accommodation_session = AccommodationSession.objects.create(
            academic_session=self.session, name="2030 residence", is_open=True,
        )
        hostel = Hostel.objects.create(name="Unity Hall", code="UNITY")
        block = HostelBlock.objects.create(hostel=hostel, name="A")
        room = HostelRoom.objects.create(block=block, code="A01")
        self.bed = HostelBed.objects.create(room=room, code="1")
        self.application = AccommodationApplication.objects.create(
            student=self.student, accommodation_session=self.accommodation_session,
        )

    def tearDown(self):
        reset_current_institution(self.token)

    def test_student_can_only_apply_once_per_accommodation_session(self):
        duplicate = AccommodationApplication(
            institution=self.institution, student=self.student,
            accommodation_session=self.accommodation_session,
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_active_bed_cannot_be_allocated_twice(self):
        AccommodationAllocation.objects.create(
            application=self.application, student=self.student, bed=self.bed,
        )
        other_student = User.all_objects.create_user(
            username="second-accommodation-student", password="safe-password-123",
            role=User.Role.STUDENT, institution=self.institution,
        )
        other_application = AccommodationApplication.objects.create(
            student=other_student, accommodation_session=self.accommodation_session,
        )
        duplicate = AccommodationAllocation(
            institution=self.institution, application=other_application,
            student=other_student, bed=self.bed,
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_tenant_manager_does_not_return_another_institutions_inventory(self):
        other = Institution.objects.create(
            name="Other University", institution_code="OTHER-ACCOMMODATION",
            email="other-accommodation@example.test", subdomain="other-accommodation",
            status=Institution.Status.ACTIVE,
        )
        other_token = set_current_institution(other)
        try:
            other_session = AcademicSession.objects.create(name="2030/2031", is_current=True)
            other_accommodation_session = AccommodationSession.objects.create(
                academic_session=other_session, name="Other residence", is_open=True,
            )
            other_hostel = Hostel.objects.create(name="Other Hall", code="OTHER")
            self.assertEqual(AccommodationSession.objects.count(), 1)
            self.assertEqual(Hostel.objects.count(), 1)
            self.assertEqual(AccommodationSession.objects.get(), other_accommodation_session)
            self.assertEqual(Hostel.objects.get(), other_hostel)
        finally:
            reset_current_institution(other_token)
