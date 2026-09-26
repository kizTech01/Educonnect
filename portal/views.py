import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from functools import wraps
from pathlib import Path

from django.apps import apps
from django.contrib import messages
from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Count, Max, OuterRef, Prefetch, Subquery, Sum
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import ensure_csrf_cookie

from .forms import (
    CourseForm,
    CourseResultForm,
    ResultImportForm,
    ResultReportFilterForm,
    CourseDetailsForm,
    CourseBrowseFilterForm,
    CourseStudentGroupForm,
    CourseMaterialBatchForm,
    AcademicSessionForm,
    AccommodationApplicationForm,
    AccommodationDecisionForm,
    AccommodationSessionForm,
    AIAutomationUploadForm,
    DepartmentForm,
    DepartmentLecturerUploadForm,
    CourseAllocationUploadForm,
    FacultyForm,
    InstitutionCreateForm,
    InstitutionUpdateForm,
    SubscriptionPlanForm,
    SubscriptionPlanEditForm,
    SubscriptionPlanDurationForm,
    SubscriptionPlanDurationEditForm,
    SubscriptionPaymentGatewayForm,
    ScreeningIntegrationForm,
    SubscriptionRenewalForm,
    PlatformAdminForm,
    DepartmentPaymentGatewayForm,
    DepartmentCoursePaymentGatewayForm,
    DepartmentalAdditionalDocumentForm,
    DepartmentalAssociationForm,
    DepartmentalSearchForm,
    DepartmentalStudentPaymentForm,
    HandbookForm,
    HandbookFilterForm,
    HostelBedForm,
    HostelBlockForm,
    HostelForm,
    HostelRoomForm,
    LecturerCourseUpdateForm,
    LecturerProfileForm,
    LecturerUserForm,
    LecturerSignupForm,
    NotificationForm,
    PortalAuthenticationForm,
    ResultReviewForm,
    StudentCourseFilterForm,
    StudentProfileForm,
    StudentUserForm,
    StaffUserForm,
    StudentSignupForm,
    TimetableFilterForm,
    TimetableForm,
    LecturerCourseFilterForm,
    AdminProfileForm,
    InstitutionAdminPasswordChangeForm,
    UserPasswordResetForm,
)
from .models import (
    AcademicSession,
    AccommodationAllocation,
    AccommodationApplication,
    AccommodationSession,
    Course,
    CourseResult,
    CoursePaymentGateway,
    CourseMaterial,
    Department,
    DepartmentLecturerUpload,
    CourseAllocationUpload,
    Faculty,
    InstitutionProfile,
    Institution,
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
    CoursePayment,
    CourseStudentGroup,
    CourseStudentGroupMembership,
    MaterialAccess,
    Notification,
    NotificationAttachment,
    NotificationRecipient,
    LecturerCourseRegistration,
    StudentCourseRegistration,
    Timetable,
    User,
    UserAlert,
    AuditLog,
    AIAutomationJob,
    RoleAssignment,
    Payment,
    Programme,
    Subscription,
    SubscriptionPlan,
    SubscriptionPlanDuration,
    SubscriptionPaymentGateway,
    GuardianRelationship,
    ScreeningIntegration,
    ScreeningApplication,
    TimeStampedModel,
    curriculum_for_department,
    curriculum_for_programme,
)
from .services import (
    TEMPORARY_ACCOUNT_PASSWORD,
    grant_free_trial_subscription,
    deliver_notification,
    ensure_default_admin_user,
    initialize_paystack_transaction,
    paystack_amount_in_kobo,
    sync_material_access_for_material,
    sync_material_access_for_registration,
    verify_paystack_transaction,
    create_subscription_payment,
    subscription_paystack_secret_key,
    verify_and_finalize_subscription_payment,
)
from .pdf import build_exam_card_pdf, build_pdf_document
from .automation import (
    analyze_institution_automation,
    execute_institution_automation,
    import_course_allocations,
    import_course_results,
    import_department_lecturers,
    import_handbook_courses,
)

LOGIN_ROLES = set(User.Role.values)
SCREENING_SIGNATURE_HEADER = "HTTP_X_EDUCONNECT_SIGNATURE"
SCREENING_TIMESTAMP_HEADER = "HTTP_X_EDUCONNECT_TIMESTAMP"
SCREENING_INSTITUTION_HEADER = "HTTP_X_EDUCONNECT_INSTITUTION"


def institution_overview_queryset():
    """Institution rows enriched with the figures shown to platform admins."""
    latest_subscription = Subscription.objects.filter(
        institution=OuterRef("pk"),
    ).order_by("-end_date", "-created_at")
    return Institution.objects.annotate(
        student_count=Count(
            "users",
            filter=Q(users__role=User.Role.STUDENT),
            distinct=True,
        ),
        staff_count=Count(
            "users",
            filter=Q(users__role__in=[User.Role.LECTURER, User.Role.ADMIN]),
            distinct=True,
        ),
        plan_name=Subquery(latest_subscription.values("plan__name")[:1]),
        subscription_status=Subquery(latest_subscription.values("status")[:1]),
    ).prefetch_related(
        Prefetch(
            "users",
            queryset=User.all_objects.filter(
                role=User.Role.ADMIN,
                is_superuser=False,
            ).order_by("email", "id"),
            to_attr="institution_administrators",
        )
    )


@transaction.atomic
def delete_institution_data(institution):
    """Permanently remove an institution's records and uploaded files.

    Platform-managed subscription plans are deliberately retained because they
    can be shared by several institutions.  The deletion audit entry is written
    by the caller after this function completes, so it remains as the sole
    platform-level record of the destructive action.
    """
    tenant_models = [
        model
        for model in apps.get_models()
        if issubclass(model, TimeStampedModel) and not model._meta.abstract
    ]

    # Django does not remove files from storage when model records are deleted.
    # Collect every tenant-owned upload first, then remove the physical files
    # only after the corresponding database rows have been removed.
    uploads = []
    for model in [User, *tenant_models]:
        manager = getattr(model, "all_objects", model._default_manager)
        for field in model._meta.fields:
            if isinstance(field, models.FileField):
                uploads.extend(
                    (field.storage, name)
                    for name in manager.filter(institution=institution)
                    .exclude(**{field.name: ""})
                    .values_list(field.name, flat=True)
                )

    # These records use PROTECT for tenant integrity, so remove them before
    # their referenced tenant, users, departments, or subscription records.
    ScreeningApplication.objects.filter(institution=institution).delete()
    AuditLog.objects.filter(institution=institution).delete()
    Payment.objects.filter(institution=institution).delete()
    Subscription.objects.filter(institution=institution).delete()

    # Delete dependent records before their parents (courses before
    # departments, departments before faculties, etc.), then remove users.
    for model in reversed(tenant_models):
        model.all_objects.filter(institution=institution).delete()
    User.all_objects.filter(institution=institution).delete()

    if institution.logo:
        uploads.append((institution.logo.storage, institution.logo.name))
    institution.delete()

    files_to_delete = tuple(uploads)

    def delete_uploaded_files():
        deleted_uploads = set()
        for storage, name in files_to_delete:
            if not name:
                continue
            key = (id(storage), name)
            if key not in deleted_uploads:
                storage.delete(name)
                deleted_uploads.add(key)

    transaction.on_commit(delete_uploaded_files)


def _login_throttle_key(request, username):
    identity = f"{request.META.get('REMOTE_ADDR', '')}:{username.lower()}"
    return f"portal-login:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _login_is_throttled(request, username):
    return int(cache.get(_login_throttle_key(request, username), 0)) >= 5


def _record_failed_login(request, username):
    key = _login_throttle_key(request, username)
    cache.set(key, int(cache.get(key, 0)) + 1, timeout=15 * 60)


def _clear_failed_logins(request, username):
    cache.delete(_login_throttle_key(request, username))


def institution_logo(request):
    """Serve the public institution logo without exposing other uploaded files."""
    institution = getattr(request, "institution", None) or InstitutionProfile.objects.first()
    if institution is None or not institution.logo:
        raise Http404("Institution logo not found.")
    return FileResponse(institution.logo.open("rb"), as_attachment=False)


def login_form_for_role(role, data=None):
    kwargs = {"initial": {"role": role}}
    if data is not None:
        kwargs["data"] = data
    return PortalAuthenticationForm(**kwargs)


def signup_form_for_role(role, data=None):
    if role == User.Role.STUDENT:
        form_class = StudentSignupForm
    elif role == User.Role.LECTURER:
        form_class = LecturerSignupForm
    else:
        return None
    return form_class(data) if data is not None else form_class()


def auth_page_context(role, login_form=None, signup_form=None, *, is_super_admin=False):
    ensure_default_admin_user()
    role_label = "Super Administrator" if is_super_admin else User.Role(role).label if role else "Institution"
    signup_available = not is_super_admin and role in {User.Role.STUDENT, User.Role.LECTURER}
    signup_form = signup_form if signup_form is not None else signup_form_for_role(role)
    return {
        "auth_role": role,
        "role_label": role_label,
        "page_title": f"{role_label} Login",
        "page_description": (
            "Use your superuser credentials to manage the Educonnect platform."
            if is_super_admin
            else "Use your institution administrator credentials to continue."
            if role == User.Role.ADMIN
            else f"Use your {role_label.lower()} credentials to enter your dashboard."
        ),
        "login_form": login_form or login_form_for_role(role),
        "login_action": reverse("portal:super-admin-login") if is_super_admin else reverse("portal:role-login", kwargs={"role": role}) if role else reverse("portal:login"),
        "login_button_label": "Sign in securely" if not role else f"Enter {role_label} Dashboard",
        "signup_available": signup_available,
        "signup_form": signup_form,
        "signup_action": reverse("portal:student-signup" if role == User.Role.STUDENT else "portal:lecturer-signup")
        if signup_available
        else None,
        "signup_toggle_label": "Sign up",
        "signup_title": f"Create {role_label} Account",
        "signup_description": (
            "Students can create a new account here and sign in immediately."
            if role == User.Role.STUDENT
            else "Lecturers can create an account here. Admin approval is required before first dashboard access."
        ),
        "is_student": role == User.Role.STUDENT,
        "is_lecturer": role == User.Role.LECTURER,
    }


def signup_page_context(role, signup_form=None):
    ensure_default_admin_user()
    role_label = User.Role(role).label
    return {
        "auth_role": role,
        "role_label": role_label,
        "page_title": f"{role_label} Sign Up",
        "page_description": (
            "Create your student account and start using the portal."
            if role == User.Role.STUDENT
            else "Create your lecturer account. Admin approval is required before dashboard access."
        ),
        "signup_form": signup_form or signup_form_for_role(role),
        "signup_action": reverse("portal:student-signup" if role == User.Role.STUDENT else "portal:lecturer-signup"),
        "back_url": reverse("portal:role-login", kwargs={"role": role}),
        "is_student": role == User.Role.STUDENT,
        "is_lecturer": role == User.Role.LECTURER,
        "programme_departments": list(
            Programme.objects.filter(is_active=True).values("id", "name", "department_id")
        ) if role == User.Role.STUDENT else [],
    }


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("portal:home")
            if request.user.role == User.Role.LECTURER and not request.user.is_approved:
                logout(request)
                messages.warning(request, "Your lecturer account is awaiting admin approval.")
                return redirect("portal:home")
            if request.user.is_superuser:
                return redirect("portal:super-admin-dashboard")
            if request.user.has_role(*roles):
                return view_func(request, *args, **kwargs)
            raise PermissionDenied

        return never_cache(_wrapped)

    return decorator


def super_admin_required(view_func):
    @wraps(view_func)
    @never_cache
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("portal:super-admin-login")
        if not request.user.is_superuser:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return _wrapped


def _client_ip(request):
    return request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")).split(",")[0].strip() or None


