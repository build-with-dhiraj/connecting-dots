"""Coverage tests for `workers.mailto_poller`.

Pure-Python coverage of URL extraction, MIME body decoding, and the IMAP poll
loop with a stub `imaplib.IMAP4_SSL`. No live IMAP, no live dispatch — both are
patched. Tests must be deterministic and < 1s each.
"""
from __future__ import annotations

import base64
import email
from email.message import EmailMessage
from typing import Any
from unittest import mock

import pytest

from workers import mailto_poller


# --------------------------------------------------------------------------- #
# _first_url
# --------------------------------------------------------------------------- #
class TestFirstUrl:
    def test_no_url(self):
        assert mailto_poller._first_url("just some text") is None

    def test_empty_string(self):
        assert mailto_poller._first_url("") is None

    def test_simple_url(self):
        assert (
            mailto_poller._first_url("check this https://example.com out")
            == "https://example.com"
        )

    def test_http_url(self):
        assert mailto_poller._first_url("see http://foo.bar/baz") == "http://foo.bar/baz"

    def test_url_with_query_params(self):
        text = "open https://example.com/p?a=1&b=2 now"
        assert mailto_poller._first_url(text) == "https://example.com/p?a=1&b=2"

    def test_multiple_urls_picks_first(self):
        text = "first https://a.com/one then https://b.com/two"
        assert mailto_poller._first_url(text) == "https://a.com/one"

    def test_trailing_punctuation_stripped(self):
        assert mailto_poller._first_url("visit https://example.com.") == "https://example.com"
        assert mailto_poller._first_url("visit https://example.com!") == "https://example.com"
        assert (
            mailto_poller._first_url("see https://example.com/page,")
            == "https://example.com/page"
        )

    def test_url_inside_brackets(self):
        """Regex excludes closing brackets, then trailing punct strip handles the rest."""
        assert (
            mailto_poller._first_url("(see https://example.com/p)")
            == "https://example.com/p"
        )

    def test_malformed_no_scheme(self):
        assert mailto_poller._first_url("ftp://example.com or example.com") is None

    def test_uppercase_scheme(self):
        assert (
            mailto_poller._first_url("HTTPS://EXAMPLE.COM/X")
            == "HTTPS://EXAMPLE.COM/X"
        )


# --------------------------------------------------------------------------- #
# _extract_body_text
# --------------------------------------------------------------------------- #
class TestExtractBodyText:
    def test_plain_text(self):
        msg = EmailMessage()
        msg.set_content("hello https://example.com world")
        text = mailto_poller._extract_body_text(msg)
        assert "https://example.com" in text

    def test_html_only(self):
        msg = EmailMessage()
        msg.set_content("<html><body><a href='https://example.com'>x</a></body></html>",
                        subtype="html")
        text = mailto_poller._extract_body_text(msg)
        # HTML stripping should leave the URL detectable.
        assert "https://example.com" in text

    def test_multipart_prefers_text_over_html(self):
        msg = EmailMessage()
        msg.set_content("plain https://plain.example.com")
        msg.add_alternative(
            "<html><body><a href='https://html.example.com'>x</a></body></html>",
            subtype="html",
        )
        text = mailto_poller._extract_body_text(msg)
        assert "https://plain.example.com" in text
        # html branch should not be returned when text/plain exists
        assert "https://html.example.com" not in text

    def test_multipart_html_fallback_when_no_plain(self):
        """If only HTML parts exist, return them with tags stripped."""
        # Hand-craft a multipart with only text/html — EmailMessage doesn't make
        # this easy via set_content alone, so build it from a raw string.
        raw = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/alternative; boundary=BB\r\n"
            "\r\n"
            "--BB\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>visit https://only-html.example.com today</p></body></html>\r\n"
            "--BB--\r\n"
        )
        msg = email.message_from_string(raw)
        text = mailto_poller._extract_body_text(msg)
        assert "https://only-html.example.com" in text

    def test_base64_encoded_payload(self):
        body = "hello https://b64.example.com world"
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        raw = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            f"{encoded}\r\n"
        )
        msg = email.message_from_string(raw)
        text = mailto_poller._extract_body_text(msg)
        assert "https://b64.example.com" in text

    def test_missing_payload(self):
        """A header-only message returns empty string, not a crash."""
        raw = "Subject: no body here\r\n\r\n"
        msg = email.message_from_string(raw)
        text = mailto_poller._extract_body_text(msg)
        assert text == "" or text.strip() == ""

    def test_attachment_part_skipped(self):
        raw = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=BB\r\n"
            "\r\n"
            "--BB\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "see https://body.example.com\r\n"
            "--BB\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Disposition: attachment; filename=junk.txt\r\n"
            "\r\n"
            "https://attachment.example.com\r\n"
            "--BB--\r\n"
        )
        msg = email.message_from_string(raw)
        text = mailto_poller._extract_body_text(msg)
        assert "https://body.example.com" in text
        assert "https://attachment.example.com" not in text


