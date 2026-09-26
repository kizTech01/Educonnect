from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import FileExtensionValidator
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


LEVEL_CHOICES = [
    ("100", "100"),
    ("200", "200"),
    ("300", "300"),
    ("400", "400"),
    ("500", "500"),
]


_current_institution = ContextVar("current_institution", default=None)


def set_current_institution(institution):
    """Set the request tenant used by tenant-aware querysets."""
    return _current_institution.set(institution)


def reset_current_institution(token):
    _current_institution.reset(token)


def get_current_institution():
    return _current_institution.get()


def get_default_institution():
    """Compatibility tenant for data created outside a resolved portal host."""
    institution, _ = Institution.objects.get_or_create(
        institution_code="EDUCONNECT-LEGACY",
        defaults={
            "name": "Educonnect Legacy Institution",
            "institution_type": Institution.Type.OTHER,
            "email": "support@educonnect.local",
            "subdomain": "legacy",
            "status": Institution.Status.ACTIVE,
        },
    )
    return institution


def generate_api_secret():
    return uuid4().hex


class Institution(models.Model):
    """A customer organisation hosted by the shared Educonnect application."""

    class Type(models.TextChoices):
        UNIVERSITY = "university", "University"
        POLYTECHNIC = "polytechnic", "Polytechnic"
        COLLEGE = "college", "College"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        PENDING = "pending", "Pending"
        EXPIRED = "expired", "Expired"
        ARCHIVED = "archived", "Archived"

    name = models.CharField(max_length=200)
    institution_code = models.CharField(max_length=32, unique=True)
    institution_type = models.CharField(max_length=20, choices=Type.choices, default=Type.UNIVERSITY)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, default="Nigeria")
    timezone = models.CharField(max_length=64, default="Africa/Lagos", blank=True)
    academic_configuration = models.JSONField(default=dict, blank=True)
    logo = models.FileField(upload_to="institutions/%Y/%m/", blank=True)
    subdomain = models.SlugField(max_length=63, unique=True)
    custom_domain = models.CharField(max_length=253, blank=True, unique=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    @property
    def current_subscription(self):
        return self.subscriptions.order_by("-end_date", "-created_at").first()

    @property
    def is_operational(self):
        subscription = self.current_subscription
        return self.status == self.Status.ACTIVE and bool(subscription and subscription.allows_access)

    def __str__(self):
        return self.name


class TenantQuerySet(models.QuerySet):
    def for_institution(self, institution):
        return self.filter(institution=institution)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Filters tenant-owned records automatically during a portal request.

    Platform jobs and Django admin run without a request tenant and deliberately
    see all records; they must still apply explicit institution filters.
    """

    def get_queryset(self):
        queryset = super().get_queryset()
        institution = get_current_institution()
        return queryset.filter(institution=institution) if institution else queryset


class TenantUserManager(UserManager):
    """Django's UserManager API plus the same request-tenant filtering."""

    def get_queryset(self):
        queryset = super().get_queryset()
        institution = get_current_institution()
        return queryset.filter(institution=institution) if institution else queryset

    def for_institution(self, institution):
        return self.get_queryset().filter(institution=institution)


class TimeStampedModel(models.Model):
    """Base for data owned by exactly one institution."""
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        null=True,
        blank=True,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        current = get_current_institution()
        if current:
            if self.institution_id and self.institution_id != current.pk:
                raise ValidationError("Cross-institution data writes are not permitted.")
            self.institution = current
        elif not self.institution_id:
            self.institution = get_default_institution()
        super().save(*args, **kwargs)


class InstitutionProfile(TimeStampedModel):
    """The administrator-managed identity shown within one institution portal."""

    name = models.CharField(max_length=200, default="Educonnect")
    logo = models.FileField(
        upload_to="institution/%Y/%m/", blank=True,
        validators=[FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "svg", "webp"])],
    )
    website = models.URLField(blank=True)
    email = models.EmailField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["institution"],
                name="unique_institution_profile",
            ),
        ]

    def save(self, *args, **kwargs):
        # Each tenant has one profile.  Retain the original singleton-like
        # editing behaviour, but scope it to the tenant instead of allowing a
        # second institution to overwrite the first institution's profile.
        if not self.pk and self.institution_id:
            existing = InstitutionProfile.all_objects.filter(
                institution_id=self.institution_id
            ).first()
            if existing:
                self.pk = existing.pk
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Faculty(TimeStampedModel):
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=20)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "code"], name="unique_institution_faculty_code"),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Department(TimeStampedModel):
    faculty = models.ForeignKey(Faculty, on_delete=models.PROTECT, related_name="departments")
    head_of_department = models.OneToOneField(
        "User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="headed_department",
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=20)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "code"], name="unique_institution_department_code"),
        ]

    def save(self, *args, **kwargs):
        # Legacy integrations that create a department directly still receive a
        # valid faculty; the admin workflow always asks for a real faculty.
        if not self.faculty_id:
            faculty, _ = Faculty.objects.get_or_create(
                code="UNASSIGNED",
                defaults={"name": "Unassigned Faculty", "description": "Departments awaiting faculty assignment."},
            )
            self.faculty = faculty
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.code})"