def _screening_json(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _screening_api_authenticate(request, institution_code):
    """Authenticate a request signed by the separately deployed screening app."""
    header_code = request.META.get(SCREENING_INSTITUTION_HEADER, "").strip()
    if not header_code or header_code.casefold() != institution_code.casefold():
        return None, JsonResponse({"detail": "Institution credentials do not match this endpoint."}, status=403)
    institution = Institution.objects.filter(institution_code__iexact=institution_code).first()
    if not institution or not institution.is_operational:
        return None, JsonResponse({"detail": "Online Screening is unavailable for this institution."}, status=403)
    integration = getattr(institution, "screening_integration", None)
    if not integration:
        return None, JsonResponse({"detail": "Screening integration is not configured."}, status=409)
    try:
        timestamp = int(request.META.get(SCREENING_TIMESTAMP_HEADER, ""))
    except (TypeError, ValueError):
        return None, JsonResponse({"detail": "A valid request timestamp is required."}, status=401)
    max_skew = getattr(settings, "SCREENING_API_MAX_CLOCK_SKEW_SECONDS", 300)
    if abs(int(timezone.now().timestamp()) - timestamp) > max_skew:
        return None, JsonResponse({"detail": "The signed request has expired."}, status=401)
    signature = request.META.get(SCREENING_SIGNATURE_HEADER, "")
    signed_content = str(timestamp).encode("utf-8") + b"." + request.body
    expected = hmac.new(integration.api_secret.encode("utf-8"), signed_content, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return None, JsonResponse({"detail": "Invalid API credentials."}, status=401)
    replay_key = "screening-api-signature:" + hashlib.sha256(
        f"{institution.id}:{signature}".encode("utf-8")
    ).hexdigest()
    if not cache.add(replay_key, True, timeout=max_skew):
        return None, JsonResponse({"detail": "Duplicate signed request rejected."}, status=409)
    return (institution, integration), None


def _screening_lookup(institution, payload, field, model, *, required=False):
    code = str(payload.get(field, "") or "").strip()
    if not code:
        if required:
            raise ValidationError({field: f"{field.replace('_', ' ').capitalize()} is required."})
        return None
    lookup_field = "code" if model is Department else "name"
    instance = model.all_objects.filter(institution=institution, **{lookup_field: code}).first()
    if not instance:
        raise ValidationError({field: "The supplied value does not belong to this institution."})
    return instance


def ensure_department_gateway_credentials(department):
    """Create an unconfigured gateway; an HOD must add the real Paystack keys."""
    return DepartmentPaymentGateway.objects.get_or_create(department=department)[0]


def ensure_all_department_gateways():
    for department in Department.objects.all():
        ensure_department_gateway_credentials(department)


def department_payment_gateway_for_payment(department):
    if not department:
        return None
    return ensure_department_gateway_credentials(department)


def course_payment_gateway():
    # Course checkout must use the credentials the admin saved for the current site.
    # Do not silently seed fallback keys here, or the UI can look configured while
    # payments still route through the wrong Paystack account.
    gateway, _ = CoursePaymentGateway.objects.get_or_create(slug="courses")
    return gateway


def course_payment_gateway_for_department(department):
    """Return the HOD-managed API for paid courses in one department."""
    if not department:
        return None
    return DepartmentCoursePaymentGateway.objects.filter(department=department).first()


ROLE_MODULES = {
    User.Role.MIS: (("Student Records", "students"), ("Data Quality", "data-quality"), ("Reports", "reports")),
    User.Role.BURSARY: (("Payments", "payments"), ("Outstanding Balances", "balances"), ("Finance Reports", "reports")),
    User.Role.ADMISSION_OFFICER: (("Applications", "applications"), ("Admission Reports", "reports")),
    User.Role.ACADEMIC_PLANNING: (("Academic Structure", "academic-structure"), ("Course Catalogue", "courses"), ("Curriculum Reports", "reports")),
    User.Role.HOD: (("Students", "students"), ("Lecturers", "lecturers"), ("Courses", "courses"), ("My Assigned Courses", "my-courses"), ("Course Allocation", "allocations"), ("All Results", "results"), ("Reports", "reports"), ("Exam Officer", "exam-officer")),
    User.Role.EXAM_OFFICER: (("Exam Eligibility", "eligibility"), ("Course Registrations", "registrations"), ("Exam Cards", "exam-cards"), ("All Results", "results"), ("Reports", "reports")),
    User.Role.SENATE_MEMBER: (("Academic Reports", "reports"), ("Course Catalogue", "courses")),
    User.Role.PARENT_GUARDIAN: (("Linked Students", "linked-students"), ("Academic Progress", "progress"), ("Payments", "payments")),
}


def active_role(request):
    """Use a session role only when it is genuinely assigned to this user."""
    if not request.user.is_authenticated or request.user.is_superuser:
        return None
    roles = request.user.active_roles()
    # RequestFactory-based integrations and a few internal callers do not add
    # SessionMiddleware.  They should fall back to the primary role just as a
    # newly authenticated browser session does.
    selected = getattr(request, "session", {}).get("active_role")
    if selected in roles:
        return selected
    return request.user.role if request.user.role in roles else sorted(roles)[0]


def _role_dashboard_name(role):
    if role == User.Role.ADMIN:
        return "portal:admin-dashboard"
    if role in {User.Role.LECTURER, User.Role.HOD, User.Role.EXAM_OFFICER}:
        return "portal:lecturer-dashboard"
    if role == User.Role.STUDENT:
        return "portal:student-dashboard"
    return "portal:role-workspace"


def sidebar_links(request):
    current = request.resolver_match.url_name if request.resolver_match else ""
    selected_role = active_role(request)
    unread_message_count = 0
    if request.user.is_authenticated and selected_role == User.Role.STUDENT:
        unread_message_count = NotificationRecipient.objects.filter(
            student=request.user,
            is_deleted=False,
            is_read=False,
        ).count()
    if request.user.is_superuser or selected_role == User.Role.ADMIN:
        links = [
            {"label": "Dashboard", "url_name": "portal:admin-dashboard"},
            {
                "label": "Departmental",
                "children": [
                    {"label": "View APIs", "url_name": "portal:admin-departmental-apis"},
                    {"label": "View Fees", "url_name": "portal:admin-departmental-fees"},
                    {"label": "Students", "url_name": "portal:admin-departmental-download"},
                ],
            },
            {"label": "Departments", "url_name": "portal:admin-departments"},
            {"label": "HODs", "url_name": "portal:admin-hods"},
            {"label": "Faculty", "url_name": "portal:admin-faculties"},
            {"label": "Academic Sessions", "url_name": "portal:admin-about"},
            {"label": "Courses", "url_name": "portal:admin-courses"},
            {
                "label": "Paid Courses",
                "url": f'{reverse("portal:admin-courses")}?fee_type=paid',
                "active": current == "admin-courses" and request.GET.get("fee_type") == "paid",
            },
            {"label": "Users", "url_name": "portal:admin-users"},
            {"label": "Subscription & Billing", "url_name": "portal:billing-overview"},
            {"label": "Profile", "url_name": "portal:profile"},
        ]
        links.insert(-1, {"label": "EduConnect AI", "url_name": "portal:ai-assistant"})
    elif selected_role == User.Role.HOD:
        links = [
            {"label": "Dashboard", "url_name": "portal:lecturer-dashboard"},
            {"label": "Programmes", "url_name": "portal:hod-programmes"},
            {
                "label": "Departmental",
                "children": [
                    {"label": "Set Departmental Fees", "url_name": "portal:hod-departmental-fees"},
                    {"label": "API & Documents", "url_name": "portal:hod-api-and-document"},
                ],
            },
        ]
        for label, slug in ROLE_MODULES[User.Role.HOD]:
            links.append(
                {"label": label, "url_name": "portal:hod-exam-officer"}
                if slug == "exam-officer"
                else {"label": label, "url_name": "portal:department-all-results"}
                if slug == "results"
                else {"label": label, "url_name": "portal:hod-reports"}
                if slug == "reports"
                else {"label": label, "url_name": "portal:lecturer-courses"}
                if slug == "my-courses"
                else {"label": label, "url_name": "portal:role-module", "kwargs": {"module": slug}}
            )
    elif selected_role == User.Role.EXAM_OFFICER:
        links = [{"label": "Dashboard", "url_name": "portal:lecturer-dashboard"}]
        links.extend(
            {"label": label, "url_name": "portal:department-all-results"}
            if slug == "results"
            else {"label": label, "url_name": "portal:hod-reports"}
            if slug == "reports"
            else {"label": label, "url_name": "portal:role-module", "kwargs": {"module": slug}}
            for label, slug in ROLE_MODULES[User.Role.EXAM_OFFICER]
        )
    elif selected_role == User.Role.LECTURER:
        departmental_links = [
            {"label": "Students", "url_name": "portal:lecturer-departmental-download"},
        ]
        links = [
            {"label": "Dashboard", "url_name": "portal:lecturer-dashboard"},
            {
                "label": "Paid courses",
                "url": f'{reverse("portal:lecturer-courses")}?fee_type=paid',
                "active": current == "lecturer-courses" and request.GET.get("fee_type") == "paid",
            },
            {
                "label": "Departmental",
                "children": departmental_links,
            },
            {"label": "Register Courses", "url_name": "portal:lecturer-courses"},
            {"label": "Messages", "url_name": "portal:lecturer-messages"},
            {"label": "Timetable and Handbook", "url_name": "portal:lecturer-documents"},
            {"label": "Profile", "url_name": "portal:profile"},
        ]
        if getattr(request.user, "headed_department", None) or request.user.has_role(User.Role.HOD):
            links.insert(1, {"label": "Programmes", "url_name": "portal:hod-programmes"})
        links.insert(-1, {"label": "EduConnect AI", "url_name": "portal:ai-assistant"})
    elif selected_role in ROLE_MODULES:
        links = [
            {"label": "Dashboard", "url_name": "portal:role-workspace"},
            *[
                {"label": label, "url_name": "portal:role-module", "kwargs": {"module": slug}}
                for label, slug in ROLE_MODULES[selected_role]
            ],
            {"label": "Profile", "url_name": "portal:profile"},
        ]
        links.insert(-1, {"label": "EduConnect AI", "url_name": "portal:ai-assistant"})
        if selected_role == User.Role.BURSARY:
            links.insert(-1, {"label": "Accommodation", "url_name": "portal:accommodation-admin"})
    else:
        links = [
            {"label": "Dashboard", "url_name": "portal:student-dashboard"},
            {"label": "Departmental", "url_name": "portal:student-departmental"},
            {"label": "Course Registration", "url_name": "portal:student-courses"},
            {"label": "Results", "url_name": "portal:student-results"},
            {"label": "Accommodation", "url_name": "portal:student-accommodation"},
            {"label": "Messages", "url_name": "portal:student-messages", "badge_count": unread_message_count},
            {"label": "Timetable and Handbook", "url_name": "portal:student-documents"},
            {"label": "Profile", "url_name": "portal:profile"},
        ]
        links.insert(-1, {"label": "EduConnect AI", "url_name": "portal:ai-assistant"})
    if selected_role in {User.Role.HOD, User.Role.EXAM_OFFICER}:
        links.append({"label": "Profile", "url_name": "portal:profile"})
        links.insert(-1, {"label": "EduConnect AI", "url_name": "portal:ai-assistant"})
    resolved_links = []
    for item in links:
        if "children" in item:
            children = []
            active = False
            for child in item["children"]:
                child_active = current == child["url_name"].split(":")[1]
                active = active or child_active
                children.append({"label": child["label"], "url": reverse(child["url_name"], kwargs=child.get("kwargs")), "active": child_active})
            resolved_links.append({"label": item["label"], "children": children, "active": active})
        else:
            url = item.get("url") or reverse(item["url_name"], kwargs=item.get("kwargs"))
            active = item.get("active")
            if active is None:
                active = current == item["url_name"].split(":")[1]
            resolved_links.append({
                "label": item["label"],
                "url": url,
                "active": active,
                "badge_count": item.get("badge_count", 0),
            })
    return resolved_links


def dashboard_context(request, title, **extra):
    selectable_roles = []
    selected_role = active_role(request)
    if request.user.is_authenticated and not request.user.is_superuser:
        selectable_roles = [
            (value, User.Role(value).label)
            for value in sorted(request.user.active_roles())
        ]
    context = {
        "section_title": title,
        "sidebar_links": sidebar_links(request),
        "active_role": selected_role,
        "selectable_roles": selectable_roles,
    }
    context.update(extra)
    return context


def _redirect_with_query(request, url_name, **kwargs):
    url = reverse(url_name, kwargs=kwargs or None)
    query_string = request.GET.urlencode()
    return f"{url}?{query_string}" if query_string else url


def _redirect_with_anchor(url_name, anchor, **kwargs):
    return f"{reverse(url_name, kwargs=kwargs or None)}#{anchor}"


def _form_error_labels(form):
    labels = []
    for field_name in form.errors.keys():
        field = form.fields.get(field_name)
        if field is None:
            continue
        label = field.label or field_name.replace("_", " ").title()
        labels.append(label)
    return labels


def _search_filter(queryset, search, *fields):
    search = (search or "").strip()
    if not search:
        return queryset
    q = Q()
    for field in fields:
        q |= Q(**{f"{field}__icontains": search})
    return queryset.filter(q)


def _initialize_checkout_or_message(request, *, secret_key, email, amount, reference, callback_url, metadata):
    if not email:
        raise ValueError("A valid student email address is required before starting payment.")
    return initialize_paystack_transaction(
        secret_key=secret_key,
        email=email,
        amount=amount,
        reference=reference,
        callback_url=callback_url,
        metadata=metadata,
    )


def _is_paystack_transport_error(exc):
    return str(exc) == "Unable to reach Paystack right now. Please try again shortly."


def _render_paystack_browser_checkout(
    request,
    *,
    title,
    checkout_heading,
    payment,
    checkout_label,
    checkout_value,
    callback_url,
    cancel_url,
    notice,
):
    payment_amount = getattr(payment, "amount", None)
    if payment_amount is None:
        payment_amount = getattr(payment, "total_amount", None)
    context = dashboard_context(
        request,
        title,
        payment=payment,
        checkout_heading=checkout_heading,
        checkout_label=checkout_label,
        checkout_value=checkout_value,
        checkout_amount_display=f"{payment_amount:.2f}",
        checkout_amount_in_kobo=paystack_amount_in_kobo(payment_amount),
        checkout_public_key=payment.paystack_public_key_used,
        checkout_email=request.user.email,
        checkout_callback_url=callback_url,
        checkout_cancel_url=cancel_url,
        checkout_notice=notice,
    )
    return render(request, "portal/paystack_browser_checkout.html", context)


def _paystack_reference_from_request(request):
    return (request.GET.get("reference") or request.GET.get("trxref") or "").strip()


def _student_identifier(user):
    return user.id_number or user.username or str(user.pk)


def _verification_matches_payment(verification, *, reference, amount):
    return (
        verification.get("reference") == reference
        and verification.get("status") == "success"
        and int(verification.get("amount") or 0) == paystack_amount_in_kobo(amount)
    )


def _configured_paystack_webhook_secrets():
    secrets = set()
    if settings.PAYSTACK_SECRET_KEY:
        secrets.add(settings.PAYSTACK_SECRET_KEY)
    subscription_gateway = SubscriptionPaymentGateway.objects.filter(
        slug="subscription",
        is_active=True,
    ).first()
    if subscription_gateway and subscription_gateway.is_configured:
        secrets.add(subscription_gateway.paystack_secret_key)
    course_gateway = course_payment_gateway()
    if course_gateway.paystack_secret_key:
        secrets.add(course_gateway.paystack_secret_key)
    secrets.update(
        DepartmentPaymentGateway.objects.exclude(paystack_secret_key="").values_list("paystack_secret_key", flat=True)
    )
    secrets.update(
        DepartmentCoursePaymentGateway.objects.exclude(paystack_secret_key="").values_list("paystack_secret_key", flat=True)
    )
    # Keep in-flight payments verifiable if an admin rotates a gateway key before
    # Paystack delivers the final webhook.
    secrets.update(
        CoursePayment.objects.exclude(paystack_secret_key_used="").values_list("paystack_secret_key_used", flat=True)
    )
    secrets.update(
        DepartmentalPayment.objects.exclude(paystack_secret_key_used="").values_list("paystack_secret_key_used", flat=True)
    )
    return [secret for secret in secrets if secret]


def _paystack_signing_secret(request, secret_keys):
    signature = request.headers.get("X-Paystack-Signature", "")
    for secret_key in secret_keys:
        expected = hmac.new(
            secret_key.encode("utf-8"),
            request.body,
            hashlib.sha512,
        ).hexdigest()
        if hmac.compare_digest(signature, expected):
            return secret_key
    return ""


def _paystack_signature_is_valid(request, secret_keys):
    return bool(_paystack_signing_secret(request, secret_keys))


def _mark_payment_paid_from_paystack(reference, amount, *, signing_secret):
    if not reference:
        return False
    course_payment = CoursePayment.objects.filter(paystack_reference=reference).select_related("course").first()
    if (
        course_payment
        and hmac.compare_digest(course_payment.paystack_secret_key_used, signing_secret)
        and int(amount or 0) == paystack_amount_in_kobo(course_payment.amount)
    ):
        _finalize_course_payment(course_payment)
        return True
    departmental_payment = DepartmentalPayment.objects.filter(paystack_reference=reference).first()
    if (
        departmental_payment
        and hmac.compare_digest(departmental_payment.paystack_secret_key_used, signing_secret)
        and int(amount or 0) == paystack_amount_in_kobo(departmental_payment.total_amount)
    ):
        _finalize_departmental_payment(departmental_payment)
        return True
    return False


def _finalize_course_payment(payment):
    with transaction.atomic():
        payment = CoursePayment.objects.select_for_update().select_related("student", "course", "session").get(pk=payment.pk)
        if not payment.is_paid:
            payment.status = CoursePayment.Status.PAID
            payment.save(update_fields=["status", "paid_at", "updated_at"])
        # A level change can invalidate an in-flight checkout. Keep its audit
        # record as paid, but never let it restore the cleared registration.
        if not payment.is_active_for_registration:
            return
        registration, created = StudentCourseRegistration.objects.get_or_create(
            student=payment.student,
            course=payment.course,
            session=payment.session,
            defaults={"registered_by": payment.student},
        )
        if created:
            sync_material_access_for_registration(registration)
        _assign_student_to_existing_course_group(payment.student, payment.course, payment.session)
        _sync_course_material_access_for_paid_course(payment.student, payment.course)


def _finalize_departmental_payment(payment):
    with transaction.atomic():
        payment = DepartmentalPayment.objects.select_for_update().get(pk=payment.pk)
        if not payment.is_paid:
            payment.status = DepartmentalPayment.Status.PAID
            payment.save(update_fields=["status", "paid_at", "updated_at"])


def _sync_course_material_access_for_paid_course(student, course):
    course_materials = CourseMaterial.objects.filter(course=course)
    for material in course_materials:
        access, _ = MaterialAccess.objects.get_or_create(
            material=material,
            student=student,
            defaults={"status": MaterialAccess.Status.PAID},
        )
        if access.status != MaterialAccess.Status.BLOCKED:
            if access.status != MaterialAccess.Status.PAID:
                access.status = MaterialAccess.Status.PAID
                access.save(update_fields=["status", "updated_at"])


DEPARTMENTAL_CONSTANTS = [
    ("departmental_fee", "Departmental Fee"),
    ("acf", "ACF"),
    ("mssn", "MSSN"),
]


def ensure_departmental_defaults():
    for code, name in DEPARTMENTAL_CONSTANTS:
        DepartmentalAssociation.objects.get_or_create(
            code=code,
            department=None,
            defaults={"name": name, "is_constant": True},
        )


def departmental_associations_for_department(department):
    """Return the shared defaults plus associations created for one department."""
    if not department:
        return DepartmentalAssociation.objects.none()
    return DepartmentalAssociation.objects.filter(Q(department__isnull=True) | Q(department=department))


def current_departmental_session():
    ensure_departmental_defaults()
    session = AcademicSession.objects.filter(is_current=True).first()
    if session:
        return session
    created_session = AcademicSession.objects.create(name=f"{timezone.now().year}/{timezone.now().year + 1}", is_current=True)
    for department in Department.objects.all():
        for association in departmental_associations_for_department(department):
            DepartmentalFee.objects.get_or_create(
                session=created_session,
                department=department,
                association=association,
                defaults={"amount": Decimal("0.00")},
            )
    return created_session


def reset_lecturer_course_registrations():
    """Clear teaching assignments so a new session starts with fresh allocations."""
    registration_count = LecturerCourseRegistration.objects.count()
    LecturerCourseRegistration.objects.all().delete()
    Course.objects.exclude(lecturer__isnull=True).update(lecturer=None)
    return registration_count


def current_departmental_semester_label(reference_time=None):
    reference_time = reference_time or timezone.now()
    local_time = timezone.localtime(reference_time)
    return "First Semester" if local_time.month <= 6 else "Second Semester"


def fee_map_for_session(session, department):
    ensure_departmental_defaults()
    fee_map = {}
    associations = departmental_associations_for_department(department)
    for association in associations:
        fee, _ = DepartmentalFee.objects.get_or_create(
            session=session,
            department=department,
            association=association,
            defaults={"amount": Decimal("0.00")},
        )
        fee_map[association.code] = fee
    return fee_map


def ensure_departmental_fees_for_department(department):
    ensure_departmental_defaults()
    for session in AcademicSession.objects.all():
        for association in departmental_associations_for_department(department):
            DepartmentalFee.objects.get_or_create(
                session=session,
                department=department,
                association=association,
                defaults={"amount": Decimal("0.00")},
            )


def departmental_payment_queryset():
    return (
        DepartmentalPayment.objects.select_related("student", "department", "session", "student__department")
        .prefetch_related("items__association", "documents")
        .filter(status=DepartmentalPayment.Status.PAID)
    )


def current_course_payment_queryset():
    """Course payments made for the academic session currently in progress."""
    return CoursePayment.objects.filter(
        session=current_departmental_session(),
        is_active_for_registration=True,
    )


def _active_course_payment(student, course, session):
    return (
        CoursePayment.objects.filter(
            student=student,
            course=course,
            session=session,
            is_active_for_registration=True,
        )
        .order_by("-enrollment_sequence", "-created_at")
        .first()
    )


def _next_course_payment_sequence(student, course, session):
    latest_sequence = CoursePayment.objects.filter(
        student=student,
        course=course,
        session=session,
    ).aggregate(latest=Max("enrollment_sequence"))["latest"]
    return (latest_sequence or 0) + 1


def paid_course_students_queryset(course, session):
    """Current-session enrolled students with an active paid course payment."""
    return (
        CoursePayment.objects.filter(
            course=course,
            session=session,
            status=CoursePayment.Status.PAID,
            is_active_for_registration=True,
            student__student_registrations__course=course,
            student__student_registrations__session=session,
        )
        .select_related("student", "student__department", "session")
        .order_by("student__id_number", "student__username")
        .distinct()
    )


def _assign_student_to_existing_course_group(student, course, session):
    """Add a newly paid student to an enabled course grouping, when one exists."""
    if CourseStudentGroupMembership.objects.filter(
        student=student,
        group__course=course,
        group__session=session,
    ).exists():
        return

    groups = CourseStudentGroup.objects.filter(course=course, session=session)
    grouping_method = groups.values_list("grouping_method", flat=True).first()
    if not grouping_method:
        return

    if grouping_method == CourseStudentGroup.GroupingMethod.DEPARTMENT:
        target_group = groups.filter(department=student.department).first()
        if target_group is None:
            department = student.department
            base_name = department.name if department else "No department"
            group_name = base_name
            if groups.filter(name=group_name).exists():
                group_name = f"{base_name} ({department.code})" if department else "No department (new)"
            target_group = CourseStudentGroup.objects.create(
                course=course,
                lecturer=course.lecturer,
                session=session,
                name=group_name,
                grouping_method=grouping_method,
                department=department,
            )
    else:
        target_group = groups.annotate(student_total=Count("memberships")).order_by("student_total", "name").first()

    CourseStudentGroupMembership.objects.get_or_create(group=target_group, student=student)


def departmental_download_context(request, scope="admin"):
    search_form = DepartmentalSearchForm(request.GET or None)
    current_session = current_departmental_session()
    # Keep the overview focused on the active session. Selecting a student
    # below loads that student's complete paid departmental history.
    payments = departmental_payment_queryset().filter(session=current_session)
    if scope == "lecturer" and request.user.department_id:
        payments = payments.filter(department=request.user.department)
    selected_payment = None
    selected_student = None
    selected_student_payments = DepartmentalPayment.objects.none()
    selected_payment_id = request.GET.get("payment")
    if selected_payment_id:
        selected_payment = payments.filter(pk=selected_payment_id).first()
    if search_form.is_valid():
        student_id = search_form.cleaned_data.get("student_id")
        department = search_form.cleaned_data.get("department")
        level = search_form.cleaned_data.get("level")
        if student_id:
            payments = payments.filter(student__id_number__icontains=student_id)
        if department:
            payments = payments.filter(department=department)
        if level:
            payments = payments.filter(student__level=level)
        if not selected_payment:
            exact_match = payments.filter(student__id_number=student_id).first() if student_id else None
            selected_payment = exact_match or payments.first()
    if selected_payment:
        selected_student = selected_payment.student
        selected_student_payments = departmental_payment_queryset().filter(student=selected_student)
        if scope == "lecturer" and request.user.department_id:
            selected_student_payments = selected_student_payments.filter(department=request.user.department)
    department_totals = (
        payments.values("department__id", "department__name")
        .annotate(total=Count("id"))
        .order_by("department__name")
    )
    level_totals = (
        payments.values("department__id", "department__name", "student__level")
        .annotate(total=Count("id"))
        .order_by("department__name", "student__level")
    )
    return {
        "departmental_search_form": search_form,
        "departmental_selected_payment": selected_payment,
        "departmental_selected_student": selected_student,
        "departmental_selected_student_payments": selected_student_payments,
        "departmental_completed_payments": payments[:50],
        "departmental_department_totals": department_totals,
        "departmental_level_totals": level_totals,
        "departmental_download_scope": scope,
        "departmental_current_session": current_session,
    }


def _course_catalog_queryset(user=None):
    current_session = current_departmental_session()
    queryset = Course.objects.select_related("department", "lecturer").filter(
        Q(academic_session=current_session) | Q(academic_session__isnull=True)
    )
    if user and user.role == User.Role.LECTURER:
        queryset = queryset.filter(Q(lecturer__isnull=True) | Q(lecturer=user))
    return queryset


def _student_curriculum(user):
    """Get the curriculum fixed to a student's entry cohort.

    Existing accounts are assigned once on their first course visit, preserving
    the curriculum that was active at deployment. New accounts are assigned at
    signup, before they can register a course.
    """
    if not user.department_id:
        return None
    current_session = current_departmental_session()
    if (
        user.curriculum_id and user.curriculum.department_id == user.department_id
        and user.curriculum.programme_id == user.programme_id
    ):
        # Keep cohorts fixed within a session. A curriculum published for a
        # newly current session takes effect for its students.
        if not current_session or user.curriculum.effective_session_id == current_session.id:
            return user.curriculum
        replacement = (
            curriculum_for_programme(user.programme, current_session)
            if user.programme_id else curriculum_for_department(user.department, current_session)
        )
        if replacement and replacement.effective_session_id == current_session.id:
            user.curriculum = replacement
            user.save(update_fields=["curriculum"])
            return replacement
        return user.curriculum
    curriculum = (
        curriculum_for_programme(user.programme, current_departmental_session())
        if user.programme_id else curriculum_for_department(user.department, current_departmental_session())
    )
    if curriculum:
        user.curriculum = curriculum
        user.save(update_fields=["curriculum"])
    return curriculum


def _student_course_catalog_queryset(student):
    curriculum = _student_curriculum(student)
    if not curriculum:
        return Course.objects.none()
    # The many-to-many curriculum relation permits a single shared course to
    # appear in more than one programme without exposing either programme's
    # private courses to the other.  The FK fallback preserves pre-migration
    # course records until they are next saved.
    queryset = Course.objects.select_related("department", "lecturer", "curriculum").filter(
        department=student.department,
    ).filter(Q(curricula=curriculum) | Q(curricula__isnull=True, curriculum=curriculum))
    if student.programme_id:
        queryset = queryset.filter(programmes=student.programme)
    else:
        queryset = queryset.filter(programmes__isnull=True)
    # A student moves through the same curriculum as their level changes. Old
    # records that predate a stored level remain browsable for compatibility.
    if student.level:
        queryset = queryset.filter(level=student.level)
    return queryset.distinct()


def _student_course_row_context(registration, payment, group_name=None):
    course = registration.course
    is_paid = course.is_free or bool(payment and payment.is_paid)
    display_amount = payment.amount if payment else course.amount
    return {
        "registration": registration,
        "payment": payment,
        "display_amount": display_amount,
        "is_paid": is_paid,
        "needs_payment": not is_paid,
        "can_download": bool(course.file) and is_paid,
        "group_name": group_name,
    }


def _student_group_names_by_course(student, session):
    memberships = CourseStudentGroupMembership.objects.filter(
        student=student,
        group__session=session,
    ).select_related("group", "group__course")
    return {membership.group.course_id: membership.group.name for membership in memberships}


def _lecturer_student_scope(request):
    filter_form = LecturerCourseFilterForm(request.GET or None)
    courses = lecturer_registered_courses_queryset(request.user)
    search = ""
    if filter_form.is_valid():
        department = filter_form.cleaned_data.get("department")
        level = filter_form.cleaned_data.get("level")
        search = filter_form.cleaned_data.get("search")
        if department:
            courses = courses.filter(department=department)
        if level:
            courses = courses.filter(level=level)
        courses = _search_filter(courses, search, "code", "title", "department__name")

    students = (
        User.objects.filter(role=User.Role.STUDENT, student_registrations__course__in=courses)
        .distinct()
        .select_related("department")
    )
    students = _search_filter(students, search, "id_number", "username", "first_name", "last_name")
    return filter_form, courses, students


def home_context(
    student_login_form=None,
):
    ensure_default_admin_user()
    return {
        "student_login_form": student_login_form or PortalAuthenticationForm(initial={"role": User.Role.STUDENT}),
        "department_count": Department.objects.count(),
        "course_count": Course.objects.count(),
        "student_count": User.objects.filter(role=User.Role.STUDENT).count(),
        "material_count": CourseMaterial.objects.count(),
        "featured_courses": Course.objects.select_related("department", "lecturer")[:6],
    }


@ensure_csrf_cookie
@never_cache
def index(request):
    return render(request, "portal/index.html", home_context())


@ensure_csrf_cookie
@never_cache
def portal_login(request, role=None):
    ensure_default_admin_user()
    requested_role = role
    if requested_role and requested_role not in LOGIN_ROLES:
        messages.error(request, "Please choose a valid login page.")
        return redirect("portal:home")

    if request.method == "GET":
        return render(request, "portal/auth_login.html", auth_page_context(requested_role))

    form = PortalAuthenticationForm(request.POST, initial={"role": requested_role})
    if not form.is_valid():
        if role:
            form.add_error(None, "Please complete the login form correctly.")
            return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
        messages.error(request, "Please complete the login form correctly.")
        return redirect("portal:home")

    submitted_role = form.cleaned_data["role"]
    username = form.cleaned_data["username"]
    password = form.cleaned_data["password"]

    if requested_role and requested_role != submitted_role:
        if requested_role:
            form.add_error(None, "Please use the login form for this account type.")
            return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
        messages.error(request, "Please use the correct login section for this account.")
        return redirect("portal:home")

    active_role = requested_role or submitted_role

    if _login_is_throttled(request, username):
        form.add_error(None, "Too many failed sign-in attempts. Please try again in 15 minutes.")
        return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))

    user = authenticate(
        request,
        username=username,
        password=password,
    )

    if not user:
        _record_failed_login(request, username)
        if requested_role:
            form.add_error(None, "Invalid username or password.")
            return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
        messages.error(request, "Invalid username or password.")
        return redirect("portal:home")
    if active_role == User.Role.ADMIN:
        if user.is_superuser:
            form.add_error(None, "Super administrator accounts must use the super administrator login.")
            return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
        if not user.has_role(User.Role.ADMIN):
            if requested_role:
                form.add_error(None, "This account is not an admin account.")
                return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
            messages.error(request, "This account is not an admin account.")
            return redirect("portal:home")
    elif active_role and not user.has_role(active_role):
        if requested_role:
            form.add_error(None, "Please use the correct login section for this account.")
            return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
        messages.error(request, "Please use the correct login section for this account.")
        return redirect("portal:home")
    if user.role == User.Role.LECTURER and not user.is_approved:
        if requested_role:
            form.add_error(None, "Your lecturer account is waiting for admin approval.")
            return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
        messages.warning(request, "Your lecturer account is waiting for admin approval.")
        return redirect("portal:home")

    _clear_failed_logins(request, username)
    login(request, user)
    AuditLog.objects.create(
        institution=user.institution,
        user=user,
        action="login",
        description="User signed in.",
        ip_address=request.META.get("REMOTE_ADDR") or None,
    )
    return redirect("portal:dashboard")


@ensure_csrf_cookie
@never_cache
def student_signup(request):
    if request.method == "GET":
        return render(request, "portal/auth_signup.html", signup_page_context(
            User.Role.STUDENT,
            signup_form=StudentSignupForm(institution=getattr(request, "institution", None)),
        ))
    form = StudentSignupForm(request.POST, institution=getattr(request, "institution", None))
    if form.is_valid():
        user = form.save()
        user.curriculum = (
            curriculum_for_programme(user.programme, current_departmental_session())
            if user.programme_id else curriculum_for_department(user.department, current_departmental_session())
        )
        user.save(update_fields=["curriculum"])
        authenticated_user = authenticate(
            request,
            username=form.cleaned_data["username"],
            password=form.cleaned_data["password1"],
        )
        if authenticated_user:
            login(request, authenticated_user)
        else:
            login(request, user)
        messages.success(request, "Student account created successfully. You are now signed in.")
        return redirect("portal:student-dashboard")
    return render(request, "portal/auth_signup.html", signup_page_context(User.Role.STUDENT, signup_form=form))


@ensure_csrf_cookie
@never_cache
def lecturer_signup(request):
    if request.method == "GET":
        return render(request, "portal/auth_signup.html", signup_page_context(
            User.Role.LECTURER,
            signup_form=LecturerSignupForm(institution=getattr(request, "institution", None)),
        ))
    form = LecturerSignupForm(request.POST, institution=getattr(request, "institution", None))
    if form.is_valid():
        form.save()
        messages.success(request, "Lecturer account created successfully.")
        messages.warning(request, "Your lecturer account is pending admin approval before first dashboard access.")
        return redirect("portal:role-login", role=User.Role.LECTURER)
    return render(
        request,
        "portal/auth_signup.html",
        signup_page_context(User.Role.LECTURER, signup_form=form),
    )


