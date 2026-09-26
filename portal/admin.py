from django.contrib import admin

from .models import (
    AcademicSession,
    AccommodationAllocation,
    AccommodationApplication,
    AccommodationSession,
    Course,
    CourseResult,
    CoursePayment,
    CourseStudentGroup,
    CourseStudentGroupMembership,
    CoursePaymentGateway,
    CourseMaterial,
    Department,
    DepartmentLecturerUpload,
    CourseAllocationUpload,
    DepartmentPaymentGateway,
    DepartmentCoursePaymentGateway,
    DepartmentalAssociation,
    DepartmentalFee,
    DepartmentalPayment,
    DepartmentalPaymentDocument,
    DepartmentalPaymentItem,
    Handbook,
    Hostel,
    HostelBed,
    HostelBlock,
    HostelRoom,
    Faculty,
    InstitutionProfile,
    Institution,
    LecturerCourseRegistration,
    MaterialAccess,
    Notification,
    NotificationAttachment,
    NotificationRecipient,
    StudentCourseRegistration,
    Timetable,
    User,
    AuditLog,
    GuardianRelationship,
    RoleAssignment,
    Payment,
    Programme,
    Subscription,
    SubscriptionPlan,
    ScreeningIntegration,
    ScreeningApplication,
)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "full_name", "role", "department", "level", "is_approved", "is_active")
    list_filter = ("role", "department", "level", "is_approved", "is_active")
    search_fields = ("username", "first_name", "last_name", "id_number", "email")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "faculty", "created_at")
    list_filter = ("faculty",)
    search_fields = ("name", "code", "faculty__name")


@admin.register(DepartmentCoursePaymentGateway)
class DepartmentCoursePaymentGatewayAdmin(admin.ModelAdmin):
    list_display = ("department", "paystack_public_key", "updated_at")
    search_fields = ("department__name", "department__code")


@admin.register(Faculty)
class FacultyAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "created_at")
    search_fields = ("name", "code")


@admin.register(InstitutionProfile)
class InstitutionProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "website", "email", "updated_at")


@admin.register(NotificationAttachment)
class NotificationAttachmentAdmin(admin.ModelAdmin):
    list_display = ("notification", "file", "created_at")
    search_fields = ("notification__subject", "file")


@admin.register(DepartmentLecturerUpload)
class DepartmentLecturerUploadAdmin(admin.ModelAdmin):
    list_display = ("department", "file", "uploaded_by", "created_at")


@admin.register(CourseAllocationUpload)
class CourseAllocationUploadAdmin(admin.ModelAdmin):
    list_display = ("department", "file", "uploaded_by", "created_at")


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "department", "lecturer", "level", "semester", "is_free", "amount", "venue")
    list_filter = ("department", "level", "semester", "is_free")
    search_fields = ("code", "title", "venue", "department__name", "lecturer__username")


@admin.register(CourseResult)
class CourseResultAdmin(admin.ModelAdmin):
    list_display = ("student", "course", "session", "score", "grade", "status", "reviewed_by")
    list_filter = ("status", "session", "course__department")
    search_fields = ("student__username", "student__id_number", "course__code")
    readonly_fields = ("grade", "grade_point", "credit_units", "submitted_at", "reviewed_at", "published_at")


@admin.register(StudentCourseRegistration)
class StudentCourseRegistrationAdmin(admin.ModelAdmin):
    list_display = ("student", "course", "registered_by", "created_at")
    list_filter = ("course__department",)
    search_fields = ("student__username", "student__id_number", "course__code")


@admin.register(LecturerCourseRegistration)
class LecturerCourseRegistrationAdmin(admin.ModelAdmin):
    list_display = ("lecturer", "course", "created_at")
    list_filter = ("course__department",)
    search_fields = ("lecturer__username", "course__code")