class DepartmentLecturerUpload(TimeStampedModel):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="lecturer_uploads")
    file = models.FileField(upload_to="department_uploads/lecturers/%Y/%m/")
    uploaded_by = models.ForeignKey("User", on_delete=models.SET_NULL, null=True, related_name="lecturer_uploads")
    processing_summary = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class CourseAllocationUpload(TimeStampedModel):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="course_allocation_uploads")
    file = models.FileField(upload_to="department_uploads/course_allocations/%Y/%m/")
    uploaded_by = models.ForeignKey("User", on_delete=models.SET_NULL, null=True, related_name="course_allocation_uploads")
    processing_summary = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class User(AbstractUser):
    class Role(models.TextChoices):
        # ``admin`` deliberately remains the persisted legacy value.  It is
        # now the Institution Admin role, so existing accounts keep working.
        ADMIN = "admin", "Institution Admin"
        STUDENT = "student", "Student"
        LECTURER = "lecturer", "Lecturer"
        MIS = "mis", "MIS"
        BURSARY = "bursary", "Bursary"
        ADMISSION_OFFICER = "admission_officer", "Admission Officer"
        ACADEMIC_PLANNING = "academic_planning", "Academic Planning"
        HOD = "hod", "HOD"
        EXAM_OFFICER = "exam_officer", "Exam Officer"
        SENATE_MEMBER = "senate_member", "Senate Member"
        PARENT_GUARDIAN = "parent_guardian", "Parent/Guardian"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STUDENT)
    institution = models.ForeignKey(
        Institution,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="users",
    )
    department = models.ForeignKey(
        Department,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="users",
    )
    programme = models.ForeignKey(
        "Programme",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="students",
    )
    curriculum = models.ForeignKey(
        "Curriculum",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="students",
        help_text="The curriculum assigned when this student joined their department.",
    )
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, blank=True)
    id_number = models.CharField(max_length=30, null=True, blank=True)
    phone_number = models.CharField(max_length=30, blank=True)
    passport_photo = models.FileField(
        upload_to="passports/%Y/%m/",
        blank=True,
        validators=[FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png"])],
    )
    is_approved = models.BooleanField(default=True)
    email_class_reminders = models.BooleanField(default=True)
    browser_alerts_enabled = models.BooleanField(default=False)
    class_reminder_alerts_enabled = models.BooleanField(default=False)

    objects = TenantUserManager()
    all_objects = UserManager()

    class Meta:
        ordering = ["username"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "id_number"],
                name="unique_institution_user_id_number",
            ),
        ]

    def save(self, *args, **kwargs):
        current = get_current_institution()
        if current:
            if self.institution_id and self.institution_id != current.pk:
                raise ValidationError("A user cannot be moved between institutions in a tenant request.")
            self.institution = current
        elif not self.institution_id and not self.is_superuser:
            self.institution = get_default_institution()
        if self.role != self.Role.LECTURER:
            self.is_approved = True
        # Freeze a new student's course version at account creation. A later
        # handbook upload therefore affects only students who join afterwards.
        if self._state.adding and self.role == self.Role.STUDENT and self.department_id and not self.curriculum_id:
            self.curriculum = (
                curriculum_for_programme(self.programme)
                if self.programme_id else curriculum_for_department(self.department)
            )
        super().save(*args, **kwargs)

    def clean(self):
        if self.programme_id:
            if self.role != self.Role.STUDENT:
                raise ValidationError({"programme": "Only student accounts can be assigned to a programme."})
            if self.department_id and self.programme.department_id != self.department_id:
                raise ValidationError({"programme": "The programme must belong to the student's department."})
            if self.institution_id and self.programme.institution_id != self.institution_id:
                raise ValidationError({"programme": "The programme must belong to the same institution."})

    @property
    def full_name(self):
        return self.get_full_name() or self.username

    def __str__(self):
        return self.full_name

    def active_roles(self):
        """Return roles assigned to this user, including the legacy primary role.

        The primary field is retained for login and old integrations.  New
        assignments support staff with more than one institutional duty.
        """
        roles = {self.role}
        if self.pk:
            roles.update(
                self.role_assignments.filter(is_active=True).values_list("role", flat=True)
            )
        return roles

    def has_role(self, *roles):
        return self.is_superuser or bool(set(roles).intersection(self.active_roles()))


