"""Generic web fallback handler — always matches.

This is the catch-all when no other handler claims a URL (i.e. not YouTube,
Instagram, LinkedIn, PDF). It fetches the page with a 10 s timeout, parses
OpenGraph metadata for title/description/image/site_name/type, and uses
`trafilatura` to extract clean readable body text.

Library choice: trafilatura over defuddle. trafilatura is pure-Python, the
de-facto choice for readable-text extraction in Python ETL pipelines, and
plays cleanly with our httpx-based fetch (we control UA, timeouts, redirect
policy). defuddle is a Node-based CLI — invoking it per-URL would add a
subprocess boundary and a Node runtime dependency to the Python worker.

PDFs are out of scope (component #5 owns them) but the handler degrades
gracefully if it sees a `.pdf` URL: it sets a degraded record with the
filename as title rather than crashing on binary content.
"""
from __future__ import annotations

import logging
from typing import Final
from urllib.parse import urlparse

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
    # Fallback <title> if og:title missing.
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


class WebHandler:
    """Fallback handler. `matches()` always returns True — order in the
    dispatcher's registry must place this LAST."""

    name = "web"

    def matches(self, url: str) -> bool:  # noqa: ARG002 — fallback signature
        return True

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        original_url = str(envelope.url)

        # Cheap pre-check — skip the fetch for .pdf URLs entirely.
        if original_url.lower().endswith(".pdf"):
            return _pdf_degraded(envelope, original_url)

        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
                resp = client.get(original_url, headers={"User-Agent": _DESKTOP_UA})
        except httpx.HTTPError as exc:
            logger.info("web fetch failed for %s: %s", original_url, exc)
            return _degraded(envelope, original_url, f"http error: {exc.__class__.__name__}")

        final_url = str(resp.url)
        content_type = resp.headers.get("content-type")

        if _is_pdf(final_url, content_type):
            return _pdf_degraded(envelope, final_url)

        if resp.status_code >= 400 or not resp.text:
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