@require_http_methods(["GET", "POST"])
def portal_logout(request):
    if request.user.is_authenticated:
        AuditLog.objects.create(
            institution=request.user.institution,
            user=request.user,
            action="logout",
            description="User signed out.",
            ip_address=request.META.get("REMOTE_ADDR") or None,
        )
    logout(request)
    if request.method == "POST":
        messages.success(request, "You have been signed out.")
    return redirect("portal:home")


@csrf_exempt
@never_cache
def paystack_webhook(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "POST required."}, status=405)
    signing_secret = _paystack_signing_secret(request, _configured_paystack_webhook_secrets())
    if not signing_secret:
        return JsonResponse({"ok": False, "message": "Invalid Paystack signature."}, status=400)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "message": "Invalid JSON payload."}, status=400)

    event = payload.get("event", "")
    data = payload.get("data") or {}
    processed = False
    if event == "charge.success":
        processed = _mark_payment_paid_from_paystack(
            data.get("reference"),
            data.get("amount"),
            signing_secret=signing_secret,
        )
        subscription_secret = subscription_paystack_secret_key()
        if (
            not processed
            and subscription_secret
            and hmac.compare_digest(signing_secret, subscription_secret)
            and Payment.objects.filter(reference=data.get("reference")).exists()
        ):
            try:
                _, processed = verify_and_finalize_subscription_payment(data.get("reference"))
            except ValueError:
                # A failed verification is recorded by the finalizer; do not
                # acknowledge it as a successful subscription update.
                processed = False
    return JsonResponse({"ok": True, "event": event, "processed": processed})


def dashboard_redirect(request):
    if not request.user.is_authenticated:
        return redirect("portal:home")
    if request.user.is_superuser:
        return redirect("portal:super-admin-dashboard")
    return redirect(_role_dashboard_name(active_role(request)))


@require_http_methods(["POST"])
def switch_role(request):
    if not request.user.is_authenticated or request.user.is_superuser:
        raise PermissionDenied
    requested_role = request.POST.get("role", "")
    if requested_role not in request.user.active_roles():
        raise PermissionDenied("You can only switch to a role assigned to your account.")
    request.session["active_role"] = requested_role
    return redirect("portal:dashboard")


@role_required(*ROLE_MODULES.keys())
def role_workspace(request):
    selected_role = active_role(request)
    roles = [User.Role(value).label for value in sorted(request.user.active_roles())]
    return render(request, "portal/role_workspace.html", dashboard_context(
        request, f"{User.Role(selected_role).label} Workspace", assigned_roles=roles,
        modules=ROLE_MODULES.get(selected_role, ()),
    ))


@role_required(*ROLE_MODULES.keys())
def role_module(request, module):
    """Tenant- and role-scoped operational views for the institutional roles."""
    selected_role = active_role(request)
    available = dict(ROLE_MODULES.get(selected_role, ()))
    if module not in available:
        raise PermissionDenied("This module is not assigned to your active role.")
    if module == "exam-officer":
        return redirect("portal:hod-exam-officer")

    title = available[module]
    department = None
    if selected_role in {User.Role.HOD, User.Role.EXAM_OFFICER}:
        department = hod_department_for(request) if selected_role == User.Role.HOD else request.user.department
    students = User.objects.filter(role=User.Role.STUDENT)
    lecturers = User.objects.filter(role=User.Role.LECTURER)
    courses = Course.objects.all()
    if department:
        students = students.filter(department=department)
        lecturers = lecturers.filter(department=department)
        courses = courses.filter(department=department)

    rows, columns, notice = [], (), ""
    if module in {"students", "linked-students"}:
        if module == "linked-students":
            students = User.objects.filter(guardians__guardian=request.user, guardians__is_active=True)
        rows = list(students.values_list("full_name", "id_number", "department__name", "level")[:100])
        columns = ("Student", "ID", "Department", "Level")
    elif module == "lecturers":
        rows = list(lecturers.values_list("full_name", "id_number", "department__name", "email")[:100])
        columns = ("Lecturer", "Staff ID", "Department", "Email")
    elif module in {"courses", "academic-structure"}:
        rows = list(courses.values_list("code", "title", "department__name", "level")[:100])
        columns = ("Code", "Course", "Department", "Level")
    elif module == "allocations":
        rows = list(LecturerCourseRegistration.objects.filter(course__in=courses).values_list("course__code", "course__title", "lecturer__full_name")[:100])
        columns = ("Course", "Title", "Lecturer")
    elif module in {"payments", "balances", "exam-cards", "eligibility"}:
        payments = DepartmentalPayment.objects.select_related("student", "department", "session")
        if department:
            payments = payments.filter(department=department)
        if module == "exam-cards":
            payments = payments.filter(status=DepartmentalPayment.Status.PAID)
        if module == "balances":
            payments = payments.exclude(status=DepartmentalPayment.Status.PAID)
        rows = list(payments.values_list("student__full_name", "department__name", "session__name", "status", "total_amount")[:100])
        columns = ("Student", "Department", "Session", "Status", "Amount")
    elif module == "applications":
        rows = list(ScreeningApplication.objects.values_list("external_application_id", "first_name", "last_name", "programme", "status")[:100])
        columns = ("Application", "First name", "Last name", "Programme", "Status")
    elif module == "registrations":
        rows = list(StudentCourseRegistration.objects.filter(course__in=courses).values_list("student__full_name", "course__code", "course__title", "session__name")[:100])
        columns = ("Student", "Course", "Title", "Session")
    elif module == "data-quality":
        rows = list(User.objects.filter(Q(email="") | Q(id_number__isnull=True) | Q(id_number="")).values_list("full_name", "role", "email", "id_number")[:100])
        columns = ("User", "Role", "Email", "ID")
        notice = "Records below need a missing identity field reviewed."
    elif module in {"reports", "results", "progress"}:
        notice = "This tenant-scoped summary is read-only. Result entry and publication remain subject to the existing academic approval workflow."
        rows = [("Students", students.count()), ("Lecturers", lecturers.count()), ("Courses", courses.count())]
        columns = ("Metric", "Count")

    return render(request, "portal/role_module.html", dashboard_context(
        request, title, module=module, rows=rows, columns=columns, notice=notice, department=department,
    ))


@ensure_csrf_cookie
@never_cache
def super_admin_login(request):
    if request.method == "GET":
        return render(request, "portal/auth_login.html", auth_page_context(User.Role.ADMIN, is_super_admin=True))
    form = PortalAuthenticationForm(request.POST, initial={"role": User.Role.ADMIN})
    if not form.is_valid():
        form.add_error(None, "Please complete the login form correctly.")
        return render(request, "portal/auth_login.html", auth_page_context(User.Role.ADMIN, login_form=form, is_super_admin=True))
    username = form.cleaned_data["username"]
    if _login_is_throttled(request, username):
        form.add_error(None, "Too many failed sign-in attempts. Please try again in 15 minutes.")
        return render(request, "portal/auth_login.html", auth_page_context(User.Role.ADMIN, login_form=form, is_super_admin=True))
    user = authenticate(request, username=username, password=form.cleaned_data["password"])
    if not user or not user.is_superuser:
        _record_failed_login(request, username)
        form.add_error(None, "Invalid super administrator credentials.")
        return render(request, "portal/auth_login.html", auth_page_context(User.Role.ADMIN, login_form=form, is_super_admin=True))
    _clear_failed_logins(request, username)
    login(request, user)
    AuditLog.objects.create(user=user, action="login", description="Super administrator signed in.")
    return redirect("portal:super-admin-dashboard")


@super_admin_required
def super_admin_dashboard(request):
    institutions = Institution.objects.all()
    payments = Payment.objects.filter(status=Payment.Status.SUCCESS)
    today = timezone.localdate()
    month_start = today.replace(day=1)
    annual_start = today.replace(month=1, day=1)
    context = {
        "institution_count": institutions.count(),
        "active_institution_count": institutions.filter(status=Institution.Status.ACTIVE).count(),
        "suspended_institution_count": institutions.filter(status=Institution.Status.SUSPENDED).count(),
        "expired_institution_count": institutions.filter(status=Institution.Status.EXPIRED).count(),
        "active_subscription_count": Subscription.objects.filter(status__in=[Subscription.Status.ACTIVE, Subscription.Status.EXPIRING_SOON]).count(),
        "expiring_subscription_count": Subscription.objects.filter(end_date__range=[today, today + timedelta(days=60)]).count(),
        "monthly_revenue": payments.filter(paid_at__date__gte=month_start).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
        "annual_revenue": payments.filter(paid_at__date__gte=annual_start).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
        "recent_payments": payments.select_related("institution", "plan")[:8],
        "recent_institutions": institutions.order_by("-created_at")[:8],
        "institution_user_counts": institution_overview_queryset()[:20],
    }
    return render(request, "portal/super_admin_dashboard.html", context)


@super_admin_required
def super_admin_institutions(request):
    form = InstitutionCreateForm()
    if request.method == "POST":
        form = InstitutionCreateForm(request.POST, request.FILES)
        if form.is_valid():
            with transaction.atomic():
                institution = form.save()
                institution.status = Institution.Status.ACTIVE
                institution.save(update_fields=["status", "updated_at"])
                name_parts = institution.name.split(maxsplit=1)
                admin = User.all_objects.create_user(
                    username=institution.email,
                    email=institution.email,
                    password=TEMPORARY_ACCOUNT_PASSWORD,
                    first_name=name_parts[0],
                    last_name=name_parts[1] if len(name_parts) > 1 else "",
                    role=User.Role.ADMIN,
                    institution=institution,
                )
                free_trial, _ = grant_free_trial_subscription(institution)
                AuditLog.objects.create(
                    user=request.user,
                    institution=institution,
                    action="institution_created",
                    description=(
                        f"Created {institution.name}, institution admin {admin.username}, and a six-month free trial "
                        f"ending {free_trial.end_date:%d %B %Y}."
                    ),
                    object_type="Institution",
                    object_id=str(institution.id),
                )
            messages.success(
                request,
                f"{institution.name} has been created. The administrator signs in with {admin.email} "
                f"and the temporary password '{TEMPORARY_ACCOUNT_PASSWORD}'. "
                f"A six-month free trial is active until {free_trial.end_date:%d %B %Y}. "
                f"Portal: https://{institution.subdomain}.{settings.PLATFORM_BASE_DOMAIN}",
            )
            return redirect("portal:super-admin-institutions")
    return render(request, "portal/super_admin_institutions.html", {
        "institutions": institution_overview_queryset(),
        "form": form,
    })


@super_admin_required
@require_http_methods(["GET", "POST"])
def super_admin_institution_edit(request, institution_id):
    institution = get_object_or_404(Institution, pk=institution_id)
    if request.method == "POST":
        form = InstitutionUpdateForm(request.POST, request.FILES, instance=institution)
        if form.is_valid():
            with transaction.atomic():
                institution = form.save()
                administrator = (
                    User.all_objects.filter(
                        institution=institution,
                        role=User.Role.ADMIN,
                        is_superuser=False,
                    )
                    .order_by("id")
                    .first()
                )
                if administrator and administrator.email != institution.email:
                    administrator.email = institution.email
                    administrator.username = institution.email
                    administrator.save(update_fields=["email", "username"])
                AuditLog.objects.create(
                    user=request.user,
                    institution=institution,
                    action="institution_updated",
                    description=f"Updated institution {institution.name}.",
                    object_type="Institution",
                    object_id=str(institution.id),
                )
            messages.success(request, f"{institution.name} has been updated.")
            return redirect("portal:super-admin-institutions")
    else:
        form = InstitutionUpdateForm(instance=institution)
    return render(request, "portal/super_admin_institution_form.html", {
        "form": form,
        "institution": institution,
    })


@super_admin_required
@require_http_methods(["POST"])
def super_admin_institution_delete(request, institution_id):
    institution = get_object_or_404(Institution, pk=institution_id)
    confirmation = request.POST.get("confirmation", "").strip()
    if confirmation != institution.name:
        messages.error(request, "To delete an institution, enter its exact name as confirmation.")
        return redirect("portal:super-admin-institution-edit", institution_id=institution.id)

    institution_name = institution.name
    with transaction.atomic():
        delete_institution_data(institution)
        AuditLog.objects.create(
            user=request.user,
            action="institution_deleted",
            description=f"Permanently deleted institution {institution_name} and its tenant data.",
            object_type="Institution",
        )
    messages.success(request, f"{institution_name} and its tenant data were permanently deleted.")
    return redirect("portal:super-admin-institutions")


@super_admin_required
@require_http_methods(["POST"])
def super_admin_institution_status(request, institution_id, status):
    if status not in {Institution.Status.ACTIVE, Institution.Status.SUSPENDED}:
        raise Http404
    institution = get_object_or_404(Institution, pk=institution_id)
    institution.status = status
    institution.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        user=request.user, institution=institution, action=f"institution_{status}",
        description=f"Institution status changed to {status}.", object_type="Institution", object_id=str(institution.id),
    )
    return redirect("portal:super-admin-institutions")


@super_admin_required
@require_http_methods(["GET", "POST"])
def super_admin_screening_configuration(request, institution_id):
    institution = get_object_or_404(Institution, pk=institution_id)
    integration, _ = ScreeningIntegration.objects.get_or_create(institution=institution)
    if request.method == "POST":
        if request.POST.get("action") == "rotate-secret":
            integration.rotate_secret()
            AuditLog.objects.create(
                user=request.user, institution=institution, action="screening_api_secret_rotated",
                description=f"Rotated the Screening API secret for {institution.name}.", object_type="ScreeningIntegration", object_id=str(integration.id),
            )
            messages.success(request, "A new API secret has been generated. Copy it now and update the Screening application.")
            return redirect("portal:super-admin-screening-configuration", institution_id=institution.id)
        form = ScreeningIntegrationForm(request.POST, instance=integration, institution=institution)
        if form.is_valid():
            integration = form.save()
            AuditLog.objects.create(
                user=request.user, institution=institution, action="screening_configuration_updated",
                description=f"Updated Online Screening configuration for {institution.name}.", object_type="ScreeningIntegration", object_id=str(integration.id),
            )
            messages.success(request, "Screening configuration saved.")
            return redirect("portal:super-admin-screening-configuration", institution_id=institution.id)
    else:
        form = ScreeningIntegrationForm(instance=integration, institution=institution)
    screening_host = f"apply.{institution.subdomain}.{settings.PLATFORM_BASE_DOMAIN}"
    return render(request, "portal/super_admin_screening_configuration.html", {
        "institution": institution, "integration": integration, "form": form, "screening_host": screening_host,
    })


@super_admin_required
@require_http_methods(["POST"])
def super_admin_reset_institution_admin_password(request, user_id):
    administrator = get_object_or_404(
        User.all_objects,
        pk=user_id,
        role=User.Role.ADMIN,
        is_superuser=False,
    )
    if not administrator.email:
        messages.error(request, "This institution administrator has no email address, so a verified reset cannot be sent.")
        return redirect("portal:super-admin-users" if request.POST.get("return_to") == "users" else "portal:super-admin-institutions")

    administrator.set_password(TEMPORARY_ACCOUNT_PASSWORD)
    administrator.save(update_fields=["password"])
    _send_password_reset_verification(request, administrator)
    AuditLog.objects.create(
        user=request.user,
        institution=administrator.institution,
        action="institution_admin_password_reset",
        description=f"Reset the password for institution administrator {administrator.username}.",
        object_type="User",
        object_id=str(administrator.id),
    )
    messages.success(
        request,
        f"{administrator.email}'s password was reset to '{TEMPORARY_ACCOUNT_PASSWORD}' and a verification link was sent.",
    )
    return redirect("portal:super-admin-users" if request.POST.get("return_to") == "users" else "portal:super-admin-institutions")


@super_admin_required
def super_admin_plans(request):
    form = SubscriptionPlanForm()
    duration_form = SubscriptionPlanDurationForm()
    if request.method == "POST":
        if request.POST.get("action") == "add-duration":
            duration_form = SubscriptionPlanDurationForm(request.POST)
            if duration_form.is_valid():
                duration = duration_form.save()
                AuditLog.objects.create(
                    user=request.user,
                    action="subscription_plan_duration_created",
                    description=f"Added {duration.duration_label} pricing for {duration.plan.name}.",
                )
                messages.success(request, f"Added {duration.duration_label} pricing for {duration.plan.name}.")
                return redirect("portal:super-admin-plans")
        else:
            form = SubscriptionPlanForm(request.POST)
            if form.is_valid():
                plan = form.save()
                AuditLog.objects.create(user=request.user, action="subscription_plan_created", description=f"Created plan {plan.name}.")
                messages.success(request, "Subscription plan created with its first duration and amount.")
                return redirect("portal:super-admin-plans")
    return render(request, "portal/super_admin_plans.html", {
        "plans": SubscriptionPlan.objects.prefetch_related("durations"),
        "form": form,
        "duration_form": duration_form,
    })


@super_admin_required
@require_http_methods(["GET", "POST"])
def super_admin_plan_edit(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, pk=plan_id)
    if plan.is_trial:
        raise Http404
    form = SubscriptionPlanEditForm(request.POST or None, instance=plan)
    if request.method == "POST" and form.is_valid():
        plan = form.save()
        AuditLog.objects.create(
            user=request.user,
            action="subscription_plan_updated",
            description=f"Updated plan {plan.name}.",
            object_type="SubscriptionPlan",
            object_id=str(plan.id),
        )
        messages.success(request, f"{plan.name} has been updated.")
        return redirect("portal:super-admin-plans")
    return render(request, "portal/super_admin_plan_form.html", {"form": form, "plan": plan})


@super_admin_required
@require_http_methods(["GET", "POST"])
def super_admin_plan_duration_edit(request, duration_id):
    duration = get_object_or_404(SubscriptionPlanDuration.objects.select_related("plan"), pk=duration_id)
    if duration.plan.is_trial:
        raise Http404
    form = SubscriptionPlanDurationEditForm(request.POST or None, instance=duration)
    if request.method == "POST" and form.is_valid():
        duration = form.save()
        AuditLog.objects.create(
            user=request.user,
            action="subscription_plan_duration_updated",
            description=f"Updated {duration.duration_label} pricing for {duration.plan.name}.",
            object_type="SubscriptionPlanDuration",
            object_id=str(duration.id),
        )
        messages.success(request, f"{duration.duration_label} pricing has been updated.")
        return redirect("portal:super-admin-plans")
    return render(request, "portal/super_admin_plan_duration_form.html", {"form": form, "duration": duration})


@super_admin_required
@require_http_methods(["POST"])
def super_admin_plan_toggle(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, pk=plan_id)
    if plan.is_trial:
        raise Http404
    plan.is_active = not plan.is_active
    plan.save(update_fields=["is_active", "updated_at"])
    AuditLog.objects.create(
        user=request.user,
        action="subscription_plan_activated" if plan.is_active else "subscription_plan_deactivated",
        description=f"{'Activated' if plan.is_active else 'Deactivated'} plan {plan.name}.",
        object_type="SubscriptionPlan",
        object_id=str(plan.id),
    )
    messages.success(request, f"{plan.name} has been {'activated' if plan.is_active else 'deactivated'}.")
    return redirect("portal:super-admin-plans")


@super_admin_required
@require_http_methods(["POST"])
def super_admin_plan_delete(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, pk=plan_id)
    if plan.is_trial:
        raise Http404
    if plan.subscriptions.exists() or plan.payments.exists():
        messages.error(request, f"{plan.name} has subscription or payment history and cannot be deleted. Deactivate it instead.")
        return redirect("portal:super-admin-plans")
    plan_name = plan.name
    plan.delete()
    AuditLog.objects.create(
        user=request.user,
        action="subscription_plan_deleted",
        description=f"Deleted unused plan {plan_name}.",
        object_type="SubscriptionPlan",
    )
    messages.success(request, f"{plan_name} has been deleted.")
    return redirect("portal:super-admin-plans")


@super_admin_required
@require_http_methods(["GET"])
def super_admin_subscriptions(request):
    return render(request, "portal/super_admin_subscriptions.html", {
        "subscriptions": Subscription.objects.select_related("institution", "plan")[:100],
    })


@super_admin_required
def super_admin_payments(request):
    return render(request, "portal/super_admin_payments.html", {"payments": Payment.objects.select_related("institution", "plan")[:200]})


@super_admin_required
def super_admin_audit_logs(request):
    logs = AuditLog.objects.select_related("institution", "user")
    query = request.GET.get("q", "").strip()
    if query:
        logs = logs.filter(Q(action__icontains=query) | Q(description__icontains=query) | Q(institution__name__icontains=query))
    return render(request, "portal/super_admin_audit_logs.html", {"logs": logs[:300], "query": query})


@super_admin_required
def super_admin_users(request):
    form = PlatformAdminForm()
    if request.method == "POST":
        form = PlatformAdminForm(request.POST)
        if form.is_valid():
            administrator = User.all_objects.create_superuser(
                username=form.cleaned_data["username"], email=form.cleaned_data["email"], password=form.cleaned_data["password"],
            )
            AuditLog.objects.create(user=request.user, action="platform_administrator_created", description=f"Created platform administrator {administrator.username}.")
            messages.success(request, "Platform administrator created.")
            return redirect("portal:super-admin-users")
    return render(request, "portal/super_admin_users.html", {
        "users": User.all_objects.filter(is_superuser=True),
        "institution_admins": User.all_objects.filter(
            role=User.Role.ADMIN, is_superuser=False,
        ).select_related("institution").order_by("institution__name", "username"),
        "form": form,
    })


@super_admin_required
def super_admin_reports(request):
    return render(request, "portal/super_admin_reports.html", {
        "institution_rows": Institution.objects.annotate(
            users_total=Count("users", distinct=True),
            student_total=Count("users", filter=Q(users__role=User.Role.STUDENT), distinct=True),
            lecturer_total=Count("users", filter=Q(users__role=User.Role.LECTURER), distinct=True),
            revenue=Sum("subscription_payments__amount", filter=Q(subscription_payments__status=Payment.Status.SUCCESS)),
        ),
    })


@super_admin_required
def super_admin_settings(request):
    gateway, _ = SubscriptionPaymentGateway.objects.get_or_create(slug="subscription")
    form = SubscriptionPaymentGatewayForm(instance=gateway)
    if request.method == "POST":
        form = SubscriptionPaymentGatewayForm(request.POST, instance=gateway)
        if form.is_valid():
            form.save()
            AuditLog.objects.create(user=request.user, action="subscription_gateway_updated", description="Updated the subscription payment gateway.")
            messages.success(request, "Subscription payment gateway saved.")
            return redirect("portal:super-admin-settings")
    return render(request, "portal/super_admin_settings.html", {
        "platform_domain": settings.PLATFORM_BASE_DOMAIN,
        "paystack_configured": gateway.is_configured or bool(settings.PAYSTACK_SECRET_KEY and settings.PAYSTACK_PUBLIC_KEY),
        "gateway_form": form,
    })