class RoleAssignment(TimeStampedModel):
    """An auditable, tenant-owned role granted to an existing user."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="role_assignments")
    role = models.CharField(max_length=32, choices=User.Role.choices)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL)
    assigned_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="roles_assigned")
    is_active = models.BooleanField(default=True)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["institution", "user", "role", "department"], name="unique_tenant_user_role"),
        ]
        indexes = [models.Index(fields=["institution", "role", "is_active"])]

    def clean(self):
        if self.user_id and self.institution_id and self.user.institution_id != self.institution_id:
            raise ValidationError("A role can only be assigned within the user's institution.")
        if self.department_id and self.institution_id and self.department.institution_id != self.institution_id:
            raise ValidationError("A role department must belong to the same institution.")

    def save(self, *args, **kwargs):
        if self.user_id and not self.institution_id:
            self.institution = self.user.institution
        self.full_clean()
        return super().save(*args, **kwargs)


class GuardianRelationship(TimeStampedModel):
    """Explicit, revocable guardian access to a student record."""

    guardian = models.ForeignKey(User, on_delete=models.CASCADE, related_name="guarded_students")
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="guardians")
    relationship = models.CharField(max_length=50, default="Guardian")
    can_view_results = models.BooleanField(default=True)
    can_view_finance = models.BooleanField(default=True)
    can_view_attendance = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["institution", "guardian", "student"], name="unique_guardian_student_link")]

    def clean(self):
        if self.guardian_id and self.student_id and self.guardian.institution_id != self.student.institution_id:
            raise ValidationError("Guardians and students must belong to the same institution.")
        if self.student_id and self.student.role != User.Role.STUDENT:
            raise ValidationError("Guardian access can only be linked to a student.")

    def save(self, *args, **kwargs):
        if self.student_id and not self.institution_id:
            self.institution = self.student.institution
        self.full_clean()
        return super().save(*args, **kwargs)


class AIAutomationJob(TimeStampedModel):
    """Reviewable bulk operation proposal; analysis never writes target data."""

    class Status(models.TextChoices):
        ANALYZED = "analyzed", "Analyzed"
        APPROVED = "approved", "Approved"
        EXECUTED = "executed", "Executed"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    file = models.FileField(upload_to="ai_automation/%Y/%m/")
    command = models.TextField()
    action = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ANALYZED)
    analysis = models.JSONField(default=dict, blank=True)
    proposed_changes = models.JSONField(default=list, blank=True)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="ai_automation_requests")
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="ai_automation_approvals")
    approved_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    execution_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["institution", "status", "created_at"])]


class SubscriptionPlan(models.Model):
    """Database-managed commercial plans; no prices belong in views or templates."""

    class BillingPeriod(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"
        CUSTOM = "custom", "Custom"

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    billing_period = models.CharField(max_length=20, choices=BillingPeriod.choices)
    custom_duration_days = models.PositiveIntegerField(null=True, blank=True)
    max_students = models.PositiveIntegerField(null=True, blank=True)
    max_staff = models.PositiveIntegerField(null=True, blank=True)
    max_storage_mb = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_trial = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["price", "name"]

    @property
    def duration_days(self):
        return {
            self.BillingPeriod.MONTHLY: 30,
            self.BillingPeriod.QUARTERLY: 90,
            self.BillingPeriod.YEARLY: 365,
        }.get(self.billing_period, self.custom_duration_days or 0)

    @property
    def duration_label(self):
        return {
            183: "6 months",
            365: "1 year",
            730: "2 years",
            1825: "5 years",
        }.get(self.duration_days, f"{self.duration_days} days")

    def clean(self):
        if self.price < 0:
            raise ValidationError({"price": "A plan price cannot be negative."})
        if self.billing_period == self.BillingPeriod.CUSTOM and not self.custom_duration_days:
            raise ValidationError({"custom_duration_days": "Custom plans need a duration."})

    def __str__(self):
        return self.name


class SubscriptionPlanDuration(models.Model):
    """A purchasable duration and price for a platform subscription plan."""

    DURATION_CHOICES = (
        (183, "6 months"),
        (365, "1 year"),
        (730, "2 years"),
        (1825, "5 years"),
    )

    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.CASCADE, related_name="durations")
    duration_days = models.PositiveIntegerField(choices=DURATION_CHOICES)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["plan__name", "duration_days"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "duration_days"],
                name="unique_subscription_plan_duration",
            ),
        ]

    @property
    def duration_label(self):
        return dict(self.DURATION_CHOICES).get(self.duration_days, f"{self.duration_days} days")

    def __str__(self):
        return f"{self.plan.name} — {self.duration_label}"


class SubscriptionPaymentGateway(models.Model):
    """The platform-owned gateway used for institution subscription payments."""

    slug = models.SlugField(max_length=30, unique=True, default="subscription")
    paystack_public_key = models.CharField(max_length=255, blank=True)
    paystack_secret_key = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_configured(self):
        return bool(self.is_active and self.paystack_public_key and self.paystack_secret_key)

    def __str__(self):
        return "Subscription payment gateway"


class Subscription(models.Model):
    class Status(models.TextChoices):
        TRIAL = "trial", "Trial"
        ACTIVE = "active", "Active"
        EXPIRING_SOON = "expiring_soon", "Expiring soon"
        EXPIRED = "expired", "Expired"
        SUSPENDED = "suspended", "Suspended"
        CANCELLED = "cancelled", "Cancelled"

    institution = models.ForeignKey(Institution, on_delete=models.PROTECT, related_name="subscriptions")
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.PROTECT, related_name="subscriptions")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIAL)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    payment_reference = models.CharField(max_length=100, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-end_date", "-created_at"]
        indexes = [models.Index(fields=["institution", "status", "end_date"])]

    @property
    def computed_status(self):
        if self.status in {self.Status.SUSPENDED, self.Status.CANCELLED}:
            return self.status
        today = timezone.localdate()
        if self.end_date < today:
            return self.Status.EXPIRED
        if self.end_date <= today + timedelta(days=60):
            return self.Status.EXPIRING_SOON
        return self.Status.ACTIVE

    @property
    def allows_access(self):
        return self.computed_status in {self.Status.TRIAL, self.Status.ACTIVE, self.Status.EXPIRING_SOON}

    def refresh_status(self, *, commit=True):
        computed = self.computed_status
        if computed != self.status:
            self.status = computed
            if commit:
                self.save(update_fields=["status", "updated_at"])
        return computed

    def clean(self):
        if self.end_date < self.start_date:
            raise ValidationError({"end_date": "The end date cannot be before the start date."})

    def __str__(self):
        return f"{self.institution} — {self.plan} ({self.end_date})"


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Successful"
        FAILED = "failed", "Failed"
        ABANDONED = "abandoned", "Abandoned"

    institution = models.ForeignKey(Institution, on_delete=models.PROTECT, related_name="subscription_payments")
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name="payments")
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.PROTECT, related_name="payments")
    duration_days = models.PositiveIntegerField(null=True, blank=True)
    reference = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="NGN")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    gateway_response = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    receipt_number = models.CharField(max_length=50, blank=True, unique=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["institution", "status", "created_at"])]

    def __str__(self):
        return f"{self.reference} ({self.status})"


class SubscriptionEvent(models.Model):
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="events")
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    event_type = models.CharField(max_length=50)
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AuditLog(models.Model):
    institution = models.ForeignKey(Institution, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_logs")
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_logs")
    action = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["institution", "action", "created_at"])]


class ScreeningIntegration(models.Model):
    """Tenant-specific configuration and shared secret for an external screening service."""

    institution = models.OneToOneField(Institution, on_delete=models.CASCADE, related_name="screening_integration")
    is_open = models.BooleanField(default=False)
    admission_session = models.ForeignKey("AcademicSession", null=True, blank=True, on_delete=models.SET_NULL)
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    application_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    available_programmes = models.JSONField(default=list, blank=True)
    admission_requirements = models.JSONField(default=dict, blank=True)
    required_documents = models.JSONField(default=list, blank=True)
    applicant_categories = models.JSONField(default=list, blank=True)
    utme_de_settings = models.JSONField(default=dict, blank=True)
    workflow_settings = models.JSONField(default=dict, blank=True)
    notification_settings = models.JSONField(default=dict, blank=True)
    api_secret = models.CharField(max_length=64, default=generate_api_secret, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "screening integration"

    def clean(self):
        if (
            self.admission_session_id
            and self.institution_id
            and self.admission_session.institution_id != self.institution_id
        ):
            raise ValidationError({"admission_session": "Choose an academic session from this institution."})

    def rotate_secret(self):
        self.api_secret = generate_api_secret()
        self.save(update_fields=["api_secret", "updated_at"])
        return self.api_secret

    @property
    def is_accepting_applications(self):
        now = timezone.now()
        return bool(
            self.is_open
            and (self.opens_at is None or self.opens_at <= now)
            and (self.closes_at is None or now <= self.closes_at)
        )


class ScreeningApplication(models.Model):
    """A minimal integration ledger; the full applicant workflow remains external."""

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        APPROVED = "approved", "Approved"
        ADMITTED = "admitted", "Admitted"
        TRANSFERRED = "transferred", "Transferred"
        REJECTED = "rejected", "Rejected"

    institution = models.ForeignKey(Institution, on_delete=models.PROTECT, related_name="screening_applications")
    external_application_id = models.CharField(max_length=100)
    applicant_reference = models.CharField(max_length=100, blank=True)
    jamb_number = models.CharField(max_length=30, blank=True)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    academic_session = models.ForeignKey("AcademicSession", null=True, blank=True, on_delete=models.PROTECT)
    programme = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    payload = models.JSONField(default=dict, blank=True)
    admitted_at = models.DateTimeField(null=True, blank=True)
    student = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="screening_admissions")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "external_application_id"], name="unique_screening_application_per_institution"),
        ]
        indexes = [models.Index(fields=["institution", "status", "external_application_id"])]

    def __str__(self):
        return f"{self.institution.institution_code}: {self.external_application_id}"


class SubscriptionNotification(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="notifications")
    days_before_expiry = models.IntegerField()
    channel = models.CharField(max_length=20, default="email")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SENT)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "days_before_expiry", "channel"],
                name="unique_subscription_notification",
            )
        ]


class Course(TimeStampedModel):
    class Semester(models.TextChoices):
        FIRST = "first", "First Semester"
        SECOND = "second", "Second Semester"

    class Day(models.TextChoices):
        MONDAY = "Monday", "Monday"
        TUESDAY = "Tuesday", "Tuesday"
        WEDNESDAY = "Wednesday", "Wednesday"
        THURSDAY = "Thursday", "Thursday"
        FRIDAY = "Friday", "Friday"
        SATURDAY = "Saturday", "Saturday"
        SUNDAY = "Sunday", "Sunday"

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="courses")
    academic_session = models.ForeignKey(
        "AcademicSession",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="courses",
    )
    curriculum = models.ForeignKey(
        "Curriculum",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="courses",
    )
    # ``curriculum`` is retained as the source/legacy curriculum for backwards
    # compatibility.  A course can also be included in more than one programme
    # curriculum, which is what makes a genuinely shared course possible.
    curricula = models.ManyToManyField("Curriculum", blank=True, related_name="catalogue_courses")
    programmes = models.ManyToManyField("Programme", blank=True, related_name="courses")
    lecturer = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="teaching_courses",
        limit_choices_to={"role": User.Role.LECTURER},
    )
    code = models.CharField(max_length=20)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES)
    semester = models.CharField(max_length=20, choices=Semester.choices, default=Semester.FIRST)
    credit_units = models.PositiveSmallIntegerField(default=2)
    schedule_day = models.CharField(max_length=15, choices=Day.choices, blank=True)
    schedule_time = models.TimeField(null=True, blank=True)
    venue = models.CharField(max_length=120, blank=True)
    file = models.FileField(upload_to="courses/%Y/%m/", blank=True)
    is_free = models.BooleanField(default=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["department", "code", "curriculum"],
                name="unique_department_curriculum_course",
            )
        ]

    def clean(self):
        if self.amount and self.amount > 0:
            self.is_free = False
        elif self.is_free:
            self.amount = Decimal("0.00")
        elif self.amount <= 0:
            raise ValidationError({"amount": "Paid courses must have an amount greater than zero."})

    def save(self, *args, **kwargs):
        if not self.academic_session_id:
            self.academic_session = AcademicSession.objects.filter(is_current=True).first()
        if not self.curriculum_id and self.department_id:
            session = self.academic_session or AcademicSession.objects.filter(is_current=True).first()
            if session:
                self.curriculum = curriculum_for_department(self.department, session)
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} - {self.title}"


class StudentCourseRegistration(TimeStampedModel):
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="student_registrations",
        limit_choices_to={"role": User.Role.STUDENT},
    )
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="student_registrations")
    session = models.ForeignKey(
        "AcademicSession",
        on_delete=models.PROTECT,
        related_name="student_course_registrations",
        null=True,
        blank=True,
    )
    registered_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="student_course_actions",
    )

    class Meta:
        ordering = ["course__code"]
        constraints = [
            models.UniqueConstraint(fields=["student", "course", "session"], name="unique_student_course_registration")
        ]
        indexes = [
            models.Index(fields=["student", "course", "session"]),
        ]

    def save(self, *args, **kwargs):
        if not self.session_id:
            self.session = AcademicSession.objects.filter(is_current=True).first()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student.full_name} - {self.course.code}"


class CourseResult(TimeStampedModel):
    """A reviewable result record for one registered student and course.

    Results are not published merely because a lecturer enters a score.  They
    progress from draft to submitted, then require an Exam Officer approval;
    students only see published records.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted for review"
        APPROVED = "approved", "Approved"
        PUBLISHED = "published", "Published"
        RETURNED = "returned", "Returned for correction"

    student = models.ForeignKey(User, on_delete=models.PROTECT, related_name="course_results")
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="results")
    session = models.ForeignKey("AcademicSession", on_delete=models.PROTECT, related_name="course_results")
    semester = models.CharField(max_length=20, choices=Course.Semester.choices, default=Course.Semester.FIRST)
    # Keep the original total for compatibility while preserving the assessment
    # components lecturers use to calculate it.
    ca_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    test_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    exam_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    other_assessment_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    assessment_breakdown = models.JSONField(default=dict, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2)
    grade = models.CharField(max_length=2, blank=True)
    grade_point = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.00"))
    credit_units = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="results_submitted")
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="results_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["course__code", "student__last_name", "student__first_name"]
        constraints = [
            models.UniqueConstraint(fields=["student", "course", "session", "semester"], name="unique_student_course_result"),
            models.CheckConstraint(condition=models.Q(score__gte=0) & models.Q(score__lte=100), name="course_result_score_range"),
        ]
        indexes = [models.Index(fields=["institution", "status", "session"])]

    @staticmethod
    def grade_for_score(score):
        value = Decimal(str(score))
        if value >= 70:
            return "A", Decimal("5.00")
        if value >= 60:
            return "B", Decimal("4.00")
        if value >= 50:
            return "C", Decimal("3.00")
        if value >= 45:
            return "D", Decimal("2.00")
        if value >= 40:
            return "E", Decimal("1.00")
        return "F", Decimal("0.00")

    def clean(self):
        if self.student_id and self.student.role != User.Role.STUDENT:
            raise ValidationError({"student": "Results can only be recorded for students."})
        if self.course_id and self.session_id and self.course.academic_session_id and self.course.academic_session_id != self.session_id:
            raise ValidationError({"session": "The selected session does not match this course."})
        if self.course_id and self.semester and self.course.semester != self.semester:
            raise ValidationError({"semester": "The selected semester does not match this course."})
        for field in ("student", "course", "session"):
            item = getattr(self, field, None)
            if item is not None and self.institution_id and getattr(item, "institution_id", self.institution_id) != self.institution_id:
                raise ValidationError("Result records cannot cross institutions.")

    def save(self, *args, **kwargs):
        if self.course_id:
            self.credit_units = self.course.credit_units
        if self.score is not None:
            self.grade, self.grade_point = self.grade_for_score(self.score)
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student.full_name} / {self.course.code} / {self.session.name}"


