from decimal import Decimal
from datetime import date

from django import forms
from django.utils.text import slugify

from .models import (
    AcademicSession,
    Course,
    CoursePaymentGateway,
    CourseMaterial,
    Department,
    DepartmentPaymentGateway,
    DepartmentalAssociation,
    DepartmentalFee,
    DepartmentalPaymentDocument,
    Handbook,
    LecturerCourseRegistration,
    Notification,
    StudentCourseRegistration,
    Timetable,
    User,
    LEVEL_CHOICES,
)


LEVEL_FILTER_CHOICES = tuple([("", "All levels")] + LEVEL_CHOICES)
SEMESTER_FILTER_CHOICES = tuple([("", "All semesters")] + list(Course.Semester.choices))


class PortalAuthenticationForm(forms.Form):
    username = forms.CharField(max_length=150)
    password = forms.CharField(widget=forms.PasswordInput)
    role = forms.ChoiceField(choices=User.Role.choices, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != "role":
                field.widget.attrs["class"] = "input-field"


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                continue
            widget.attrs["class"] = f'{widget.attrs.get("class", "")} input-field'.strip()


class CoursePricingMixin:
    def clean(self):
        cleaned_data = super().clean()
        is_free = cleaned_data.get("is_free")
        amount = cleaned_data.get("amount")
        if amount in (None, ""):
            amount = Decimal("0.00")
        if amount > 0:
            cleaned_data["is_free"] = False
            cleaned_data["amount"] = amount
        elif is_free:
            cleaned_data["amount"] = Decimal("0.00")
        elif amount <= 0:
            self.add_error("amount", "Paid courses must include an amount greater than zero.")
        return cleaned_data


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        if data in self.empty_values:
            return []
        if not isinstance(data, (list, tuple)):
            data = [data]
        cleaned_files = []
        for file_data in data:
            cleaned_files.append(super().clean(file_data, initial))
        return cleaned_files


class DepartmentForm(StyledModelForm):
    class Meta:
        model = Department
        fields = ["name"]

    def save(self, commit=True):
        department = super().save(commit=False)
        base_code = slugify(department.name).upper().replace("-", "")
        base_code = base_code[:20] or "DEPT"
        code = base_code
        suffix = 1
        while Department.objects.exclude(pk=department.pk).filter(code=code).exists():
            suffix += 1
            code = f"{base_code[: max(1, 20 - len(str(suffix))) ]}{suffix}"
        department.code = code
        if commit:
            department.save()
        return department


class CourseForm(CoursePricingMixin, StyledModelForm):
    class Meta:
        model = Course
        fields = ["department", "title", "code", "lecturer", "level", "semester", "file", "is_free", "amount"]
        widgets = {
            "amount": forms.NumberInput(attrs={"step": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["amount"].required = False


class CourseDetailsForm(CoursePricingMixin, StyledModelForm):
    class Meta:
        model = Course
        fields = [
            "title",
            "description",
            "level",
            "semester",
            "credit_units",
            "schedule_day",
            "schedule_time",
            "venue",
            "file",
            "is_free",
            "amount",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "schedule_time": forms.TimeInput(attrs={"type": "time"}),
            "amount": forms.NumberInput(attrs={"step": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["amount"].required = False


class LecturerCourseUpdateForm(CourseDetailsForm):
    pass


class StudentCourseFilterForm(forms.Form):
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    level = forms.ChoiceField(choices=LEVEL_FILTER_CHOICES, required=False)
    search = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs["class"] = "input-field"


class CourseBrowseFilterForm(forms.Form):
    FEE_TYPE_CHOICES = (
        ("", "All courses"),
        ("free", "Free courses"),
        ("paid", "Paid courses"),
    )

    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    level = forms.ChoiceField(choices=LEVEL_FILTER_CHOICES, required=False)
    fee_type = forms.ChoiceField(choices=FEE_TYPE_CHOICES, required=False)
    search = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"


class TimetableFilterForm(forms.Form):
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    semester = forms.ChoiceField(
        choices=SEMESTER_FILTER_CHOICES,
        required=False,
    )
    level = forms.ChoiceField(choices=LEVEL_FILTER_CHOICES, required=False)
    search = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"


class LecturerCourseFilterForm(forms.Form):
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    level = forms.ChoiceField(choices=LEVEL_FILTER_CHOICES, required=False)
    search = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"


class TimetableForm(StyledModelForm):
    class Meta:
        model = Timetable
        fields = ["department", "semester", "level", "file"]
        widgets = {
            "semester": forms.Select(choices=Course.Semester.choices),
            "level": forms.Select(choices=LEVEL_CHOICES),
        }

    def save(self, commit=True):
        timetable = super().save(commit=False)
        timetable.title = f"{timetable.department.name} {timetable.level} {timetable.semester} Timetable".strip()
        year = date.today().year
        timetable.academic_session = f"{year}/{year + 1}"
        timetable.description = ""
        if commit:
            timetable.save()
        return timetable


class HandbookFilterForm(forms.Form):
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    search = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"


class HandbookForm(StyledModelForm):
    class Meta:
        model = Handbook
        fields = ["department", "file"]

    def save(self, commit=True):
        handbook = super().save(commit=False)
        handbook.title = f"{handbook.department.name} Handbook"
        year = date.today().year
        handbook.academic_session = f"{year}/{year + 1}"
        handbook.semester = ""
        handbook.description = ""
        if commit:
            handbook.save()
        return handbook


class CourseMaterialForm(StyledModelForm):
    class Meta:
        model = CourseMaterial
        fields = ["course", "title", "description", "file", "is_free", "amount", "is_download_enabled"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "amount": forms.NumberInput(attrs={"step": "0.01"}),
        }

    def __init__(self, *args, user=None, allow_all_courses=False, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Course.objects.all()
        if user and not allow_all_courses:
            queryset = Course.objects.filter(lecturer=user)
        self.fields["course"].queryset = queryset.select_related("department")

    def clean(self):
        cleaned_data = super().clean()
        is_free = cleaned_data.get("is_free")
        amount = cleaned_data.get("amount") or Decimal("0.00")
        if is_free:
            cleaned_data["amount"] = Decimal("0.00")
        elif amount <= 0:
            self.add_error("amount", "Paid materials must include an amount.")
        return cleaned_data


class CourseMaterialBatchForm(forms.Form):
    course = forms.ModelChoiceField(queryset=Course.objects.none())
    title = forms.CharField(max_length=200)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    files = MultipleFileField(widget=MultipleFileInput())
    is_free = forms.BooleanField(required=False, initial=True)
    amount = forms.DecimalField(required=False, max_digits=10, decimal_places=2, widget=forms.NumberInput(attrs={"step": "0.01"}))
    is_download_enabled = forms.BooleanField(required=False, initial=True)

    def __init__(self, *args, user=None, allow_all_courses=False, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Course.objects.all()
        if user and not allow_all_courses:
            queryset = Course.objects.filter(lecturer=user)
        self.fields["course"].queryset = queryset.select_related("department", "lecturer")
        for field in self.fields.values():
            if isinstance(field.widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.ClearableFileInput)):
                continue
            field.widget.attrs["class"] = f'{field.widget.attrs.get("class", "")} input-field'.strip()

    def clean(self):
        cleaned_data = super().clean()
        is_free = cleaned_data.get("is_free")
        amount = cleaned_data.get("amount") or Decimal("0.00")
        if is_free:
            cleaned_data["amount"] = Decimal("0.00")
        elif amount <= 0:
            self.add_error("amount", "Paid materials must include an amount.")
        return cleaned_data


class NotificationForm(StyledModelForm):
    level = forms.ChoiceField(choices=LEVEL_FILTER_CHOICES, required=False)
    departments = forms.ModelMultipleChoiceField(
        queryset=Department.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    courses = forms.ModelMultipleChoiceField(
        queryset=Course.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Notification
        fields = ["subject", "body", "departments", "level", "courses"]
        widgets = {"body": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user and user.role == User.Role.LECTURER:
            self.fields.pop("courses")
        else:
            self.fields["courses"].queryset = Course.objects.all()


class BaseProfileForm(StyledModelForm):
    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "id_number",
            "department",
            "level",
            "phone_number",
            "passport_photo",
            "email_class_reminders",
            "class_reminder_alerts_enabled",
        ]


class StudentProfileForm(BaseProfileForm):
    class Meta(BaseProfileForm.Meta):
        fields = BaseProfileForm.Meta.fields


class LecturerProfileForm(BaseProfileForm):
    class Meta(BaseProfileForm.Meta):
        fields = [field for field in BaseProfileForm.Meta.fields if field != "level"]


class BaseUserForm(StyledModelForm):
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "id_number",
            "department",
            "level",
            "phone_number",
        ]

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("password1") != cleaned_data.get("password2"):
            self.add_error("password2", "Passwords do not match.")
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class StudentUserForm(BaseUserForm):
    class Meta(BaseUserForm.Meta):
        fields = BaseUserForm.Meta.fields

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.STUDENT
        user.is_approved = True
        if commit:
            user.save()
        return user


class LecturerUserForm(BaseUserForm):
    class Meta(BaseUserForm.Meta):
        fields = [field for field in BaseUserForm.Meta.fields if field != "level"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.LECTURER
        user.is_approved = False
        if commit:
            user.save()
        return user


class RequiredProfileFieldsMixin:
    required_fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in self.required_fields:
            field = self.fields[field_name]
            field.required = True
            if not field.widget.attrs.get("placeholder"):
                field.widget.attrs["placeholder"] = field.label


class StudentSignupForm(RequiredProfileFieldsMixin, StudentUserForm):
    required_fields = (
        "username",
        "first_name",
        "last_name",
        "email",
        "id_number",
        "department",
        "level",
        "phone_number",
    )


class LecturerSignupForm(RequiredProfileFieldsMixin, LecturerUserForm):
    required_fields = (
        "username",
        "first_name",
        "last_name",
        "email",
        "id_number",
        "department",
        "phone_number",
    )


class DepartmentPaymentGatewayForm(StyledModelForm):
    class Meta:
        model = DepartmentPaymentGateway
        fields = ["paystack_public_key", "paystack_secret_key"]
        widgets = {
            "paystack_secret_key": forms.PasswordInput(render_value=True),
        }


class CoursePaymentGatewayForm(StyledModelForm):
    class Meta:
        model = CoursePaymentGateway
        fields = ["paystack_public_key", "paystack_secret_key"]
        widgets = {
            "paystack_secret_key": forms.PasswordInput(render_value=True),
        }


class AcademicSessionForm(StyledModelForm):
    class Meta:
        model = AcademicSession
        fields = ["name", "is_current"]


class DepartmentalAssociationForm(StyledModelForm):
    class Meta:
        model = DepartmentalAssociation
        fields = ["name"]


class DepartmentalStudentPaymentForm(forms.Form):
    white_form = forms.FileField()
    school_receipt = forms.FileField()
    supporting_documents = MultipleFileField(
        required=False,
        widget=MultipleFileInput(),
    )
    association_choice = forms.ChoiceField(
        choices=(),
        widget=forms.RadioSelect,
    )
    additional_associations = forms.MultipleChoiceField(
        required=False,
        choices=(),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, session=None, fee_map=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session
        self.fee_map = fee_map or {}
        acf = self.fee_map.get("acf")
        mssn = self.fee_map.get("mssn")
        self.fields["association_choice"].choices = [
            ("acf", f"ACF Fee ({acf.amount})") if acf else None,
            ("mssn", f"MSSN Fee ({mssn.amount})") if mssn else None,
        ]
        self.fields["association_choice"].choices = [choice for choice in self.fields["association_choice"].choices if choice]
        if self.fields["association_choice"].choices:
            default_association = self.fields["association_choice"].choices[0][0]
            self.fields["association_choice"].initial = default_association
            if self.is_bound and not self.data.get(self.add_prefix("association_choice")):
                self.data = self.data.copy()
                self.data[self.add_prefix("association_choice")] = default_association
        optional_choices = [
            (code, f"{fee.association.name} ({fee.amount})")
            for code, fee in self.fee_map.items()
            if code not in {"departmental_fee", "acf", "mssn"}
        ]
        self.fields["additional_associations"].choices = optional_choices
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, (forms.RadioSelect, forms.CheckboxSelectMultiple)):
                continue
            widget.attrs["class"] = f'{widget.attrs.get("class", "")} input-field'.strip()


class DepartmentalSearchForm(forms.Form):
    student_id = forms.CharField(required=False, label="Student ID")
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    level = forms.ChoiceField(choices=LEVEL_FILTER_CHOICES, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"