# --------------------------------------------------------------------------- #
# poll_once
# --------------------------------------------------------------------------- #
def _make_imap_message(
    *,
    subject: str = "test",
    body: str = "see https://example.com/x",
    message_id: str = "<abc@example.com>",
    sender: str = "alice@example.com",
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "me@example.com"
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 01 Jan 2024 00:00:00 +0000"
    msg.set_content(body)
    return msg.as_bytes()


class _FakeIMAP:
    """Minimal stand-in for `imaplib.IMAP4_SSL`."""

    def __init__(self, uids_to_msgs: dict[bytes, bytes], select_ok: bool = True):
        self._uids_to_msgs = uids_to_msgs
        self._select_ok = select_ok
        self.selected: str | None = None
        self.logged_in = False
        self.logged_out = False
        self.closed = False
        self.stored: list[tuple[bytes, str, str]] = []

    def login(self, user: str, password: str) -> None:
        self.logged_in = True

    def select(self, mailbox: str) -> tuple[str, Any]:
        self.selected = mailbox
        return ("OK" if self._select_ok else "NO", [b""])

    def search(self, charset: Any, criteria: str) -> tuple[str, list[bytes]]:
        if not self._uids_to_msgs:
            return ("OK", [b""])
        return ("OK", [b" ".join(self._uids_to_msgs.keys())])

    def fetch(self, uid: bytes, what: str) -> tuple[str, list[Any]]:
        raw = self._uids_to_msgs.get(uid)
        if raw is None:
            return ("NO", [])
        return ("OK", [(b"header", raw)])

    def store(self, uid: bytes, cmd: str, flags: str) -> tuple[str, Any]:
        self.stored.append((uid, cmd, flags))
        return ("OK", [b""])

    def close(self) -> None:
        self.closed = True

    def logout(self) -> None:
        self.logged_out = True


@pytest.fixture
def imap_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IMAP_USER", "me@example.com")
    monkeypatch.setenv("IMAP_APP_PASSWORD", "app-pw")
    monkeypatch.setenv("IMAP_LABEL", "connecting-dots")


class TestPollOnce:
    def test_dispatches_and_marks_seen(self, imap_env):
        msgs = {b"42": _make_imap_message(body="grab https://example.com/post please")}
        fake = _FakeIMAP(msgs)
        with mock.patch.object(mailto_poller, "_connect", return_value=fake), \
             mock.patch.object(mailto_poller, "dispatch_url") as mock_dispatch:
            n = mailto_poller.poll_once()
        assert n == 1
        assert mock_dispatch.call_count == 1
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["url"] == "https://example.com/post"
        assert kwargs["source"] == "mailto"
        # Message-ID survives as the dedupe key.
        assert kwargs["message_id"] == "<abc@example.com>"
        # Marked seen.
        assert fake.stored == [(b"42", "+FLAGS", "\\Seen")]
        assert fake.logged_out is True

    def test_no_unread_is_noop(self, imap_env):
        fake = _FakeIMAP({})
        with mock.patch.object(mailto_poller, "_connect", return_value=fake), \
             mock.patch.object(mailto_poller, "dispatch_url") as mock_dispatch:
            n = mailto_poller.poll_once()
        assert n == 0
        mock_dispatch.assert_not_called()
        assert fake.stored == []

    def test_multiple_messages(self, imap_env):
        msgs = {
            b"1": _make_imap_message(
                body="link https://a.example.com", message_id="<a@x>"
            ),
            b"2": _make_imap_message(
                body="link https://b.example.com", message_id="<b@x>"
            ),
        }
        fake = _FakeIMAP(msgs)
        with mock.patch.object(mailto_poller, "_connect", return_value=fake), \
             mock.patch.object(mailto_poller, "dispatch_url") as mock_dispatch:
            n = mailto_poller.poll_once()
        assert n == 2
        assert mock_dispatch.call_count == 2
        urls = {c.kwargs["url"] for c in mock_dispatch.call_args_list}
        assert urls == {"https://a.example.com", "https://b.example.com"}

    def test_url_in_subject_when_body_has_none(self, imap_env):
        msgs = {
            b"7": _make_imap_message(
                subject="check https://from-subject.example.com",
                body="no link in body here",
            )
        }
        fake = _FakeIMAP(msgs)
        with mock.patch.object(mailto_poller, "_connect", return_value=fake), \
             mock.patch.object(mailto_poller, "dispatch_url") as mock_dispatch:
            n = mailto_poller.poll_once()
        assert n == 1
        assert mock_dispatch.call_args.kwargs["url"] == "https://from-subject.example.com"

    def test_no_url_anywhere_skips_and_leaves_unread(self, imap_env):
        msgs = {b"9": _make_imap_message(subject="hello", body="no urls here")}
        fake = _FakeIMAP(msgs)
        with mock.patch.object(mailto_poller, "_connect", return_value=fake), \
             mock.patch.object(mailto_poller, "dispatch_url") as mock_dispatch:
            n = mailto_poller.poll_once()
        assert n == 0
        mock_dispatch.assert_not_called()
        assert fake.stored == []  # NOT marked seen — will retry

    def test_synthetic_message_id_when_rfc_id_missing(self, imap_env):
        """When the email has no Message-ID header, fall back to mailto:<label>:<uid>."""
        msg = EmailMessage()
        msg["Subject"] = "no msg id"
        msg["From"] = "alice@example.com"
        msg["To"] = "me@example.com"
        msg.set_content("see https://example.com/x")
        msgs = {b"55": msg.as_bytes()}
        fake = _FakeIMAP(msgs)
        with mock.patch.object(mailto_poller, "_connect", return_value=fake), \
             mock.patch.object(mailto_poller, "dispatch_url") as mock_dispatch:
            n = mailto_poller.poll_once()
        assert n == 1
        assert mock_dispatch.call_args.kwargs["message_id"] == "mailto:connecting-dots:55"

    def test_dispatch_failure_leaves_message_unread(self, imap_env):
        msgs = {b"3": _make_imap_message()}
        fake = _FakeIMAP(msgs)
        with mock.patch.object(mailto_poller, "_connect", return_value=fake), \
             mock.patch.object(
                 mailto_poller, "dispatch_url", side_effect=RuntimeError("boom")
             ):
            n = mailto_poller.poll_once()
        assert n == 0
        assert fake.stored == []  # NOT marked seen — will retry

    def test_select_label_failure_raises(self, imap_env):
        fake = _FakeIMAP({}, select_ok=False)
        with mock.patch.object(mailto_poller, "_connect", return_value=fake), \
             pytest.raises(RuntimeError, match="Could not select"):
            mailto_poller.poll_once()
        # logout still attempted even on error.
        assert fake.logged_out is True

    def test_imap_connect_failure_propagates(self, imap_env):
        with mock.patch.object(
            mailto_poller, "_connect", side_effect=OSError("network down")
        ):
            with pytest.raises(OSError, match="network down"):
                mailto_poller.poll_once()

    def test_missing_required_env_var_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("IMAP_USER", raising=False)
        monkeypatch.delenv("IMAP_APP_PASSWORD", raising=False)
        with pytest.raises(RuntimeError, match="Missing required env var"):
            mailto_poller.poll_once()