class CoursePayment(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"

    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="course_payments",
        limit_choices_to={"role": User.Role.STUDENT},
    )
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="payments")
    session = models.ForeignKey(
        "AcademicSession",
        on_delete=models.PROTECT,
        related_name="course_payments",
        null=True,
        blank=True,
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    enrollment_sequence = models.PositiveIntegerField(default=1)
    is_active_for_registration = models.BooleanField(default=True)
    paystack_reference = models.CharField(max_length=64, blank=True, default="")
    paystack_public_key_used = models.CharField(max_length=255, blank=True)
    paystack_secret_key_used = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course", "session", "enrollment_sequence"],
                name="unique_course_payment",
            ),
        ]

    def clean(self):
        if self.course.is_free:
            self.amount = Decimal("0.00")
            self.status = self.Status.PAID
        elif self.amount <= 0:
            raise ValidationError({"amount": "Payment amount must be greater than zero."})

    def rotate_paystack_reference(self):
        self.paystack_reference = f"COURSE-{timezone.now():%Y%m%d}-{uuid4().hex[:10].upper()}"

    @property
    def is_paid(self):
        return self.status == self.Status.PAID

    @property
    def can_register(self):
        return self.course.is_free or self.is_paid

    def save(self, *args, **kwargs):
        if not self.session_id:
            current_session = AcademicSession.objects.filter(is_current=True).first()
            if current_session:
                self.session = current_session
        if self.course.is_free:
            self.amount = Decimal("0.00")
            self.status = self.Status.PAID
        if not self.paystack_reference:
            self.rotate_paystack_reference()
        if self.status == self.Status.PAID and not self.paid_at:
            self.paid_at = timezone.now()
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student.full_name} - {self.course.code}"


