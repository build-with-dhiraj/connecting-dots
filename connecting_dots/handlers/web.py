"""Generic web fallback handler — always matches *http(s)* URLs.

This is the catch-all when no other handler claims a URL (i.e. not YouTube,
Instagram, LinkedIn, PDF). It streams the page with a 10 s timeout and a
5 MB byte cap, parses OpenGraph metadata for title/description/image/
site_name/type, and uses `trafilatura` to extract clean readable body text.

Security posture:
- **Scheme allowlist**: `matches()` only accepts `http(s)://` URLs with a
  host — `mailto:`, `ftp://`, `javascript:`, `data:`, `file://` are rejected
  outright so the dispatcher's fallback can't be used as an exfiltration
  primitive.
- **SSRF guard**: every host (initial + each redirect hop) is resolved via
  `socket.getaddrinfo` and any address falling in private / loopback /
  link-local / metadata / unspecified ranges is rejected. `httpx`'s built-in
  redirect follower is disabled so we re-check each hop ourselves and cap
  the chain at 5.
- **Response size cap**: 5 MB streamed-byte counter aborts oversized
  responses before trafilatura/BeautifulSoup ever see them.

PDFs are out of scope (component #5 owns them) but the handler degrades
gracefully if it sees a `.pdf` URL.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Final
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

from connecting_dots.generated.inbound_envelope import InboundEnvelope
from connecting_dots.types import NoteRecord

logger = logging.getLogger(__name__)

_DESKTOP_UA: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT_S: Final = 10.0
_OG_KEYS: Final = ("title", "description", "image", "site_name", "type")
_MAX_RESPONSE_BYTES: Final = 5 * 1024 * 1024  # 5 MB
_MAX_REDIRECTS: Final = 5
_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})

_BINARY_CT_PREFIXES: Final = (
    "image/",
    "video/",
    "audio/",
    "font/",
    "application/octet-stream",
    "application/zip",
    "application/x-tar",
    "application/x-gzip",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument",
    "application/msword",
)


def _is_blocked_ip(ip_str: str) -> bool:
    """True for any IP we never want to fetch from.

    Covers RFC1918 private space, loopback, link-local (incl. cloud-metadata
    169.254.169.254), CGNAT, the IPv4 "this network" range, IPv6 loopback /
    unique-local / link-local, and anything otherwise reserved or
    unspecified.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # un-parseable → treat as hostile
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _ssrf_safe_host(host: str) -> bool:
    """Resolve `host` and ensure every returned address is publicly routable."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        logger.info("ssrf guard: dns resolution failed for %s: %s", host, exc)
        return False
    if not infos:
        return False
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            logger.warning("ssrf guard: blocked %s -> %s", host, ip)
            return False
    return True


def _is_binary_content_type(content_type: str) -> bool:
    """True for content types we should not feed to the HTML extractor."""
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return False
    return any(ct.startswith(prefix) for prefix in _BINARY_CT_PREFIXES)


def _parse_metadata(html: str) -> dict[str, str]:
    """Pull og:* tags + <title> from raw HTML. Missing fields are simply absent."""
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        prop = tag.get("property") or tag.get("name")
        content = tag.get("content")
        if not prop or not content:
            continue
        prop = prop.lower()
        if prop.startswith("og:"):
            key = prop[3:]
            if key in _OG_KEYS:
                meta[key] = content.strip()
    if "title" not in meta and soup.title and soup.title.string:
        meta["title"] = soup.title.string.strip()
    return meta


def _is_pdf(url: str, content_type: str | None) -> bool:
    if url.lower().endswith(".pdf"):
        return True
    if content_type and "application/pdf" in content_type.lower():
        return True
    return False


def _degraded(envelope: InboundEnvelope, url: str, reason: str) -> NoteRecord:
    return NoteRecord(
        source=envelope.source.value,
        handler="web",
        url=url,
        title=url,
        text="",
        captured_at=envelope.captured_at,
        raw_meta={"extraction_failed": True, "reason": reason, "original_url": str(envelope.url)},
    )


def _pdf_degraded(envelope: InboundEnvelope, url: str) -> NoteRecord:
    """Light-touch PDF placeholder until component #5 PDF handler ships."""
    parsed = urlparse(url)
    filename = parsed.path.rsplit("/", 1)[-1] or url
    return NoteRecord(
        source=envelope.source.value,
        handler="web",
        url=url,
        title=filename,
        text="",
        captured_at=envelope.captured_at,
        raw_meta={
            "extraction_failed": True,
            "reason": "pdf — defer to component #5 PDF handler",
            "content_type": "application/pdf",
            "original_url": str(envelope.url),
        },
    )


