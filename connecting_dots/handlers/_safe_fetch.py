"""Shared SSRF / redirect / size-cap guards for outbound HTTP fetches.

Every handler that pulls live content from the internet (web, instagram,
linkedin) MUST go through this module rather than calling `httpx` /
`urllib.request.urlopen` directly. The contract is identical across
handlers so the threat surface stays uniform:

- **Scheme allowlist**: only `http(s)://` URLs with a host pass; `file://`,
  `ftp://`, `javascript:`, `data:`, `mailto:` are rejected outright.
- **SSRF guard**: every host (initial + each redirect hop) is resolved via
  `socket.getaddrinfo`. Any returned IP that falls in private / loopback /
  link-local (incl. cloud-metadata 169.254.169.254) / CGNAT / multicast /
  reserved / unspecified ranges aborts the request.
- **Manual redirect chain**: `httpx` redirect following is disabled so the
  guard re-runs at every hop. Chain is capped at 5 redirects.
- **Response size cap**: 5 MB streamed-byte counter aborts oversized
  responses before the parser ever sees them.

The module exposes three primitives:
- `is_blocked_ip(ip_str)` — classify a single IP literal.
- `ssrf_safe_host(host)` — resolve `host` and return True iff every
  resolved address is publicly routable.
- `ssrf_check_url(url)` — combined scheme + host check (used before
  individual requests where you don't need the size-cap streamer).
- `fetch_with_guards(client, url, headers=None)` — perform a streamed GET
  with the full redirect-chain + size-cap dance. Returns
  `(response | None, final_url, error_reason | None)`.

Constants `MAX_REDIRECTS`, `MAX_RESPONSE_BYTES`, `ALLOWED_SCHEMES` are
exported so callers can reference the same limits in their error messages.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Final
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
MAX_REDIRECTS: Final[int] = 5
MAX_RESPONSE_BYTES: Final[int] = 5 * 1024 * 1024  # 5 MB


def is_blocked_ip(ip_str: str) -> bool:
    """True for any IP we never want to fetch from.

    Covers RFC1918 private space, loopback, link-local (incl. cloud-metadata
    169.254.169.254), CGNAT, the IPv4 "this network" range, IPv6 loopback /
    unique-local / link-local, and anything otherwise reserved, multicast,
    or unspecified.
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


def ssrf_safe_host(host: str) -> bool:
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
        if is_blocked_ip(ip):
            logger.warning("ssrf guard: blocked %s -> %s", host, ip)
            return False
    return True


def ssrf_check_url(url: str) -> bool:
    """True iff `url` is http(s), has a host, and the host resolves to a
    publicly-routable address. Use this before a one-shot request when you
    don't need streaming/size-cap behavior."""
    try:
        parts = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        return False
    return ssrf_safe_host(parts.hostname)


def fetch_with_guards(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response | None, str, str | None]:
    """Follow up to MAX_REDIRECTS, SSRF-checking each hop, capping body at MAX_RESPONSE_BYTES.

    Returns `(response_or_None, final_url, error_reason)`. On rejection the
    response is None and `error_reason` carries a short, human-readable
    reason suitable for `NoteRecord.raw_meta.reason`.
    """
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            parsed = urlparse(current)
        except (ValueError, TypeError):
            return None, current, "malformed url"
        if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
            return None, current, f"unsupported scheme: {parsed.scheme!r}"
        if not ssrf_safe_host(parsed.hostname):
            return None, current, "ssrf blocked: non-public address"

        try:
            with client.stream(
                "GET",
                current,
                headers=headers,
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
                    if total > MAX_RESPONSE_BYTES:
                        return (
                            None,
                            str(stream.url),
                            f"response exceeded {MAX_RESPONSE_BYTES} bytes",
                        )
                    chunks.append(chunk)
                stream._content = b"".join(chunks)  # noqa: SLF001 — httpx exposes no public setter
                return stream, str(stream.url), None
        except httpx.HTTPError as exc:
            return None, current, f"http error: {exc.__class__.__name__}"

    return None, current, f"too many redirects (>{MAX_REDIRECTS})"


__all__ = (
    "ALLOWED_SCHEMES",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "is_blocked_ip",
    "ssrf_safe_host",
    "ssrf_check_url",
    "fetch_with_guards",
)