class CourseStudentGroup(TimeStampedModel):
    class GroupingMethod(models.TextChoices):
        DEPARTMENT = "department", "By department"
        RANDOM = "random", "Random"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="student_groups")
    lecturer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="course_student_groups")
    session = models.ForeignKey("AcademicSession", on_delete=models.PROTECT, related_name="course_student_groups")
    name = models.CharField(max_length=80)
    grouping_method = models.CharField(max_length=20, choices=GroupingMethod.choices)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True, related_name="course_student_groups")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["course", "session", "name"], name="unique_course_student_group_name"),
        ]

    def __str__(self):
        return f"{self.course.code} - {self.name}"


class CourseStudentGroupMembership(TimeStampedModel):
    group = models.ForeignKey(CourseStudentGroup, on_delete=models.CASCADE, related_name="memberships")
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="course_group_memberships")

    class Meta:
        ordering = ["student__last_name", "student__first_name", "student__username"]
        constraints = [
            models.UniqueConstraint(fields=["group", "student"], name="unique_course_group_member"),
        ]

    def __str__(self):
        return f"{self.group} - {self.student.full_name}"


class LecturerCourseRegistration(TimeStampedModel):
    lecturer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="lecturer_registrations",
        limit_choices_to={"role": User.Role.LECTURER},
    )
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="lecturer_registrations")

    class Meta:
        ordering = ["course__code"]
        constraints = [
            models.UniqueConstraint(fields=["lecturer", "course"], name="unique_lecturer_course_registration")
        ]

    def __str__(self):
        return f"{self.lecturer.full_name} - {self.course.code}"


class DepartmentDocument(TimeStampedModel):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="%(class)ss")
    title = models.CharField(max_length=200)
    academic_session = models.CharField(max_length=20)
    semester = models.CharField(max_length=20, blank=True)
    description = models.TextField(blank=True)
    file = models.FileField(upload_to="documents/%Y/%m/")

    class Meta:
        abstract = True
        ordering = ["department__name", "title"]

    def __str__(self):
        return self.title


class Timetable(DepartmentDocument):
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES)


class Handbook(DepartmentDocument):
    # A blank programme is a department-wide handbook retained for institutions
    # that do not offer separate programmes.
    programme = models.ForeignKey(
        "Programme", null=True, blank=True, on_delete=models.CASCADE,
        related_name="handbooks",
    )

    def clean(self):
        if self.programme_id and self.department_id and self.programme.department_id != self.department_id:
            raise ValidationError({"programme": "The handbook programme must belong to its department."})


