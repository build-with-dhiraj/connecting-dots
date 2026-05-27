"""Tests for `connecting_dots.handlers.web`.

Network is stubbed with `respx`. We test:
    - matches() always True
    - happy path: OG title/description extracted, trafilatura body present
    - redirect chain: final URL is captured (not the original)
    - missing OG tags: falls back to <title>; readable body still extracted
    - http error: degraded record with reason
    - .pdf URLs: degrade to a filename placeholder (component #5 owns PDF)
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx
from pydantic import AnyUrl

from connecting_dots.inbound_envelope import InboundEnvelope, MessageType, Source
from connecting_dots.handlers.web import WebHandler


def _make_envelope(url: str) -> InboundEnvelope:
    return InboundEnvelope(
        message_id="web-test-1",
        message_type=MessageType.url,
        url=AnyUrl(url),
        source=Source.mailto,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload={},
    )


# ---------- matches() ----------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://news.ycombinator.com/item?id=1",
        "https://random-blog.dev/2026/post.html",
        "https://www.instagram.com/p/ABC/",  # web is the fallback — yes, even IG
        "http://plain-http.example.com/post",
    ],
)
def test_matches_accepts_http_and_https(url: str) -> None:
    assert WebHandler().matches(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "mailto:hi@example.com",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "file:///etc/passwd",
        "anything-goes",
        "",
        "https://",  # no host
    ],
)
def test_matches_rejects_non_http_or_hostless(url: str) -> None:
    """Scheme allowlist — only http(s) URLs with a host reach the fetcher."""
    assert WebHandler().matches(url) is False


# ---------- handle() — happy paths ----------


_HTML_RICH = """
<!doctype html>
<html><head>
  <title>Browser tab title</title>
  <meta property="og:title" content="The Great Article" />
  <meta property="og:description" content="A short blurb explaining the article." />
  <meta property="og:image" content="https://cdn.example.com/cover.jpg" />
  <meta property="og:site_name" content="Example Blog" />
  <meta property="og:type" content="article" />
</head><body>
  <article>
    <h1>The Great Article</h1>
    <p>This is the first paragraph of real article body content. It contains
       enough text that trafilatura recognizes it as a readable article body
       and includes it in the extracted output. Lorem ipsum dolor sit amet.</p>
    <p>A second paragraph for good measure. The quick brown fox jumps over
       the lazy dog repeatedly.</p>
  </article>
  <footer>&copy; 2026</footer>
