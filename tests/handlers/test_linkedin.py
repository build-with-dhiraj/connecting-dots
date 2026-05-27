"""Tests for `connecting_dots.handlers.linkedin._fetch_og` SSRF / size-cap guards.

These mirror the protection class added in `b22a40a` for `web.py` and
`instagram.py`. The LinkedIn handler now routes its live-fetch path
through `connecting_dots.handlers._safe_fetch.fetch_with_guards`, which:

- rejects URLs whose host resolves into private / loopback / link-local
  (cloud-metadata) / multicast / reserved / unspecified ranges,
- re-runs that SSRF check at every redirect hop (max 5),
- caps the response body at 5 MB before the parser sees it.

Network is stubbed with `respx`; we never hit the real LinkedIn.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx
from pydantic import AnyUrl

from connecting_dots.handlers.linkedin import LinkedInHandler
from connecting_dots.inbound_envelope import InboundEnvelope, MessageType, Source


def _make_envelope(url: str) -> InboundEnvelope:
    return InboundEnvelope(
        message_id="linkedin-test-1",
        message_type=MessageType.url,
        url=AnyUrl(url),
        source=Source.linkedin,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload={},
    )


def test_fetch_og_blocks_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    """A LinkedIn-shaped URL whose host resolves to a loopback IP must be
    blocked before any HTTP request. The handler should emit a degraded
    NoteRecord with `raw_meta.ssrf_blocked=True`."""
    # Pretend `www.linkedin.com` resolves to 127.0.0.1 — this is the only
    # way to test the SSRF guard without an actual private-DNS setup. We
    # patch `socket.getaddrinfo` in the shared safe_fetch module.
    import connecting_dots.handlers._safe_fetch as sf

    def fake_getaddrinfo(host: str, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        # Return a single AF_INET tuple shaped like real getaddrinfo output.
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(sf.socket, "getaddrinfo", fake_getaddrinfo)

    env = _make_envelope("https://www.linkedin.com/posts/abc_xyz")
    note = LinkedInHandler().handle(env)

    assert note.handler == "linkedin"
    assert note.raw_meta.get("ssrf_blocked") is True
    assert "ssrf" in str(note.raw_meta.get("fetch_error", "")).lower()
    # The degraded record still carries the original URL + a derived title.
    assert note.url == "https://www.linkedin.com/posts/abc_xyz"
    assert note.title  # non-empty fallback title


@respx.mock
def test_fetch_og_blocks_redirect_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public LinkedIn URL that 302s to a cloud-metadata IP must abort at
    the redirect hop. The metadata host is 169.254.169.254 — link-local."""
    import connecting_dots.handlers._safe_fetch as sf

    real_getaddrinfo = sf.socket.getaddrinfo

    def fake_getaddrinfo(host: str, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        # linkedin.com / www.linkedin.com → public-looking IP (8.8.8.8);
        # 169.254.169.254 → resolve to itself so the IP block triggers.
        if "linkedin.com" in host:
            return [(2, 1, 6, "", ("8.8.8.8", 0))]
        if host == "169.254.169.254":
            return [(2, 1, 6, "", ("169.254.169.254", 0))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(sf.socket, "getaddrinfo", fake_getaddrinfo)

    start_url = "https://www.linkedin.com/posts/abc"
    respx.get(start_url).mock(
        return_value=httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
        )
    )

    env = _make_envelope(start_url)
    note = LinkedInHandler().handle(env)

    assert note.raw_meta.get("ssrf_blocked") is True
    assert "ssrf" in str(note.raw_meta.get("fetch_error", "")).lower()


@respx.mock
def test_fetch_og_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response body larger than MAX_RESPONSE_BYTES (5 MB) must abort and
    surface `raw_meta.size_cap_exceeded=True`."""
    import connecting_dots.handlers._safe_fetch as sf

    def fake_getaddrinfo(host: str, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(sf.socket, "getaddrinfo", fake_getaddrinfo)

    url = "https://www.linkedin.com/pulse/huge-article"
    # 6 MB > 5 MB cap. We don't stream chunk-by-chunk in respx but the
    # streamer in fetch_with_guards still iterates in chunks and totals
    # them; the first read pulls everything so the size counter trips.
    oversized_body = b"<html>" + (b"x" * (6 * 1024 * 1024)) + b"</html>"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            content=oversized_body,
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )

    env = _make_envelope(url)
    note = LinkedInHandler().handle(env)

    assert note.raw_meta.get("size_cap_exceeded") is True
    assert "exceeded" in str(note.raw_meta.get("fetch_error", "")).lower()
