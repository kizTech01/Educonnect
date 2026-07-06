from django.contrib import admin

from .models import (
    AcademicSession,
    Course,
    CoursePayment,
    CoursePaymentGateway,
    CourseMaterial,
    Department,
    DepartmentPaymentGateway,
    DepartmentalAssociation,
    DepartmentalFee,
    DepartmentalPayment,
    DepartmentalPaymentDocument,
    DepartmentalPaymentItem,
    Handbook,
    LecturerCourseRegistration,
    MaterialAccess,
    Notification,
    NotificationRecipient,
    StudentCourseRegistration,
    Timetable,
    User,
)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "full_name", "role", "department", "level", "is_approved", "is_active")
    list_filter = ("role", "department", "level", "is_approved", "is_active")
    search_fields = ("username", "first_name", "last_name", "id_number", "email")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "created_at")
    search_fields = ("name", "code")


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "department", "lecturer", "level", "semester", "is_free", "amount", "venue")
    list_filter = ("department", "level", "semester", "is_free")
    search_fields = ("code", "title", "venue", "department__name", "lecturer__username")


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
    list_display = ("student", "course", "amount", "status", "paystack_reference", "paid_at", "created_at")
    list_filter = ("status", "course__department")
    search_fields = ("student__username", "student__id_number", "course__code", "paystack_reference")


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
