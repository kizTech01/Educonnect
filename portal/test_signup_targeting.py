from django.test import TestCase
from django.urls import reverse

from .forms import StudentSignupForm
from .models import Department, Faculty, Programme, get_default_institution


class StudentSignupTargetingTests(TestCase):
    def setUp(self):
        self.institution = get_default_institution()
        self.science = Faculty.objects.create(
            institution=self.institution,
            name="Signup Science",
            code="SGSCI",
        )
        self.arts = Faculty.objects.create(
            institution=self.institution,
            name="Signup Arts",
            code="SGART",
        )
        self.biology = Department.objects.create(
            institution=self.institution,
            faculty=self.science,
            name="Signup Biology",
            code="SGBIO",
        )
        self.chemistry = Department.objects.create(
            institution=self.institution,
            faculty=self.science,
            name="Signup Chemistry",
            code="SGCHM",
        )
        self.history = Department.objects.create(
            institution=self.institution,
            faculty=self.arts,
            name="Signup History",
            code="SGHIS",
        )
        self.biology_programme = Programme.objects.create(
            institution=self.institution,
            department=self.biology,
            name="Signup Botany",
            code="SGBOT",
        )
        self.history_programme = Programme.objects.create(
            institution=self.institution,
            department=self.history,
            name="Signup History Studies",
            code="SGHST",
        )

    def test_signup_querysets_follow_selected_faculty_and_department(self):
        unselected_form = StudentSignupForm(institution=self.institution)
        self.assertEqual(
            set(unselected_form.fields["department"].queryset.values_list("id", flat=True)),
            {self.biology.pk, self.chemistry.pk, self.history.pk},
        )
        self.assertFalse(unselected_form.fields["programme"].queryset.exists())

        selected_form = StudentSignupForm(
            data={"faculty": str(self.science.pk), "department": str(self.biology.pk)},
            institution=self.institution,
        )
        self.assertEqual(
            set(selected_form.fields["department"].queryset.values_list("id", flat=True)),
            {self.biology.pk, self.chemistry.pk},
        )
        self.assertEqual(
            set(selected_form.fields["programme"].queryset.values_list("id", flat=True)),
            {self.biology_programme.pk},
        )

        no_programmes_form = StudentSignupForm(
            data={"faculty": str(self.science.pk), "department": str(self.chemistry.pk)},
            institution=self.institution,
        )
        self.assertFalse(no_programmes_form.fields["programme"].queryset.exists())

    def test_signup_page_supplies_only_this_institutions_department_and_programme_options(self):
        response = self.client.get(reverse("portal:student-signup"))

        self.assertEqual(response.status_code, 200)
        department_ids = {item["id"] for item in response.context["signup_departments"]}
        programme_ids = {item["id"] for item in response.context["programme_departments"]}
        self.assertTrue({self.biology.pk, self.chemistry.pk, self.history.pk}.issubset(department_ids))
        self.assertTrue({self.biology_programme.pk, self.history_programme.pk}.issubset(programme_ids))
        self.assertContains(response, 'id="signup-departments"')

    def test_signup_form_rejects_department_outside_selected_faculty(self):
        form = StudentSignupForm(
            data={
                "first_name": "Faculty",
                "last_name": "Mismatch",
                "email": "faculty-mismatch@example.test",
                "id_number": "SIGNUP-MISMATCH",
                "faculty": str(self.science.pk),
                "department": str(self.history.pk),
                "level": "100",
                "phone_number": "08000000000",
                "password1": "River-Sunrise-Maple-2026!",
                "password2": "River-Sunrise-Maple-2026!",
                "accept_terms": "on",
            },
            institution=self.institution,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("department", form.errors)