@super_admin_required
def super_admin_profile(request):
    if request.method == "POST":
        request.user.first_name = request.POST.get("first_name", "").strip()
        request.user.last_name = request.POST.get("last_name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        if email:
            request.user.email = email
        request.user.save(update_fields=["first_name", "last_name", "email"])
        AuditLog.objects.create(user=request.user, action="profile_updated", description="Super administrator updated their profile.")
        messages.success(request, "Profile updated.")
        return redirect("portal:super-admin-profile")
    return render(request, "portal/super_admin_profile.html")


@super_admin_required
@require_http_methods(["GET", "POST"])
def super_admin_ai(request):
    answer = None
    if request.method == "POST":
        question = request.POST.get("question", "").casefold()
        if "expir" in question or "subscription" in question:
            answer = f"{Subscription.objects.filter(end_date__lte=timezone.localdate() + timedelta(days=60)).count()} subscription(s) expire within 60 days."
        else:
            answer = f"There are {Institution.objects.filter(status=Institution.Status.ACTIVE).count()} active institutions on the platform."
    return render(request, "portal/super_admin_ai.html", {"answer": answer})


@role_required(User.Role.ADMIN, User.Role.ADMISSION_OFFICER)
def screening_portal(request):
    institution = request.institution
    integration = get_object_or_404(ScreeningIntegration, institution=institution)
    url_template = getattr(settings, "SCREENING_APPLICATION_URL_TEMPLATE", "https://apply.{subdomain}.{base_domain}")
    return redirect(url_template.format(subdomain=institution.subdomain, base_domain=settings.PLATFORM_BASE_DOMAIN, institution_code=institution.institution_code))


AI_ROLE_EXAMPLES = {
    User.Role.ADMIN: ("How many students are in this institution?", "Which departments are configured?"),
    User.Role.MIS: ("How many student records need a department?", "How many student records are active?"),
    User.Role.BURSARY: ("How many payments are pending?", "What is the paid total?"),
    User.Role.ADMISSION_OFFICER: ("How many applications are pending?", "How many applicants have been admitted?"),
    User.Role.ACADEMIC_PLANNING: ("Which academic session is current?", "How many courses are in the catalogue?"),
    User.Role.HOD: ("How many students are in my department?", "Which lecturers are in my department?"),
    User.Role.EXAM_OFFICER: ("How many students have registrations in my department?", "How many department courses are available?"),
    User.Role.LECTURER: ("What are my assigned courses?", "How many materials have I uploaded?"),
    User.Role.SENATE_MEMBER: ("How many departments does this institution have?", "Which academic session is current?"),
    User.Role.STUDENT: ("What are my registered courses?", "What is my department?"),
    User.Role.PARENT_GUARDIAN: ("Which students are linked to me?", "Can I view linked-student finance information?"),
}


def _role_department(user, role):
    """Return only the department assigned to an active departmental role."""
    if role == User.Role.HOD:
        department = Department.objects.filter(head_of_department=user, pk=user.department_id).first()
        if department:
            return department
    assignment = RoleAssignment.objects.filter(
        user=user, role=role, is_active=True, department__isnull=False,
    ).select_related("department").first()
    return assignment.department if assignment else user.department


def _ai_answer(user, role, question):
    """Use an explicit role allow-list; never use the union of user roles."""
    question = question.casefold()
    institution_id = user.institution_id
    if not institution_id:
        return "Your account is not attached to an institution, so no institutional information is available."

    students = User.all_objects.filter(institution_id=institution_id, role=User.Role.STUDENT)
    courses = Course.objects.filter(institution_id=institution_id)
    faculties = Faculty.objects.filter(institution_id=institution_id)
    departments = Department.objects.filter(institution_id=institution_id)

    if role == User.Role.STUDENT:
        if "course" in question:
            names = ", ".join(StudentCourseRegistration.objects.filter(student=user).select_related("course").values_list("course__code", flat=True)[:12])
            return f"Your registered courses are: {names or 'none yet'}."
        if "department" in question:
            return f"Your department is {user.department.name if user.department else 'not assigned yet'}."
        return "I can help with your own courses, payments, timetable, documents, and course registration."

    if role == User.Role.LECTURER:
        assignments = LecturerCourseRegistration.objects.filter(lecturer=user).select_related("course")
        if "material" in question:
            return f"You have uploaded {CourseMaterial.objects.filter(lecturer=user).count()} course material(s)."
        if "student" in question:
            return f"{StudentCourseRegistration.objects.filter(course_id__in=assignments.values('course_id')).values('student_id').distinct().count()} student(s) are registered on your assigned courses."
        names = ", ".join(assignments.values_list("course__code", flat=True)[:12])
        return f"Your assigned courses are: {names or 'none yet'}. I can also help with your materials and messages."

    if role in {User.Role.HOD, User.Role.EXAM_OFFICER}:
        department = _role_department(user, role)
        if not department:
            return f"No department is assigned to your {User.Role(role).label} role."
        department_courses = courses.filter(department=department)
        if "lecturer" in question and role == User.Role.HOD:
            count = User.all_objects.filter(institution_id=institution_id, department=department, role=User.Role.LECTURER).count()
            return f"{count} lecturer(s) belong to {department.name}."
        if "student" in question or "registration" in question:
            if role == User.Role.EXAM_OFFICER:
                count = StudentCourseRegistration.objects.filter(course__department=department).values("student_id").distinct().count()
            else:
                count = students.filter(department=department).count()
            return f"{count} student(s) are within your authorised {department.name} scope."
        return f"{department_courses.count()} course(s) are within your authorised {department.name} scope."

    if role == User.Role.PARENT_GUARDIAN:
        links = user.guarded_students.filter(is_active=True).select_related("student")
        if "finance" in question or "payment" in question or "fee" in question:
            names = ", ".join(link.student.full_name for link in links if link.can_view_finance)
            return f"You may view finance information for: {names or 'no linked students'}."
        if "result" in question:
            names = ", ".join(link.student.full_name for link in links if link.can_view_results)
            return f"You may view permitted results for: {names or 'no linked students'}."
        return "Your linked students are: " + (", ".join(link.student.full_name for link in links) or "none yet") + "."

    if role == User.Role.BURSARY:
        if "pending" in question or "outstanding" in question or "failed" in question:
            count = DepartmentalPayment.objects.exclude(status=DepartmentalPayment.Status.PAID).count() + CoursePayment.objects.exclude(status=CoursePayment.Status.PAID).count()
            return f"There are {count} non-paid departmental or course-payment record(s) requiring authorised follow-up."
        total = (DepartmentalPayment.objects.filter(status=DepartmentalPayment.Status.PAID).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")) + (CoursePayment.objects.filter(status=CoursePayment.Status.PAID).aggregate(total=Sum("amount"))["total"] or Decimal("0"))
        return f"Authorised paid departmental and course-payment total: ₦{total:,.2f}."

    if role == User.Role.ADMISSION_OFFICER:
        applications = ScreeningApplication.objects.filter(institution_id=institution_id)
        if "pending" in question:
            return f"{applications.filter(status=ScreeningApplication.Status.PENDING).count()} application(s) are pending."
        return f"{applications.filter(status=ScreeningApplication.Status.ADMITTED).count()} application(s) have been admitted."

    if role == User.Role.ACADEMIC_PLANNING:
        if "session" in question:
            current = AcademicSession.objects.filter(is_current=True).values_list("name", flat=True).first()
            return f"The current academic session is {current or 'not configured'}."
        return f"The academic structure contains {faculties.count()} facult(ies), {departments.count()} department(s), and {courses.count()} course(s)."

    if role == User.Role.MIS:
        if "missing" in question or "quality" in question:
            return f"{students.filter(department__isnull=True).count()} student record(s) have no department assignment."
        return f"There are {students.count()} student record(s) in your authorised MIS scope."

    if role == User.Role.SENATE_MEMBER:
        current = AcademicSession.objects.filter(is_current=True).values_list("name", flat=True).first()
        return f"Academic summary: {faculties.count()} facult(ies), {departments.count()} department(s), {courses.count()} course(s); current session: {current or 'not configured'}."

    # Institution Admin receives tenant-wide operational summaries only.
    if role == User.Role.ADMIN:
        if "department" in question:
            return "Departments: " + (", ".join(departments.values_list("name", flat=True)[:20]) or "none") + "."
        staff_count = User.all_objects.filter(institution_id=institution_id).exclude(role=User.Role.STUDENT).count()
        return f"This institution has {students.count()} student(s), {staff_count} staff account(s), and {courses.count()} course(s). AI Automation is not available."

    return "No AI scope is configured for this active role."


@role_required(*User.Role.values)
@require_http_methods(["GET", "POST"])
def ai_assistant(request):
    answer = None
    question = ""
    if request.method == "POST":
        question = request.POST.get("question", "").strip()
        if question:
            selected_role = active_role(request)
            answer = _ai_answer(request.user, selected_role, question)
            AuditLog.objects.create(
                user=request.user, institution=request.institution, action="ai_assistant_query",
                description="A user queried the active-role, institution-scoped EduConnect AI assistant.", object_type="EduConnectAI",
                new_value={"active_role": selected_role},
                ip_address=_client_ip(request),
            )
    selected_role = active_role(request)
    return render(request, "portal/ai_assistant.html", dashboard_context(
        request, "EduConnect AI", answer=answer, question=question,
        ai_role_label=User.Role(selected_role).label,
        ai_examples=AI_ROLE_EXAMPLES.get(selected_role, ()),
    ))


@role_required(User.Role.ADMIN, User.Role.MIS)
@require_http_methods(["GET", "POST"])
def ai_automation(request):
    """Tenant-scoped, human-approved document automation workflow."""
    is_institution_admin = request.user.has_role(User.Role.ADMIN)
    jobs = AIAutomationJob.objects.select_related("requested_by", "approved_by").order_by("-created_at")
    if not is_institution_admin:
        jobs = jobs.filter(requested_by=request.user)

    form = AIAutomationUploadForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "analyze":
            form = AIAutomationUploadForm(request.POST, request.FILES)
            if form.is_valid():
                upload = form.cleaned_data["file"]
                command = form.cleaned_data["command"]
                try:
                    proposal = analyze_institution_automation(upload, command)
                except (ValueError, ValidationError) as exc:
                    form.add_error(None, str(exc))
                else:
                    job = AIAutomationJob.objects.create(
                        file=upload,
                        command=command,
                        action=proposal["action"],
                        analysis=proposal["analysis"],
                        proposed_changes=proposal["proposed_changes"],
                        requested_by=request.user,
                    )
                    AuditLog.objects.create(
                        institution=request.institution, user=request.user,
                        action="ai_automation_analyzed", object_type="AIAutomationJob", object_id=str(job.id),
                        new_value={"action": job.action, "proposed_count": len(job.proposed_changes)},
                        description="AI/document automation proposal generated; no records were changed.",
                        ip_address=_client_ip(request),
                    )
                    messages.success(request, "Proposal analyzed. Review it before an Institution Admin approves execution.")
                    return redirect("portal:ai-automation")
        elif action in {"approve", "execute"}:
            if not is_institution_admin:
                raise PermissionDenied("Only an Institution Admin may approve or execute automation.")
            job = get_object_or_404(AIAutomationJob.objects, pk=request.POST.get("job_id"))
            if action == "approve":
                if job.status != AIAutomationJob.Status.ANALYZED:
                    messages.error(request, "Only an analyzed proposal can be approved.")
                else:
                    job.status = AIAutomationJob.Status.APPROVED
                    job.approved_by = request.user
                    job.approved_at = timezone.now()
                    job.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
                    AuditLog.objects.create(
                        institution=request.institution, user=request.user,
                        action="ai_automation_approved", object_type="AIAutomationJob", object_id=str(job.id),
                        new_value={"action": job.action, "proposed_count": len(job.proposed_changes)},
                        description="Institution Admin approved a reviewed automation proposal.", ip_address=_client_ip(request),
                    )
                    messages.success(request, "Proposal approved. Execute it only after confirming the displayed changes.")
            else:
                try:
                    summary = execute_institution_automation(job, request.user)
                except ValueError as exc:
                    messages.error(request, str(exc))
                else:
                    AuditLog.objects.create(
                        institution=request.institution, user=request.user,
                        action="ai_automation_executed", object_type="AIAutomationJob", object_id=str(job.id),
                        new_value=summary,
                        description="Institution Admin executed a previously approved automation proposal.", ip_address=_client_ip(request),
                    )
                    messages.success(request, f"Automation executed: {summary['created']} account(s) created; {summary['skipped']} skipped.")
            return redirect("portal:ai-automation")

    return render(request, "portal/ai_automation.html", dashboard_context(
        request, "AI Import Review", form=form, jobs=jobs, is_institution_admin=is_institution_admin,
    ))


@csrf_exempt
@require_http_methods(["GET"])
def api_institution_detail(request, institution_code):
    authenticated, error = _screening_api_authenticate(request, institution_code)
    if error:
        return error
    institution, integration = authenticated
    return JsonResponse({
        "institution_code": institution.institution_code,
        "name": institution.name,
        "status": institution.status,
        "screening_configured": True,
        "screening_open": integration.is_accepting_applications,
        "admission_session": integration.admission_session.name if integration.admission_session else None,
        "available_programmes": integration.available_programmes,
    })


def _screening_application_from_payload(institution, payload):
    application_id = str(payload.get("application_id", "")).strip()
    if not application_id:
        raise ValidationError({"application_id": "Application ID is required."})
    existing = ScreeningApplication.objects.filter(
        institution=institution, external_application_id=application_id,
    ).first()
    first_name = str(payload.get("first_name", existing.first_name if existing else "")).strip()
    last_name = str(payload.get("last_name", existing.last_name if existing else "")).strip()
    if not first_name or not last_name:
        raise ValidationError({"applicant": "First name and last name are required."})
    department = _screening_lookup(institution, payload, "department_code", Department)
    academic_session = _screening_lookup(institution, payload, "academic_session", AcademicSession)
    defaults = {
        "applicant_reference": str(payload.get("applicant_reference", "")).strip(),
        "jamb_number": str(payload.get("jamb_number", "")).strip(),
        "first_name": first_name,
        "last_name": last_name,
        "email": str(payload.get("email", "")).strip(),
        "programme": str(payload.get("programme", "")).strip(),
        "payload": payload.get("data", {}) if isinstance(payload.get("data", {}), dict) else {},
    }
    if department:
        defaults["department"] = department
    if academic_session:
        defaults["academic_session"] = academic_session
    application, created = ScreeningApplication.objects.update_or_create(
        institution=institution, external_application_id=application_id, defaults=defaults,
    )
    return application, created


@csrf_exempt
@require_http_methods(["POST"])
def api_screening_applications(request):
    institution_code = request.META.get(SCREENING_INSTITUTION_HEADER, "")
    authenticated, error = _screening_api_authenticate(request, institution_code)
    if error:
        return error
    payload = _screening_json(request)
    if payload is None:
        return JsonResponse({"detail": "Request body must be a JSON object."}, status=400)
    institution, _ = authenticated
    try:
        with transaction.atomic():
            application, created = _screening_application_from_payload(institution, payload)
            AuditLog.objects.create(
                institution=institution, action="screening_application_received",
                description=f"Screening application {application.external_application_id} {'created' if created else 'updated'} through the API.",
                object_type="ScreeningApplication", object_id=str(application.id), ip_address=_client_ip(request),
            )
    except ValidationError as exc:
        return JsonResponse({"detail": "Invalid application payload.", "errors": exc.message_dict}, status=400)
    return JsonResponse({"application_id": application.external_application_id, "status": application.status, "created": created}, status=201 if created else 200)


@csrf_exempt
@require_http_methods(["GET"])
def api_screening_application_status(request, institution_code, application_id):
    authenticated, error = _screening_api_authenticate(request, institution_code)
    if error:
        return error
    institution, _ = authenticated
    application = get_object_or_404(ScreeningApplication, institution=institution, external_application_id=application_id)
    return JsonResponse({
        "application_id": application.external_application_id,
        "status": application.status,
        "student_id": application.student_id,
        "admitted_at": application.admitted_at.isoformat() if application.admitted_at else None,
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_screening_admit(request, institution_code, application_id):
    authenticated, error = _screening_api_authenticate(request, institution_code)
    if error:
        return error
    institution, _ = authenticated
    application = get_object_or_404(ScreeningApplication, institution=institution, external_application_id=application_id)
    if application.status == ScreeningApplication.Status.REJECTED:
        return JsonResponse({"detail": "A rejected application cannot be admitted."}, status=409)
    application.status = ScreeningApplication.Status.ADMITTED
    application.admitted_at = timezone.now()
    application.save(update_fields=["status", "admitted_at", "updated_at"])
    AuditLog.objects.create(
        institution=institution, action="screening_application_admitted",
        description=f"Screening application {application.external_application_id} was admitted through the API.",
        object_type="ScreeningApplication", object_id=str(application.id), ip_address=_client_ip(request),
    )
    return JsonResponse({"application_id": application.external_application_id, "status": application.status})


@csrf_exempt
@require_http_methods(["POST"])
def api_student_from_screening(request):
    institution_code = request.META.get(SCREENING_INSTITUTION_HEADER, "")
    authenticated, error = _screening_api_authenticate(request, institution_code)
    if error:
        return error
    payload = _screening_json(request)
    if payload is None:
        return JsonResponse({"detail": "Request body must be a JSON object."}, status=400)
    institution, _ = authenticated
    application_id = str(payload.get("application_id", "")).strip()
    application = ScreeningApplication.objects.filter(institution=institution, external_application_id=application_id).first()
    if not application:
        return JsonResponse({"detail": "Application not found for this institution."}, status=404)
    if application.status not in {ScreeningApplication.Status.ADMITTED, ScreeningApplication.Status.TRANSFERRED}:
        return JsonResponse({"detail": "Only admitted applications can create student records."}, status=409)
    try:
        department = _screening_lookup(institution, payload, "department_code", Department, required=not application.department_id) or application.department
        session = _screening_lookup(institution, payload, "academic_session", AcademicSession, required=not application.academic_session_id) or application.academic_session
        if not department or not session:
            raise ValidationError({"admission": "A valid department and academic session are required."})
        identifier = str(payload.get("student_id") or application.jamb_number or application.applicant_reference or application.external_application_id).strip()
        if not identifier:
            raise ValidationError({"student_id": "A student or admission reference is required."})
        with transaction.atomic():
            application = ScreeningApplication.objects.select_for_update().get(pk=application.pk)
            student = application.student or User.all_objects.filter(institution=institution, id_number=identifier).first()
            created = student is None
            if created:
                username_base = f"{institution.institution_code}-{identifier}"[:150]
                username = username_base
                suffix = 2
                while User.all_objects.filter(username=username).exists():
                    username = f"{username_base[:145]}-{suffix}"
                    suffix += 1
                student = User.all_objects.create_user(
                    username=username, password=None, first_name=application.first_name, last_name=application.last_name,
                    email=application.email, role=User.Role.STUDENT, institution=institution, department=department,
                    id_number=identifier, level=str(payload.get("level", "")),
                )
            else:
                student.department = department
                if payload.get("level"):
                    student.level = str(payload["level"])
                student.save(update_fields=["department", "level"])
            application.department = department
            application.academic_session = session
            application.student = student
            application.status = ScreeningApplication.Status.TRANSFERRED
            application.save(update_fields=["department", "academic_session", "student", "status", "updated_at"])
            AuditLog.objects.create(
                institution=institution, action="screening_student_transferred",
                description=f"Created or linked student {student.username} from screening application {application.external_application_id}.",
                object_type="User", object_id=str(student.id), ip_address=_client_ip(request),
            )
    except ValidationError as exc:
        return JsonResponse({"detail": "Invalid student transfer payload.", "errors": exc.message_dict}, status=400)
    return JsonResponse({"application_id": application.external_application_id, "student_id": student.id, "username": student.username, "created": created}, status=201 if created else 200)


@role_required(User.Role.ADMIN)
def billing_overview(request):
    institution = getattr(request, "institution", None)
    if institution is None:
        raise PermissionDenied
    return render(request, "portal/billing_overview.html", {
        "subscription": institution.current_subscription,
        "payments": Payment.objects.filter(institution=institution).select_related("plan")[:30],
        "plans": SubscriptionPlan.objects.filter(is_active=True),
    })


@role_required(User.Role.ADMIN)
def billing_renew(request):
    institution = getattr(request, "institution", None)
    if institution is None:
        raise PermissionDenied
    form = SubscriptionRenewalForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not subscription_paystack_secret_key():
            form.add_error(None, "Subscription billing is not configured. Please contact Educonnect support.")
        else:
            try:
                payment, checkout = create_subscription_payment(
                    institution=institution,
                    plan_duration=form.cleaned_data["plan_duration"],
                    email=request.user.email or institution.email,
                    callback_url=request.build_absolute_uri(reverse("portal:billing-callback")),
                )
                AuditLog.objects.create(user=request.user, institution=institution, action="subscription_checkout_started", description=f"Started checkout {payment.reference}.")
                return redirect(checkout["authorization_url"])
            except ValueError as exc:
                form.add_error(None, str(exc))
    return render(request, "portal/billing_renew.html", {"form": form, "institution": institution})


@role_required(User.Role.ADMIN)
def billing_callback(request):
    reference = request.GET.get("reference", "")
    payment = get_object_or_404(Payment, reference=reference, institution=getattr(request, "institution", None))
    try:
        _, changed = verify_and_finalize_subscription_payment(payment.reference)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("portal:billing-overview")
    messages.success(request, "Payment verified and subscription updated." if changed else "This payment has already been processed.")
    return redirect("portal:billing-receipt", payment_id=payment.id)


@role_required(User.Role.ADMIN)
def billing_receipt(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("institution", "plan", "subscription"), pk=payment_id, institution=getattr(request, "institution", None), status=Payment.Status.SUCCESS)
    response = render(request, "portal/payment_receipt.html", {"payment": payment})
    response["Content-Disposition"] = f'attachment; filename="educonnect-receipt-{payment.reference}.html"'
    return response


@role_required(User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN)
def profile(request):
    if request.user.role == User.Role.ADMIN:
        form_class = AdminProfileForm
    elif request.user.role == User.Role.LECTURER:
        form_class = LecturerProfileForm
    else:
        form_class = StudentProfileForm
    form = form_class(instance=request.user)
    password_form = (
        InstitutionAdminPasswordChangeForm(request.user)
        if request.user.role == User.Role.ADMIN
        else None
    )
    if request.method == "POST":
        previous_level = request.user.level
        previous_department_id = request.user.department_id
        previous_programme_id = request.user.programme_id
        form = form_class(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            if (
                request.user.role == User.Role.STUDENT
                and (request.user.department_id != previous_department_id or request.user.programme_id != previous_programme_id)
            ):
                request.user.curriculum = (
                    curriculum_for_programme(request.user.programme, current_departmental_session())
                    if request.user.programme_id else curriculum_for_department(
                        request.user.department, current_departmental_session(),
                    )
                )
                request.user.save(update_fields=["curriculum"])
            if (
                request.user.role == User.Role.STUDENT
                and request.user.level != previous_level
            ):
                current_session = current_departmental_session()
                current_registrations = StudentCourseRegistration.objects.filter(
                    student=request.user,
                    session=current_session,
                )
                paid_course_ids = current_registrations.filter(course__is_free=False).values_list("course_id", flat=True)
                CoursePayment.objects.filter(
                    student=request.user,
                    course_id__in=paid_course_ids,
                    session=current_session,
                    is_active_for_registration=True,
                ).update(is_active_for_registration=False, updated_at=timezone.now())
                current_registrations.delete()
                messages.info(
                    request,
                    "Your level changed, so your previous course registrations were cleared. Paid courses require a new payment before you register again.",
                )
            messages.success(request, "Your profile information has been updated.")
            return redirect("portal:profile")
    return render(
        request,
        "portal/profile.html",
        dashboard_context(
            request,
            "Profile",
            form=form,
            password_form=password_form,
            browser_alerts_enabled=request.user.browser_alerts_enabled,
        ),
    )


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def institution_admin_change_password(request):
    """Allow a signed-in institution administrator to replace their password."""
    if not request.user.institution_id:
        raise PermissionDenied

    form = InstitutionAdminPasswordChangeForm(request.user, request.POST)
    if form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        AuditLog.objects.create(
            user=user,
            institution=user.institution,
            action="institution_admin_password_changed",
            description="Institution administrator changed their password.",
            object_type="User",
            object_id=str(user.id),
        )
        messages.success(request, "Your password has been changed.")
        return redirect("portal:profile")

    messages.error(request, "Please correct the password form errors below.")
    return render(
        request,
        "portal/profile.html",
        dashboard_context(
            request,
            "Profile",
            form=AdminProfileForm(instance=request.user),
            password_form=form,
            browser_alerts_enabled=request.user.browser_alerts_enabled,
        ),
        status=400,
    )


def _send_password_reset_verification(request, user):
    form = UserPasswordResetForm({"email": user.email}, target_user=user)
    if not form.is_valid():
        return False
    form.save(
        request=request,
        use_https=request.is_secure(),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@educonnect.local"),
        subject_template_name="portal/emails/password_reset_subject.txt",
        email_template_name="portal/emails/password_reset_email.txt",
    )
    return True


@role_required(User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN)
def send_profile_password_reset(request):
    if request.method != "POST":
        raise PermissionDenied
    if not request.user.email:
        messages.error(request, "Add an email address to your profile before requesting a password reset.")
        return redirect("portal:profile")

    if _send_password_reset_verification(request, request.user):
        messages.success(request, f"A password reset verification link has been sent to {request.user.email}.")
    else:
        messages.error(request, "We could not send the password reset email right now.")
    return redirect("portal:profile")


def lecturer_registered_courses_queryset(user):
    # Older and admin-imported courses may have a lecturer assigned directly
    # before a LecturerCourseRegistration record exists. Include both forms so
    # those courses remain visible and manageable by their assigned lecturer.
    return Course.objects.filter(
        Q(lecturer=user) | Q(lecturer_registrations__lecturer=user)
    ).select_related("department", "lecturer").distinct()


def _create_course_materials_from_upload(form, *, lecturer):
    uploaded_files = form.cleaned_data.get("files") or []
    if not uploaded_files:
        return []
    course = form.cleaned_data["course"]
    base_title = form.cleaned_data["title"].strip()
    created_materials = []
    for uploaded_file in uploaded_files:
        file_stem = Path(uploaded_file.name).stem
        title = base_title if len(uploaded_files) == 1 else f"{base_title} - {file_stem}"
        material = CourseMaterial.objects.create(
            course=course,
            lecturer=lecturer,
            title=title,
            description=form.cleaned_data.get("description", ""),
            file=uploaded_file,
            is_free=form.cleaned_data.get("is_free", True),
            amount=form.cleaned_data.get("amount") or Decimal("0.00"),
            is_download_enabled=form.cleaned_data.get("is_download_enabled", True),
        )
        sync_material_access_for_material(material)
        created_materials.append(material)
    return created_materials


def _append_querystring(url, request):
    query_string = request.META.get("QUERY_STRING", "")
    if query_string:
        return f"{url}?{query_string}"
    return url


def _filtered_timetables(request, prefix="tt"):
    form = TimetableFilterForm(request.GET or None, prefix=prefix)
    queryset = Timetable.objects.select_related("department")
    if form.is_valid():
        department = form.cleaned_data.get("department")
        semester = form.cleaned_data.get("semester")
        level = form.cleaned_data.get("level")
        search = form.cleaned_data.get("search")
        if department:
            queryset = queryset.filter(department=department)
        if semester:
            queryset = queryset.filter(semester=semester)
        if level:
            queryset = queryset.filter(level=level)
        queryset = _search_filter(queryset, search, "title", "department__name", "description")
    return form, queryset


def _filtered_handbooks(request, prefix="hb"):
    form = HandbookFilterForm(request.GET or None, prefix=prefix)
    queryset = Handbook.objects.select_related("department")
    if form.is_valid():
        department = form.cleaned_data.get("department")
        search = form.cleaned_data.get("search")
        if department:
            queryset = queryset.filter(department=department)
        queryset = _search_filter(queryset, search, "title", "department__name", "description")
    return form, queryset


@role_required(User.Role.STUDENT)
def student_dashboard(request):
    current_session = current_departmental_session()
    registrations = StudentCourseRegistration.objects.filter(
        student=request.user,
        session=current_session,
    ).select_related("course", "course__lecturer")
    messages_qs = NotificationRecipient.objects.filter(student=request.user, is_deleted=False)
    payments = {
        payment.course_id: payment
        for payment in current_course_payment_queryset().filter(student=request.user)
    }
    group_names = _student_group_names_by_course(request.user, current_session)
    registration_rows = [
        _student_course_row_context(
            registration,
            payments.get(registration.course_id),
            group_names.get(registration.course_id),
        )
        for registration in registrations
    ]
    downloadable_course_files = registrations.filter(course__file__gt="").count()
    downloadable_materials = CourseMaterial.objects.filter(course__student_registrations__student=request.user).count()
    paid_downloads = sum(
        1
        for row in registration_rows
        if not row["registration"].course.is_free and row["is_paid"]
    )
    pending_paid_courses = sum(
        1
        for row in registration_rows
        if not row["registration"].course.is_free and row["needs_payment"]
    )
    context = dashboard_context(
        request,
        "Student Dashboard",
        registration_rows=registration_rows,
        recent_messages=messages_qs.select_related("notification", "notification__sender")[:5],
        download_count=downloadable_course_files + downloadable_materials,
        timetable_count=Timetable.objects.filter(department=request.user.department).count(),
        handbook_count=Handbook.objects.filter(department=request.user.department).count(),
        paid_downloads=paid_downloads,
        outstanding_download_payments=pending_paid_courses,
        programme=request.user.programme,
    )
    return render(request, "portal/student_dashboard.html", context)


@role_required(User.Role.STUDENT)
def student_departmental(request):
    session = current_departmental_session()
    fee_map = fee_map_for_session(session, request.user.department) if request.user.department_id else {}
    history = (
        DepartmentalPayment.objects.filter(student=request.user)
        .select_related("session", "department")
        .prefetch_related("items__association")
    )
    existing_payment = history.filter(session=session).first()
    form = DepartmentalStudentPaymentForm(session=session, fee_map=fee_map)
    additional_document_form = DepartmentalAdditionalDocumentForm()

    if request.method == "POST" and request.POST.get("action") == "add-documents":
        additional_document_form = DepartmentalAdditionalDocumentForm(request.POST, request.FILES)
        if not existing_payment or not existing_payment.is_paid:
            messages.error(request, "Complete departmental payment before adding extra documents.")
        elif additional_document_form.is_valid():
            for uploaded_file in additional_document_form.cleaned_data["additional_documents"]:
                DepartmentalPaymentDocument.objects.create(
                    payment=existing_payment,
                    category=DepartmentalPaymentDocument.Category.ADDITIONAL,
                    title=uploaded_file.name,
                    file=uploaded_file,
                )
            messages.success(request, "Additional departmental document(s) uploaded.")
            return redirect("portal:student-departmental")
        else:
            messages.error(request, "Choose at least one document to upload.")

    if request.method == "POST" and request.POST.get("action") != "add-documents":
        form = DepartmentalStudentPaymentForm(request.POST, request.FILES, session=session, fee_map=fee_map)
        gateway = department_payment_gateway_for_payment(request.user.department)
        if not request.user.department_id:
            messages.error(request, "Your account does not have a department yet.")
        elif not gateway or not gateway.is_configured:
            messages.error(request, "Your department payment gateway has not been configured by the admin yet.")
        elif existing_payment and existing_payment.is_paid:
            messages.info(request, "You have already completed departmental payment for the current session.")
        elif form.is_valid():
            selected_codes = ["departmental_fee", form.cleaned_data["association_choice"], *form.cleaned_data["additional_associations"]]
            selected_fees = [fee_map[code] for code in selected_codes if code in fee_map]
            total_amount = sum((fee.amount for fee in selected_fees), Decimal("0.00"))
            if total_amount <= 0:
                messages.error(request, "Departmental fees for your selection must be greater than zero before payment can start.")
                return redirect("portal:student-departmental")
            summary = ", ".join(fee.association.name for fee in selected_fees)
            white_form = form.cleaned_data.get("white_form")
            school_receipt = form.cleaned_data.get("school_receipt")
            supporting_documents = form.cleaned_data.get("supporting_documents", [])
            with transaction.atomic():
                payment = existing_payment or DepartmentalPayment(student=request.user, department=request.user.department, session=session)
                payment.department = request.user.department
                payment.status = DepartmentalPayment.Status.PENDING
                payment.total_amount = total_amount
                payment.association_summary = summary
                payment.paid_at = None
                payment.paystack_public_key_used = gateway.paystack_public_key
                payment.paystack_secret_key_used = gateway.paystack_secret_key
                payment.save()
                payment.items.all().delete()
                for fee in selected_fees:
                    DepartmentalPaymentItem.objects.create(
                        payment=payment,
                        association=fee.association,
                        amount=fee.amount,
                    )
                if white_form or school_receipt or supporting_documents:
                    payment.documents.all().delete()
                if white_form:
                    DepartmentalPaymentDocument.objects.create(
                        payment=payment,
                        category=DepartmentalPaymentDocument.Category.WHITE_FORM,
                        title="White Form",
                        file=white_form,
                    )
                if school_receipt:
                    DepartmentalPaymentDocument.objects.create(
                        payment=payment,
                        category=DepartmentalPaymentDocument.Category.SCHOOL_RECEIPT,
                        title="School Receipt",
                        file=school_receipt,
                    )
                for uploaded_file in supporting_documents:
                    DepartmentalPaymentDocument.objects.create(
                        payment=payment,
                        category=DepartmentalPaymentDocument.Category.MEDICAL_FITNESS,
                        title=uploaded_file.name,
                        file=uploaded_file,
                    )
            callback_url = request.build_absolute_uri(
                reverse("portal:departmental-payment-callback", args=[payment.id])
            )
            try:
                checkout = _initialize_checkout_or_message(
                    request,
                    secret_key=gateway.paystack_secret_key,
                    email=request.user.email,
                    amount=payment.total_amount,
                    reference=payment.paystack_reference,
                    callback_url=callback_url,
                    metadata={
                        "student_id": request.user.id_number or request.user.username,
                        "department": request.user.department.code,
                        "session": session.name,
                        "payment_type": "departmental",
                    },
                )
            except ValueError as exc:
                if _is_paystack_transport_error(exc):
                    messages.warning(
                        request,
                        "The server could not reach Paystack directly, so checkout is opening in your browser instead.",
                    )
                    return _render_paystack_browser_checkout(
                        request,
                        title="Departmental Payment",
                        checkout_heading="Pay Departmental Fees",
                        payment=payment,
                        checkout_label="Session",
                        checkout_value=session.name,
                        callback_url=callback_url,
                        cancel_url=reverse("portal:student-departmental"),
                        notice="Paystack could not be reached from the server, but your browser can continue the checkout directly.",
                    )
                messages.error(request, str(exc))
                return redirect("portal:student-departmental")
            messages.success(request, "Departmental payment request prepared. Continue on Paystack to complete payment.")
            return redirect(checkout["authorization_url"])
        else:
            missing_fields = _form_error_labels(form)
            if missing_fields:
                messages.error(
                    request,
                    "Please complete these departmental payment fields before starting payment: "
                    + ", ".join(missing_fields)
                    + ".",
                )
            else:
                messages.error(request, "Please complete the highlighted departmental payment fields before starting payment.")

    context = dashboard_context(
        request,
        "Departmental",
        current_session=session,
        current_semester=current_departmental_semester_label(),
        fee_map=fee_map,
        departmental_form=form,
        additional_document_form=additional_document_form,
        departmental_payment=existing_payment,
        departmental_history=history,
    )
    return render(request, "portal/student_departmental.html", context)


@role_required(User.Role.STUDENT)
def student_courses(request):
    current_session = current_departmental_session()
    filter_form = StudentCourseFilterForm(request.GET or None, student=request.user)

    available_courses = _student_course_catalog_queryset(request.user)
    # A bound but invalid query (such as a different department ID) must not
    # fall back to the full catalogue. It remains empty and displays the form
    # error, preserving the student's department and curriculum boundaries.
    catalog_is_filtered = filter_form.is_bound
    if filter_form.is_valid():
        department = filter_form.cleaned_data.get("department")
        level = filter_form.cleaned_data.get("level")
        search = filter_form.cleaned_data.get("search")
        catalog_is_filtered = bool(department or level or search)
        if department:
            available_courses = available_courses.filter(department=department)
        if level:
            available_courses = available_courses.filter(level=level)
        available_courses = _search_filter(available_courses, search, "code", "title", "department__name", "lecturer__username")

    available_courses = available_courses.distinct() if catalog_is_filtered else Course.objects.none()
    registrations = StudentCourseRegistration.objects.filter(
        student=request.user,
        session=current_session,
    ).select_related("course", "course__lecturer")
    registered_course_ids = set(registrations.values_list("course_id", flat=True))
    payments = {
        payment.course_id: payment
        for payment in current_course_payment_queryset().filter(student=request.user)
    }
    group_names = _student_group_names_by_course(request.user, current_session)
    paid_course_ids = {course_id for course_id, payment in payments.items() if payment.is_paid}
    registration_rows = [
        _student_course_row_context(
            registration,
            payments.get(registration.course_id),
            group_names.get(registration.course_id),
        )
        for registration in registrations
    ]

    if request.method == "POST":
        action = request.POST.get("action")
        course = get_object_or_404(_student_course_catalog_queryset(request.user), pk=request.POST.get("course_id"))

        if action == "pay-course":
            if course.is_free:
                messages.info(request, "This course does not require payment.")
            else:
                gateway = course_payment_gateway_for_department(course.department)
                if not gateway or not gateway.is_configured:
                    messages.error(request, f"The HOD has not configured a paid-course API for {course.department.name} yet.")
                    return redirect(_redirect_with_query(request, "portal:student-courses"))
                payment = _active_course_payment(request.user, course, current_session)
                if payment is None:
                    payment = CoursePayment.objects.create(
                        student=request.user,
                        course=course,
                        session=current_session,
                        enrollment_sequence=_next_course_payment_sequence(request.user, course, current_session),
                        amount=course.amount,
                        status=CoursePayment.Status.PENDING,
                        paystack_public_key_used=gateway.paystack_public_key,
                        paystack_secret_key_used=gateway.paystack_secret_key,
                    )
                if payment.is_paid:
                    messages.info(request, f"{course.code} has already been paid.")
                    return redirect(_redirect_with_query(request, "portal:student-courses"))
                payment.amount = course.amount
                payment.status = CoursePayment.Status.PENDING
                payment.paid_at = None
                payment.paystack_public_key_used = gateway.paystack_public_key
                payment.paystack_secret_key_used = gateway.paystack_secret_key
                payment.rotate_paystack_reference()
                payment.save()
                callback_url = request.build_absolute_uri(
                    reverse("portal:course-payment-callback", args=[payment.id])
                )
                try:
                    checkout = _initialize_checkout_or_message(
                        request,
                        secret_key=gateway.paystack_secret_key,
                        email=request.user.email,
                        amount=payment.amount,
                        reference=payment.paystack_reference,
                        callback_url=callback_url,
                        metadata={
                            "student_id": _student_identifier(request.user),
                            "student_username": request.user.username,
                            "student_email": request.user.email,
                            "department": course.department.code,
                            "course_code": course.code,
                            "course_title": course.title,
                            "payment_type": "course",
                        },
                    )
                except ValueError as exc:
                    if _is_paystack_transport_error(exc):
                        messages.warning(
                            request,
                            "The server could not reach Paystack directly, so checkout is opening in your browser instead.",
                        )
                        return _render_paystack_browser_checkout(
                            request,
                            title="Course Payment",
                            checkout_heading=f"Pay for {course.code}",
                            payment=payment,
                            checkout_label="Course",
                            checkout_value=f"{course.code} - {course.title}",
                            callback_url=callback_url,
                            cancel_url=reverse("portal:student-courses"),
                            notice="Paystack could not be reached from the server, but your browser can continue the checkout directly.",
                        )
                    messages.error(request, str(exc))
                    return redirect(_redirect_with_query(request, "portal:student-courses"))
                messages.success(request, f"Course payment request prepared for {course.code}. Continue on Paystack to complete payment.")
                return redirect(checkout["authorization_url"])
            return redirect(_redirect_with_query(request, "portal:student-courses"))

        if action == "register-course":
            payment = payments.get(course.id) or _active_course_payment(request.user, course, current_session)
            if not course.is_free and not (payment and payment.is_paid):
                messages.error(request, "Please complete the course payment before registering this course.")
                return redirect(_redirect_with_query(request, "portal:student-courses"))
            registration, created = StudentCourseRegistration.objects.get_or_create(
                student=request.user,
                course=course,
                session=current_session,
                defaults={"registered_by": request.user},
            )
            if created:
                sync_material_access_for_registration(registration)
                messages.success(request, f"{course.code} registered successfully.")
            else:
                messages.info(request, f"{course.code} is already registered.")
            if not course.is_free:
                _assign_student_to_existing_course_group(request.user, course, current_session)
            return redirect(_redirect_with_query(request, "portal:student-courses"))

    context = dashboard_context(
        request,
        "Course Registration",
        filter_form=filter_form,
        registration_rows=registration_rows,
        registered_course_ids=registered_course_ids,
        available_courses=available_courses,
        catalog_is_filtered=catalog_is_filtered,
        paid_course_ids=paid_course_ids,
    )
    return render(request, "portal/student_courses.html", context)


@role_required(User.Role.STUDENT)
def departmental_payment_callback(request, payment_id):
    payment = get_object_or_404(
        DepartmentalPayment.objects.select_related("student", "department", "session"),
        pk=payment_id,
        student=request.user,
    )
    reference = _paystack_reference_from_request(request)
    if reference and reference != payment.paystack_reference:
        messages.error(request, "The departmental payment reference did not match this record.")
        return redirect("portal:student-departmental")
    if not reference:
        messages.warning(request, "Departmental payment is still pending confirmation.")
        return redirect("portal:student-departmental")
    try:
        verification = verify_paystack_transaction(payment.paystack_secret_key_used, reference)
    except ValueError as exc:
        payment.refresh_from_db(fields=["status", "paid_at"])
        if _is_paystack_transport_error(exc) and payment.is_paid:
            _finalize_departmental_payment(payment)
            messages.success(request, "Departmental payment confirmed successfully.")
        elif _is_paystack_transport_error(exc):
            messages.warning(request, "Departmental payment was submitted, but confirmation is still in progress. Please refresh shortly.")
        else:
            messages.error(request, str(exc))
        return redirect("portal:student-departmental")
    if _verification_matches_payment(verification, reference=payment.paystack_reference, amount=payment.total_amount):
        _finalize_departmental_payment(payment)
        messages.success(request, "Departmental payment confirmed successfully.")
    else:
        payment.status = DepartmentalPayment.Status.FAILED
        payment.paid_at = None
        payment.save(update_fields=["status", "paid_at", "updated_at"])
        messages.warning(request, "Departmental payment was not confirmed by the gateway.")
    return redirect("portal:student-departmental")


@role_required(User.Role.STUDENT)
def course_payment_callback(request, payment_id):
    payment = get_object_or_404(
        CoursePayment.objects.select_related("student", "course"),
        pk=payment_id,
        student=request.user,
    )
    reference = _paystack_reference_from_request(request)
    if reference and reference != payment.paystack_reference:
        messages.error(request, "The course payment reference did not match this record.")
        return redirect("portal:student-courses")
    if not reference:
        messages.warning(request, f"{payment.course.code} payment is still pending confirmation.")
        return redirect("portal:student-courses")
    try:
        verification = verify_paystack_transaction(payment.paystack_secret_key_used, reference)
    except ValueError as exc:
        payment.refresh_from_db(fields=["status", "paid_at"])
        if _is_paystack_transport_error(exc) and payment.is_paid:
            _finalize_course_payment(payment)
            messages.success(request, f"{payment.course.code} payment confirmed successfully.")
        elif _is_paystack_transport_error(exc):
            messages.warning(request, f"{payment.course.code} payment was submitted, but confirmation is still in progress. Please refresh shortly.")
        else:
            messages.error(request, str(exc))
        return redirect("portal:student-courses")
    if _verification_matches_payment(verification, reference=payment.paystack_reference, amount=payment.amount):
        _finalize_course_payment(payment)
        messages.success(request, f"{payment.course.code} payment confirmed successfully.")
    else:
        payment.status = CoursePayment.Status.PENDING
        payment.paid_at = None
        payment.save(update_fields=["status", "paid_at", "updated_at"])
        messages.warning(request, f"{payment.course.code} payment is still pending confirmation.")
    return redirect("portal:student-courses")


@role_required(User.Role.STUDENT)
def student_unregister_course(request, registration_id):
    registration = get_object_or_404(
        StudentCourseRegistration,
        pk=registration_id,
        student=request.user,
        session=current_departmental_session(),
    )
    if request.method == "POST":
        registration.delete()
        messages.success(request, "Course registration removed.")
    return redirect(_redirect_with_query(request, "portal:student-courses"))


@role_required(User.Role.STUDENT)
def student_materials(request):
    search = request.GET.get("search", "").strip()
    registrations = (
        StudentCourseRegistration.objects.filter(student=request.user, session=current_departmental_session())
        .select_related("course", "course__lecturer", "course__department")
    )
    registrations = _search_filter(registrations, search, "course__title", "course__code", "course__lecturer__username", "course__department__name")
    course_ids = list(registrations.values_list("course_id", flat=True))
    payments = {
        payment.course_id: payment
        for payment in current_course_payment_queryset().filter(student=request.user, course_id__in=course_ids)
    }
    materials = (
        CourseMaterial.objects.filter(course_id__in=course_ids)
        .select_related("course", "lecturer")
        .order_by("course__code", "title")
    )
    access_records = {
        access.material_id: access
        for access in MaterialAccess.objects.filter(student=request.user, material__course_id__in=course_ids).select_related("material")
    }
    materials_by_course = {}
    for material in materials:
        materials_by_course.setdefault(material.course_id, []).append(
            {"material": material, "access": access_records.get(material.id)}
        )
    download_rows = []
    for registration in registrations:
        payment = payments.get(registration.course_id)
        course_row = _student_course_row_context(registration, payment)
        download_rows.append(
            {
                **course_row,
                "materials": materials_by_course.get(registration.course_id, []),
                "course_file_available": course_row["can_download"],
            }
        )
    context = dashboard_context(request, "Downloads", download_rows=download_rows)
    return render(request, "portal/student_materials.html", context)


@role_required(User.Role.STUDENT)
def student_messages(request):
    recipients = NotificationRecipient.objects.filter(student=request.user, is_deleted=False).select_related(
        "notification",
        "notification__sender",
    ).prefetch_related("notification__attachments")
    selected_message = None
    recipient_id = request.GET.get("open")
    if recipient_id:
        selected_message = get_object_or_404(recipients, pk=recipient_id)
        if not selected_message.is_read:
            selected_message.is_read = True
            selected_message.save(update_fields=["is_read", "updated_at"])
        UserAlert.objects.filter(
            recipient=request.user,
            dedupe_key=f"message:{selected_message.notification_id}:{request.user.id}",
        ).update(is_read=True, updated_at=timezone.now())
    context = dashboard_context(
        request,
        "Notifications & Messages",
        recipients=recipients,
        selected_message=selected_message,
    )
    return render(request, "portal/student_messages.html", context)


@role_required(User.Role.STUDENT)
def student_delete_message(request, recipient_id):
    recipient = get_object_or_404(NotificationRecipient, pk=recipient_id, student=request.user)
    if request.method == "POST":
        recipient.is_deleted = True
        recipient.save(update_fields=["is_deleted", "updated_at"])
        UserAlert.objects.filter(
            recipient=request.user,
            dedupe_key=f"message:{recipient.notification_id}:{request.user.id}",
        ).update(is_read=True, updated_at=timezone.now())
        messages.success(request, "Message deleted from your inbox.")
    return redirect("portal:student-messages")


@role_required(User.Role.STUDENT, User.Role.LECTURER)
def update_alert_preferences(request):
    if request.user.role not in {User.Role.STUDENT, User.Role.LECTURER}:
        raise PermissionDenied
    if request.method != "POST":
        raise PermissionDenied
    enable_browser_alerts = request.POST.get("browser_alerts_enabled") == "true"
    enable_class_alerts = request.POST.get("class_reminder_alerts_enabled") == "true"
    request.user.browser_alerts_enabled = enable_browser_alerts
    request.user.class_reminder_alerts_enabled = enable_browser_alerts and enable_class_alerts
    request.user.save(update_fields=["browser_alerts_enabled", "class_reminder_alerts_enabled"])
    return JsonResponse(
        {
            "ok": True,
            "browser_alerts_enabled": request.user.browser_alerts_enabled,
            "class_reminder_alerts_enabled": request.user.class_reminder_alerts_enabled,
        }
    )


@role_required(User.Role.STUDENT, User.Role.LECTURER)
def alerts_feed(request):
    if request.user.role not in {User.Role.STUDENT, User.Role.LECTURER}:
        raise PermissionDenied
    alerts_queryset = UserAlert.objects.filter(
        recipient=request.user,
        browser_delivered_at__isnull=True,
    )
    if request.user.role != User.Role.STUDENT:
        alerts_queryset = alerts_queryset.exclude(alert_type=UserAlert.AlertType.MESSAGE)
    alerts = list(alerts_queryset.order_by("-created_at")[:10])
    now = timezone.now()
    if alerts:
        UserAlert.objects.filter(pk__in=[alert.id for alert in alerts]).update(browser_delivered_at=now, updated_at=now)
    return JsonResponse(
        {
            "alerts": [
                {
                    "id": alert.id,
                    "title": alert.title,
                    "body": alert.body,
                    "target_url": alert.target_url,
                    "alert_type": alert.alert_type,
                    "created_at": alert.created_at.isoformat(),
                }
                for alert in alerts
            ]
        }
    )


@role_required(User.Role.STUDENT)
def student_documents(request):
    timetable_filter_form, timetables = _filtered_timetables(request, prefix="sttf")
    handbook_filter_form, handbooks = _filtered_handbooks(request, prefix="shbf")
    if request.user.department_id:
        timetable_filter_form.fields["department"].queryset = Department.objects.filter(id=request.user.department_id)
        timetable_filter_form.fields["department"].initial = request.user.department_id
        handbook_filter_form.fields["department"].queryset = Department.objects.filter(id=request.user.department_id)
        handbook_filter_form.fields["department"].initial = request.user.department_id
        timetables = timetables.filter(department=request.user.department)
        handbooks = handbooks.filter(department=request.user.department)
    context = dashboard_context(
        request,
        "Timetable and Handbook",
        timetable_filter_form=timetable_filter_form,
        handbook_filter_form=handbook_filter_form,
        timetables=timetables,
        handbooks=handbooks,
    )
    return render(request, "portal/student_documents.html", context)


@role_required(User.Role.LECTURER)
def lecturer_dashboard(request):
    registered_courses = lecturer_registered_courses_queryset(request.user)
    context = dashboard_context(
        request,
        "Lecturer Dashboard",
        registered_courses=registered_courses[:6],
        student_count=User.objects.filter(
            role=User.Role.STUDENT,
            student_registrations__course__lecturer=request.user,
        ).distinct().count(),
        upload_count=registered_courses.exclude(file="").count(),
        paid_course_count=registered_courses.filter(is_free=False).count(),
        message_count=Notification.objects.filter(sender=request.user).count(),
    )
    return render(request, "portal/lecturer_dashboard.html", context)


@role_required(User.Role.LECTURER)
def lecturer_departmental(request):
    context = dashboard_context(
        request,
        "Departmental Download",
        **departmental_download_context(request, scope="lecturer"),
    )
    return render(request, "portal/lecturer_departmental.html", context)


@role_required(User.Role.LECTURER)
def lecturer_courses(request):
    filter_form = LecturerCourseFilterForm(request.GET or None)
    available_courses = _course_catalog_queryset().filter(lecturer__isnull=True)
    paid_courses_only = request.GET.get("fee_type") == "paid"
    catalog_is_filtered = False
    if filter_form.is_valid():
        department = filter_form.cleaned_data.get("department")
        level = filter_form.cleaned_data.get("level")
        search = filter_form.cleaned_data.get("search")
        catalog_is_filtered = bool(department or level or search)
        if department:
            available_courses = available_courses.filter(department=department)
        if level:
            available_courses = available_courses.filter(level=level)
        available_courses = _search_filter(available_courses, search, "code", "title", "department__name")
    available_courses = available_courses.distinct() if catalog_is_filtered else Course.objects.none()

    registered_courses = lecturer_registered_courses_queryset(request.user)
    if paid_courses_only:
        registered_courses = registered_courses.filter(is_free=False)
        available_courses = Course.objects.none()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "register-course":
            course = get_object_or_404(
                _course_catalog_queryset().filter(lecturer__isnull=True),
                pk=request.POST.get("course_id"),
            )
            course.lecturer = request.user
            course.save(update_fields=["lecturer", "updated_at"])
            registration, created = LecturerCourseRegistration.objects.get_or_create(lecturer=request.user, course=course)
            if created:
                messages.success(request, "Course registered for your lecturer workspace.")
            else:
                messages.info(request, "That course is already in your workspace.")
            return redirect(_redirect_with_query(request, "portal:lecturer-courses"))
        elif action == "remove-course":
            course = get_object_or_404(registered_courses, pk=request.POST.get("course_id"))
            with transaction.atomic():
                LecturerCourseRegistration.objects.filter(lecturer=request.user, course=course).delete()
                if course.lecturer_id == request.user.id:
                    course.lecturer = None
                    course.save(update_fields=["lecturer", "updated_at"])
            messages.success(request, "Course removed from your lecturer workspace.")
            return redirect(_redirect_with_query(request, "portal:lecturer-courses"))
        elif action == "update-course":
            course = get_object_or_404(registered_courses, pk=request.POST.get("course_id"))
            update_form = LecturerCourseUpdateForm(request.POST, request.FILES, instance=course)
            if update_form.is_valid():
                update_form.save()
                messages.success(request, "Course schedule and details updated. Registered users will receive reminders 30 minutes before class.")
                return redirect(_redirect_with_query(request, "portal:lecturer-courses"))

    edit_course = None
    edit_form = None
    edit_id = request.GET.get("edit")
    if edit_id:
        edit_course = get_object_or_404(registered_courses, pk=edit_id)
        edit_form = LecturerCourseUpdateForm(instance=edit_course)
    section_title = "Paid Courses" if paid_courses_only else "Register Courses"
    context = dashboard_context(
        request,
        section_title,
        filter_form=filter_form,
        available_courses=available_courses,
        catalog_is_filtered=catalog_is_filtered,
        registered_courses=registered_courses,
        edit_course=edit_course,
        edit_form=edit_form,
        paid_courses_only=paid_courses_only,
    )
    return render(request, "portal/lecturer_courses.html", context)


@role_required(User.Role.LECTURER)
def lecturer_paid_course_student_search(request, course_id):
    course = get_object_or_404(
        lecturer_registered_courses_queryset(request.user),
        pk=course_id,
        is_free=False,
    )
    current_session = current_departmental_session()
    student_id = request.GET.get("student_id", "").strip()
    payments = paid_course_students_queryset(course, current_session)
    if student_id:
        payments = payments.filter(student__id_number__icontains=student_id)
    context = dashboard_context(
        request,
        f"Paid Students: {course.code}",
        course=course,
        student_id_search=student_id,
        paid_student_payments=payments,
        current_academic_session=current_session,
    )
    return render(request, "portal/lecturer_paid_course_students.html", context)


@role_required(User.Role.LECTURER)
def lecturer_course_groups(request, course_id):
    course = get_object_or_404(lecturer_registered_courses_queryset(request.user), pk=course_id, is_free=False)
    current_session = current_departmental_session()
    form = CourseStudentGroupForm(initial={
        "enable_grouping": CourseStudentGroup.objects.filter(course=course, session=current_session).exists(),
    })
    paid_payments = paid_course_students_queryset(course, current_session)

    if request.method == "POST":
        if request.POST.get("action") == "delete-group":
            group = get_object_or_404(
                CourseStudentGroup,
                pk=request.POST.get("group_id"),
                course=course,
                session=current_session,
                lecturer=request.user,
            )
            group_name = group.name
            group.delete()
            messages.success(request, f"{group_name} was deleted.")
            return redirect("portal:lecturer-course-groups", course_id=course.id)

        form = CourseStudentGroupForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                existing_groups = CourseStudentGroup.objects.filter(course=course, session=current_session)
                CourseStudentGroupMembership.objects.filter(group__in=existing_groups).delete()
                existing_groups.delete()
                if not form.cleaned_data["enable_grouping"]:
                    messages.success(request, "Student grouping is off. You can still download all paid student IDs.")
                    return redirect("portal:lecturer-course-groups", course_id=course.id)
                grouping_method = form.cleaned_data["grouping_method"]
                students = [payment.student for payment in paid_payments]
                groups = []
                if grouping_method == CourseStudentGroup.GroupingMethod.DEPARTMENT:
                    departments = {}
                    for student in students:
                        departments.setdefault(student.department_id, []).append(student)
                    for department_id, members in departments.items():
                        department = members[0].department if department_id else None
                        group = CourseStudentGroup.objects.create(
                            course=course, lecturer=request.user, session=current_session,
                            name=department.name if department else "No department",
                            grouping_method=grouping_method, department=department,
                        )
                        groups.append((group, members))
                else:
                    groups = [
                        (CourseStudentGroup.objects.create(
                            course=course, lecturer=request.user, session=current_session,
                            name=name, grouping_method=grouping_method,
                        ), [])
                        for name in form.cleaned_data["group_names"]
                    ]
                    import random
                    random.SystemRandom().shuffle(students)
                    for index, student in enumerate(students):
                        groups[index % len(groups)][1].append(student)
                CourseStudentGroupMembership.objects.bulk_create([
                    CourseStudentGroupMembership(institution=request.institution, group=group, student=student)
                    for group, members in groups for student in members
                ])
            messages.success(request, f"{len(groups)} student group(s) created from {len(students)} paid student(s).")
            return redirect("portal:lecturer-course-groups", course_id=course.id)

    groups = (
        CourseStudentGroup.objects.filter(course=course, session=current_session)
        .select_related("department")
        .annotate(student_total=Count("memberships"))
    )
    return render(request, "portal/lecturer_course_groups.html", dashboard_context(
        request, f"Student Groups: {course.code}", course=course,
        current_academic_session=current_session, form=form, groups=groups,
        paid_student_total=paid_payments.count(),
    ))


@role_required(User.Role.LECTURER)
def lecturer_materials(request):
    form = CourseMaterialBatchForm(user=request.user)
    search = request.GET.get("search", "").strip()
    all_materials = CourseMaterial.objects.select_related("course", "lecturer")
    all_materials = _search_filter(all_materials, search, "title", "course__code", "lecturer__username", "course__department__name")
    materials = CourseMaterial.objects.filter(lecturer=request.user).select_related("course")
    materials = _search_filter(materials, search, "title", "course__code", "course__department__name")
    if request.method == "POST":
        form = CourseMaterialBatchForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            created_materials = _create_course_materials_from_upload(form, lecturer=request.user)
            if created_materials:
                messages.success(request, f"{len(created_materials)} course file(s) uploaded and published.")
                return redirect(_redirect_with_query(request, "portal:lecturer-materials"))
            messages.error(request, "Please choose at least one file to upload.")

    material_rows = []
    for material in materials:
        stats = material.access_records.values("status").annotate(total=Count("id"))
        material_rows.append({"material": material, "stats": {item["status"]: item["total"] for item in stats}})
    context = dashboard_context(
        request,
        "Materials",
        form=form,
        material_rows=material_rows,
        all_materials=all_materials,
    )
    return render(request, "portal/lecturer_materials.html", context)


@role_required(User.Role.LECTURER)
def lecturer_delete_material(request, material_id):
    material = get_object_or_404(CourseMaterial, pk=material_id, lecturer=request.user)
    if request.method == "POST":
        material.delete()
        messages.success(request, "Material deleted.")
    return redirect(_redirect_with_query(request, "portal:lecturer-materials"))


@role_required(User.Role.LECTURER)
def lecturer_students(request):
    context = dashboard_context(
        request,
        "Users",
        **departmental_download_context(request, scope="lecturer"),
    )
    return render(request, "portal/departmental_students.html", context)


@role_required(User.Role.LECTURER)
def lecturer_students_pdf(request):
    payments = departmental_payment_queryset().filter(session=current_departmental_session())
    if request.user.department_id:
        payments = payments.filter(department=request.user.department)
    search_form = DepartmentalSearchForm(request.GET or None)
    if search_form.is_valid():
        student_id = search_form.cleaned_data.get("student_id")
        department = search_form.cleaned_data.get("department")
        level = search_form.cleaned_data.get("level")
        if student_id:
            payments = payments.filter(student__id_number__icontains=student_id)
        if department:
            payments = payments.filter(department=department)
        if level:
            payments = payments.filter(student__level=level)
    student_ids = [payment.student.id_number for payment in payments if payment.student.id_number]
    subtitle_parts = []
    if search_form.is_valid():
        department = search_form.cleaned_data.get("department")
        level = search_form.cleaned_data.get("level")
        search = search_form.cleaned_data.get("student_id")
        if department:
            subtitle_parts.append(f"Department {department.name}")
        if level:
            subtitle_parts.append(f"Level {level}")
        if search:
            subtitle_parts.append(f"Search {search}")
    subtitle = " | ".join(subtitle_parts) if subtitle_parts else None
    pdf_bytes = build_pdf_document(
        "Lecturer Student ID List",
        student_ids or ["No matching students found."],
        subtitle=subtitle,
    )
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="lecturer-student-ids.pdf"'
    return response


@role_required(User.Role.ADMIN)
def admin_departmental_users(request):
    context = dashboard_context(
        request,
        "Users",
        **departmental_download_context(request, scope="admin"),
    )
    return render(request, "portal/departmental_students.html", context)


@role_required(User.Role.LECTURER)
def lecturer_update_material_access(request, access_id, status):
    access = get_object_or_404(MaterialAccess, pk=access_id, material__lecturer=request.user)
    valid_statuses = {choice[0] for choice in MaterialAccess.Status.choices}
    if request.method == "POST" and status in valid_statuses:
        access.status = status
        access.save(update_fields=["status", "updated_at"])
        messages.success(request, "Material access updated.")
    return redirect(_redirect_with_query(request, "portal:lecturer-students"))


@role_required(User.Role.LECTURER)
def lecturer_messages(request):
    form = NotificationForm(user=request.user)
    sent_notifications = (
        Notification.objects.filter(sender=request.user)
        .annotate(recipient_total=Count("recipients"))
        .prefetch_related("departments", "courses", "attachments")
    )
    if request.method == "POST":
        form = NotificationForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            notification = form.save(commit=False)
            notification.sender = request.user
            notification.save()
            form.save_m2m()
            NotificationAttachment.objects.bulk_create([
                NotificationAttachment(institution=request.institution, notification=notification, file=attachment)
                for attachment in form.cleaned_data["attachments"]
            ])
            deliver_notification(notification)
            messages.success(request, "Message sent to the selected students and departments.")
            return redirect(_redirect_with_query(request, "portal:lecturer-messages"))
    context = dashboard_context(request, "Messages", form=form, sent_notifications=sent_notifications)
    return render(request, "portal/lecturer_messages.html", context)


@role_required(User.Role.LECTURER)
def lecturer_delete_message(request, notification_id):
    notification = get_object_or_404(Notification, pk=notification_id, sender=request.user)
    if request.method == "POST":
        UserAlert.objects.filter(
            alert_type=UserAlert.AlertType.MESSAGE,
            dedupe_key__startswith=f"message:{notification.id}:",
        ).delete()
        notification.delete()
        messages.success(request, "Sent message deleted.")
    return redirect("portal:lecturer-messages")


@role_required(User.Role.LECTURER)
def lecturer_documents(request):
    timetable_filter_form, timetables = _filtered_timetables(request, prefix="tt")
    handbook_filter_form, handbooks = _filtered_handbooks(request, prefix="hb")
    timetable_form = TimetableForm()
    if request.method == "POST" and request.POST.get("action") == "create-timetable":
        timetable_form = TimetableForm(request.POST, request.FILES)
        if timetable_form.is_valid():
            timetable_form.save()
            messages.success(request, "Timetable uploaded.")
            return redirect(_append_querystring(reverse("portal:lecturer-documents"), request))
    context = dashboard_context(
        request,
        "Timetable and Handbook",
        timetable_form=timetable_form,
        timetable_filter_form=timetable_filter_form,
        timetables=timetables,
        handbook_filter_form=handbook_filter_form,
        handbooks=handbooks,
    )
    return render(request, "portal/lecturer_documents.html", context)


@role_required(User.Role.STUDENT)
@require_http_methods(["GET", "POST"])
def student_accommodation(request):
    """Let a student apply once per open accommodation session.

    The application is intentionally separated from allocation: students never
    submit a bed identifier, so a crafted request cannot reserve or replace a
    bed.  Allocation remains an authorised staff workflow below.
    """
    applications = AccommodationApplication.objects.filter(student=request.user).select_related(
        "accommodation_session__academic_session", "preferred_hostel", "allocation__bed__room__block__hostel"
    )
    form = AccommodationApplicationForm(student=request.user)
    if request.method == "POST":
        form = AccommodationApplicationForm(request.POST, student=request.user)
        if form.is_valid():
            try:
                with transaction.atomic():
                    application = form.save(commit=False)
                    application.student = request.user
                    application.institution = request.institution
                    application.full_clean()
                    application.save()
            except IntegrityError:
                form.add_error("accommodation_session", "You already have an application for that accommodation session.")
            else:
                AuditLog.objects.create(
                    institution=request.institution, user=request.user,
                    action="accommodation_application_created", object_type="AccommodationApplication",
                    object_id=str(application.pk), description="Student submitted an accommodation application.",
                    ip_address=_client_ip(request),
                )
                messages.success(request, "Your accommodation application has been submitted for review.")
                return redirect("portal:student-accommodation")
    return render(request, "portal/student_accommodation.html", dashboard_context(
        request, "Accommodation", form=form, applications=applications,
    ))


@role_required(User.Role.ADMIN, User.Role.BURSARY)
@require_http_methods(["GET", "POST"])
def accommodation_admin(request):
    """Tenant-scoped control centre for inventory and applications."""
    forms_by_action = {
        "session": AccommodationSessionForm,
        "hostel": HostelForm,
        "block": HostelBlockForm,
        "room": HostelRoomForm,
        "bed": HostelBedForm,
    }
    active_form = None
    if request.method == "POST":
        action = request.POST.get("action", "")
        form_class = forms_by_action.get(action)
        if not form_class:
            raise PermissionDenied("Unsupported accommodation action.")
        active_form = form_class(request.POST)
        if active_form.is_valid():
            record = active_form.save(commit=False)
            record.institution = request.institution
            record.full_clean()
            record.save()
            AuditLog.objects.create(
                institution=request.institution, user=request.user,
                action=f"accommodation_{action}_created", object_type=record.__class__.__name__,
                object_id=str(record.pk), description=f"Created accommodation {action} {record}.",
                ip_address=_client_ip(request),
            )
            messages.success(request, f"{action.title()} saved.")
            return redirect("portal:accommodation-admin")

    beds = HostelBed.objects.filter(is_available=True, room__is_available=True, room__block__hostel__is_active=True)
    occupied = AccommodationAllocation.objects.filter(status=AccommodationAllocation.Status.ACTIVE).count()
    context = dashboard_context(
        request, "Accommodation Management",
        session_form=active_form if isinstance(active_form, AccommodationSessionForm) else AccommodationSessionForm(),
        hostel_form=active_form if isinstance(active_form, HostelForm) else HostelForm(),
        block_form=active_form if isinstance(active_form, HostelBlockForm) else HostelBlockForm(),
        room_form=active_form if isinstance(active_form, HostelRoomForm) else HostelRoomForm(),
        bed_form=active_form if isinstance(active_form, HostelBedForm) else HostelBedForm(),
        accommodation_sessions=AccommodationSession.objects.select_related("academic_session")[:20],
        hostels=Hostel.objects.prefetch_related("blocks__rooms__beds")[:30],
        applications=AccommodationApplication.objects.select_related(
            "student", "accommodation_session", "preferred_hostel"
        ).order_by("status", "-created_at")[:100],
        total_beds=beds.count(), occupied_beds=occupied, available_beds=max(beds.count() - occupied, 0),
        pending_applications=AccommodationApplication.objects.filter(status=AccommodationApplication.Status.PENDING).count(),
    )
    return render(request, "portal/accommodation_admin.html", context)


@role_required(User.Role.ADMIN, User.Role.BURSARY)
@require_http_methods(["POST"])
def accommodation_application_decision(request, application_id):
    """Approve/reject an application and atomically allocate a free bed."""
    application = get_object_or_404(AccommodationApplication.objects.select_related("student"), pk=application_id)
    action = request.POST.get("action")
    if action not in {"approve", "reject", "cancel-allocation"}:
        raise PermissionDenied("Unsupported accommodation decision.")
    form = AccommodationDecisionForm(request.POST, application=application)
    if action == "approve" and not form.is_valid():
        messages.error(request, "Choose an available bed before approving this application.")
        return redirect("portal:accommodation-admin")

    try:
        with transaction.atomic():
            application = AccommodationApplication.objects.select_for_update().get(pk=application.pk)
            if action == "approve":
                if application.status in {AccommodationApplication.Status.REJECTED, AccommodationApplication.Status.CANCELLED}:
                    raise ValidationError("This application cannot be approved in its current state.")
                bed = HostelBed.objects.select_for_update().get(pk=form.cleaned_data["bed"].pk)
                if not bed.is_available or not bed.room.is_available or not bed.room.block.hostel.is_active:
                    raise ValidationError("The selected bed is unavailable.")
                if AccommodationAllocation.objects.filter(bed=bed, status=AccommodationAllocation.Status.ACTIVE).exists():
                    raise ValidationError("The selected bed has just been allocated to another student.")
                allocation, created = AccommodationAllocation.objects.get_or_create(
                    application=application,
                    defaults={"student": application.student, "bed": bed, "institution": request.institution, "allocated_by": request.user},
                )
                if not created and allocation.status == AccommodationAllocation.Status.CANCELLED:
                    allocation.bed = bed
                    allocation.status = AccommodationAllocation.Status.ACTIVE
                    allocation.cancelled_at = None
                    allocation.cancellation_reason = ""
                    allocation.allocated_by = request.user
                    allocation.full_clean()
                    allocation.save()
                elif not created and allocation.status == AccommodationAllocation.Status.ACTIVE:
                    raise ValidationError("This application already has an active allocation.")
                application.status = AccommodationApplication.Status.APPROVED
            elif action == "reject":
                if hasattr(application, "allocation") and application.allocation.status == AccommodationAllocation.Status.ACTIVE:
                    raise ValidationError("Cancel the active allocation before rejecting this application.")
                application.status = AccommodationApplication.Status.REJECTED
            else:
                allocation = getattr(application, "allocation", None)
                if not allocation or allocation.status != AccommodationAllocation.Status.ACTIVE:
                    raise ValidationError("This application has no active allocation to cancel.")
                allocation.cancel(reason=request.POST.get("decision_note", ""))
                application.status = AccommodationApplication.Status.CANCELLED
            application.reviewed_by = request.user
            application.reviewed_at = timezone.now()
            application.decision_note = request.POST.get("decision_note", "").strip()
            application.full_clean()
            application.save(update_fields=["status", "reviewed_by", "reviewed_at", "decision_note", "updated_at"])
    except (IntegrityError, ValidationError) as exc:
        messages.error(request, getattr(exc, "message", str(exc)))
    else:
        AuditLog.objects.create(
            institution=request.institution, user=request.user,
            action=f"accommodation_application_{action}", object_type="AccommodationApplication",
            object_id=str(application.pk), new_value={"status": application.status},
            description=f"Accommodation application was {action}d.", ip_address=_client_ip(request),
        )
        messages.success(request, "Accommodation application updated.")
    return redirect("portal:accommodation-admin")


@role_required(User.Role.LECTURER)
@require_http_methods(["GET", "POST"])
def lecturer_results(request):
    """Enter results only for courses assigned to the active lecturer."""
    if active_role(request) != User.Role.LECTURER:
        raise PermissionDenied("Switch to your Lecturer role to enter results.")
    assigned_courses = lecturer_registered_courses_queryset(request.user)
    edit_result = None
    if request.GET.get("edit"):
        edit_result = get_object_or_404(CourseResult.objects.filter(course__in=assigned_courses), pk=request.GET["edit"])
    form = CourseResultForm(user=request.user, instance=edit_result)
    import_form = ResultImportForm(user=request.user)
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "import":
            import_form = ResultImportForm(request.POST, request.FILES, user=request.user)
            if import_form.is_valid():
                try:
                    summary = import_course_results(
                        import_form.cleaned_data["file"],
                        course=import_form.cleaned_data["course"],
                        session=import_form.cleaned_data["session"],
                        semester=import_form.cleaned_data["semester"],
                        lecturer=request.user,
                        institution=request.institution,
                    )
                except ValueError as exc:
                    import_form.add_error("file", str(exc))
                else:
                    AuditLog.objects.create(
                        institution=request.institution, user=request.user, action="result_file_imported",
                        object_type="CourseResult", description=summary, ip_address=_client_ip(request),
                    )
                    messages.success(request, summary)
                    return redirect("portal:lecturer-results")
            form = CourseResultForm(user=request.user, instance=edit_result)
        elif action not in {"save", "submit"}:
            raise PermissionDenied("Unsupported result action.")
        else:
            candidate = CourseResultForm(request.POST, user=request.user, instance=edit_result)
            if candidate.is_valid():
                existing = CourseResult.objects.filter(
                    student=candidate.cleaned_data["student"], course=candidate.cleaned_data["course"],
                    session=candidate.cleaned_data["session"], semester=candidate.cleaned_data["semester"],
                ).first()
                if existing and existing.pk != getattr(edit_result, "pk", None) and existing.status not in {CourseResult.Status.DRAFT, CourseResult.Status.RETURNED}:
                    candidate.add_error(None, "This result has already been submitted for review and can no longer be edited.")
                else:
                    form = CourseResultForm(request.POST, user=request.user, instance=existing or edit_result)
                    if form.is_valid():
                        result = form.save(commit=False)
                        result.institution = request.institution
                        result.submitted_by = request.user
                        if action == "submit":
                            result.status = CourseResult.Status.SUBMITTED
                            result.submitted_at = timezone.now()
                        else:
                            result.status = CourseResult.Status.DRAFT
                        result.save()
                        AuditLog.objects.create(
                            institution=request.institution, user=request.user,
                            action=f"result_{'submitted' if action == 'submit' else 'saved'}",
                            object_type="CourseResult", object_id=str(result.pk),
                            new_value={"score": str(result.score), "grade": result.grade, "status": result.status},
                            description=f"Lecturer {action}d a course result.", ip_address=_client_ip(request),
                        )
                        messages.success(request, "Result saved." if action == "save" else "Result submitted for Exam Officer review.")
                        return redirect("portal:lecturer-results")
                    candidate = form
            form = candidate
    results = CourseResult.objects.filter(
        course__in=assigned_courses
    ).select_related("student", "course", "session")[:150]
    return render(request, "portal/lecturer_results.html", dashboard_context(
        request, "Result Entry", form=form, import_form=import_form, edit_result=edit_result, results=results,
    ))


@role_required(User.Role.EXAM_OFFICER, User.Role.HOD)
@require_http_methods(["GET", "POST"])
def exam_results(request):
    """Department-scoped review and publication gate for results."""
    selected_role = active_role(request)
    if selected_role == User.Role.HOD:
        department = hod_department_for(request)
    else:
        assignment = request.user.role_assignments.filter(
            role=User.Role.EXAM_OFFICER, is_active=True, department__isnull=False,
        ).select_related("department").first()
        department = assignment.department if assignment else request.user.department
    if not department:
        raise PermissionDenied("An Exam Officer must be assigned to a department.")
    results = CourseResult.objects.filter(course__department=department).select_related("student", "course", "session")
    if request.method == "POST":
        if selected_role != User.Role.EXAM_OFFICER:
            raise PermissionDenied("Only an active Exam Officer may approve or publish results.")
        result = get_object_or_404(results, pk=request.POST.get("result_id"))
        action = request.POST.get("action")
        review_form = ResultReviewForm(request.POST)
        if action not in {"approve", "publish", "return"} or not review_form.is_valid():
            raise PermissionDenied("Invalid result-review action.")
        if action == "approve" and result.status != CourseResult.Status.SUBMITTED:
            messages.error(request, "Only submitted results can be approved.")
        elif action == "publish" and result.status != CourseResult.Status.APPROVED:
            messages.error(request, "Only approved results can be published.")
        elif action == "return" and result.status not in {CourseResult.Status.SUBMITTED, CourseResult.Status.APPROVED}:
            messages.error(request, "Only submitted or approved results can be returned.")
        else:
            result.status = {
                "approve": CourseResult.Status.APPROVED,
                "publish": CourseResult.Status.PUBLISHED,
                "return": CourseResult.Status.RETURNED,
            }[action]
            result.reviewed_by = request.user
            result.reviewed_at = timezone.now()
            result.review_note = review_form.cleaned_data["note"]
            if action == "publish":
                result.published_at = timezone.now()
            result.save()
            AuditLog.objects.create(
                institution=request.institution, user=request.user, action=f"result_{action}d",
                object_type="CourseResult", object_id=str(result.pk),
                new_value={"status": result.status}, description=f"Exam Officer {action}d a result.",
                ip_address=_client_ip(request),
            )
            messages.success(request, "Result workflow updated.")
        return redirect("portal:exam-results")
    return render(request, "portal/exam_results.html", dashboard_context(
        request, "Result Review", results=results[:200], department=department,
        can_review=selected_role == User.Role.EXAM_OFFICER,
    ))


@role_required(User.Role.STUDENT)
def student_results(request):
    """Show only the signed-in student's published academic results."""
    results = CourseResult.objects.filter(student=request.user, status=CourseResult.Status.PUBLISHED).select_related("course", "session")
    total_units = sum(result.credit_units for result in results)
    quality_points = sum(result.credit_units * result.grade_point for result in results)
    gpa = (quality_points / total_units) if total_units else None
    return render(request, "portal/student_results.html", dashboard_context(
        request, "Academic Results", results=results, total_units=total_units, gpa=gpa,
    ))


@role_required(User.Role.ADMIN)
def admin_dashboard(request):
    lecturer_messages = (
        Notification.objects.filter(sender__role=User.Role.LECTURER)
        .select_related("sender")
        .annotate(recipient_total=Count("recipients"))
    )
    context = dashboard_context(
        request,
        "Admin Dashboard",
        student_count=User.objects.filter(role=User.Role.STUDENT).count(),
        lecturer_count=User.objects.filter(role=User.Role.LECTURER).count(),
        pending_lecturers=User.objects.filter(role=User.Role.LECTURER, is_approved=False).select_related("department")[:6],
        department_count=Department.objects.count(),
        course_count=Course.objects.count(),
        uploaded_course_count=Course.objects.exclude(file="").count(),
        recent_courses=Course.objects.select_related("department", "lecturer")[:5],
        lecturer_messages=lecturer_messages,
    )
    return render(request, "portal/admin_dashboard.html", context)


@role_required(User.Role.ADMIN)
def admin_departmental(request):
    return redirect("portal:admin-departmental-apis")


@role_required(User.Role.ADMIN)
def admin_departmental_apis(request):
    search = request.GET.get("search", "").strip()
    departments = _search_filter(Department.objects.all(), search, "name", "code")
    selected_department = None
    gateway = None
    department_id = request.GET.get("department")
    if department_id:
        selected_department = get_object_or_404(Department, pk=department_id)
        gateway = DepartmentPaymentGateway.objects.filter(department=selected_department).first()

    if request.method == "POST":
        messages.error(request, "Departmental payment APIs can only be changed by the department's HOD.")
        return redirect(_redirect_with_query(request, "portal:admin-departmental-apis"))

    context = dashboard_context(
        request,
        "View Departmental Payment APIs",
        departments=departments,
        selected_department=selected_department,
        gateway=gateway,
        department_search=search,
    )
    return render(request, "portal/admin_departmental_apis.html", context)


def hod_department_for(request):
    """Return the department the signed-in lecturer is allowed to manage as HOD."""
    department = Department.objects.filter(
        head_of_department=request.user,
        pk=request.user.department_id,
    ).first()
    if department is None and request.user.has_role(User.Role.HOD):
        assignment = request.user.role_assignments.filter(
            role=User.Role.HOD, is_active=True, department__isnull=False,
        ).select_related("department").first()
        department = assignment.department if assignment else None
    if department is None:
        raise PermissionDenied
    return department


@role_required(User.Role.LECTURER, User.Role.HOD)
def hod_programmes(request):
    """Show only the programmes owned by the signed-in HOD's department."""
    department = hod_department_for(request)
    programmes = Programme.objects.filter(department=department).annotate(
        student_total=Count("students", distinct=True),
        course_total=Count("courses", distinct=True),
        handbook_total=Count("handbooks", distinct=True),
    ).order_by("name")
    return render(request, "portal/hod_programmes.html", dashboard_context(
        request, "Programmes", department=department, programmes=programmes,
    ))


@role_required(User.Role.LECTURER, User.Role.HOD)
@require_http_methods(["GET", "POST"])
def hod_programme_detail(request, programme_id):
    """Manage a programme's handbook and its exclusive/shared curriculum."""
    department = hod_department_for(request)
    programme = get_object_or_404(Programme, pk=programme_id, department=department)
    current_session = current_departmental_session()
    active_curriculum = curriculum_for_programme(programme, current_session)
    handbook_form = HandbookForm(initial={"department": department, "programme": programme})

    if request.method == "POST" and request.POST.get("action") == "upload-handbook":
        form_data = request.POST.copy()
        form_data["department"] = str(department.pk)
        form_data["programme"] = str(programme.pk)
        handbook_form = HandbookForm(form_data, request.FILES)
        if handbook_form.is_valid():
            handbook = handbook_form.save()
            try:
                summary = import_handbook_courses(handbook)
            except ValueError as exc:
                messages.warning(request, f"Handbook uploaded, but course automation could not run: {exc}")
            else:
                AuditLog.objects.create(
                    institution=request.institution, user=request.user, action="programme_handbook_uploaded",
                    object_type="Handbook", object_id=str(handbook.pk), description=summary,
                    new_value={"programme_id": programme.pk}, ip_address=_client_ip(request),
                )
                messages.success(request, summary)
            return redirect("portal:hod-programme-detail", programme_id=programme.pk)
        messages.error(request, "Choose a valid handbook file.")

    elif request.method == "POST" and request.POST.get("action") == "share-course":
        course = get_object_or_404(Course.objects.filter(department=department), pk=request.POST.get("course_id"))
        course.programmes.add(programme)
        if active_curriculum:
            course.curricula.add(active_curriculum)
        AuditLog.objects.create(
            institution=request.institution, user=request.user, action="programme_course_shared",
            object_type="Course", object_id=str(course.pk), description=f"Shared {course.code} with {programme.code}.",
            new_value={"programme_id": programme.pk}, ip_address=_client_ip(request),
        )
        messages.success(request, f"{course.code} is now available to {programme.name} students in this curriculum.")
        return redirect("portal:hod-programme-detail", programme_id=programme.pk)

    programme_courses = Course.objects.filter(
        department=department, programmes=programme,
    ).filter(Q(curricula=active_curriculum) | Q(curricula__isnull=True, curriculum=active_curriculum)).select_related(
        "lecturer",
    ).prefetch_related("programmes").distinct()
    shareable_courses = Course.objects.filter(department=department).exclude(
        programmes=programme,
    ).order_by("code")
    students = User.objects.filter(role=User.Role.STUDENT, programme=programme).order_by(
        "level", "last_name", "first_name", "username",
    )
    handbooks = Handbook.objects.filter(programme=programme).order_by("-created_at")
    return render(request, "portal/hod_programme_detail.html", dashboard_context(
        request,
        programme.name,
        department=department,
        programme=programme,
        active_curriculum=active_curriculum,
        programme_courses=programme_courses,
        shareable_courses=shareable_courses,
        students=students,
        handbooks=handbooks,
        handbook_form=handbook_form,
    ))


@role_required(User.Role.LECTURER, User.Role.HOD)
@require_http_methods(["GET", "POST"])
def hod_exam_officer(request):
    """Allow only a department HOD to add Exam Officer to a lecturer account."""
    department = hod_department_for(request)
    lecturers = User.objects.filter(department=department).filter(
        Q(role=User.Role.LECTURER) | Q(role_assignments__role=User.Role.LECTURER, role_assignments__is_active=True)
    ).distinct().order_by("first_name", "last_name", "username")
    if request.method == "POST":
        lecturer = get_object_or_404(lecturers, pk=request.POST.get("lecturer_id"))
        assignment, created = RoleAssignment.objects.get_or_create(
            institution=request.institution,
            user=lecturer,
            role=User.Role.EXAM_OFFICER,
            department=department,
            defaults={"assigned_by": request.user},
        )
        if not assignment.is_active:
            assignment.is_active = True
            assignment.assigned_by = request.user
            assignment.save(update_fields=["is_active", "assigned_by", "updated_at"])
            created = True
        if created:
            AuditLog.objects.create(
                institution=request.institution, user=request.user, action="exam_officer_assigned",
                object_type="RoleAssignment", object_id=str(assignment.pk),
                new_value={"lecturer_id": lecturer.id, "department_id": department.id, "role": User.Role.EXAM_OFFICER},
                description="HOD assigned Exam Officer while retaining the lecturer account and role.", ip_address=_client_ip(request),
            )
            messages.success(request, f"{lecturer.full_name} is now an Exam Officer for {department.name}.")
        else:
            messages.info(request, f"{lecturer.full_name} is already the Exam Officer for this department.")
        return redirect("portal:hod-exam-officer")
    exam_officer_ids = set(RoleAssignment.objects.filter(
        department=department, role=User.Role.EXAM_OFFICER, is_active=True,
    ).values_list("user_id", flat=True))
    return render(request, "portal/hod_exam_officer.html", dashboard_context(
        request, "Exam Officer", department=department, lecturers=lecturers, exam_officer_ids=exam_officer_ids,
    ))


@role_required(User.Role.LECTURER, User.Role.HOD)
def hod_departmental_apis(request):
    department = hod_department_for(request)
    gateway = ensure_department_gateway_credentials(department)
    form = DepartmentPaymentGatewayForm(instance=gateway)
    course_gateway, _ = DepartmentCoursePaymentGateway.objects.get_or_create(department=department)
    course_gateway_form = DepartmentCoursePaymentGatewayForm(instance=course_gateway)
    allocation_form = CourseAllocationUploadForm()
    handbook_form = HandbookForm(initial={"department": department})
    if request.method == "POST" and request.POST.get("action") == "upload-allocations":
        allocation_form = CourseAllocationUploadForm(request.POST, request.FILES)
        if allocation_form.is_valid():
            upload = allocation_form.save(commit=False)
            upload.department = department
            upload.uploaded_by = request.user
            upload.save()
            try:
                upload.processing_summary = import_course_allocations(upload)
                upload.save(update_fields=["processing_summary", "updated_at"])
                messages.success(request, upload.processing_summary)
            except ValueError as exc:
                messages.error(request, str(exc))
            return redirect("portal:hod-api-and-document")
        messages.error(request, "Choose a valid course-allocation file.")
    elif request.method == "POST" and request.POST.get("action") == "upload-handbook":
        handbook_data = request.POST.copy()
        handbook_data["department"] = str(department.id)
        handbook_form = HandbookForm(handbook_data, request.FILES)
        if handbook_form.is_valid():
            handbook = handbook_form.save()
            try:
                messages.success(request, import_handbook_courses(handbook))
            except ValueError as exc:
                messages.warning(request, f"Handbook uploaded, but course automation could not run: {exc}")
            return redirect("portal:hod-api-and-document")
        messages.error(request, "Choose a valid handbook file.")
    elif request.method == "POST" and request.POST.get("action") == "save-course-gateway":
        course_gateway_form = DepartmentCoursePaymentGatewayForm(request.POST, instance=course_gateway)
        if course_gateway_form.is_valid():
            course_gateway_form.save()
            messages.success(request, f"Paid-course Paystack settings updated for {department.name}.")
            return redirect("portal:hod-api-and-document")
        messages.error(request, "Please correct the paid-course gateway details and try again.")
    elif request.method == "POST":
        form = DepartmentPaymentGatewayForm(request.POST, instance=gateway)
        if form.is_valid():
            form.save()
            messages.success(request, f"Departmental Paystack settings updated for {department.name}.")
            return redirect("portal:hod-api-and-document")
        messages.error(request, "Please correct the departmental gateway details and try again.")

    return render(request, "portal/hod_departmental_apis.html", dashboard_context(
        request,
        "API and Document",
        department=department,
        gateway_form=form,
        course_gateway_form=course_gateway_form,
        allocation_form=allocation_form,
        handbook_form=handbook_form,
    ))


@role_required(User.Role.ADMIN)
def admin_course_api(request):
    messages.info(request, "Paid-course APIs are configured by each department's HOD.")
    return redirect("portal:admin-dashboard")


@role_required(User.Role.ADMIN)
def admin_departmental_fees(request):
    current_session = AcademicSession.objects.filter(is_current=True).first()
    search = request.GET.get("search", "").strip()
    departments = _search_filter(Department.objects.all(), search, "name", "code")
    selected_department = None
    session_fee_rows = []
    department_id = request.GET.get("department")
    if department_id:
        selected_department = get_object_or_404(Department, pk=department_id)
        session_list = AcademicSession.objects.all()
        for session in session_list:
            session_fee_rows.append({
                "session": session,
                "fees": DepartmentalFee.objects.filter(
                    session=session,
                    department=selected_department,
                ).select_related("association"),
            })

    if request.method == "POST":
        messages.error(request, "Departmental fees can only be changed by the department's HOD.")
        return redirect(_redirect_with_query(request, "portal:admin-departmental-fees"))

    context = dashboard_context(
        request,
        "View Departmental Fees",
        departments=departments,
        selected_department=selected_department,
        session_fee_rows=session_fee_rows,
        current_departmental_session=current_session,
    )
    return render(request, "portal/admin_departmental_fees.html", context)


@role_required(User.Role.LECTURER, User.Role.HOD)
def hod_departmental_fees(request):
    department = hod_department_for(request)
    ensure_departmental_defaults()
    session = current_departmental_session()
    fee_map = fee_map_for_session(session, department)

    association_form = DepartmentalAssociationForm(department=department)
    if request.method == "POST" and request.POST.get("action") == "delete-association":
        association = get_object_or_404(
            DepartmentalAssociation,
            pk=request.POST.get("association_id"),
            department=department,
            is_constant=False,
        )
        if DepartmentalPaymentItem.objects.filter(association=association).exists():
            messages.error(request, f"{association.name} cannot be deleted because students have already paid it.")
        else:
            association.delete()
            messages.success(request, f"{association.name} was deleted from {department.name}.")
        return redirect("portal:hod-departmental-fees")
    elif request.method == "POST" and request.POST.get("action") == "create-association":
        association_form = DepartmentalAssociationForm(request.POST, department=department)
        if association_form.is_valid():
            association = association_form.save(commit=False)
            association.department = department
            association.is_constant = False
            association.save()
            for academic_session in AcademicSession.objects.all():
                DepartmentalFee.objects.get_or_create(
                    session=academic_session,
                    department=department,
                    association=association,
                    defaults={"amount": Decimal("0")},
                )
            messages.success(request, f"{association.name} is ready to price for {department.name}.")
            return redirect("portal:hod-departmental-fees")
    elif request.method == "POST":
        fees_by_association_id = {fee.association_id: fee for fee in fee_map.values()}
        updates = {}
        for association in departmental_associations_for_department(department):
            amount = request.POST.get(f"fee_{association.id}", "").strip()
            try:
                value = Decimal(amount)
                if value < 0:
                    raise InvalidOperation
            except InvalidOperation:
                messages.error(request, f"Enter a valid non-negative amount for {association.name}.")
                return redirect("portal:hod-departmental-fees")
            updates[association.id] = value
        with transaction.atomic():
            for association_id, amount in updates.items():
                fee = fees_by_association_id[association_id]
                fee.amount = amount
                fee.save(update_fields=["amount", "updated_at"])
        messages.success(request, f"Fees updated for {department.name} in {session.name}.")
        return redirect("portal:hod-departmental-fees")

    return render(request, "portal/hod_departmental_fees.html", dashboard_context(
        request,
        "Set Departmental Fees",
        department=department,
        current_academic_session=session,
        fee_map=fee_map,
        association_form=association_form,
    ))


@role_required(User.Role.ADMIN)
def admin_departmental_download(request):
    context = dashboard_context(
        request,
        "Departmental Download",
        **departmental_download_context(request, scope="admin"),
    )
    return render(request, "portal/admin_departmental_download.html", context)


@role_required(User.Role.ADMIN)
def admin_hods(request):
    search = request.GET.get("search", "").strip()
    departments = Department.objects.select_related("faculty", "head_of_department")
    departments = _search_filter(departments, search, "name", "code")
    selected_department = None
    lecturers = User.objects.none()
    department_id = request.GET.get("department")
    if department_id:
        selected_department = get_object_or_404(Department, pk=department_id)
        lecturers = User.objects.filter(
            role=User.Role.LECTURER,
            department=selected_department,
            is_approved=True,
        ).order_by("first_name", "last_name", "username")

    if request.method == "POST" and request.POST.get("action") == "assign-hod":
        selected_department = get_object_or_404(Department, pk=request.POST.get("department_id"))
        lecturer = get_object_or_404(
            User.objects.filter(
                role=User.Role.LECTURER,
                department=selected_department,
                is_approved=True,
            ),
            pk=request.POST.get("lecturer_id"),
        )
        selected_department.head_of_department = lecturer
        selected_department.save(update_fields=["head_of_department", "updated_at"])
        # Keep the legacy department pointer while granting the additive HOD
        # role required by the role switcher and HOD-only permissions.
        RoleAssignment.objects.filter(
            institution=request.institution,
            department=selected_department,
            role=User.Role.HOD,
        ).exclude(user=lecturer).update(is_active=False)
        RoleAssignment.objects.update_or_create(
            institution=request.institution,
            user=lecturer,
            role=User.Role.HOD,
            department=selected_department,
            defaults={"assigned_by": request.user, "is_active": True},
        )
        AuditLog.objects.create(
            institution=request.institution,
            user=request.user,
            action="hod_assigned",
            object_type="Department",
            object_id=str(selected_department.pk),
            new_value={"hod_id": lecturer.id, "department_id": selected_department.id},
            description="Institution Admin assigned an HOD and activated the HOD role.",
            ip_address=_client_ip(request),
        )
        messages.success(request, f"{lecturer.full_name} is now HOD of {selected_department.name}.")
        return redirect(f'{reverse("portal:admin-hods")}?department={selected_department.id}')

    return render(request, "portal/admin_hods.html", dashboard_context(
        request,
        "Heads of Department",
        departments=departments,
        selected_department=selected_department,
        lecturers=lecturers,
        department_search=search,
    ))


@role_required(User.Role.ADMIN)
def admin_departments(request):
    form = DepartmentForm()
    search = request.GET.get("search", "").strip()
    departments = Department.objects.select_related("faculty").annotate(course_total=Count("courses"))
    departments = _search_filter(departments, search, "name", "code", "description")
    if request.method == "POST":
        action = request.POST.get("action", "create-department")
        if action == "create-department":
            form = DepartmentForm(request.POST)
            if form.is_valid():
                department = form.save()
                ensure_department_gateway_credentials(department)
                ensure_departmental_fees_for_department(department)
                messages.success(request, "Department created.")
                return redirect(_redirect_with_query(request, "portal:admin-departments"))
        elif action == "upload-lecturers":
            department = get_object_or_404(Department, pk=request.POST.get("department_id"))
            upload_form = DepartmentLecturerUploadForm(request.POST, request.FILES)
            if upload_form.is_valid():
                upload = upload_form.save(commit=False)
                upload.department, upload.uploaded_by = department, request.user
                upload.save()
                try:
                    upload.processing_summary = import_department_lecturers(upload)
                    upload.save(update_fields=["processing_summary", "updated_at"])
                    messages.success(request, upload.processing_summary)
                except ValueError as exc:
                    upload.processing_summary = str(exc)
                    upload.save(update_fields=["processing_summary", "updated_at"])
                    messages.error(request, str(exc))
                return redirect(_redirect_with_query(request, "portal:admin-departments"))
            messages.error(request, "Choose a valid department-lecturers file.")
    context = dashboard_context(
        request,
        "Manage Departments",
        form=form,
        departments=departments,
    )
    return render(request, "portal/admin_departments.html", context)


@role_required(User.Role.ADMIN)
def admin_faculties(request):
    form = FacultyForm()
    faculties = Faculty.objects.annotate(department_total=Count("departments"))
    if request.method == "POST":
        form = FacultyForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Faculty created. You can now create departments under it.")
            return redirect("portal:admin-faculties")
    return render(request, "portal/admin_faculties.html", dashboard_context(request, "Manage Faculties", form=form, faculties=faculties))


@role_required(User.Role.LECTURER, User.Role.HOD)
@require_http_methods(["GET", "POST"])
def admin_programmes(request):
    """Compatibility endpoint for old programme URLs.

    Programme administration belongs to the HOD.  Keep the old URL name so
    existing bookmarks do not become broken links, but send an authorised HOD
    to the department-scoped programme workspace instead of exposing a second
    institution-wide management surface.
    """
    hod_department_for(request)
    return redirect("portal:hod-programmes")


@role_required(User.Role.ADMIN)
def admin_about(request):
    session_form = AcademicSessionForm(initial={"is_current": True})
    if request.method == "POST":
        session_form = AcademicSessionForm(request.POST)
        if session_form.is_valid():
            session = session_form.save()
            for department in Department.objects.all():
                ensure_departmental_fees_for_department(department)
            reset_count = reset_lecturer_course_registrations() if session.is_current else 0
            messages.success(
                request,
                f"Academic session {session.name} created"
                + (
                    f" and set as current. {reset_count} lecturer course registration(s) were cleared; "
                    "students start this session with no registered courses."
                    if session.is_current else "."
                ),
            )
            return redirect("portal:admin-about")
    return render(
        request,
        "portal/admin_about.html",
        dashboard_context(
            request,
            "Academic Sessions",
            session_form=session_form,
            current_session=AcademicSession.objects.filter(is_current=True).first(),
            past_sessions=AcademicSession.objects.filter(is_current=False),
        ),
    )


@role_required(User.Role.ADMIN)
def admin_delete_department(request, department_id):
    department = get_object_or_404(Department, pk=department_id)
    if request.method == "POST":
        department.delete()
        messages.success(request, "Department deleted.")
    return redirect(_redirect_with_query(request, "portal:admin-departments"))


@role_required(User.Role.ADMIN)
def admin_courses(request):
    form = CourseForm()
    filter_form = CourseBrowseFilterForm(request.GET or None)
    courses = Course.objects.select_related("department", "lecturer")
    if filter_form.is_valid():
        department = filter_form.cleaned_data.get("department")
        level = filter_form.cleaned_data.get("level")
        fee_type = filter_form.cleaned_data.get("fee_type")
        search = filter_form.cleaned_data.get("search")
        if department:
            courses = courses.filter(department=department)
        if level:
            courses = courses.filter(level=level)
        if fee_type == "free":
            courses = courses.filter(is_free=True)
        elif fee_type == "paid":
            courses = courses.filter(is_free=False)
        courses = _search_filter(courses, search, "code", "title", "department__name", "lecturer__username")
    edit_course = None
    edit_form = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create-course":
            form = CourseForm(request.POST, request.FILES)
            if form.is_valid():
                form.save()
                messages.success(request, "Course saved.")
                return redirect(_redirect_with_query(request, "portal:admin-courses"))
        elif action == "update-course":
            edit_course = get_object_or_404(Course, pk=request.POST.get("course_id"))
            edit_form = LecturerCourseUpdateForm(request.POST, request.FILES, instance=edit_course)
            if edit_form.is_valid():
                edit_form.save()
                messages.success(request, "Course details updated.")
                return redirect(_redirect_with_query(request, "portal:admin-courses"))
    edit_id = request.GET.get("edit")
    if edit_id and edit_form is None:
        edit_course = get_object_or_404(Course, pk=edit_id)
        edit_form = LecturerCourseUpdateForm(instance=edit_course)
    context = dashboard_context(
        request,
        "Manage Courses",
        form=form,
        filter_form=filter_form,
        edit_course=edit_course,
        edit_form=edit_form,
        courses=courses,
    )
    return render(request, "portal/admin_courses.html", context)


@role_required(User.Role.ADMIN)
def admin_delete_course(request, course_id):
    course = get_object_or_404(Course, pk=course_id)
    if request.method == "POST":
        course.delete()
        messages.success(request, "Course deleted.")
    return redirect(_redirect_with_query(request, "portal:admin-courses"))


@role_required(User.Role.ADMIN)
def admin_documents(request):
    timetable_filter_form, timetables = _filtered_timetables(request, prefix="ttf")
    handbook_filter_form, handbooks = _filtered_handbooks(request, prefix="hbf")
    timetable_form = TimetableForm()
    handbook_form = HandbookForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create-timetable":
            timetable_form = TimetableForm(request.POST, request.FILES)
            if timetable_form.is_valid():
                timetable_form.save()
                messages.success(request, "Timetable uploaded.")
                return redirect(_append_querystring(reverse("portal:admin-documents"), request))
        elif action == "create-handbook":
            handbook_form = HandbookForm(request.POST, request.FILES)
            if handbook_form.is_valid():
                handbook = handbook_form.save()
                try:
                    messages.success(request, import_handbook_courses(handbook))
                except ValueError as exc:
                    messages.warning(request, f"Handbook uploaded, but course automation could not run: {exc}")
                return redirect(_append_querystring(reverse("portal:admin-documents"), request))
    context = dashboard_context(
        request,
        "Manage Documents",
        timetable_form=timetable_form,
        handbook_form=handbook_form,
        timetable_filter_form=timetable_filter_form,
        handbook_filter_form=handbook_filter_form,
        timetables=timetables,
        handbooks=handbooks,
    )
    return render(request, "portal/admin_documents.html", context)


@role_required(User.Role.ADMIN)
def admin_delete_document(request, kind, document_id):
    model = Timetable if kind == "timetable" else Handbook
    document = get_object_or_404(model, pk=document_id)
    if request.method == "POST":
        document.delete()
        messages.success(request, f"{kind.title()} deleted.")
    return redirect(_redirect_with_query(request, "portal:admin-documents"))


@role_required(User.Role.ADMIN)
def admin_materials(request):
    form = CourseMaterialBatchForm(user=request.user, allow_all_courses=True)
    search = request.GET.get("search", "").strip()
    materials = CourseMaterial.objects.select_related("course", "lecturer")
    materials = _search_filter(materials, search, "title", "course__code", "lecturer__username", "course__department__name")
    if request.method == "POST":
        form = CourseMaterialBatchForm(request.POST, request.FILES, user=request.user, allow_all_courses=True)
        if form.is_valid():
            if not form.cleaned_data["course"].lecturer:
                form.add_error("course", "Assign a lecturer to the selected course before adding materials.")
            else:
                created_materials = _create_course_materials_from_upload(form, lecturer=form.cleaned_data["course"].lecturer)
                if created_materials:
                    messages.success(request, f"{len(created_materials)} course file(s) created.")
                    return redirect(_redirect_with_query(request, "portal:admin-materials"))
                messages.error(request, "Please choose at least one file to upload.")
    context = dashboard_context(request, "Manage Materials", form=form, materials=materials)
    return render(request, "portal/admin_materials.html", context)


@role_required(User.Role.ADMIN)
def admin_delete_material(request, material_id):
    material = get_object_or_404(CourseMaterial, pk=material_id)
    if request.method == "POST":
        material.delete()
        messages.success(request, "Material deleted.")
    return redirect(_redirect_with_query(request, "portal:admin-materials"))


@role_required(User.Role.ADMIN)
def admin_users(request):
    student_form = StudentUserForm(prefix="student")
    lecturer_form = LecturerUserForm(prefix="lecturer")
    staff_form = StaffUserForm(prefix="staff")
    search = request.GET.get("search", "").strip()
    students = User.objects.filter(role=User.Role.STUDENT).select_related("department")
    lecturers = User.objects.filter(role=User.Role.LECTURER).select_related("department")
    students = _search_filter(students, search, "username", "first_name", "last_name", "id_number", "department__name")
    lecturers = _search_filter(lecturers, search, "username", "first_name", "last_name", "id_number", "department__name")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create-student":
            student_form = StudentUserForm(request.POST, prefix="student")
            if student_form.is_valid():
                student_form.save()
                messages.success(request, "Student account created.")
                return redirect(_redirect_with_query(request, "portal:admin-users"))
        elif action == "create-lecturer":
            lecturer_form = LecturerUserForm(request.POST, prefix="lecturer")
            if lecturer_form.is_valid():
                lecturer_form.save()
                messages.success(request, "Lecturer account created and marked pending approval.")
                return redirect(_redirect_with_query(request, "portal:admin-users"))
        elif action == "create-staff":
            staff_form = StaffUserForm(request.POST, prefix="staff")
            if staff_form.is_valid():
                user = staff_form.save()
                assigned_role = staff_form.cleaned_data["assigned_role"]
                RoleAssignment.objects.get_or_create(
                    institution=request.institution, user=user, role=assigned_role,
                    department=user.department, defaults={"assigned_by": request.user},
                )
                if assigned_role == User.Role.HOD:
                    user.department.head_of_department = user
                    user.department.save(update_fields=["head_of_department"])
                AuditLog.objects.create(
                    institution=request.institution, user=request.user, action="staff_role_assigned",
                    object_type="User", object_id=str(user.pk), new_value={"role": assigned_role},
                    description="Institution Admin created a staff account and assigned an institutional role.", ip_address=_client_ip(request),
                )
                messages.success(request, f"Staff account created with the {User.Role(assigned_role).label} role.")
                return redirect(_redirect_with_query(request, "portal:admin-users"))
    context = dashboard_context(
        request,
        "Manage Users",
        student_form=student_form,
        lecturer_form=lecturer_form,
        staff_form=staff_form,
        students=students,
        lecturers=lecturers,
    )
    return render(request, "portal/admin_users.html", context)


@role_required(User.Role.ADMIN)
def admin_approve_lecturer(request, user_id):
    lecturer = get_object_or_404(User, pk=user_id, role=User.Role.LECTURER)
    if request.method == "POST":
        lecturer.is_approved = True
        lecturer.save(update_fields=["is_approved"])
        messages.success(request, f"{lecturer.full_name} has been approved.")
    return redirect(_redirect_with_query(request, "portal:admin-users"))


@role_required(User.Role.ADMIN)
def admin_reset_user_password(request, user_id):
    user = get_object_or_404(User, pk=user_id)
    if request.method == "POST" and not user.is_superuser:
        user.set_password("educonnect")
        user.save(update_fields=["password"])
        messages.success(request, f"{user.full_name}'s password was reset to the temporary password 'educonnect'.")
    return redirect(_redirect_with_query(request, "portal:admin-users"))


@role_required(User.Role.ADMIN)
def admin_delete_user(request, user_id):
    user = get_object_or_404(User, pk=user_id)
    if request.method == "POST" and not user.is_superuser:
        user.delete()
        messages.success(request, "User removed.")
    return redirect(_redirect_with_query(request, "portal:admin-users"))


@role_required(User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN)
def download_departmental_document(request, document_id):
    document = get_object_or_404(
        DepartmentalPaymentDocument.objects.select_related("payment", "payment__student", "payment__department"),
        pk=document_id,
    )
    if request.user.role == User.Role.STUDENT and document.payment.student_id != request.user.id:
        raise PermissionDenied
    return FileResponse(document.file.open("rb"), as_attachment=request.GET.get("download") == "1", filename=document.file.name.rsplit("/", 1)[-1])


@role_required(User.Role.STUDENT, User.Role.ADMIN)
def download_exam_card(request, payment_id):
    payment = get_object_or_404(
        DepartmentalPayment.objects.select_related("student", "department", "session"),
        pk=payment_id,
        status=DepartmentalPayment.Status.PAID,
    )
    if request.user.role == User.Role.STUDENT and payment.student_id != request.user.id:
        raise PermissionDenied
    registered_courses = [
        f"{registration.course.code} - {registration.course.title}"
        for registration in StudentCourseRegistration.objects.filter(
            student=payment.student,
            session=payment.session,
        ).select_related("course").order_by("course__code")
    ]
    pdf_bytes = build_exam_card_pdf(
        school_name=InstitutionProfile.objects.first().name if InstitutionProfile.objects.exists() else "Educonnect",
        card_title="Departmental Examination Clearance Card",
        session_label=payment.session.name,
        fields=[
            ("Student Name", payment.student.full_name),
            ("Matric No.", payment.student.id_number or "N/A"),
            ("Department", payment.department.name),
            ("Level", payment.student.level or "N/A"),
            ("Semester", payment.semester_label),
        ],
        registered_courses=registered_courses,
        portrait_text=payment.student.full_name,
        passport_photo_path=payment.student.passport_photo.path if payment.student.passport_photo else None,
    )
    session_slug = payment.session.name.replace("/", "-")
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="exam-card-{payment.student.id_number or payment.student.username}-{session_slug}.pdf"'
    return response


@role_required(User.Role.ADMIN, User.Role.LECTURER)
def departmental_student_ids_pdf(request):
    payments = departmental_payment_queryset().filter(session=current_departmental_session())
    if request.user.role == User.Role.LECTURER and request.user.department_id:
        payments = payments.filter(department=request.user.department)
    department_id = request.GET.get("department")
    level = request.GET.get("level")
    if department_id:
        payments = payments.filter(department_id=department_id)
    if level:
        payments = payments.filter(student__level=level)
    student_ids = list(payments.values_list("student__id_number", flat=True))
    subtitle_bits = []
    if department_id:
        department = Department.objects.filter(pk=department_id).first()
        if department:
            subtitle_bits.append(department.name)
    if level:
        subtitle_bits.append(f"Level {level}")
    pdf_bytes = build_pdf_document(
        "Departmental Payment Student IDs",
        student_ids or ["No matching paid students found."],
        subtitle=" | ".join(subtitle_bits) if subtitle_bits else None,
    )
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="departmental-student-ids.pdf"'
    return response


@role_required(User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN)
def download_course_file(request, course_id):
    course = get_object_or_404(Course.objects.select_related("department", "lecturer"), pk=course_id)
    if not course.file:
        raise Http404
    if request.user.role == User.Role.STUDENT:
        registration = StudentCourseRegistration.objects.filter(
            student=request.user,
            course=course,
            session=current_departmental_session(),
        ).first()
        if not registration:
            raise PermissionDenied
        if not course.is_free:
            payment = current_course_payment_queryset().filter(
                student=request.user,
                course=course,
                status=CoursePayment.Status.PAID,
            ).first()
            if not payment:
                messages.error(request, "Payment is required before downloading this course file.")
                return redirect("portal:student-courses")
    return FileResponse(course.file.open("rb"), as_attachment=request.GET.get("download") == "1", filename=course.file.name.rsplit("/", 1)[-1])


@role_required(User.Role.LECTURER)
def lecturer_paid_course_students_pdf(request, course_id):
    course = get_object_or_404(lecturer_registered_courses_queryset(request.user), pk=course_id)
    if course.is_free:
        messages.info(request, "This course is free, so there is no paid student list to download.")
        return redirect("portal:lecturer-courses")
    current_session = current_departmental_session()
    selected_group_id = request.GET.get("group")
    payments = paid_course_students_queryset(course, current_session)
    selected_group = None
    if selected_group_id:
        selected_group = get_object_or_404(
            CourseStudentGroup.objects.filter(course=course, session=current_session), pk=selected_group_id,
        )
        payments = payments.filter(student__course_group_memberships__group=selected_group)
    student_lines = [
        f"{payment.student.full_name} | {payment.student.id_number or 'No ID'} | {payment.student.department.name if payment.student.department else 'No department'}"
        for payment in payments
    ]
    pdf_bytes = build_pdf_document(
        f"Paid Students - {course.code}",
        student_lines or ["No matching paid students found for this course yet."],
        subtitle=f"{course.title} | {course.department.name} | {selected_group.name if selected_group else 'All paid students'}",
    )
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    group_suffix = f"-{selected_group.name.lower().replace(' ', '-')}" if selected_group else ""
    response["Content-Disposition"] = f'attachment; filename="{course.code.lower()}-paid-students{group_suffix}.pdf"'
    return response


@role_required(User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN)
def download_material(request, material_id):
    material = get_object_or_404(CourseMaterial.objects.select_related("course", "lecturer"), pk=material_id)
    if request.user.is_superuser or request.user.role in {User.Role.ADMIN, User.Role.LECTURER}:
        return FileResponse(material.file.open("rb"), as_attachment=request.GET.get("download") == "1", filename=material.file.name.rsplit("/", 1)[-1])
    if request.user.role != User.Role.STUDENT:
        raise PermissionDenied
    registration = StudentCourseRegistration.objects.filter(
        student=request.user,
        course=material.course,
        session=current_departmental_session(),
    ).first()
    if not registration:
        raise PermissionDenied
    access = MaterialAccess.objects.filter(material=material, student=request.user).first()
    if access and access.status == MaterialAccess.Status.BLOCKED:
        messages.error(request, "You do not currently have access to download this material.")
        return redirect("portal:student-materials")
    if not material.course.is_free:
        payment = current_course_payment_queryset().filter(
            student=request.user,
            course=material.course,
            status=CoursePayment.Status.PAID,
        ).first()
        if not payment:
            if not access or access.status == MaterialAccess.Status.BLOCKED or not access.can_download:
                messages.error(request, "You do not currently have access to download this material.")
                return redirect("portal:student-materials")
        else:
            access, _ = MaterialAccess.objects.get_or_create(
                material=material,
                student=request.user,
                defaults={"status": MaterialAccess.Status.PAID},
            )
            if access.status != MaterialAccess.Status.PAID:
                access.status = MaterialAccess.Status.PAID
                access.save(update_fields=["status", "updated_at"])
    else:
        access, _ = MaterialAccess.objects.get_or_create(
            material=material,
            student=request.user,
            defaults={"status": MaterialAccess.Status.AVAILABLE},
        )
    return FileResponse(material.file.open("rb"), as_attachment=request.GET.get("download") == "1", filename=material.file.name.rsplit("/", 1)[-1])


@role_required(User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN)
def download_message_attachment(request, notification_id):
    notification = get_object_or_404(
        Notification.objects.select_related("sender").prefetch_related("recipients"),
        pk=notification_id,
    )
    if not notification.attachment:
        raise Http404
    if request.user.role == User.Role.STUDENT:
        if not notification.recipients.filter(student=request.user, is_deleted=False).exists():
            raise PermissionDenied
    elif request.user.role == User.Role.LECTURER and notification.sender_id != request.user.id:
        raise PermissionDenied
    return FileResponse(
        notification.attachment.open("rb"),
        as_attachment=request.GET.get("download") == "1",
        filename=notification.attachment.name.rsplit("/", 1)[-1],
    )


@role_required(User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN)
def download_notification_attachment(request, attachment_id):
    attachment = get_object_or_404(
        NotificationAttachment.objects.select_related("notification", "notification__sender").prefetch_related(
            "notification__recipients"
        ),
        pk=attachment_id,
    )
    notification = attachment.notification
    if request.user.role == User.Role.STUDENT:
        if not notification.recipients.filter(student=request.user, is_deleted=False).exists():
            raise PermissionDenied
    elif request.user.role == User.Role.LECTURER and notification.sender_id != request.user.id:
        raise PermissionDenied
    return FileResponse(
        attachment.file.open("rb"),
        as_attachment=request.GET.get("download") == "1",
        filename=attachment.file.name.rsplit("/", 1)[-1],
    )


@role_required(User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN)
def download_document(request, kind, document_id):
    model = Timetable if kind == "timetable" else Handbook
    document = get_object_or_404(model.objects.select_related("department"), pk=document_id)
    if request.user.role == User.Role.STUDENT and request.user.department_id != document.department_id:
        raise PermissionDenied
    if not document.file:
        raise Http404
    return FileResponse(document.file.open("rb"), as_attachment=request.GET.get("download") == "1", filename=document.file.name.rsplit("/", 1)[-1])
