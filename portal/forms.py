from decimal import Decimal
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm
from django.core.validators import FileExtensionValidator
from django.db.models import Q
from django.utils.text import slugify

from .models import (
    AcademicSession,
    Course,
    CourseResult,
    CoursePaymentGateway,
    CourseMaterial,
    Department,
    DepartmentLecturerUpload,
    CourseAllocationUpload,
    AcademicSession,
    DepartmentPaymentGateway,
    DepartmentCoursePaymentGateway,
    DepartmentalAssociation,
    DepartmentalFee,
    DepartmentalPaymentDocument,
    Handbook,
    Faculty,
    InstitutionProfile,
    Institution,
    LecturerCourseRegistration,
    Notification,
    StudentCourseRegistration,
    Timetable,
    User,
    SubscriptionPlan,
    SubscriptionPlanDuration,
    SubscriptionPaymentGateway,
    ScreeningIntegration,
    AccommodationApplication,
    AccommodationSession,
    Hostel,
    HostelBlock,
    HostelRoom,
    HostelBed,
    Programme,
    LEVEL_CHOICES,
    curriculum_for_programme,
)


LEVEL_FILTER_CHOICES = tuple([("", "All levels")] + LEVEL_CHOICES)
SEMESTER_FILTER_CHOICES = tuple([("", "All semesters")] + list(Course.Semester.choices))


class PortalAuthenticationForm(forms.Form):
    username = forms.CharField(max_length=150)
    password = forms.CharField(widget=forms.PasswordInput)
    # Route-specific pages still submit a role, while /login/ is the secure
    # institution login that determines a permitted dashboard after auth.
    role = forms.ChoiceField(choices=User.Role.choices, widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != "role":
                field.widget.attrs["class"] = "input-field"


class AIAutomationUploadForm(forms.Form):
    """Input for a reviewable automation proposal, never direct execution."""

    file = forms.FileField(
        label="Source file",
        validators=[FileExtensionValidator(allowed_extensions=["csv", "xlsx", "pdf", "docx"])],
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.xlsx,.pdf,.docx"}),
    )
    command = forms.CharField(
        label="Requested operation",
        max_length=500,
        help_text="For example: Create lecturer accounts from this file.",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Create staff accounts from this file."}),
    )

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        if uploaded_file.size > 10 * 1024 * 1024:
            raise forms.ValidationError("Files must be 10 MB or smaller.")
        return uploaded_file


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            queryset = getattr(field, "queryset", None)
            if queryset is not None and any(item.name == "institution" for item in queryset.model._meta.fields):
                field.queryset = queryset.model._default_manager.all()
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
        fields = ["faculty", "name", "description"]

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


class FacultyForm(StyledModelForm):
    class Meta:
        model = Faculty
        fields = ["name", "description"]

    def save(self, commit=True):
        faculty = super().save(commit=False)
        base_code = slugify(faculty.name).upper().replace("-", "")[:20] or "FACULTY"
        code, suffix = base_code, 1
        while Faculty.objects.exclude(pk=faculty.pk).filter(code=code).exists():
            suffix += 1
            code = f"{base_code[:max(1, 20-len(str(suffix)))]}{suffix}"
        faculty.code = code
        if commit:
            faculty.save()
        return faculty


class InstitutionProfileForm(StyledModelForm):
    class Meta:
        model = InstitutionProfile
        fields = ["name"]
        labels = {
            "name": "University or institution name",
        }


class InstitutionCreateForm(StyledModelForm):
    logo = forms.FileField(
        required=True,
        label="Institution logo",
        help_text="Upload this institution's logo (JPG, PNG, SVG, or WebP).",
        validators=[FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "svg", "webp"])],
        widget=forms.ClearableFileInput(attrs={"accept": ".jpg,.jpeg,.png,.svg,.webp,image/jpeg,image/png,image/svg+xml,image/webp"}),
    )
    # A plain text field lets us turn an institution name or portal URL into the
    # slug stored by the model, rather than rejecting it before ``clean_subdomain``
    # can run.
    subdomain = forms.CharField(
        max_length=63,
        label="Portal subdomain",
        help_text="Enter a short name (for example, Coastal Polytechnic) or its portal URL.",
    )

    class Meta:
        model = Institution
        fields = [
            "name", "institution_code", "institution_type", "state", "email", "phone",
            "address", "country", "timezone", "logo", "subdomain", "custom_domain", "status",
        ]

    def clean_email(self):
        """Use the institution contact email as its administrator's login."""
        email = self.cleaned_data["email"].lower()
        if User.all_objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with this email already exists.")
        if User.all_objects.filter(username__iexact=email).exists():
            raise forms.ValidationError("This email is already used as a username.")
        return email

    def clean_subdomain(self):
        value = self.cleaned_data["subdomain"].strip().lower()
        parsed = urlsplit(value if "://" in value else f"//{value}")
        if parsed.hostname:
            value = parsed.hostname

        # Pasting this application's full portal address should keep only the
        # institution-specific part, e.g. coastal.educonnect.com -> coastal.
        base_domain = settings.PLATFORM_BASE_DOMAIN.lower().strip(".")
        if value.endswith(f".{base_domain}"):
            value = value[: -(len(base_domain) + 1)]

        subdomain = slugify(value)
        if not subdomain:
            raise forms.ValidationError("Enter an institution name or a valid portal subdomain.")
        if len(subdomain) > 63:
            raise forms.ValidationError("The portal subdomain must be 63 characters or fewer.")
        return subdomain


