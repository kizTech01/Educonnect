from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import AbstractUser
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


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Department(TimeStampedModel):
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=20, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        STUDENT = "student", "Student"
        LECTURER = "lecturer", "Lecturer"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STUDENT)
    department = models.ForeignKey(
        Department,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="users",
    )
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, blank=True)
    id_number = models.CharField(max_length=30, unique=True, null=True, blank=True)
    phone_number = models.CharField(max_length=30, blank=True)
    is_approved = models.BooleanField(default=True)
    email_class_reminders = models.BooleanField(default=True)
    browser_alerts_enabled = models.BooleanField(default=False)
    class_reminder_alerts_enabled = models.BooleanField(default=False)

    class Meta:
        ordering = ["username"]

    def save(self, *args, **kwargs):
        if self.role != self.Role.LECTURER:
            self.is_approved = True
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return self.get_full_name() or self.username

    def __str__(self):
        return self.full_name


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

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="courses")
    lecturer = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="teaching_courses",
        limit_choices_to={"role": User.Role.LECTURER},
    )
    code = models.CharField(max_length=20, unique=True)
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

    def clean(self):
        if self.amount and self.amount > 0:
            self.is_free = False
        elif self.is_free:
            self.amount = Decimal("0.00")
        elif self.amount <= 0:
            raise ValidationError({"amount": "Paid courses must have an amount greater than zero."})

    def save(self, *args, **kwargs):
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
            models.UniqueConstraint(fields=["student", "course"], name="unique_student_course_registration")
        ]
        indexes = [
            models.Index(fields=["student", "course"]),
        ]

    def __str__(self):
        return f"{self.student.full_name} - {self.course.code}"


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
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    paystack_reference = models.CharField(max_length=64, blank=True, default="")
    paystack_public_key_used = models.CharField(max_length=255, blank=True)
    paystack_secret_key_used = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["student", "course"], name="unique_course_payment"),
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
            models.UniqueConstraint(fields=["course"], name="unique_lecturer_course_registration")
        ]

    def clean(self):
        if self.course.lecturer_id and self.course.lecturer_id != self.lecturer_id:
            raise ValidationError("Only the lecturer assigned to a course can register it for management.")

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
    pass


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
    departments = models.ManyToManyField(Department, blank=True, related_name="notifications")
    level = models.CharField(max_length=20, blank=True)
    courses = models.ManyToManyField(Course, blank=True, related_name="notifications")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.subject


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
    slug = models.CharField(max_length=30, unique=True, default="courses")
    payment_url = models.URLField(blank=True)
    paystack_public_key = models.CharField(max_length=255, blank=True)
    paystack_secret_key = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["slug"]

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


class AcademicSession(TimeStampedModel):
    name = models.CharField(max_length=20, unique=True)
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_current", "-name"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_current:
            AcademicSession.objects.exclude(pk=self.pk).filter(is_current=True).update(is_current=False)

    def __str__(self):
        return self.name


class DepartmentalAssociation(TimeStampedModel):
    name = models.CharField(max_length=120, unique=True)
    code = models.SlugField(max_length=50, unique=True)
    is_constant = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "name"]

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
        SUPPORTING = "supporting", "Supporting Document"

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
