import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import PasswordResetForm
from django.core.files.base import ContentFile
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max
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
    CourseDetailsForm,
    CourseBrowseFilterForm,
    CourseStudentGroupForm,
    CourseMaterialBatchForm,
    AcademicSessionForm,
    DepartmentForm,
    DepartmentLecturerUploadForm,
    CourseAllocationUploadForm,
    FacultyForm,
    InstitutionProfileForm,
    DepartmentPaymentGatewayForm,
    DepartmentCoursePaymentGatewayForm,
    DepartmentalAdditionalDocumentForm,
    DepartmentalAssociationForm,
    DepartmentalSearchForm,
    DepartmentalStudentPaymentForm,
    HandbookForm,
    HandbookFilterForm,
    LecturerCourseUpdateForm,
    LecturerProfileForm,
    LecturerUserForm,
    LecturerSignupForm,
    NotificationForm,
    PortalAuthenticationForm,
    StudentCourseFilterForm,
    StudentProfileForm,
    StudentUserForm,
    StudentSignupForm,
    TimetableFilterForm,
    TimetableForm,
    LecturerCourseFilterForm,
)
from .models import (
    AcademicSession,
    Course,
    CoursePaymentGateway,
    CourseMaterial,
    Department,
    DepartmentLecturerUpload,
    CourseAllocationUpload,
    Faculty,
    InstitutionProfile,
    DepartmentPaymentGateway,
    DepartmentCoursePaymentGateway,
    DepartmentalAssociation,
    DepartmentalFee,
    DepartmentalPayment,
    DepartmentalPaymentDocument,
    DepartmentalPaymentItem,
    Handbook,
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
    curriculum_for_department,
)
from .services import (
    DEFAULT_PAYSTACK_PUBLIC_KEY,
    DEFAULT_PAYSTACK_SECRET_KEY,
    deliver_notification,
    ensure_default_admin_user,
    initialize_paystack_transaction,
    paystack_amount_in_kobo,
    sync_material_access_for_material,
    sync_material_access_for_registration,
    verify_paystack_transaction,
)
from .pdf import build_exam_card_pdf, build_pdf_document
from .automation import import_course_allocations, import_department_lecturers, import_handbook_courses
from .branding import LogoLookupError, get_institution_logo

LOGIN_ROLES = {User.Role.STUDENT, User.Role.LECTURER, User.Role.ADMIN}


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