class CourseMaterial(TimeStampedModel):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="materials")
    lecturer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="materials",
        limit_choices_to={"role": User.Role.LECTURER},
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    file = models.FileField(upload_to="materials/%Y/%m/")
    is_free = models.BooleanField(default=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    is_download_enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.is_free:
            self.amount = Decimal("0.00")
        elif self.amount <= 0:
            raise ValidationError("Paid materials must include an amount greater than zero.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.course.code} - {self.title}"


class MaterialAccess(TimeStampedModel):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        PENDING = "pending", "Pending Payment"
        PAID = "paid", "Paid"
        BLOCKED = "blocked", "Blocked"

    material = models.ForeignKey(CourseMaterial, on_delete=models.CASCADE, related_name="access_records")
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="material_access_records",
        limit_choices_to={"role": User.Role.STUDENT},
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    class Meta:
        ordering = ["student__first_name", "student__last_name", "material__title"]
        constraints = [
            models.UniqueConstraint(fields=["material", "student"], name="unique_material_student_access")
        ]

    @property
    def can_download(self):
        return self.material.is_download_enabled and self.status in {
            self.Status.AVAILABLE,
            self.Status.PAID,
        }

    @property
    def amount_due(self):
        if self.material.is_free:
            return Decimal("0.00")
        return self.material.amount

    def __str__(self):
        return f"{self.student.full_name} - {self.material.title}"


class Notification(TimeStampedModel):
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_notifications")
    subject = models.CharField(max_length=180)
    body = models.TextField()
    attachment = models.FileField(upload_to="message_attachments/%Y/%m/", blank=True)
    departments = models.ManyToManyField(Department, blank=True, related_name="notifications")
    level = models.CharField(max_length=20, blank=True)
    courses = models.ManyToManyField(Course, blank=True, related_name="notifications")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.subject


class NotificationAttachment(TimeStampedModel):
    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="message_attachments/%Y/%m/")

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.file.name.rsplit("/", 1)[-1]


class NotificationRecipient(TimeStampedModel):
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="recipients")
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notification_recipients",
        limit_choices_to={"role": User.Role.STUDENT},
    )
    is_read = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["notification", "student"], name="unique_notification_recipient")
        ]
        indexes = [
            models.Index(fields=["student", "is_deleted", "is_read"]),
        ]

    def __str__(self):
        return f"{self.student.full_name} - {self.notification.subject}"


class CoursePaymentGateway(TimeStampedModel):
    slug = models.CharField(max_length=30, default="courses")
    payment_url = models.URLField(blank=True)
    paystack_public_key = models.CharField(max_length=255, blank=True)
    paystack_secret_key = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["slug"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "slug"],
                name="unique_institution_course_gateway_slug",
            ),
        ]

    def __str__(self):
        return "Course Paystack Settings"

    @property
    def is_configured(self):
        return bool(self.paystack_public_key and self.paystack_secret_key)


class DepartmentPaymentGateway(TimeStampedModel):
    department = models.OneToOneField(Department, on_delete=models.CASCADE, related_name="payment_gateway")
    payment_url = models.URLField(blank=True)
    paystack_public_key = models.CharField(max_length=255, blank=True)
    paystack_secret_key = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["department__name"]

    def __str__(self):
        return f"{self.department.name} Paystack Settings"

    @property
    def is_configured(self):
        return bool(self.paystack_public_key and self.paystack_secret_key)


class DepartmentCoursePaymentGateway(TimeStampedModel):
    """Paystack credentials for paid courses belonging to one department."""

    department = models.OneToOneField(
        Department,
        on_delete=models.CASCADE,
        related_name="course_payment_gateway",
    )
    paystack_public_key = models.CharField(max_length=255, blank=True)
    paystack_secret_key = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["department__name"]

    def __str__(self):
        return f"{self.department.name} Course Paystack Settings"

    @property
    def is_configured(self):
        return bool(self.paystack_public_key and self.paystack_secret_key)


class AcademicSession(TimeStampedModel):
    name = models.CharField(max_length=20)
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_current", "-name"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "name"],
                name="unique_institution_academic_session_name",
            ),
        ]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_current:
            AcademicSession.objects.exclude(pk=self.pk).filter(is_current=True).update(is_current=False)

    def __str__(self):
        return self.name


class Curriculum(TimeStampedModel):
    """A department's course version, introduced by a handbook for one session."""

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="curricula")
    programme = models.ForeignKey(
        "Programme", null=True, blank=True, on_delete=models.CASCADE,
        related_name="curricula",
        help_text="Blank for a department-wide legacy curriculum.",
    )
    effective_session = models.ForeignKey(
        AcademicSession,
        on_delete=models.PROTECT,
        related_name="curricula",
    )
    version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["department__name", "-effective_session__name", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["department", "programme", "effective_session", "version"],
                name="unique_programme_curriculum_session_version",
            )
        ]

    def __str__(self):
        owner = self.programme.code if self.programme_id else self.department.code
        return f"{owner} curriculum ({self.effective_session.name}, v{self.version})"


class Programme(TimeStampedModel):
    """A course of study offered by one department.

    Programmes are deliberately tenant-owned instead of using a global choice
    list: the same programme code may legitimately be offered by more than
    one institution.
    """

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="programmes")
    name = models.CharField(max_length=180)
    code = models.CharField(max_length=30)
    award = models.CharField(max_length=80, blank=True)
    duration_years = models.PositiveSmallIntegerField(default=4)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["department__name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "code"],
                name="unique_institution_programme_code",
            ),
        ]

    def clean(self):
        if self.department_id and self.institution_id and self.department.institution_id != self.institution_id:
            raise ValidationError("A programme department must belong to the same institution.")

    def __str__(self):
        return f"{self.name} ({self.code})"


class AccommodationSession(TimeStampedModel):
    """A time-bounded allocation window for one academic session."""

    academic_session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="accommodation_sessions")
    name = models.CharField(max_length=120)
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    is_open = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "academic_session", "name"],
                name="unique_institution_accommodation_session",
            ),
        ]

    @property
    def accepting_applications(self):
        now = timezone.now()
        return self.is_open and (not self.opens_at or self.opens_at <= now) and (not self.closes_at or now <= self.closes_at)

    def clean(self):
        if self.academic_session_id and self.institution_id and self.academic_session.institution_id != self.institution_id:
            raise ValidationError("The academic session must belong to this institution.")
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValidationError({"closes_at": "Closing time must be later than opening time."})

    def __str__(self):
        return self.name