@admin.register(CoursePayment)
class CoursePaymentAdmin(admin.ModelAdmin):
    list_display = ("student", "course", "session", "enrollment_sequence", "amount", "status", "is_active_for_registration", "paid_at")
    list_filter = ("status", "course__department")
    search_fields = ("student__username", "student__id_number", "course__code", "paystack_reference")


@admin.register(CourseStudentGroup)
class CourseStudentGroupAdmin(admin.ModelAdmin):
    list_display = ("course", "name", "session", "grouping_method", "department", "lecturer")
    list_filter = ("grouping_method", "session", "course__department")
    search_fields = ("course__code", "name", "lecturer__username")


@admin.register(CourseStudentGroupMembership)
class CourseStudentGroupMembershipAdmin(admin.ModelAdmin):
    list_display = ("group", "student", "created_at")
    search_fields = ("group__course__code", "group__name", "student__username", "student__id_number")


@admin.register(CoursePaymentGateway)
class CoursePaymentGatewayAdmin(admin.ModelAdmin):
    list_display = ("slug", "is_configured", "updated_at")
    search_fields = ("slug",)


@admin.register(Timetable)
class TimetableAdmin(admin.ModelAdmin):
    list_display = ("title", "department", "level", "academic_session", "semester")
    list_filter = ("department", "level", "semester", "academic_session")
    search_fields = ("title", "department__name")


@admin.register(Handbook)
class HandbookAdmin(admin.ModelAdmin):
    list_display = ("title", "department", "academic_session", "semester")
    list_filter = ("department", "semester", "academic_session")
    search_fields = ("title", "department__name")


@admin.register(CourseMaterial)
class CourseMaterialAdmin(admin.ModelAdmin):
    list_display = ("title", "course", "lecturer", "is_free", "amount", "is_download_enabled", "created_at")
    list_filter = ("is_free", "is_download_enabled", "course__department")
    search_fields = ("title", "course__code", "lecturer__username")


@admin.register(MaterialAccess)
class MaterialAccessAdmin(admin.ModelAdmin):
    list_display = ("student", "material", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("student__username", "student__id_number", "material__title")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("subject", "sender", "level", "created_at")
    search_fields = ("subject", "sender__username")


@admin.register(NotificationRecipient)
class NotificationRecipientAdmin(admin.ModelAdmin):
    list_display = ("notification", "student", "is_read", "is_deleted", "created_at")
    list_filter = ("is_read", "is_deleted")
    search_fields = ("student__username", "notification__subject")


@admin.register(DepartmentPaymentGateway)
class DepartmentPaymentGatewayAdmin(admin.ModelAdmin):
    list_display = ("department", "is_configured", "updated_at")
    search_fields = ("department__name", "department__code")


@admin.register(AcademicSession)
class AcademicSessionAdmin(admin.ModelAdmin):
    list_display = ("name", "is_current", "created_at")
    list_filter = ("is_current",)
    search_fields = ("name",)


@admin.register(Programme)
class ProgrammeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "department", "award", "duration_years", "is_active")
    list_filter = ("is_active", "department")
    search_fields = ("name", "code", "department__name")


@admin.register(AccommodationSession)
class AccommodationSessionAdmin(admin.ModelAdmin):
    list_display = ("name", "academic_session", "is_open", "opens_at", "closes_at", "institution")
    list_filter = ("is_open", "institution")
    search_fields = ("name", "academic_session__name")


@admin.register(Hostel)
class HostelAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "gender_restriction", "manager", "is_active", "institution")
    list_filter = ("gender_restriction", "is_active", "institution")
    search_fields = ("name", "code", "manager__username")


@admin.register(HostelBlock)
class HostelBlockAdmin(admin.ModelAdmin):
    list_display = ("name", "hostel", "institution")
    list_filter = ("hostel", "institution")
    search_fields = ("name", "hostel__name")


@admin.register(HostelRoom)
class HostelRoomAdmin(admin.ModelAdmin):
    list_display = ("code", "block", "floor", "is_available", "institution")
    list_filter = ("is_available", "institution")
    search_fields = ("code", "block__hostel__name")


