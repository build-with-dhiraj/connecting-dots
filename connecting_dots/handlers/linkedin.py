"""LinkedIn URL handler.

Owns three URL shapes LinkedIn emits:
- `https://www.linkedin.com/posts/<slug>` — feed posts (most common save target)
- `https://www.linkedin.com/pulse/<slug>` — long-form articles
- `https://www.linkedin.com/feed/update/urn:li:activity:<id>` — feed activity permalinks
- `https://www.linkedin.com/in/<handle>/recent-activity/...` — profile activity views

LinkedIn aggressively blocks anonymous scrapers (similar to Instagram). For v1
the live-fetch path is intentionally minimal: desktop UA, parse OpenGraph tags,
accept a degraded extraction. The privileged path is when the URL came from
the user's own LinkedIn data export (ZIP watcher in `workers/linkedin_zip_watcher.py`):
the envelope's `raw_payload` carries `linkedin_export=True` plus the article
title and (when present) the post body, so we can build a full `NoteRecord`
without touching the network at all.

Tracking params LinkedIn appends are stripped before storage so two captures
of the same post collapse to one canonical URL downstream.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from connecting_dots.generated.inbound_envelope import InboundEnvelope
from connecting_dots.types import NoteRecord

logger = logging.getLogger(__name__)


# --- URL utilities -----------------------------------------------------------

# LinkedIn tracking/analytics params. `trk`/`trackingId`/`lipi` are the
# ubiquitous ones; the rest show up on share links and email click-throughs.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "trk",
        "trackingId",
        "lipi",
        "midToken",
        "midSig",
        "trkEmail",
        "eid",
        "otpToken",
        "li_fat_id",
        "refId",
    }
)

# Paths this handler owns. Matched on a normalized host (linkedin.com without
# the leading www/m/de etc).
_LINKEDIN_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/posts/", re.IGNORECASE),
    re.compile(r"^/pulse/", re.IGNORECASE),
    re.compile(r"^/feed/update/", re.IGNORECASE),
    re.compile(r"^/in/[^/]+/recent-activity", re.IGNORECASE),
)


def _normalize_host(host: str) -> str:
    host = (host or "").lower()
    # Strip leading `www.` / `m.` / single-locale subdomains like `de.` `fr.`
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host


def _strip_tracking(url: str) -> str:
    """Drop LinkedIn analytics params. Preserves param order for the rest."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _TRACKING_PARAMS]
    new_query = urlencode(kept)
    return urlunparse(parts._replace(query=new_query, fragment=""))


# --- OpenGraph parsing (degraded extraction fallback) ------------------------


class _OGParser(HTMLParser):
    """Pull OpenGraph + <title> from a snippet of HTML.

    Deliberately tiny: LinkedIn blocks anonymous full-page fetches, and even
    when the bot lands on a partial response the OG meta tags are usually
    present in the first few KB.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og: dict[str, str] = {}
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            prop = attr_map.get("property") or attr_map.get("name") or ""
            content = attr_map.get("content", "")
            if prop.startswith("og:") and content:
                self.og[prop] = content
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()


def _parse_og(html: str) -> tuple[str, str]:
    """Return (title, description) from an HTML snippet. Best-effort."""
    parser = _OGParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — HTMLParser can raise on adversarial markup
        pass
    title = parser.og.get("og:title") or parser.title or ""
    description = parser.og.get("og:description") or ""
    return title.strip(), description.strip()


# --- live fetch (intentionally minimal) --------------------------------------

_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _fetch_og(url: str, *, timeout: float = 8.0) -> tuple[str, str]:
    """Fetch a LinkedIn URL and return (title, description) from OG tags.

    Returns ("", "") on any failure — the caller degrades gracefully. Uses
    stdlib `urllib` so we don't add a runtime dependency for the rare path.
    """
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    req = Request(url, headers={"User-Agent": _DESKTOP_UA, "Accept": "text/html,*/*"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — http(s) only, see matches()
            charset = resp.headers.get_content_charset() or "utf-8"
            # 256 KB is enough for OG tags + falls back gracefully on huge pages.
            body = resp.read(256 * 1024).decode(charset, errors="replace")
    except (URLError, TimeoutError, ValueError) as exc:
        logger.info("[linkedin] live fetch failed for %s: %s", url, exc)
        return "", ""
    except Exception as exc:  # noqa: BLE001 — never let extraction kill dispatch
        logger.warning("[linkedin] unexpected fetch error for %s: %s", url, exc)
        return "", ""
    return _parse_og(body)


# --- handler -----------------------------------------------------------------


class LinkedInHandler:
    """Per-URL handler for LinkedIn posts/articles/feed updates."""

    name = "linkedin"

    def matches(self, url: str) -> bool:
        try:
            parts = urlparse(url)
        except ValueError:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        host = _normalize_host(parts.hostname or "")
        if host != "linkedin.com":
            return False
        path = parts.path or "/"
        return any(p.search(path) for p in _LINKEDIN_PATH_RES)

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        raw = dict(envelope.raw_payload or {})
        url = _strip_tracking(str(envelope.url))
        captured_at = envelope.captured_at or datetime.now(timezone.utc)

        # Privileged path: the LinkedIn ZIP export already gave us title + body.
        if raw.get("linkedin_export") is True:
            title = (raw.get("title") or "").strip()
            body = (raw.get("body") or "").strip()
            author = (raw.get("author") or "").strip()
            export_type = str(raw.get("type") or "saved")
            if not title:
                title = self._derive_title_from_url(url)
            return NoteRecord(
                source=str(envelope.source.value),
                handler=self.name,
                url=url,
                title=title,
                text=body,
                captured_at=captured_at,
                raw_meta={
                    "linkedin": {
                        "via": "export",
                        "type": export_type,
                        "author": author,
                    }
                },
            )

        # Live-fetch fallback. Accepts a degraded extraction silently.
        title, description = _fetch_og(url)
        if not title:
            title = self._derive_title_from_url(url)
        return NoteRecord(
            source=str(envelope.source.value),
            handler=self.name,
            url=url,
            title=title,
            text=description,
            captured_at=captured_at,
            raw_meta={"linkedin": {"via": "og-fallback", "degraded": not description}},
        )

    @staticmethod
    def _derive_title_from_url(url: str) -> str:
        try:
            parts = urlparse(url)
        except ValueError:
            return url
        slug = (parts.path or "").rstrip("/").rsplit("/", 1)[-1]
        slug = slug.replace("-", " ").replace("_", " ").strip()
        return slug or url


# Module-level singleton expected by `connecting_dots.dispatcher`'s handler registry.
linkedin_handler = LinkedInHandler()


def matches(url: str) -> bool:
    return linkedin_handler.matches(url)


def handle(envelope: InboundEnvelope) -> NoteRecord:
    return linkedin_handler.handle(envelope)


__all__: Iterable[str] = ("LinkedInHandler", "linkedin_handler", "matches", "handle")