class Hostel(TimeStampedModel):
    class Gender(models.TextChoices):
        ANY = "any", "Any student"
        FEMALE = "female", "Female"
        MALE = "male", "Male"

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30)
    gender_restriction = models.CharField(max_length=12, choices=Gender.choices, default=Gender.ANY)
    address = models.TextField(blank=True)
    manager = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_hostels")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["institution", "code"], name="unique_institution_hostel_code")]

    def clean(self):
        if self.manager_id and self.institution_id and self.manager.institution_id != self.institution_id:
            raise ValidationError("The hostel manager must belong to this institution.")

    def __str__(self):
        return f"{self.name} ({self.code})"


class HostelBlock(TimeStampedModel):
    hostel = models.ForeignKey(Hostel, on_delete=models.CASCADE, related_name="blocks")
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ["hostel__name", "name"]
        constraints = [models.UniqueConstraint(fields=["hostel", "name"], name="unique_hostel_block_name")]

    def clean(self):
        if self.hostel_id and self.institution_id and self.hostel.institution_id != self.institution_id:
            raise ValidationError("The hostel block must belong to this institution.")

    def __str__(self):
        return f"{self.hostel.code} / {self.name}"


class HostelRoom(TimeStampedModel):
    block = models.ForeignKey(HostelBlock, on_delete=models.CASCADE, related_name="rooms")
    code = models.CharField(max_length=30)
    floor = models.CharField(max_length=30, blank=True)
    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ["block__hostel__name", "block__name", "code"]
        constraints = [models.UniqueConstraint(fields=["block", "code"], name="unique_hostel_block_room_code")]

    def clean(self):
        if self.block_id and self.institution_id and self.block.institution_id != self.institution_id:
            raise ValidationError("The room must belong to this institution.")

    def __str__(self):
        return f"{self.block} / {self.code}"


class HostelBed(TimeStampedModel):
    room = models.ForeignKey(HostelRoom, on_delete=models.CASCADE, related_name="beds")
    code = models.CharField(max_length=30)
    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ["room__block__hostel__name", "room__code", "code"]
        constraints = [models.UniqueConstraint(fields=["room", "code"], name="unique_hostel_room_bed_code")]

    def clean(self):
        if self.room_id and self.institution_id and self.room.institution_id != self.institution_id:
            raise ValidationError("The bed must belong to this institution.")

    @property
    def has_active_allocation(self):
        return self.allocations.filter(status=AccommodationAllocation.Status.ACTIVE).exists()

    def __str__(self):
        return f"{self.room} / {self.code}"


class AccommodationApplication(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="accommodation_applications")
    accommodation_session = models.ForeignKey(AccommodationSession, on_delete=models.PROTECT, related_name="applications")
    preferred_hostel = models.ForeignKey(Hostel, null=True, blank=True, on_delete=models.SET_NULL, related_name="applications")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    note = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_accommodation_applications")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["student", "accommodation_session"], name="unique_student_accommodation_session_application")]
        indexes = [models.Index(fields=["institution", "status", "created_at"])]

    def clean(self):
        if self.student_id and self.student.role != User.Role.STUDENT:
            raise ValidationError({"student": "Accommodation applications are only for students."})
        for field in ("student", "accommodation_session", "preferred_hostel"):
            item = getattr(self, field, None)
            if item is not None and self.institution_id and getattr(item, "institution_id", self.institution_id) != self.institution_id:
                raise ValidationError("All accommodation records must belong to the same institution.")

    def __str__(self):
        return f"{self.student.full_name} / {self.accommodation_session}"


class AccommodationAllocation(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled"

    application = models.OneToOneField(AccommodationApplication, on_delete=models.PROTECT, related_name="allocation")
    student = models.ForeignKey(User, on_delete=models.PROTECT, related_name="accommodation_allocations")
    bed = models.ForeignKey(HostelBed, on_delete=models.PROTECT, related_name="allocations")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    allocated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="accommodation_allocations_made")
    allocated_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-allocated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["bed"], condition=models.Q(status="active"), name="unique_active_bed_allocation",
            ),
            models.UniqueConstraint(
                fields=["student", "application"], name="unique_allocation_student_application",
            ),
        ]
        indexes = [models.Index(fields=["institution", "status", "allocated_at"])]

    def clean(self):
        if self.application_id and self.student_id and self.application.student_id != self.student_id:
            raise ValidationError("An allocation student must match its application.")
        for field in ("application", "student", "bed"):
            item = getattr(self, field, None)
            if item is not None and self.institution_id and getattr(item, "institution_id", self.institution_id) != self.institution_id:
                raise ValidationError("All allocation records must belong to the same institution.")
        if self.status == self.Status.ACTIVE and self.bed_id:
            occupied = AccommodationAllocation.objects.filter(bed=self.bed, status=self.Status.ACTIVE).exclude(pk=self.pk)
            if occupied.exists():
                raise ValidationError({"bed": "This bed already has an active allocation."})

    def cancel(self, *, reason=""):
        self.status = self.Status.CANCELLED
        self.cancelled_at = timezone.now()
        self.cancellation_reason = reason
        self.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])

    def __str__(self):
        return f"{self.student.full_name} / {self.bed}"


def curriculum_for_department(department, session=None):
    """Return the curriculum in force for a department in a given session.

    A new academic session does not create a curriculum by itself.  The most
    recent handbook curriculum remains in force until that department uploads
    a replacement handbook.
    """
    if not department:
        return None
    session = session or AcademicSession.objects.filter(is_current=True).first()
    if not session:
        return None

    curriculum = Curriculum.objects.filter(
        department=department,
        effective_session=session,
    ).order_by("-version", "-created_at").first()
    if curriculum:
        return curriculum

    curriculum = Curriculum.objects.filter(
        department=department,
        effective_session__name__lte=session.name,
    ).order_by("-effective_session__name", "-version", "-created_at").first()
    if curriculum:
        return curriculum

    # This is the one-time compatibility path for a department that already
    # has courses but has not uploaded its first handbook in the portal.
    curriculum, _ = Curriculum.objects.get_or_create(
        department=department,
        effective_session=session,
    )
    Course.objects.filter(department=department, curriculum__isnull=True).update(curriculum=curriculum)
    return curriculum


