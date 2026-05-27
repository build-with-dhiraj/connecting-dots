"""Instagram handler — auth-free best-effort OG/oEmbed extraction.

Instagram aggressively blocks anonymous scrapers; we deliberately avoid
headless browsers and logged-in scraping. The extraction ladder is:

1. Try Instagram's public oEmbed endpoint. Frequently 401s now but cheap
   to try and returns clean title/author/thumbnail when it works.
2. GET the URL with a desktop User-Agent and parse `<meta property="og:*">`.
3. If both fail, return a degraded NoteRecord so the user still has the
   URL saved (and a clear `raw_meta.extraction_failed=True` flag).

Tracking params (`igshid`, `utm_*`) are stripped from the stored URL so
two saves of the same post don't appear as different captures.

Security posture (mirrors `web.py`):
- Scheme allowlist in `matches()` — `mailto:` / `file://` / `javascript:`
  envelopes never reach the IG fetch path.
- SSRF guard around every outbound request: every host (`api.instagram.com`,
  the IG post URL, and every redirect hop) is resolved via
  `socket.getaddrinfo` and rejected if any returned address is private /
  loopback / link-local / cloud-metadata / reserved.
- Redirects are manually followed and re-checked, capped at 5 hops.
"""
from __future__ import annotations

import logging
import re
from typing import Final
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from connecting_dots.inbound_envelope import InboundEnvelope
from connecting_dots.handlers._safe_fetch import MAX_REDIRECTS as _MAX_REDIRECTS
from connecting_dots.handlers._safe_fetch import ssrf_check_url as _ssrf_check_url
from connecting_dots.types import NoteRecord

logger = logging.getLogger(__name__)

# Match /p/<id>, /reel/<id>, /reels/<id>, /tv/<id> on instagram.com (incl. www. and m.).
_IG_PATH_RE: Final = re.compile(r"^/(?:p|reel|reels|tv)/[^/]+/?", re.IGNORECASE)
_IG_HOSTS: Final = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})

_DESKTOP_UA: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_OEMBED_ENDPOINT: Final = "https://api.instagram.com/oembed"
_HTTP_TIMEOUT_S: Final = 10.0
_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})


def _strip_tracking(url: str) -> str:
    """Drop `igshid` and any `utm_*` params; preserve everything else."""
    parts = urlparse(url)
    if not parts.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "igshid" and not k.lower().startswith("utm_")
    ]
    new_query = urlencode(kept)
    return urlunparse(parts._replace(query=new_query))


def _safe_get(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
) -> httpx.Response | None:
    """Manual redirect chain with SSRF + scheme re-check at every hop.

    Returns the terminal Response (which may itself be 3xx if the chain
    ended without a `Location`) or None if blocked / errored / hop cap hit.
    """
    current = url
    if params:
        # Render params into the URL exactly once so subsequent hops just
        # follow Location headers verbatim.
        merged = httpx.QueryParams(params)
        parts = urlparse(current)
        existing = httpx.QueryParams(parts.query)
        for k, v in merged.items():
            existing = existing.set(k, v)
        current = urlunparse(parts._replace(query=str(existing)))

    for _hop in range(_MAX_REDIRECTS + 1):
        if not _ssrf_check_url(current):
            logger.info("ig fetch blocked (ssrf or bad scheme): %s", current)
            return None
        try:
            resp = client.get(current, headers=headers, follow_redirects=False)
        except httpx.HTTPError as exc:
            logger.debug("ig transport error on %s: %s", current, exc)
            return None
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("location")
            if not location:
                return resp
            current = urljoin(current, location)
            continue
        return resp

    logger.info("ig fetch exceeded redirect cap for %s", url)
    return None


def _try_oembed(client: httpx.Client, url: str) -> dict[str, str] | None:
    resp = _safe_get(client, _OEMBED_ENDPOINT, params={"url": url})
    if resp is None or resp.status_code != 200:
        if resp is not None:
            logger.debug("ig oembed non-200: %s", resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "title": str(data.get("title") or "").strip(),
        "author": str(data.get("author_name") or "").strip(),
        "thumbnail": str(data.get("thumbnail_url") or "").strip(),
        "type": "oembed",
    }


def _parse_og(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    og: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name")
        content = meta.get("content")
        if not prop or not content:
            continue
        prop = prop.lower()
        if prop.startswith("og:"):
            og[prop[3:]] = content.strip()
    return og


def _try_og_scrape(client: httpx.Client, url: str) -> dict[str, str] | None:
    resp = _safe_get(client, url, headers={"User-Agent": _DESKTOP_UA})
    if resp is None or resp.status_code != 200 or not resp.text:
        if resp is not None:
            logger.debug("ig og non-200: %s", resp.status_code)
        return None
    og = _parse_og(resp.text)
    if not og:
        return None
    og["final_url"] = str(resp.url)
    return og


class InstagramHandler:
    """Anonymous Instagram extractor. See module docstring for the ladder."""

    name = "instagram"

    def matches(self, url: str) -> bool:
        try:
            parts = urlparse(url)
        except (ValueError, TypeError):
            return False
        if parts.scheme not in _ALLOWED_SCHEMES:
            return False
        host = (parts.hostname or "").lower()
        if host not in _IG_HOSTS:
            return False
        return bool(_IG_PATH_RE.match(parts.path or ""))

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        original_url = str(envelope.url)
        cleaned_url = _strip_tracking(original_url)

        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=False) as client:
            oembed = _try_oembed(client, cleaned_url)
            og = None if oembed else _try_og_scrape(client, cleaned_url)

        if oembed and oembed.get("title"):
            return NoteRecord(
                source=envelope.source.value,
                handler=self.name,
                url=cleaned_url,
                title=oembed["title"],
                text=oembed["title"],  # IG oembed has no body — title is all we get
                captured_at=envelope.captured_at,
                raw_meta={
                    "extractor": "oembed",
                    "author": oembed.get("author", ""),
                    "thumbnail": oembed.get("thumbnail", ""),
                    "original_url": original_url,
                },
            )

        if og:
            title = og.get("title") or cleaned_url
            description = og.get("description", "")
            return NoteRecord(
                source=envelope.source.value,
                handler=self.name,
                url=cleaned_url,
                title=title,
                text=description,
                captured_at=envelope.captured_at,
                raw_meta={
                    "extractor": "og",
                    "og": og,
                    "original_url": original_url,
                },
            )

        # Both paths failed — return a degraded but still-valid record.
        logger.info("ig extraction degraded for %s (anonymous block likely)", cleaned_url)
        return NoteRecord(
            source=envelope.source.value,
            handler=self.name,
            url=cleaned_url,
            title=cleaned_url,
            text="",
            captured_at=envelope.captured_at,
            raw_meta={
                "extraction_failed": True,
                "reason": "instagram anonymous block — neither oembed nor og scrape returned usable data",
                "original_url": original_url,
            },
        )