class InstitutionUpdateForm(InstitutionCreateForm):
    """Edit an institution without requiring a replacement logo or admin email."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logo"].required = False
        self.fields["logo"].help_text = "Leave blank to keep the current logo."

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        administrator = (
            User.all_objects.filter(
                institution=self.instance,
                role=User.Role.ADMIN,
                is_superuser=False,
            )
            .order_by("id")
            .first()
        )
        existing_users = User.all_objects.all()
        if administrator:
            existing_users = existing_users.exclude(pk=administrator.pk)
        if existing_users.filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with this email already exists.")
        if existing_users.filter(username__iexact=email).exists():
            raise forms.ValidationError("This email is already used as a username.")
        return email


class ScreeningIntegrationForm(StyledModelForm):
    """Platform-side settings passed to the independently deployed screener."""

    def __init__(self, *args, institution=None, **kwargs):
        super().__init__(*args, **kwargs)
        if institution is not None:
            self.fields["admission_session"].queryset = AcademicSession.all_objects.filter(
                institution=institution
            )

    class Meta:
        model = ScreeningIntegration
        fields = [
            "is_open", "admission_session", "opens_at", "closes_at", "application_fee",
            "available_programmes", "admission_requirements", "required_documents",
            "applicant_categories", "utme_de_settings", "workflow_settings", "notification_settings",
        ]
        widgets = {
            "opens_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "closes_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "available_programmes": forms.Textarea(attrs={"rows": 3, "placeholder": '["BSc Computer Science"]'}),
            "required_documents": forms.Textarea(attrs={"rows": 3, "placeholder": '["O\'Level result"]'}),
            "applicant_categories": forms.Textarea(attrs={"rows": 2, "placeholder": '["UTME", "Direct Entry"]'}),
            "admission_requirements": forms.Textarea(attrs={"rows": 3, "placeholder": '{"minimum_utme_score": 160}'}),
            "utme_de_settings": forms.Textarea(attrs={"rows": 3, "placeholder": '{"utme_enabled": true, "de_enabled": true}'}),
            "workflow_settings": forms.Textarea(attrs={"rows": 3, "placeholder": '{"verification_required": true}'}),
            "notification_settings": forms.Textarea(attrs={"rows": 3, "placeholder": '{"email": true}'}),
        }

    def clean(self):
        cleaned = super().clean()
        opens_at, closes_at = cleaned.get("opens_at"), cleaned.get("closes_at")
        if opens_at and closes_at and closes_at <= opens_at:
            self.add_error("closes_at", "The closing date must be later than the opening date.")
        return cleaned


class SubscriptionPlanForm(StyledModelForm):
    DURATION_CHOICES = (
        ("183", "6 months"),
        ("365", "1 year"),
        ("730", "2 years"),
        ("1825", "5 years"),
    )
    duration_days = forms.ChoiceField(choices=DURATION_CHOICES, label="Plan duration")

    class Meta:
        model = SubscriptionPlan
        fields = [
            "name", "description", "price",
            "max_students", "max_staff", "max_storage_mb", "is_active",
        ]
        widgets = {
            "price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def save(self, commit=True):
        plan = super().save(commit=False)
        plan.billing_period = SubscriptionPlan.BillingPeriod.CUSTOM
        plan.custom_duration_days = int(self.cleaned_data["duration_days"])
        plan.is_trial = False
        if commit:
            plan.save()
            SubscriptionPlanDuration.objects.get_or_create(
                plan=plan,
                duration_days=plan.custom_duration_days,
                defaults={"price": plan.price},
            )
        return plan


class SubscriptionPlanEditForm(StyledModelForm):
    """Edit commercial plan details without changing its existing durations."""

    class Meta:
        model = SubscriptionPlan
        fields = [
            "name", "description", "max_students", "max_staff", "max_storage_mb",
            "is_active",
        ]


class SubscriptionPlanDurationForm(StyledModelForm):
    class Meta:
        model = SubscriptionPlanDuration
        fields = ["plan", "duration_days", "price", "is_active"]
        widgets = {
            "price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price <= 0:
            raise forms.ValidationError("A subscription duration must have an amount greater than zero.")
        return price


class SubscriptionPlanDurationEditForm(StyledModelForm):
    """Change a duration's selling price or availability, never its identity."""

    class Meta:
        model = SubscriptionPlanDuration
        fields = ["price", "is_active"]
        widgets = {
            "price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price <= 0:
            raise forms.ValidationError("A subscription duration must have an amount greater than zero.")
        return price


class SubscriptionRenewalForm(forms.Form):
    plan = forms.ModelChoiceField(queryset=SubscriptionPlan.objects.none())
    plan_duration = forms.ModelChoiceField(queryset=SubscriptionPlanDuration.objects.none(), label="Plan duration")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        available_durations = SubscriptionPlanDuration.objects.filter(
            is_active=True,
            plan__is_active=True,
            plan__is_trial=False,
        ).select_related("plan")
        self.fields["plan"].queryset = SubscriptionPlan.objects.filter(
            is_active=True,
            is_trial=False,
            durations__in=available_durations,
        ).distinct()
        self.fields["plan_duration"].queryset = available_durations
        self.fields["plan"].label_from_instance = lambda plan: plan.name
        self.fields["plan_duration"].label_from_instance = (
            lambda duration: f"{duration.duration_label} — ₦{duration.price:,.2f}"
        )
        self.fields["plan"].widget.attrs["class"] = "input-field"
        self.fields["plan_duration"].widget.attrs["class"] = "input-field"

    def clean(self):
        cleaned_data = super().clean()
        plan = cleaned_data.get("plan")
        duration = cleaned_data.get("plan_duration")
        if plan and duration and duration.plan_id != plan.id:
            self.add_error("plan_duration", "Choose a duration offered by the selected plan.")
        return cleaned_data


class SubscriptionPaymentGatewayForm(StyledModelForm):
    class Meta:
        model = SubscriptionPaymentGateway
        fields = ["paystack_public_key", "paystack_secret_key", "is_active"]
        widgets = {
            "paystack_secret_key": forms.PasswordInput(),
        }

    def clean_paystack_secret_key(self):
        secret_key = self.cleaned_data["paystack_secret_key"]
        return secret_key or self.instance.paystack_secret_key


class PlatformAdminForm(forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField()
    password = forms.CharField(min_length=12, widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.all_objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already in use.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.all_objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("That email is already in use.")
        return email


class DepartmentLecturerUploadForm(StyledModelForm):
    class Meta:
        model = DepartmentLecturerUpload
        fields = ["file"]


class CourseAllocationUploadForm(StyledModelForm):
    class Meta:
        model = CourseAllocationUpload
        fields = ["file"]


class CourseForm(CoursePricingMixin, StyledModelForm):
    programmes = forms.ModelMultipleChoiceField(
        queryset=Programme.objects.none(), required=False,
        help_text="Choose one programme for an exclusive course, or several to share it.",
    )

    class Meta:
        model = Course
        fields = ["department", "title", "code", "lecturer", "level", "semester", "file", "is_free", "amount", "programmes"]
        widgets = {
            "amount": forms.NumberInput(attrs={"step": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["amount"].required = False
        self.fields["programmes"].queryset = Programme.objects.select_related("department").filter(is_active=True)
        if self.instance.pk:
            self.initial["programmes"] = self.instance.programmes.all()

    def clean(self):
        cleaned = super().clean()
        department = cleaned.get("department")
        programmes = cleaned.get("programmes")
        if department and programmes and programmes.exclude(department=department).exists():
            self.add_error("programmes", "Every selected programme must belong to the selected department.")
        return cleaned

    def save(self, commit=True):
        course = super().save(commit=commit)
        if commit:
            programmes = self.cleaned_data.get("programmes")
            course.programmes.set(programmes)
            session = course.academic_session or AcademicSession.objects.filter(is_current=True).first()
            for programme in programmes:
                curriculum = curriculum_for_programme(programme, session)
                if curriculum:
                    course.curricula.add(curriculum)
        return course


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

    def __init__(self, *args, student=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.all()
        if student is not None:
            self.fields["department"].queryset = (
                Department.objects.filter(pk=student.department_id)
                if student.department_id
                else Department.objects.none()
            )
        self.fields["search"].widget.attrs["placeholder"] = "Course code, title, or lecturer"
        for field in self.fields.values():
            if not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs["class"] = "input-field"


class CourseStudentGroupForm(forms.Form):
    enable_grouping = forms.BooleanField(
        required=False,
        label="Enable student grouping",
        help_text="Leave this off when you only need to download paid student IDs.",
    )
    grouping_method = forms.ChoiceField(
        choices=(("department", "By department"), ("random", "Random")),
        widget=forms.RadioSelect,
        initial="department",
        required=False,
    )
    group_names = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "A1, A2, A3"}),
        help_text="Required for random grouping. Separate group names with commas or new lines.",
    )

    def clean_group_names(self):
        names = self.cleaned_data["group_names"]
        names = [name.strip() for name in names.replace("\n", ",").split(",") if name.strip()]
        if len({name.casefold() for name in names}) != len(names):
            raise forms.ValidationError("Each random group name must be unique.")
        return names

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("enable_grouping") and not cleaned_data.get("grouping_method"):
            self.add_error("grouping_method", "Choose how students should be grouped.")
        if (
            cleaned_data.get("enable_grouping")
            and cleaned_data.get("grouping_method") == "random"
            and not cleaned_data.get("group_names")
        ):
            self.add_error("group_names", "Enter at least one group name for random grouping.")
        return cleaned_data


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
        self.fields["department"].queryset = Department.objects.all()
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
        self.fields["department"].queryset = Department.objects.all()
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"


class LecturerCourseFilterForm(forms.Form):
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    level = forms.ChoiceField(choices=LEVEL_FILTER_CHOICES, required=False)
    search = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.all()
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
        self.fields["department"].queryset = Department.objects.all()
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"


class HandbookForm(StyledModelForm):
    class Meta:
        model = Handbook
        fields = ["department", "programme", "file"]

    def clean(self):
        cleaned = super().clean()
        programme = cleaned.get("programme")
        department = cleaned.get("department")
        if programme and department and programme.department_id != department.id:
            self.add_error("programme", "Choose a programme under the selected department.")
        return cleaned

    def save(self, commit=True):
        handbook = super().save(commit=False)
        handbook.title = f"{handbook.programme.name if handbook.programme_id else handbook.department.name} Handbook"
        session = AcademicSession.objects.filter(is_current=True).first()
        if session is None:
            year = date.today().year
            session, _ = AcademicSession.objects.get_or_create(
                name=f"{year}/{year + 1}",
                defaults={"is_current": True},
            )
        handbook.academic_session = session.name
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
            queryset = Course.objects.filter(
                Q(lecturer_registrations__lecturer=user) | Q(lecturer=user)
            ).distinct()
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
            queryset = Course.objects.filter(
                Q(lecturer_registrations__lecturer=user) | Q(lecturer=user)
            ).distinct()
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
    attachments = MultipleFileField(
        required=False,
        label="Attach files",
        help_text="You can select more than one file.",
        widget=MultipleFileInput(),
    )
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
            "programme",
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
        fields = [field for field in BaseProfileForm.Meta.fields if field not in {"level", "programme"}]


class AdminProfileForm(StyledModelForm):
    """Keep the institution-admin sign-in email stable after provisioning."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone_number"]


class InstitutionAdminPasswordChangeForm(PasswordChangeForm):
    """Style Django's authenticated password-change form for the admin portal."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"


class UserPasswordResetForm(PasswordResetForm):
    """Send a reset link for one known account, even if legacy data has duplicate emails."""

    def __init__(self, *args, target_user=None, **kwargs):
        self.target_user = target_user
        super().__init__(*args, **kwargs)

    def get_users(self, email):
        user = self.target_user
        if (
            user
            and user.is_active
            and user.has_usable_password()
            and user.email.casefold() == email.casefold()
        ):
            yield user


class BaseUserForm(StyledModelForm):
    faculty = forms.ModelChoiceField(queryset=Faculty.objects.all(), required=False)
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
            "programme",
            "level",
            "phone_number",
        ]

    def __init__(self, *args, institution=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Set this before ModelForm validation so a duplicate student ID is a
        # field error instead of an IntegrityError after a successful-looking
        # signup submission.
        if institution:
            self.instance.institution = institution
            self.fields["faculty"].queryset = Faculty.objects.filter(institution=institution)
            self.fields["department"].queryset = Department.objects.filter(institution=institution)
            if "programme" in self.fields:
                self.fields["programme"].queryset = Programme.objects.filter(
                    institution=institution, is_active=True,
                )
        if "programme" in self.fields:
            selected_department = self.data.get(self.add_prefix("department")) or self.initial.get("department")
            if selected_department:
                self.fields["programme"].queryset = self.fields["programme"].queryset.filter(
                    department_id=selected_department,
                )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("password1") != cleaned_data.get("password2"):
            self.add_error("password2", "Passwords do not match.")
        faculty = cleaned_data.get("faculty")
        department = cleaned_data.get("department")
        if department and not faculty:
            # The department is authoritative; this preserves older signup
            # links while keeping every account within the department's faculty.
            faculty = department.faculty
            cleaned_data["faculty"] = faculty
        if faculty and department and department.faculty_id != faculty.id:
            self.add_error("department", "Choose a department under the selected faculty.")
        programme = cleaned_data.get("programme")
        if programme and department and programme.department_id != department.id:
            self.add_error("programme", "Choose a programme under the selected department.")
        if (
            "programme" in self.fields and department
            and Programme.objects.filter(department=department, is_active=True).exists()
            and not programme
        ):
            self.add_error("programme", "Choose your programme before creating a student account.")
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
        fields = [field for field in BaseUserForm.Meta.fields if field not in {"level", "programme"}]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.LECTURER
        user.is_approved = False
        if commit:
            user.save()
        return user


class StaffUserForm(BaseUserForm):
    """Institution-admin staff creation without exposing platform privileges."""
    STAFF_ROLES = [
        User.Role.MIS, User.Role.BURSARY, User.Role.ADMISSION_OFFICER,
        User.Role.ACADEMIC_PLANNING, User.Role.HOD, User.Role.LECTURER,
        User.Role.SENATE_MEMBER,
    ]
    assigned_role = forms.ChoiceField(choices=[(role, User.Role(role).label) for role in STAFF_ROLES])

    class Meta(BaseUserForm.Meta):
        fields = [field for field in BaseUserForm.Meta.fields if field not in {"level", "programme"}]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("assigned_role") == User.Role.HOD and not cleaned.get("department"):
            self.add_error("department", "An HOD must be assigned to a department.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        selected = self.cleaned_data["assigned_role"]
        # HOD is an additional academic responsibility of a lecturer.
        user.role = User.Role.LECTURER if selected == User.Role.HOD else selected
        user.is_approved = selected != User.Role.LECTURER
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
            # A configured secret must never be sent back to the browser.
            # Leaving this field blank keeps the existing key unchanged.
            "paystack_secret_key": forms.PasswordInput(),
        }

    def clean_paystack_secret_key(self):
        return self.cleaned_data["paystack_secret_key"] or self.instance.paystack_secret_key


class DepartmentCoursePaymentGatewayForm(StyledModelForm):
    class Meta:
        model = DepartmentCoursePaymentGateway
        fields = ["paystack_public_key", "paystack_secret_key"]
        widgets = {
            "paystack_secret_key": forms.PasswordInput(),
        }

    def clean_paystack_secret_key(self):
        return self.cleaned_data["paystack_secret_key"] or self.instance.paystack_secret_key


class CoursePaymentGatewayForm(StyledModelForm):
    class Meta:
        model = CoursePaymentGateway
        fields = ["paystack_public_key", "paystack_secret_key"]
        widgets = {
            "paystack_secret_key": forms.PasswordInput(),
        }

    def clean_paystack_secret_key(self):
        return self.cleaned_data["paystack_secret_key"] or self.instance.paystack_secret_key


class AcademicSessionForm(StyledModelForm):
    class Meta:
        model = AcademicSession
        fields = ["name", "is_current"]


class ProgrammeFacultyFormMixin:
    """Keep programme departments inside the faculty selected by the admin."""

    def __init__(self, *args, institution=None, **kwargs):
        super().__init__(*args, **kwargs)
        faculties = Faculty.objects.filter(institution=institution) if institution else Faculty.objects.none()
        departments = Department.objects.filter(institution=institution) if institution else Department.objects.none()
        selected_faculty = self.data.get(self.add_prefix("faculty")) or self.initial.get("faculty")
        if not selected_faculty and getattr(self, "instance", None) and self.instance.department_id:
            selected_faculty = self.instance.department.faculty_id
            self.initial["faculty"] = selected_faculty
        self.fields["faculty"].queryset = faculties
        self.fields["department"].queryset = departments
        if selected_faculty:
            self.fields["department"].queryset = departments.filter(faculty_id=selected_faculty)
        self.order_fields(["faculty", "department"])
        for field in self.fields.values():
            field.widget.attrs["class"] = f'{field.widget.attrs.get("class", "")} input-field'.strip()

    def clean(self):
        cleaned_data = super().clean()
        faculty = cleaned_data.get("faculty")
        department = cleaned_data.get("department")
        if faculty and department and department.faculty_id != faculty.id:
            self.add_error("department", "Choose a department under the selected faculty.")
        return cleaned_data


class ProgrammeForm(ProgrammeFacultyFormMixin, StyledModelForm):
    faculty = forms.ModelChoiceField(queryset=Faculty.objects.none(), required=True)

    class Meta:
        model = Programme
        fields = ["department", "name", "code", "award", "duration_years", "is_active"]


class ProgrammeImportForm(ProgrammeFacultyFormMixin, forms.Form):
    """A tenant-scoped Programme catalogue import request."""

    # The department is authoritative. Keeping faculty optional preserves the
    # established import endpoint for administrators and API clients that
    # submit a department directly; the browser UI still filters departments
    # by its selected faculty.
    faculty = forms.ModelChoiceField(queryset=Faculty.objects.none(), required=False)
    department = forms.ModelChoiceField(queryset=Department.objects.none())
    file = forms.FileField(
        label="Programme list file",
        validators=[FileExtensionValidator(allowed_extensions=["csv", "xlsx", "docx", "pdf", "txt", "jpg", "jpeg", "png", "webp"])],
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.xlsx,.docx,.pdf,.txt,.jpg,.jpeg,.png,.webp"}),
    )

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        if uploaded_file.size > 10 * 1024 * 1024:
            raise forms.ValidationError("Programme lists must be 10 MB or smaller.")
        return uploaded_file


class AccommodationSessionForm(StyledModelForm):
    class Meta:
        model = AccommodationSession
        fields = ["academic_session", "name", "opens_at", "closes_at", "is_open"]
        widgets = {
            "opens_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "closes_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


class HostelForm(StyledModelForm):
    class Meta:
        model = Hostel
        fields = ["name", "code", "gender_restriction", "address", "manager", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["manager"].queryset = User.objects.exclude(role=User.Role.STUDENT)


class HostelBlockForm(StyledModelForm):
    class Meta:
        model = HostelBlock
        fields = ["hostel", "name"]


class HostelRoomForm(StyledModelForm):
    class Meta:
        model = HostelRoom
        fields = ["block", "code", "floor", "is_available"]


class HostelBedForm(StyledModelForm):
    class Meta:
        model = HostelBed
        fields = ["room", "code", "is_available"]


class AccommodationApplicationForm(StyledModelForm):
    class Meta:
        model = AccommodationApplication
        fields = ["accommodation_session", "preferred_hostel", "note"]

    def __init__(self, *args, student=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.student = student
        self.fields["accommodation_session"].queryset = AccommodationSession.objects.filter(is_open=True)
        self.fields["preferred_hostel"].queryset = Hostel.objects.filter(is_active=True)

    def clean_accommodation_session(self):
        session = self.cleaned_data["accommodation_session"]
        if not session.accepting_applications:
            raise forms.ValidationError("This accommodation application period is not currently open.")
        return session


class AccommodationDecisionForm(forms.Form):
    decision_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    bed = forms.ModelChoiceField(queryset=HostelBed.objects.none(), required=False)

    def __init__(self, *args, application=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = application
        beds = HostelBed.objects.filter(
            is_available=True,
            room__is_available=True,
            room__block__hostel__is_active=True,
        ).select_related("room__block__hostel")
        if application and application.preferred_hostel_id:
            beds = beds.filter(room__block__hostel=application.preferred_hostel)
        self.fields["bed"].queryset = beds.exclude(allocations__status="active")
        for field in self.fields.values():
            field.widget.attrs["class"] = f'{field.widget.attrs.get("class", "")} input-field'.strip()

    def clean_bed(self):
        bed = self.cleaned_data.get("bed")
        if bed and bed.has_active_allocation:
            raise forms.ValidationError("That bed is no longer available.")
        return bed


class CourseResultForm(StyledModelForm):
    class Meta:
        model = CourseResult
        fields = [
            "student", "course", "session", "semester", "ca_score", "test_score",
            "other_assessment_score", "exam_score", "assessment_breakdown", "score",
        ]
        widgets = {
            "ca_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "test_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "other_assessment_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "exam_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "assessment_breakdown": forms.Textarea(attrs={"rows": 2, "placeholder": '{"assignment": 10, "practical": 5}'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        courses = Course.objects.none()
        if user is not None:
            courses = Course.objects.filter(
                Q(lecturer=user) | Q(lecturer_registrations__lecturer=user)
            ).distinct()
        self.fields["course"].queryset = courses
        self.fields["session"].queryset = AcademicSession.objects.all()
        self.fields["student"].queryset = User.objects.filter(role=User.Role.STUDENT)
        self.fields["semester"].required = False
        self.fields["score"].required = False
        for field_name in (
            "ca_score", "test_score", "other_assessment_score", "exam_score",
        ):
            self.fields[field_name].required = False

    def clean(self):
        cleaned = super().clean()
        student, course, session = cleaned.get("student"), cleaned.get("course"), cleaned.get("session")
        if course and not cleaned.get("semester"):
            cleaned["semester"] = course.semester
        components = sum((cleaned.get(name) or Decimal("0.00")) for name in (
            "ca_score", "test_score", "other_assessment_score", "exam_score",
        ))
        breakdown = cleaned.get("assessment_breakdown") or {}
        if not isinstance(breakdown, dict):
            self.add_error("assessment_breakdown", "Enter assessment marks as a JSON object.")
        else:
            try:
                components += sum(Decimal(str(value)) for value in breakdown.values())
            except (InvalidOperation, TypeError, ValueError):
                self.add_error("assessment_breakdown", "Every additional assessment mark must be a number.")
            else:
                if components:
                    cleaned["score"] = components
                if cleaned.get("score") is None:
                    self.add_error("score", "Enter a total score or at least one assessment score.")
                elif not Decimal("0") <= cleaned["score"] <= Decimal("100"):
                    self.add_error("score", "The combined result score must be between 0 and 100.")
        if student and course and session and not StudentCourseRegistration.objects.filter(
            student=student, course=course, session=session,
        ).exists():
            self.add_error("student", "The student is not registered for this course in the selected session.")
        return cleaned


class ResultImportForm(forms.Form):
    course = forms.ModelChoiceField(queryset=Course.objects.none())
    session = forms.ModelChoiceField(queryset=AcademicSession.objects.all())
    semester = forms.ChoiceField(choices=Course.Semester.choices, required=False)
    file = forms.FileField(
        validators=[FileExtensionValidator(allowed_extensions=["csv", "xlsx", "pdf", "docx", "txt"])],
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.xlsx,.pdf,.docx,.txt"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["course"].queryset = Course.objects.filter(
                Q(lecturer=user) | Q(lecturer_registrations__lecturer=user)
            ).distinct()
        for field in self.fields.values():
            field.widget.attrs["class"] = f'{field.widget.attrs.get("class", "")} input-field'.strip()

    def clean(self):
        cleaned = super().clean()
        course = cleaned.get("course")
        if course and not cleaned.get("semester"):
            cleaned["semester"] = course.semester
        if course and cleaned.get("semester") and course.semester != cleaned["semester"]:
            self.add_error("semester", "Choose the semester assigned to this course.")
        uploaded_file = cleaned.get("file")
        if uploaded_file and uploaded_file.size > 10 * 1024 * 1024:
            self.add_error("file", "Result files must be 10 MB or smaller.")
        return cleaned


class ResultReviewForm(forms.Form):
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class ResultReportFilterForm(forms.Form):
    session = forms.ModelChoiceField(queryset=AcademicSession.objects.all(), required=False)
    semester = forms.ChoiceField(choices=SEMESTER_FILTER_CHOICES, required=False)
    course = forms.ModelChoiceField(queryset=Course.objects.none(), required=False)
    student = forms.CharField(required=False, label="Student search")

    def __init__(self, *args, department=None, **kwargs):
        super().__init__(*args, **kwargs)
        if department is not None:
            self.fields["course"].queryset = Course.objects.filter(department=department)
        for field in self.fields.values():
            field.widget.attrs["class"] = f'{field.widget.attrs.get("class", "")} input-field'.strip()
        self.fields["student"].widget.attrs["placeholder"] = "Name, matric number, or email"


class DepartmentalAssociationForm(StyledModelForm):
    class Meta:
        model = DepartmentalAssociation
        fields = ["name"]

    def __init__(self, *args, department=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.department = department

    def clean_name(self):
        name = self.cleaned_data["name"]
        if self.department and DepartmentalAssociation.objects.filter(department=self.department, name__iexact=name).exists():
            raise forms.ValidationError("This department already has an association with that name.")
        return name


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


class DepartmentalAdditionalDocumentForm(forms.Form):
    additional_documents = MultipleFileField(
        widget=MultipleFileInput(attrs={"class": "input-field"}),
    )


class DepartmentalSearchForm(forms.Form):
    student_id = forms.CharField(required=False, label="Student ID")
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    level = forms.ChoiceField(choices=LEVEL_FILTER_CHOICES, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.all()
        for field in self.fields.values():
            field.widget.attrs["class"] = "input-field"