def curriculum_for_programme(programme, session=None):
    """Return the course version in force for one programme.

    Programme students never fall back to another programme's courses.  An
    empty initial curriculum is created when necessary so the account can be
    created before its first handbook is uploaded.
    """
    if not programme:
        return None
    session = session or AcademicSession.objects.filter(is_current=True).first()
    if not session:
        return None
    curriculum = Curriculum.objects.filter(
        programme=programme,
        effective_session=session,
    ).order_by("-version", "-created_at").first()
    if curriculum:
        return curriculum
    curriculum = Curriculum.objects.filter(
        programme=programme,
        effective_session__name__lte=session.name,
    ).order_by("-effective_session__name", "-version", "-created_at").first()
    if curriculum:
        return curriculum
    return Curriculum.objects.create(
        department=programme.department,
        programme=programme,
        effective_session=session,
    )


class DepartmentalAssociation(TimeStampedModel):
    # Constant associations are shared (department is null); custom
    # associations belong to exactly one department.
    department = models.ForeignKey(
        Department,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="departmental_associations",
    )
    name = models.CharField(max_length=120)
    code = models.SlugField(max_length=50)
    is_constant = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "name"]
        constraints = [
            models.UniqueConstraint(fields=["department", "name"], name="unique_department_association_name"),
            models.UniqueConstraint(fields=["department", "code"], name="unique_department_association_code"),
        ]

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class DepartmentalFee(TimeStampedModel):
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE, related_name="departmental_fees")
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="departmental_fees", null=True, blank=True)
    association = models.ForeignKey(DepartmentalAssociation, on_delete=models.CASCADE, related_name="session_fees")
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["department__name", "session__name", "association__created_at", "association__name"]
        constraints = [
            models.UniqueConstraint(fields=["session", "department", "association"], name="unique_session_department_association_fee"),
        ]

    def __str__(self):
        return f"{self.department.name} - {self.session.name} - {self.association.name}"


class DepartmentalPayment(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="departmental_payments",
        limit_choices_to={"role": User.Role.STUDENT},
    )
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="departmental_payments")
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE, related_name="payments")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    association_summary = models.CharField(max_length=255, blank=True)
    paystack_reference = models.CharField(max_length=64, unique=True, default="", blank=True)
    paystack_public_key_used = models.CharField(max_length=255, blank=True)
    paystack_secret_key_used = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["student", "session"], name="unique_student_departmental_session_payment"),
        ]

    def save(self, *args, **kwargs):
        if not self.paystack_reference:
            self.paystack_reference = f"DEPT-{timezone.now():%Y%m%d}-{uuid4().hex[:10].upper()}"
        if self.status == self.Status.PAID and not self.paid_at:
            self.paid_at = timezone.now()
        super().save(*args, **kwargs)

    @property
    def is_paid(self):
        return self.status == self.Status.PAID

    @property
    def semester_label(self):
        reference_time = self.paid_at or self.created_at or timezone.now()
        local_time = timezone.localtime(reference_time)
        return "First Semester" if local_time.month <= 6 else "Second Semester"

    def __str__(self):
        return f"{self.student.full_name} - {self.session.name}"


class DepartmentalPaymentItem(TimeStampedModel):
    payment = models.ForeignKey(DepartmentalPayment, on_delete=models.CASCADE, related_name="items")
    association = models.ForeignKey(DepartmentalAssociation, on_delete=models.PROTECT, related_name="payment_items")
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["association__created_at", "association__name"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "association"], name="unique_departmental_payment_item"),
        ]

    def __str__(self):
        return f"{self.payment} - {self.association.name}"


class DepartmentalPaymentDocument(TimeStampedModel):
    class Category(models.TextChoices):
        WHITE_FORM = "white_form", "White Form"
        SCHOOL_RECEIPT = "school_receipt", "School Receipt"
        # Keep the stored value stable so existing uploaded documents retain
        # their category when the label is renamed.
        MEDICAL_FITNESS = "supporting", "Medical fitness"
        ADDITIONAL = "additional", "Additional Document"

    payment = models.ForeignKey(DepartmentalPayment, on_delete=models.CASCADE, related_name="documents")
    category = models.CharField(max_length=20, choices=Category.choices)
    title = models.CharField(max_length=180)
    file = models.FileField(upload_to="departmental/%Y/%m/")

    class Meta:
        ordering = ["category", "title", "created_at"]

    def __str__(self):
        return self.title


class UserAlert(TimeStampedModel):
    class AlertType(models.TextChoices):
        MESSAGE = "message", "Message"
        CLASS_REMINDER = "class_reminder", "Class Reminder"

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="alerts")
    alert_type = models.CharField(max_length=30, choices=AlertType.choices)
    title = models.CharField(max_length=180)
    body = models.TextField()
    target_url = models.CharField(max_length=255, blank=True)
    dedupe_key = models.CharField(max_length=255)
    is_read = models.BooleanField(default=False)
    browser_delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["recipient", "dedupe_key"], name="unique_user_alert_dedupe"),
        ]
        indexes = [
            models.Index(fields=["recipient", "browser_delivered_at", "is_read"]),
        ]

    def __str__(self):
        return f"{self.recipient.full_name} - {self.title}"


class CourseReminderDispatch(TimeStampedModel):
    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        ALERT = "alert", "Alert"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="reminder_dispatches")
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="course_reminder_dispatches")
    scheduled_for = models.DateTimeField()
    channel = models.CharField(max_length=20, choices=Channel.choices)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-scheduled_for", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "recipient", "scheduled_for", "channel"],
                name="unique_course_reminder_dispatch",
            ),
        ]
        indexes = [
            models.Index(fields=["scheduled_for", "channel"]),
        ]

    def __str__(self):
        return f"{self.course.code} - {self.recipient.full_name} - {self.channel}"
