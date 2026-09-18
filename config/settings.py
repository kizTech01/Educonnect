import os
from pathlib import Path

from config.domain import core_domain_settings, parse_csv, unique

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE variables from a local .env file."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


load_env_file(BASE_DIR / ".env")


# Security
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-local-development-key-change-me",
)

DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in {"1", "true", "yes", "on"}

# CORE_DOMAIN is the single source of truth for the shared platform host and
# every institution subdomain. PLATFORM_BASE_DOMAIN remains as a compatibility
# alias for the tenant middleware, views, forms, and templates.
CORE_DOMAIN, core_allowed_hosts, core_csrf_origins = core_domain_settings(
    os.getenv("CORE_DOMAIN", "")
)
PLATFORM_BASE_DOMAIN = CORE_DOMAIN

ALLOWED_HOSTS = unique(
    [*core_allowed_hosts, *parse_csv(os.getenv("DJANGO_ALLOWED_HOSTS", ""))]
)
SCREENING_APPLICATION_URL_TEMPLATE = os.getenv(
    "SCREENING_APPLICATION_URL_TEMPLATE",
    "https://apply.{subdomain}.{base_domain}",
).strip()
SCREENING_API_MAX_CLOCK_SKEW_SECONDS = int(os.getenv("SCREENING_API_MAX_CLOCK_SKEW_SECONDS", "300"))
# Render automatically provides this variable.
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS = unique([*ALLOWED_HOSTS, RENDER_EXTERNAL_HOSTNAME])

if DEBUG:
    ALLOWED_HOSTS = unique([*ALLOWED_HOSTS, "localhost", "127.0.0.1"])


CSRF_TRUSTED_ORIGINS = unique(
    [*core_csrf_origins, *parse_csv(os.getenv("CSRF_TRUSTED_ORIGINS", ""))]
)


# Applications
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "portal",
]


# Middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "portal.middleware.TenantResolutionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "portal.middleware.TenantAccessMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


ROOT_URLCONF = "config.urls"


# Templates
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
            "portal.context_processors.institution",
            ],
        },
    },
]


WSGI_APPLICATION = "config.wsgi.application"


# Database
# Keep local development isolated from a production DATABASE_URL that may be
# present in .env. Render does not set this flag, so it continues to use its
# managed PostgreSQL database in production.
USE_LOCAL_SQLITE = os.getenv("DJANGO_USE_LOCAL_SQLITE", "False").lower() == "true"
DATABASE_URL = None if USE_LOCAL_SQLITE else os.getenv("DATABASE_URL")

if DATABASE_URL:
    import dj_database_url

    DATABASES = {
        "default": dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    db_engine = os.getenv("DB_ENGINE", "sqlite").lower()

    if db_engine == "postgresql":
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": os.getenv("DB_NAME", "educonnect"),
                "USER": os.getenv("DB_USER", "postgres"),
                "PASSWORD": os.getenv("DB_PASSWORD", ""),
                "HOST": os.getenv("DB_HOST", "127.0.0.1"),
                "PORT": os.getenv("DB_PORT", "5432"),
                "CONN_MAX_AGE": int(
                    os.getenv("DB_CONN_MAX_AGE", "60")
                ),
                "OPTIONS": {
                    "sslmode": os.getenv("DB_SSLMODE", "prefer"),
                },
            }
        }

    elif db_engine == "mysql":
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.mysql",
                "NAME": os.getenv("DB_NAME", "educonnect"),
                "USER": os.getenv("DB_USER", "root"),
                "PASSWORD": os.getenv("DB_PASSWORD", ""),
                "HOST": os.getenv("DB_HOST", "127.0.0.1"),
                "PORT": os.getenv("DB_PORT", "3306"),
                "CONN_MAX_AGE": int(
                    os.getenv("DB_CONN_MAX_AGE", "60")
                ),
                "OPTIONS": {
                    "charset": "utf8mb4",
                    "init_command": (
                        "SET sql_mode='STRICT_TRANS_TABLES'"
                    ),
                },
            }
        }

    else:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
            }
        }


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        )
    },
]

# Use only in automated tests to keep CI fast. Production and local development
# retain Django's default secure password hashers.
if os.getenv("DJANGO_TEST_FAST_PASSWORD_HASHER", "").lower() in {"1", "true", "yes", "on"}:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Africa/Lagos")
USE_I18N = True
USE_TZ = True


# Static files
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

assets_directory = BASE_DIR / "assets"

STATICFILES_DIRS = (
    [assets_directory]
    if assets_directory.exists()
    else []
)


# Uploaded media
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# Storage
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },


    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}


# Authentication
AUTH_USER_MODEL = "portal.User"

LOGIN_URL = "portal:home"
LOGIN_REDIRECT_URL = "portal:dashboard"
LOGOUT_REDIRECT_URL = "portal:home"


# Email
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend",
)

EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")

EMAIL_USE_TLS = (
    os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
)

EMAIL_USE_SSL = (
    os.getenv("EMAIL_USE_SSL", "False").lower() == "true"
)

EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10"))

DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL",
    "EduConnect <noreply@educonnect.local>",
)

SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Optional OpenAI-powered structured extraction for handbook and departmental
# import files. The portal keeps a local parser as a fallback when this is blank.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_AUTOMATION_MODEL = os.getenv("OPENAI_AUTOMATION_MODEL", "gpt-4.1-mini").strip()
OPENAI_LOGO_LOOKUP_MODEL = os.getenv("OPENAI_LOGO_LOOKUP_MODEL", "gpt-5").strip()
OPENAI_LOGO_PROCESSING_MODEL = os.getenv("OPENAI_LOGO_PROCESSING_MODEL", "gpt-image-2").strip()


# Optional bootstrap administrator
BOOTSTRAP_ADMIN_USERNAME = os.getenv(
    "BOOTSTRAP_ADMIN_USERNAME",
    "",
).strip()

BOOTSTRAP_ADMIN_PASSWORD = os.getenv(
    "BOOTSTRAP_ADMIN_PASSWORD",
    "",
).strip()

BOOTSTRAP_ADMIN_EMAIL = os.getenv(
    "BOOTSTRAP_ADMIN_EMAIL",
    "",
).strip()


# Production security
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = (
        "HTTP_X_FORWARDED_PROTO",
        "https",
    )

    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

    SECURE_HSTS_SECONDS = int(
        os.getenv("SECURE_HSTS_SECONDS", "3600")
    )
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Platform subscription payments use a platform-owned Paystack account.  Course
# and departmental gateways remain separate, existing institution workflows.
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "").strip()
