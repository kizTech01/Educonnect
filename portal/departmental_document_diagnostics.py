"""Temporary, log-only diagnostics for departmental document downloads.

Remove this module and its call sites after the production 404 incident has
been diagnosed. The helper deliberately emits only routing, tenant, record,
and storage status -- never credentials, session material, payment secrets,
or file contents.
"""

import logging
import re


LOGGER_NAME = "portal.departmental_document_diagnostic"
LOGGER = logging.getLogger(LOGGER_NAME)
PREFIX = "DEPARTMENTAL_DOCUMENT_DIAGNOSTIC"
PATH_PATTERN = re.compile(r"^/departmental/documents/(?P<document_id>\d+)/download/$")


def log_departmental_document_diagnostic(request, event, **details):
    """Write an incident-only structured log line for the protected route."""
    match = PATH_PATTERN.match(request.path)
    if not match:
        return

    institution = getattr(request, "institution", None)
    fields = {
        "event": event,
        "path": request.path,
        "document_id": match.group("document_id"),
        "tenant_id": getattr(institution, "pk", None),
        "tenant_name": getattr(institution, "name", None),
        **details,
    }
    # ``repr`` gives unambiguous, one-line values without emitting arbitrary
    # objects such as request bodies, sessions, or uploaded-file contents.
    rendered = " ".join(f"{key}={value!r}" for key, value in fields.items())
    LOGGER.warning("%s %s", PREFIX, rendered)