def _fetch_with_ssrf_and_size_cap(
    client: httpx.Client, url: str
) -> tuple[httpx.Response | None, str, str | None]:
    """Follow up to 5 redirects, SSRF-checking each hop, capping body at 5 MB.

    Returns `(response_or_None, final_url, error_reason)`. On rejection the
    response is None and `error_reason` carries the degraded-record reason.
    """
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
            return None, current, f"unsupported scheme: {parsed.scheme!r}"
        if not _ssrf_safe_host(parsed.hostname):
            return None, current, "ssrf blocked: non-public address"

        try:
            with client.stream(
                "GET",
                current,
                headers={"User-Agent": _DESKTOP_UA},
                follow_redirects=False,
            ) as stream:
                if 300 <= stream.status_code < 400:
                    location = stream.headers.get("location")
                    if not location:
                        stream.read()
                        return stream, current, None
                    current = urljoin(current, location)
                    continue

                total = 0
                chunks: list[bytes] = []
                for chunk in stream.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        return (
                            None,
                            str(stream.url),
                            f"response exceeded {_MAX_RESPONSE_BYTES} bytes",
                        )
                    chunks.append(chunk)
                stream._content = b"".join(chunks)  # noqa: SLF001 — httpx exposes no public setter
                return stream, str(stream.url), None
        except httpx.HTTPError as exc:
            return None, current, f"http error: {exc.__class__.__name__}"

    return None, current, f"too many redirects (>{_MAX_REDIRECTS})"


class WebHandler:
    """Fallback handler. `matches()` returns True only for routable http(s)
    URLs; order in the dispatcher's registry must place this LAST."""

    name = "web"

    def matches(self, url: str) -> bool:
        try:
            parts = urlparse(url)
        except (ValueError, TypeError):
            return False
        if parts.scheme not in _ALLOWED_SCHEMES:
            return False
        if not parts.hostname:
            return False
        return True

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        original_url = str(envelope.url)

        parsed = urlparse(original_url)
        if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
            return _degraded(envelope, original_url, f"unsupported scheme: {parsed.scheme!r}")

        if original_url.lower().endswith(".pdf"):
            return _pdf_degraded(envelope, original_url)

        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=False) as client:
                resp, final_url, err = _fetch_with_ssrf_and_size_cap(client, original_url)
        except httpx.HTTPError as exc:
            logger.info("web fetch failed for %s: %s", original_url, exc)
            return _degraded(envelope, original_url, f"http error: {exc.__class__.__name__}")

        if err is not None or resp is None:
            return _degraded(envelope, final_url, err or "no response")

        content_type = resp.headers.get("content-type")

        if _is_pdf(final_url, content_type):
            return _pdf_degraded(envelope, final_url)

        if resp.status_code >= 400:
            return _degraded(envelope, final_url, f"status {resp.status_code}")

        # Block obvious binary / non-text content types so we don't feed bytes
        # to BeautifulSoup. text/plain passes through — many mock servers and
        # some real sites use it, and trafilatura tolerates it. Empty
        # content-type also passes through.
        if content_type and _is_binary_content_type(content_type):
            return _degraded(envelope, final_url, f"non-html content-type: {content_type}")

        if not resp.text:
            return _degraded(envelope, final_url, f"status {resp.status_code}")

        og = _parse_metadata(resp.text)
        body = trafilatura.extract(resp.text, url=final_url, favor_recall=True) or ""

        title = og.get("title") or final_url
        return NoteRecord(
            source=envelope.source.value,
            handler=self.name,
            url=final_url,
            title=title,
            text=body,
            captured_at=envelope.captured_at,
            raw_meta={
                "extractor": "trafilatura+og",
                "og": og,
                "final_url": final_url,
                "original_url": original_url,
                "status_code": resp.status_code,
            },
        )
