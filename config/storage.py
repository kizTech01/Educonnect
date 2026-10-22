"""Storage configuration helpers shared by settings and tests.

R2 is S3-compatible, but its objects are deliberately kept private.  The
application therefore streams files through authenticated Django views rather
than using public media URLs.
"""

from django.core.exceptions import ImproperlyConfigured


R2_REQUIRED_ENVIRONMENT_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_STORAGE_BUCKET_NAME",
    "AWS_S3_ENDPOINT_URL",
    "AWS_S3_REGION_NAME",
    "AWS_DEFAULT_ACL",
    "AWS_QUERYSTRING_AUTH",
)


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def r2_environment_status(environment):
    """Return whether R2 settings are absent, complete, or partially set."""
    values = {
        name: str(environment.get(name, "") or "").strip()
        for name in R2_REQUIRED_ENVIRONMENT_VARIABLES
    }
    missing = tuple(name for name, value in values.items() if not value)
    return values, missing


def default_media_storage(environment, *, debug: bool):
    """Build Django's default media storage without exposing credentials.

    Local development keeps Django's filesystem storage.  A production
    process with any R2 setting but an incomplete configuration fails at
    startup instead of silently accepting uploads on ephemeral storage.
    """
    values, missing = r2_environment_status(environment)
    supplied = bool(set(R2_REQUIRED_ENVIRONMENT_VARIABLES) - set(missing))
    complete = not missing

    if supplied and not complete:
        if not debug:
            raise ImproperlyConfigured(
                "Incomplete Cloudflare R2 media configuration; missing "
                + ", ".join(missing)
                + "."
            )
        return {"BACKEND": "django.core.files.storage.FileSystemStorage"}

    if complete:
        acl = values["AWS_DEFAULT_ACL"].lower()
        if acl not in {"", "private"}:
            raise ImproperlyConfigured(
                "AWS_DEFAULT_ACL must be blank or private for EduConnect media."
            )
        return {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "access_key": values["AWS_ACCESS_KEY_ID"],
                "secret_key": values["AWS_SECRET_ACCESS_KEY"],
                "bucket_name": values["AWS_STORAGE_BUCKET_NAME"],
                "endpoint_url": values["AWS_S3_ENDPOINT_URL"],
                "region_name": values["AWS_S3_REGION_NAME"] or "auto",
                "addressing_style": "path",
                # R2 objects remain private; the portal does not expose
                # FieldFile.url links.  None prevents object ACL headers.
                "default_acl": None,
                "querystring_auth": _as_bool(values["AWS_QUERYSTRING_AUTH"]),
                # Match FileSystemStorage's collision-safe behavior.  A new
                # upload must never overwrite an existing object by name.
                "file_overwrite": False,
            },
        }

    if not debug:
        raise ImproperlyConfigured(
            "Cloudflare R2 media configuration is required when DJANGO_DEBUG is False."
        )
    return {"BACKEND": "django.core.files.storage.FileSystemStorage"}