@admin.register(HostelBed)
class HostelBedAdmin(admin.ModelAdmin):
    list_display = ("code", "room", "is_available", "institution")
    list_filter = ("is_available", "institution")
    search_fields = ("code", "room__code", "room__block__hostel__name")


@admin.register(AccommodationApplication)
class AccommodationApplicationAdmin(admin.ModelAdmin):
    list_display = ("student", "accommodation_session", "preferred_hostel", "status", "reviewed_by", "institution")
    list_filter = ("status", "institution")
    search_fields = ("student__username", "student__id_number", "preferred_hostel__name")


@admin.register(AccommodationAllocation)
class AccommodationAllocationAdmin(admin.ModelAdmin):
    list_display = ("student", "bed", "status", "allocated_by", "allocated_at", "institution")
    list_filter = ("status", "institution")
    search_fields = ("student__username", "student__id_number", "bed__code")


@admin.register(DepartmentalAssociation)
class DepartmentalAssociationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_constant", "created_at")
    list_filter = ("is_constant",)
    search_fields = ("name", "code")


@admin.register(DepartmentalFee)
class DepartmentalFeeAdmin(admin.ModelAdmin):
    list_display = ("session", "association", "amount", "updated_at")
    list_filter = ("session",)
    search_fields = ("session__name", "association__name")


class DepartmentalPaymentItemInline(admin.TabularInline):
    model = DepartmentalPaymentItem
    extra = 0


class DepartmentalPaymentDocumentInline(admin.TabularInline):
    model = DepartmentalPaymentDocument
    extra = 0


@admin.register(DepartmentalPayment)
class DepartmentalPaymentAdmin(admin.ModelAdmin):
    list_display = ("student", "department", "session", "status", "total_amount", "paid_at")
    list_filter = ("status", "department", "session")
    search_fields = ("student__username", "student__id_number", "paystack_reference")
    inlines = [DepartmentalPaymentItemInline, DepartmentalPaymentDocumentInline]


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ("name", "institution_code", "subdomain", "status", "created_at")
    list_filter = ("institution_type", "status")
    search_fields = ("name", "institution_code", "subdomain", "email")


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "billing_period", "is_active")
    list_filter = ("billing_period", "is_active")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("institution", "plan", "end_date", "status", "amount")
    list_filter = ("status", "plan")
    search_fields = ("institution__name", "payment_reference")


@admin.register(Payment)
class SubscriptionPaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "institution", "plan", "amount", "status", "paid_at")
    list_filter = ("status", "currency")
    search_fields = ("reference", "institution__name", "receipt_number")
    readonly_fields = ("reference", "gateway_response", "paid_at", "receipt_number")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "institution", "user", "ip_address")
    list_filter = ("action", "institution")
    search_fields = ("action", "description", "object_id")
    readonly_fields = ("created_at",)


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "department", "institution", "is_active", "assigned_by", "assigned_at")
    list_filter = ("role", "is_active", "institution")
    search_fields = ("user__username", "user__email", "department__name")


@admin.register(GuardianRelationship)
class GuardianRelationshipAdmin(admin.ModelAdmin):
    list_display = ("guardian", "student", "relationship", "institution", "is_active")
    list_filter = ("is_active", "institution")
    search_fields = ("guardian__username", "student__username")


@admin.register(ScreeningIntegration)
class ScreeningIntegrationAdmin(admin.ModelAdmin):
    list_display = ("institution", "is_open", "admission_session", "updated_at")
    list_filter = ("is_open",)
    search_fields = ("institution__name",)
    readonly_fields = ("api_secret", "created_at", "updated_at")


@admin.register(ScreeningApplication)
class ScreeningApplicationAdmin(admin.ModelAdmin):
    list_display = ("external_application_id", "institution", "status", "department", "student", "updated_at")
    list_filter = ("status", "institution")
    search_fields = ("external_application_id", "applicant_reference", "jamb_number", "email")
    readonly_fields = ("created_at", "updated_at")
