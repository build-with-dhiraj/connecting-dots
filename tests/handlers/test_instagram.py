"""Tests for `connecting_dots.handlers.instagram`.

Network is stubbed with `respx` — we never hit live Instagram. The handler
has three branches we exercise:
    1. oEmbed succeeds → record built from oembed JSON
    2. oEmbed fails, OG scrape succeeds → record built from og:* tags
    3. Both fail → degraded record with extraction_failed=True
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx
from pydantic import AnyUrl

from connecting_dots.generated.inbound_envelope import InboundEnvelope, Source
from connecting_dots.handlers.instagram import InstagramHandler, _strip_tracking


def _make_envelope(url: str) -> InboundEnvelope:
    return InboundEnvelope(
        message_id="ig-test-1",
        url=AnyUrl(url),
        source=Source.whatsapp,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload={},
    )


# ---------- matches() ----------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/ABC123def/",
        "https://instagram.com/p/ABC123def",
        "https://www.instagram.com/reel/ABC123def/",
        "https://www.instagram.com/reels/ABC123def/",
        "https://www.instagram.com/tv/ABC123def/",
        "https://m.instagram.com/p/ABC123def/",
        "https://www.instagram.com/p/ABC123def/?igshid=xyz",
    ],
)
def test_matches_accepts_ig_post_types(url: str) -> None:
    assert InstagramHandler().matches(url), f"should match: {url}"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/some_user/",  # profile, not a post
        "https://www.instagram.com/",
        "https://instagram.com/explore/tags/cats/",
        "https://example.com/p/ABC123def/",  # wrong host
        "https://twitter.com/x/status/1",
        "not a url",
    ],
)
def test_matches_rejects_non_ig_posts(url: str) -> None:
    assert not InstagramHandler().matches(url), f"should not match: {url}"


# ---------- _strip_tracking() ----------


def test_strip_tracking_removes_igshid_and_utm() -> None:
    url = "https://www.instagram.com/p/ABC/?igshid=foo&utm_source=x&keep=yes"
    cleaned = _strip_tracking(url)
    assert "igshid" not in cleaned
    assert "utm_source" not in cleaned
    assert "keep=yes" in cleaned


def test_strip_tracking_is_noop_when_clean() -> None:
    url = "https://www.instagram.com/p/ABC/"
    assert _strip_tracking(url) == url


# ---------- handle() — branches ----------


@respx.mock
def test_handle_oembed_success() -> None:
    url = "https://www.instagram.com/p/ABC123def/"
    respx.get("https://api.instagram.com/oembed").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "A lovely sunset on the dock",
                "author_name": "naturepics",
                "thumbnail_url": "https://scontent.cdninstagram.com/thumb.jpg",
            },
        )
    )

    note = InstagramHandler().handle(_make_envelope(url))

    assert note.handler == "instagram"
    assert note.source == "whatsapp"
    assert note.title == "A lovely sunset on the dock"
    assert note.text == "A lovely sunset on the dock"
    assert note.raw_meta["extractor"] == "oembed"
    assert note.raw_meta["author"] == "naturepics"
    assert note.raw_meta.get("extraction_failed") is None


@respx.mock
def test_handle_falls_back_to_og_when_oembed_401() -> None:
    url = "https://www.instagram.com/reel/XYZ789ghi/"
    respx.get("https://api.instagram.com/oembed").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    html = """
    <html><head>
      <meta property="og:title" content="Reel by @cooluser" />
      <meta property="og:description" content="Watch the dog do a trick" />
      <meta property="og:image" content="https://ig.cdn/img.jpg" />
      <meta property="og:type" content="video.other" />
    </head><body></body></html>
    """
    respx.get(url).mock(return_value=httpx.Response(200, text=html))

    note = InstagramHandler().handle(_make_envelope(url))

    assert note.handler == "instagram"
    assert note.title == "Reel by @cooluser"
    assert note.text == "Watch the dog do a trick"
    assert note.raw_meta["extractor"] == "og"
    assert note.raw_meta["og"]["image"] == "https://ig.cdn/img.jpg"


@respx.mock
def test_handle_degrades_when_both_paths_fail() -> None:
    """The 'IG blocked anonymous traffic entirely' scenario."""
    url = "https://www.instagram.com/p/blockedpost/"
    respx.get("https://api.instagram.com/oembed").mock(
        return_value=httpx.Response(401)
    )
    respx.get(url).mock(return_value=httpx.Response(403, text=""))

    note = InstagramHandler().handle(_make_envelope(url))

    assert note.handler == "instagram"
    assert note.title == url
    assert note.text == ""
    assert note.raw_meta["extraction_failed"] is True
    assert "anonymous block" in note.raw_meta["reason"]


@respx.mock
def test_handle_strips_tracking_params_before_save() -> None:
    """The stored URL should be the cleaned version, not the original tracking URL."""
    dirty = "https://www.instagram.com/p/ABC/?igshid=foo&utm_source=x"
    clean = "https://www.instagram.com/p/ABC/"
    respx.get("https://api.instagram.com/oembed").mock(
        return_value=httpx.Response(200, json={"title": "Post"})
    )

    note = InstagramHandler().handle(_make_envelope(dirty))

    assert note.url == clean
    assert note.raw_meta["original_url"] == dirty


@respx.mock
def test_handle_degrades_when_og_tags_missing() -> None:
    """OG scrape returns 200 but the page has no og:* metadata — degrade gracefully.

    Tightened: locks down the full degraded-record contract so future
    regressions in the failed-record shape are caught.
    """
    url = "https://www.instagram.com/p/empty/"
    respx.get("https://api.instagram.com/oembed").mock(
        return_value=httpx.Response(401)
    )
    respx.get(url).mock(return_value=httpx.Response(200, text="<html><body>nope</body></html>"))

    note = InstagramHandler().handle(_make_envelope(url))

    assert note.handler == "instagram"
    assert note.url == url
    assert note.title == url, "degraded title falls back to the cleaned URL"
    assert note.text == "", "degraded record has empty text body"
    assert note.raw_meta["extraction_failed"] is True
    assert note.raw_meta["reason"] == (
        "instagram anonymous block — neither oembed nor og scrape returned usable data"
    )
    assert note.raw_meta["original_url"] == url
    # Should NOT carry an `extractor` key (only success paths do).
    assert "extractor" not in note.raw_meta


# ---------- new security / robustness coverage ----------


def test_ssrf_check_blocks_internal_and_bad_schemes() -> None:
    """The internal SSRF helper rejects loopback, RFC1918, cloud-metadata,
    link-local, and non-http schemes — exercised directly so we don't
    need to coerce the handler into using a non-IG host."""
    from connecting_dots.handlers.instagram import _ssrf_check_url

    assert _ssrf_check_url("http://127.0.0.1/x") is False
    assert _ssrf_check_url("http://169.254.169.254/latest/meta-data/") is False
    assert _ssrf_check_url("http://10.0.0.1/x") is False
    assert _ssrf_check_url("http://192.168.1.1/x") is False
    assert _ssrf_check_url("http://172.16.0.1/x") is False
    assert _ssrf_check_url("file:///etc/passwd") is False
    assert _ssrf_check_url("javascript:alert(1)") is False
    assert _ssrf_check_url("https://") is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "mailto:a@b.com",
        "ftp://www.instagram.com/p/ABC/",
    ],
)
def test_matches_rejects_non_http_schemes(url: str) -> None:
    assert InstagramHandler().matches(url) is False
