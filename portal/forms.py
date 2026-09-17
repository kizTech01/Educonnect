from decimal import Decimal
from datetime import date
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
    Subscription,
    SubscriptionPlan,
    SubscriptionPlanDuration,
    SubscriptionPaymentGateway,
    ScreeningIntegration,
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
            "max_students", "max_staff", "max_storage_mb", "features", "is_active",
        ]
        widgets = {
            "price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "features": forms.Textarea(attrs={"rows": 4, "placeholder": '["Feature one", "Feature two"]'}),
        }

    def clean_features(self):
        features = self.cleaned_data["features"]
        if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
            raise forms.ValidationError("Features must be a JSON array of text values.")
        return features

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


class SubscriptionAssignmentForm(StyledModelForm):
    class Meta:
        model = Subscription
        fields = ["plan", "start_date", "end_date", "status", "amount"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }


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
        fields = ["department", "file"]

    def save(self, commit=True):
        handbook = super().save(commit=False)
        handbook.title = f"{handbook.department.name} Handbook"
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
            "level",
            "phone_number",
        ]

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


class DepartmentCoursePaymentGatewayForm(StyledModelForm):
    class Meta:
        model = DepartmentCoursePaymentGateway
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