def auth_page_context(role, login_form=None, signup_form=None):
    ensure_default_admin_user()
    role_label = User.Role(role).label
    signup_available = role in {User.Role.STUDENT, User.Role.LECTURER}
    signup_form = signup_form if signup_form is not None else signup_form_for_role(role)
    return {
        "auth_role": role,
        "role_label": role_label,
        "page_title": f"{role_label} Login",
        "page_description": (
            "Use your portal credentials to continue."
            if role == User.Role.ADMIN
            else f"Use your {role_label.lower()} credentials to enter your dashboard."
        ),
        "login_form": login_form or login_form_for_role(role),
        "login_action": reverse("portal:role-login", kwargs={"role": role}),
        "login_button_label": f"Enter {role_label} Dashboard",
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
            if request.user.is_superuser or request.user.role in roles:
                return view_func(request, *args, **kwargs)
            raise PermissionDenied

        return never_cache(_wrapped)

    return decorator


def ensure_department_gateway_credentials(department):
    gateway, _ = DepartmentPaymentGateway.objects.get_or_create(
        department=department,
        defaults={
            "paystack_public_key": DEFAULT_PAYSTACK_PUBLIC_KEY,
            "paystack_secret_key": DEFAULT_PAYSTACK_SECRET_KEY,
        },
    )
    updated_fields = []
    if not gateway.paystack_public_key:
        gateway.paystack_public_key = DEFAULT_PAYSTACK_PUBLIC_KEY
        updated_fields.append("paystack_public_key")
    if not gateway.paystack_secret_key:
        gateway.paystack_secret_key = DEFAULT_PAYSTACK_SECRET_KEY
        updated_fields.append("paystack_secret_key")
    if updated_fields:
        updated_fields.append("updated_at")
        gateway.save(update_fields=updated_fields)
    return gateway


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


def sidebar_links(request):
    current = request.resolver_match.url_name if request.resolver_match else ""
    unread_message_count = 0
    if request.user.is_authenticated and request.user.role == User.Role.STUDENT:
        unread_message_count = NotificationRecipient.objects.filter(
            student=request.user,
            is_deleted=False,
            is_read=False,
        ).count()
    if request.user.is_superuser or request.user.role == User.Role.ADMIN:
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
            {"label": "About", "url_name": "portal:admin-about"},
            {"label": "Courses", "url_name": "portal:admin-courses"},
            {
                "label": "Paid Courses",
                "url": f'{reverse("portal:admin-courses")}?fee_type=paid',
                "active": current == "admin-courses" and request.GET.get("fee_type") == "paid",
            },
            {"label": "Timetable and Handbook", "url_name": "portal:admin-documents"},
            {"label": "Users", "url_name": "portal:admin-users"},
        ]
    elif request.user.role == User.Role.LECTURER:
        is_hod = Department.objects.filter(
            head_of_department=request.user,
            pk=request.user.department_id,
        ).exists()
        departmental_links = [
            {"label": "Students", "url_name": "portal:lecturer-departmental-download"},
        ]
        if is_hod:
            departmental_links.extend([
                {"label": "API and Document", "url_name": "portal:hod-api-and-document"},
                {"label": "Set Fee", "url_name": "portal:hod-departmental-fees"},
            ])
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
    else:
        links = [
            {"label": "Dashboard", "url_name": "portal:student-dashboard"},
            {"label": "Departmental", "url_name": "portal:student-departmental"},
            {"label": "Course Registration", "url_name": "portal:student-courses"},
            {"label": "Messages", "url_name": "portal:student-messages", "badge_count": unread_message_count},
            {"label": "Timetable and Handbook", "url_name": "portal:student-documents"},
            {"label": "Profile", "url_name": "portal:profile"},
        ]
    resolved_links = []
    for item in links:
        if "children" in item:
            children = []
            active = False
            for child in item["children"]:
                child_active = current == child["url_name"].split(":")[1]
                active = active or child_active
                children.append({"label": child["label"], "url": reverse(child["url_name"]), "active": child_active})
            resolved_links.append({"label": item["label"], "children": children, "active": active})
        else:
            url = item.get("url") or reverse(item["url_name"])
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
    context = {
        "section_title": title,
        "sidebar_links": sidebar_links(request),
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
    secrets = {DEFAULT_PAYSTACK_SECRET_KEY}
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
    if user.curriculum_id and user.curriculum.department_id == user.department_id:
        return user.curriculum
    curriculum = curriculum_for_department(user.department, current_departmental_session())
    if curriculum:
        user.curriculum = curriculum
        user.save(update_fields=["curriculum"])
    return curriculum


def _student_course_catalog_queryset(student):
    curriculum = _student_curriculum(student)
    if not curriculum:
        return Course.objects.none()
    queryset = Course.objects.select_related("department", "lecturer", "curriculum").filter(
        department=student.department,
        curriculum=curriculum,
    )
    # A student moves through the same curriculum as their level changes. Old
    # records that predate a stored level remain browsable for compatibility.
    if student.level:
        queryset = queryset.filter(level=student.level)
    return queryset


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
    requested_role = role or request.POST.get("role")
    if requested_role not in LOGIN_ROLES:
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

    role = form.cleaned_data["role"]
    username = form.cleaned_data["username"]
    password = form.cleaned_data["password"]

    if requested_role != role:
        if requested_role:
            form.add_error(None, "Please use the login form for this account type.")
            return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
        messages.error(request, "Please use the correct login section for this account.")
        return redirect("portal:home")

    active_role = requested_role

    user = authenticate(
        request,
        username=username,
        password=password,
    )

    if not user:
        if requested_role:
            form.add_error(None, "Invalid username or password.")
            return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
        messages.error(request, "Invalid username or password.")
        return redirect("portal:home")
    if active_role == User.Role.ADMIN:
        if not (user.is_superuser or user.role == User.Role.ADMIN):
            if requested_role:
                form.add_error(None, "This account is not an admin account.")
                return render(request, "portal/auth_login.html", auth_page_context(requested_role, login_form=form))
            messages.error(request, "This account is not an admin account.")
            return redirect("portal:home")
    elif user.role != active_role:
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

    login(request, user)
    return redirect("portal:dashboard")


@ensure_csrf_cookie
@never_cache
def student_signup(request):
    if request.method == "GET":
        return render(request, "portal/auth_signup.html", signup_page_context(User.Role.STUDENT))
    form = StudentSignupForm(request.POST)
    if form.is_valid():
        user = form.save()
        user.curriculum = curriculum_for_department(
            user.department,
            current_departmental_session(),
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
        return render(request, "portal/auth_signup.html", signup_page_context(User.Role.LECTURER))
    form = LecturerSignupForm(request.POST)
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
    return JsonResponse({"ok": True, "event": event, "processed": processed})


def dashboard_redirect(request):
    if not request.user.is_authenticated:
        return redirect("portal:home")
    if request.user.is_superuser or request.user.role == User.Role.ADMIN:
        return redirect("portal:admin-dashboard")
    if request.user.role == User.Role.LECTURER:
        return redirect("portal:lecturer-dashboard")
    return redirect("portal:student-dashboard")


@role_required(User.Role.STUDENT, User.Role.LECTURER)
def profile(request):
    form_class = LecturerProfileForm if request.user.role == User.Role.LECTURER else StudentProfileForm
    form = form_class(instance=request.user)
    if request.method == "POST":
        previous_level = request.user.level
        previous_department_id = request.user.department_id
        form = form_class(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            if request.user.role == User.Role.STUDENT and request.user.department_id != previous_department_id:
                request.user.curriculum = curriculum_for_department(
                    request.user.department,
                    current_departmental_session(),
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
            browser_alerts_enabled=request.user.browser_alerts_enabled,
        ),
    )


@role_required(User.Role.STUDENT, User.Role.LECTURER)
def send_profile_password_reset(request):
    if request.method != "POST":
        raise PermissionDenied
    if not request.user.email:
        messages.error(request, "Add an email address to your profile before requesting a password reset.")
        return redirect("portal:profile")

    form = PasswordResetForm({"email": request.user.email})
    if form.is_valid():
        form.save(
            request=request,
            use_https=request.is_secure(),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@educonnect.local"),
            subject_template_name="portal/emails/password_reset_subject.txt",
            email_template_name="portal/emails/password_reset_email.txt",
        )
        messages.success(request, f"A password reset verification link has been sent to {request.user.email}.")
    else:
        messages.error(request, "We could not send the password reset email right now.")
    return redirect("portal:profile")


def lecturer_registered_courses_queryset(user):
    return Course.objects.filter(lecturer_registrations__lecturer=user).select_related("department", "lecturer").distinct()


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
    filter_form = StudentCourseFilterForm(request.GET or None)
    if request.user.department_id:
        filter_form.fields["department"].queryset = Department.objects.filter(pk=request.user.department_id)

    available_courses = _student_course_catalog_queryset(request.user)
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
                    CourseStudentGroupMembership(group=group, student=student)
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
                NotificationAttachment(notification=notification, file=attachment)
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
    if department is None:
        raise PermissionDenied
    return department


@role_required(User.Role.LECTURER)
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


@role_required(User.Role.LECTURER)
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


@role_required(User.Role.ADMIN)
def admin_about(request):
    institution = InstitutionProfile.objects.first()
    if institution is None:
        institution = InstitutionProfile.objects.create(name="Educonnect")
    form = InstitutionProfileForm(instance=institution)
    session_form = AcademicSessionForm(initial={"is_current": True})
    if request.method == "POST":
        if request.POST.get("action") == "create-academic-session":
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
        else:
            form = InstitutionProfileForm(request.POST, instance=institution)
            if form.is_valid():
                updated_institution = form.save(commit=False)
                if updated_institution.name:
                    try:
                        logo_bytes, extension = get_institution_logo(updated_institution.name)
                    except LogoLookupError as exc:
                        updated_institution.save()
                        messages.warning(
                            request,
                            f"Institution name saved, but an official logo could not be verified: {exc}",
                        )
                    else:
                        updated_institution.logo.save(
                            f"institution-logo.{extension}",
                            ContentFile(logo_bytes),
                            save=False,
                        )
                        updated_institution.save()
                        messages.success(request, "Institution name and verified official logo saved.")
                else:
                    updated_institution.save()
                    messages.success(request, "Institution name saved.")
                return redirect("portal:admin-about")
    return render(
        request,
        "portal/admin_about.html",
        dashboard_context(
            request,
            "About Your Institution",
            form=form,
            institution=institution,
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
    context = dashboard_context(
        request,
        "Manage Users",
        student_form=student_form,
        lecturer_form=lecturer_form,
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