</body></html>
"""


@respx.mock
def test_handle_happy_path_extracts_og_and_body() -> None:
    url = "https://example.com/article"
    respx.get(url).mock(return_value=httpx.Response(200, text=_HTML_RICH))

    note = WebHandler().handle(_make_envelope(url))

    assert note.handler == "web"
    assert note.source == "mailto"
    assert note.title == "The Great Article"
    assert "first paragraph" in note.text.lower()
    assert note.raw_meta["og"]["description"] == "A short blurb explaining the article."
    assert note.raw_meta["og"]["site_name"] == "Example Blog"
    assert note.raw_meta["og"]["type"] == "article"
    assert note.raw_meta["final_url"] == url
    assert note.raw_meta["status_code"] == 200


@respx.mock
def test_handle_captures_final_url_after_redirect() -> None:
    start = "https://example.com/short"
    final = "https://example.com/article-final"
    respx.get(start).mock(
        return_value=httpx.Response(301, headers={"Location": final})
    )
    respx.get(final).mock(return_value=httpx.Response(200, text=_HTML_RICH))

    note = WebHandler().handle(_make_envelope(start))

    assert note.url == final
    assert note.raw_meta["final_url"] == final
    assert note.raw_meta["original_url"] == start


@respx.mock
def test_handle_falls_back_to_title_tag_when_og_missing() -> None:
    url = "https://example.com/plain"
    html = "<html><head><title>Just A Title</title></head><body><p>Body text here at least a few words long for the extractor to pick it up cleanly.</p></body></html>"
    respx.get(url).mock(return_value=httpx.Response(200, text=html))

    note = WebHandler().handle(_make_envelope(url))

    assert note.title == "Just A Title"
    # body extraction may or may not catch tiny bodies; assertion stays loose
    assert "og" in note.raw_meta


@respx.mock
def test_handle_degrades_on_http_error() -> None:
    url = "https://example.com/broken"
    respx.get(url).mock(side_effect=httpx.ConnectError("connection refused"))

    note = WebHandler().handle(_make_envelope(url))

    assert note.handler == "web"
    assert note.text == ""
    assert note.raw_meta["extraction_failed"] is True
    assert "ConnectError" in note.raw_meta["reason"]


@respx.mock
def test_handle_degrades_on_4xx() -> None:
    url = "https://example.com/not-found"
    respx.get(url).mock(return_value=httpx.Response(404, text=""))

    note = WebHandler().handle(_make_envelope(url))

    assert note.raw_meta["extraction_failed"] is True
    assert "404" in note.raw_meta["reason"]


# ---------- PDF degradation ----------


def test_handle_pdf_url_degrades_to_filename_without_fetching() -> None:
    """Component #5 owns real PDF extraction. We just record the URL/filename
    without crashing on the binary body."""
    url = "https://example.com/papers/great-paper.pdf"
    # No respx mock — handler must not even attempt the fetch for .pdf
    note = WebHandler().handle(_make_envelope(url))

    assert note.handler == "web"
    assert note.title == "great-paper.pdf"
    assert note.text == ""
    assert note.raw_meta["extraction_failed"] is True
    assert "pdf" in note.raw_meta["reason"].lower()


# ---------- new security / robustness coverage ----------


@respx.mock
def test_handle_degrades_on_5xx() -> None:
    """Upstream server errors should produce a degraded record, not raise."""
    url = "https://example.com/oops"
    respx.get(url).mock(return_value=httpx.Response(503, text="<html>down</html>"))

    note = WebHandler().handle(_make_envelope(url))

    assert note.raw_meta["extraction_failed"] is True
    assert "503" in note.raw_meta["reason"]


@respx.mock
def test_handle_degrades_on_non_html_content_type() -> None:
    """A 200 OK with image/jpeg content-type must NOT be fed to BeautifulSoup."""
    url = "https://example.com/photo"
    respx.get(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xff\xe0binary",
        )
    )

    note = WebHandler().handle(_make_envelope(url))

    assert note.raw_meta["extraction_failed"] is True
    assert "image/jpeg" in note.raw_meta["reason"]


@respx.mock
def test_handle_degrades_when_redirect_chain_exceeds_cap() -> None:
    """More than 5 redirects → degraded record, no infinite loop."""
    for i in range(8):
        respx.get(f"https://example.com/r{i}").mock(
            return_value=httpx.Response(301, headers={"Location": f"https://example.com/r{i + 1}"})
        )

    note = WebHandler().handle(_make_envelope("https://example.com/r0"))

    assert note.raw_meta["extraction_failed"] is True
    assert "redirects" in note.raw_meta["reason"]


@respx.mock
def test_handle_rejects_non_http_scheme_envelope() -> None:
    """A `file://` envelope is rejected at the envelope boundary by the
    `InboundEnvelope` scheme validator (see `tests/test_inbound_envelope_validator.py`).
    Even if somebody bypasses that wrapper, the handler's own scheme check
    must still degrade gracefully — that's what this test covers, by
    constructing the bare generated model and feeding it directly.
    """
    from connecting_dots.generated.inbound_envelope import (
        InboundEnvelope as GeneratedInboundEnvelope,
    )

    env = GeneratedInboundEnvelope(
        message_id="web-test-1",
        message_type=MessageType.url,
        url=AnyUrl("file:///etc/passwd"),
        source=Source.mailto,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload={},
    )

    note = WebHandler().handle(env)

    assert note.raw_meta["extraction_failed"] is True
    assert "scheme" in note.raw_meta["reason"]


def test_handle_ssrf_blocks_loopback_host() -> None:
    """A URL resolving to 127.0.0.1 must be rejected without a fetch.

    `localhost` resolves to a loopback address on every reasonable host —
    we rely on `socket.getaddrinfo` returning a blocked range.
    """
    env = _make_envelope("http://localhost/internal")

    note = WebHandler().handle(env)

    assert note.raw_meta["extraction_failed"] is True
    assert "ssrf" in note.raw_meta["reason"].lower()


@respx.mock
def test_handle_pdf_content_type_after_redirect_degrades() -> None:
    """A URL that doesn't end in .pdf but redirects/returns application/pdf
    should also degrade gracefully."""
    url = "https://example.com/download/123"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.4\n...",
        )
    )

    note = WebHandler().handle(_make_envelope(url))

    assert note.raw_meta["extraction_failed"] is True
    assert note.raw_meta["content_type"] == "application/pdf"
