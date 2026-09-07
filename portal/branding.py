"""Locate and prepare institution logos for use throughout the portal."""
import base64
import ipaddress
import json
import mimetypes
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4
from html.parser import HTMLParser

from django.conf import settings


class LogoLookupError(ValueError):
    """Raised when an official institution logo cannot be safely retrieved."""


class LogoProcessingError(ValueError):
    """Raised when an uploaded logo cannot be made presentation-ready."""


def remove_logo_background(uploaded_logo):
    """Return an uploaded logo as a transparent PNG, prepared by GPT Image."""
    api_key = getattr(settings, "OPENAI_API_KEY", "")
    if not api_key:
        raise LogoProcessingError("Set OPENAI_API_KEY to remove a logo background automatically.")

    image_bytes = uploaded_logo.read()
    try:
        uploaded_logo.seek(0)
    except (AttributeError, OSError):
        pass

    if not image_bytes:
        raise LogoProcessingError("The uploaded logo was empty.")
    if len(image_bytes) > 8_000_000:
        raise LogoProcessingError("Upload a logo smaller than 8 MB.")

    filename = _safe_upload_filename(getattr(uploaded_logo, "name", "institution-logo.png"))
    content_type = _logo_content_type(filename, getattr(uploaded_logo, "content_type", ""))
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise LogoProcessingError("Upload the logo as a PNG, JPG, or WebP image.")

    body, boundary = _image_edit_request_body(image_bytes, filename, content_type)
    request = urllib.request.Request(
        "https://api.openai.com/v1/images/edits",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
        encoded_image = payload["data"][0]["b64_json"]
        processed_logo = base64.b64decode(encoded_image, validate=True)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise LogoProcessingError("AI could not prepare this logo. Please try again.") from exc

    if not processed_logo or len(processed_logo) > 12_000_000:
        raise LogoProcessingError("AI returned an invalid logo image. Please try again.")
    return processed_logo, "png"


def _image_edit_request_body(image_bytes, filename, content_type):
    """Build a multipart body without adding another HTTP client dependency."""
    boundary = f"----EduConnectLogo{uuid4().hex}"
    text_fields = {
        "model": getattr(settings, "OPENAI_LOGO_PROCESSING_MODEL", "gpt-image-2"),
        "prompt": (
            "Preserve the supplied institution logo exactly: do not alter its text, colours, "
            "symbols, proportions, or layout. Remove every background element and return only "
            "the clean logo, centred with comfortable transparent padding for use in a website header."
        ),
        "background": "transparent",
        "output_format": "png",
        "size": "1024x1024",
        "quality": "medium",
    }
    chunks = []
    for name, value in text_fields.items():
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ))
    chunks.extend((
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="image[]"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        image_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ))
    return b"".join(chunks), boundary


def _safe_upload_filename(filename):
    filename = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    return filename.replace('"', "").replace("\r", "").replace("\n", "") or "institution-logo.png"


def _logo_content_type(filename, content_type):
    if content_type in {"image/jpeg", "image/png", "image/webp"}:
        return content_type
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class _LogoCandidateParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.candidates = []

    def handle_starttag(self, tag, attrs):
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag == "img" and attributes.get("src"):
            signal = " ".join(
                attributes.get(key, "") for key in ("class", "id", "alt", "src")
            ).lower()
            if any(word in signal for word in ("logo", "brand", "crest", "seal", "emblem")):
                self.candidates.append((0, attributes["src"]))
        elif tag == "link" and attributes.get("href"):
            if "icon" in attributes.get("rel", "").lower():
                self.candidates.append((1, attributes["href"]))
        elif tag == "meta" and attributes.get("content"):
            if attributes.get("property", "").lower() == "og:image":
                self.candidates.append((2, attributes["content"]))


def get_institution_logo(institution_name):
    """Find an official public logo and return ``(image_bytes, extension)``."""
    api_key = getattr(settings, "OPENAI_API_KEY", "")
    if not api_key:
        raise LogoLookupError("Set OPENAI_API_KEY to look up the official institution logo automatically.")

    website_url = _find_official_website(institution_name, api_key)
    for candidate_url in _homepage_logo_candidates(website_url):
        image = _download_image(candidate_url)
        if image:
            return image
    raise LogoLookupError("No official logo image was found on the institution's public website.")


def _find_official_website(institution_name, api_key):
    prompt = (
        f"Find the official public website for the university or institution named {institution_name!r}. "
        "Use web search. Return only the verified official homepage URL, beginning with https://. "
        "Return an empty response if it cannot be verified. Do not use social media, directories, "
        "Wikipedia, or third-party listing sites."
    )
    payload = json.dumps({
        "model": getattr(settings, "OPENAI_LOGO_LOOKUP_MODEL", "gpt-5"),
        "tools": [{"type": "web_search"}],
        "input": prompt,
        "store": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
        content = result.get("output_text") or _output_text(result.get("output", []))
        match = re.search(r"https?://[^\s<>()\[\]{}\"']+", content)
        website_url = match.group(0).rstrip(".,;:") if match else ""
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise LogoLookupError("The official institution website could not be verified.") from exc
    if not _is_public_http_url(website_url):
        raise LogoLookupError("The official institution website could not be verified.")
    return website_url


def _output_text(output):
    return "".join(
        content.get("text", "")
        for item in output
        if item.get("type") == "message"
        for content in item.get("content", [])
        if content.get("type") == "output_text"
    )


def _homepage_logo_candidates(website_url):
    html, final_url = _download_text(website_url)
    parser = _LogoCandidateParser()
    parser.feed(html)
    candidates = [urllib.parse.urljoin(final_url, source) for _, source in sorted(parser.candidates)]
    candidates.append(urllib.parse.urljoin(final_url, "/favicon.ico"))
    return list(dict.fromkeys(url for url in candidates if _is_public_http_url(url)))


def _download_text(url):
    response = _safe_open(url, timeout=25)
    content_type = response.headers.get_content_type()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise LogoLookupError("The official institution website did not return a webpage.")
    return (
        response.read(2_000_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace"),
        response.url,
    )


def _download_image(url):
    try:
        response = _safe_open(url, timeout=25)
        content_type = response.headers.get_content_type()
        is_favicon = urllib.parse.urlparse(url).path.lower().endswith(".ico")
        if not content_type.startswith("image/") and not (is_favicon and content_type == "application/octet-stream"):
            return None
        image = response.read(8_000_001)
        if not image or len(image) > 8_000_000:
            return None
        extension = {
            "image/jpeg": "jpg", "image/png": "png", "image/svg+xml": "svg",
            "image/webp": "webp", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico",
        }.get(content_type, "png")
        return image, extension
    except (LogoLookupError, UnicodeError, urllib.error.URLError):
        return None


def _safe_open(url, timeout, redirect_count=0):
    if not _is_public_http_url(url):
        raise LogoLookupError("An unsafe logo source was rejected.")
    request = urllib.request.Request(url, headers={"User-Agent": "EduConnect institution-logo lookup/1.0"})
    try:
        return urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        redirect_url = exc.headers.get("Location")
        if exc.code in {301, 302, 303, 307, 308} and redirect_url and redirect_count < 3:
            return _safe_open(urllib.parse.urljoin(url, redirect_url), timeout, redirect_count + 1)
        raise LogoLookupError("The official logo source could not be opened.") from exc


def _is_public_http_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False
