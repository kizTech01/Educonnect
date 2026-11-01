"""Fields for confidential values stored in the application database."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


class EncryptedTextField(models.CharField):
    """Fernet-encrypted text with plaintext migration and key rotation support.

    ``PAYSTACK_CREDENTIALS_ENCRYPTION_KEY`` is always the write key.  During
    a rotation, deploy the new value there and place the old key(s) in
    ``PAYSTACK_CREDENTIALS_PREVIOUS_ENCRYPTION_KEYS``.  Reading an old value
    succeeds and its next save transparently re-encrypts it with the new key.
    """

    prefix = "enc:v1:"

    def _configured_keys(self):
        configured_key = getattr(settings, "PAYSTACK_CREDENTIALS_ENCRYPTION_KEY", "").strip()
        if configured_key:
            try:
                primary = Fernet(configured_key.encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise ImproperlyConfigured(
                    "PAYSTACK_CREDENTIALS_ENCRYPTION_KEY must be a valid Fernet key."
                ) from exc
            previous_values = getattr(
                settings, "PAYSTACK_CREDENTIALS_PREVIOUS_ENCRYPTION_KEYS", ""
            )
            previous_keys = [key.strip() for key in previous_values.split(",") if key.strip()]
            try:
                return [primary, *(Fernet(key.encode("ascii")) for key in previous_keys)]
            except (ValueError, TypeError) as exc:
                raise ImproperlyConfigured(
                    "PAYSTACK_CREDENTIALS_PREVIOUS_ENCRYPTION_KEYS must contain valid Fernet keys."
                ) from exc
        if not (settings.DEBUG or getattr(settings, "TESTING", False)):
            raise ImproperlyConfigured(
                "PAYSTACK_CREDENTIALS_ENCRYPTION_KEY is required when DJANGO_DEBUG is False."
            )
        digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
        return [Fernet(base64.urlsafe_b64encode(digest))]

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        if not isinstance(value, str) or not value.startswith(self.prefix):
            return value
        encrypted_value = value[len(self.prefix):].encode("ascii")
        for fernet in self._configured_keys():
            try:
                return fernet.decrypt(encrypted_value).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError):
                continue
        raise ImproperlyConfigured("Unable to decrypt a stored payment credential.")

    def to_python(self, value):
        return value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        if value.startswith(self.prefix):
            return value
        encrypted = self._configured_keys()[0].encrypt(value.encode("utf-8")).decode("ascii")
        return f"{self.prefix}{encrypted}"
