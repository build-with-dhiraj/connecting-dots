"""Tests for `workers.linkedin_zip_watcher` + `connecting_dots.handlers.linkedin`.

We build synthetic LinkedIn-export ZIPs in tmp dirs — no network, no real
export needed. Dispatch is mocked so we can assert idempotency directly.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import AnyUrl

from connecting_dots.inbound_envelope import InboundEnvelope, MessageType, Source
from connecting_dots.handlers.linkedin import LinkedInHandler, _strip_tracking
from workers import linkedin_zip_watcher as w


# --------------------------------------------------------------------------- #
# Fixtures: build a minimal LinkedIn export ZIP
# --------------------------------------------------------------------------- #


def _write_csv(zf: zipfile.ZipFile, name: str, header: list[str], rows: list[list[str]]) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    zf.writestr(name, buf.getvalue())


def _make_export_zip(
    path: Path,
    *,
    saved_rows: list[list[str]] | None = None,
    reaction_rows: list[list[str]] | None = None,
    include_other: bool = True,
) -> Path:
    """Mint a tmp ZIP that mimics LinkedIn's monthly export."""
    saved_rows = saved_rows if saved_rows is not None else [
        [
            "2024-03-14 10:42:11 UTC",
            "Why staff engineers write less code",
            "https://www.linkedin.com/pulse/why-staff-engineers-write-less-code-jane-doe?trk=pulse-share",
            "Jane Doe",
        ],
        [
            "2024-04-01 09:15:00 UTC",
            "Notes on async Python",
            "https://www.linkedin.com/posts/john-smith_async-python-activity-1234",
            "John Smith",
        ],
    ]
    reaction_rows = reaction_rows if reaction_rows is not None else [
        ["2024-03-20", "LIKE", "https://www.linkedin.com/feed/update/urn:li:activity:9999"],
    ]

    with zipfile.ZipFile(path, "w") as zf:
        _write_csv(
            zf,
            "Saved Articles.csv",
            ["SavedAt", "ArticleTitle", "ArticleURL", "ArticleAuthor"],
            saved_rows,
        )
        _write_csv(
            zf,
            "Reactions.csv",
            ["Date", "Type", "Link"],
            reaction_rows,
        )
        if include_other:
            # Realistic noise that we should ignore.
            _write_csv(zf, "Comments.csv", ["Date", "Link", "Message"], [])
            _write_csv(zf, "Shares.csv", ["Date", "ShareLink"], [])
    return path


# --------------------------------------------------------------------------- #
# Watcher: end-to-end
# --------------------------------------------------------------------------- #


class _DispatchSink:
    """Drop-in for `dispatch_url` that records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_sweep_dispatches_saved_and_reactions(tmp_path: Path) -> None:
    inbox = tmp_path / "linkedin-inbox"
    inbox.mkdir()
    _make_export_zip(inbox / "linkedin_export_2024-04.zip")

    sink = _DispatchSink()
    n = w.sweep_once(inbox, dispatch=sink)

    assert n == 3  # 2 saved + 1 reaction
    sources = {c["source"] for c in sink.calls}
    assert sources == {"linkedin"}
    types = sorted(c["raw_payload"]["type"] for c in sink.calls)
    assert types == ["reaction", "saved", "saved"]

    # All envelopes carry linkedin_export marker so the handler short-circuits.
    assert all(c["raw_payload"]["linkedin_export"] is True for c in sink.calls)
    # All message_ids are deterministic + namespaced.
    assert all(c["raw_payload"]["message_id"].startswith("linkedin:") for c in sink.calls)


def test_processed_zip_is_moved(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zip_path = _make_export_zip(inbox / "export.zip")

    sink = _DispatchSink()
    w.sweep_once(inbox, dispatch=sink)

    assert not zip_path.exists(), "processed zip should have been moved out of the inbox root"
    assert (inbox / ".processed" / "export.zip").exists()
    assert any((inbox / ".unpacked").iterdir())


def test_idempotency_on_reimport(tmp_path: Path) -> None:
    """Reimporting the same export must yield identical message_ids."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _make_export_zip(inbox / "export_a.zip")

    sink1 = _DispatchSink()
    w.sweep_once(inbox, dispatch=sink1)

    # Drop the same payload again — fresh ZIP with the same rows.
    _make_export_zip(inbox / "export_b.zip")
    sink2 = _DispatchSink()
    w.sweep_once(inbox, dispatch=sink2)

    ids1 = sorted(c["raw_payload"]["message_id"] for c in sink1.calls)
    ids2 = sorted(c["raw_payload"]["message_id"] for c in sink2.calls)
    assert ids1 == ids2, "deterministic message_ids must match across re-imports"


def test_malformed_zip_is_skipped_not_crashed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bad = inbox / "not-really-a-zip.zip"
    bad.write_bytes(b"this is not a zip file")

    sink = _DispatchSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 0
    # Malformed ZIPs stay put (we don't move them — leave for inspection).
    assert bad.exists()


def test_non_linkedin_zip_is_skipped(tmp_path: Path) -> None:
    """A valid ZIP that isn't a LinkedIn export should be a no-op dispatch."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zp = inbox / "random.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("notes.txt", "hello")
        zf.writestr("data.csv", "a,b\n1,2\n")

    sink = _DispatchSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 0


def test_alternate_column_aliases(tmp_path: Path) -> None:
    """The resolver must tolerate header drift (`URL` vs `ArticleURL` etc.)."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zp = inbox / "export_alt.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        _write_csv(
            zf,
            "saved_articles.csv",  # underscored variant
            ["Date", "Title", "URL", "Author"],  # all alternate aliases
            [["2024-05-01", "Some article", "https://www.linkedin.com/pulse/x", "Author X"]],
        )
    sink = _DispatchSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 1
    assert sink.calls[0]["raw_payload"]["title"] == "Some article"


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    """Zip-slip attempt must abort extraction, not dispatch anything."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zp = inbox / "evil.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        # A genuine-looking saved-articles entry — passes the export check.
        _write_csv(
            zf,
            "Saved Articles.csv",
            ["SavedAt", "ArticleTitle", "ArticleURL", "ArticleAuthor"],
            [["2024-05-01", "x", "https://www.linkedin.com/pulse/x", "a"]],
        )
        # And a path-traversal sibling. _safe_extract should refuse the archive.
        zf.writestr("../escaped.txt", "nope")
    sink = _DispatchSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 0


def test_safe_extract_rejects_sibling_directory_bypass(tmp_path: Path) -> None:
    """The classic `startswith`-on-string bypass: dest=`foo`, entry=`../foo_evil/x`.

    The naive check `str(member).startswith(str(dest))` returns True because
    `/tmp/.../foo_evil/x` starts with `/tmp/.../foo`. The fixed implementation
    uses `Path.is_relative_to` which inspects path components, not the prefix.
    """
    dest = tmp_path / "dest"
    # Build a ZIP whose member resolves to a sibling directory of `dest`.
    zp = tmp_path / "sibling.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("../dest_evil/poison.txt", "nope")
    with zipfile.ZipFile(zp) as zf:
        with pytest.raises(RuntimeError, match="unsafe path"):
            w._safe_extract(zf, dest)
    assert not (tmp_path / "dest_evil").exists()


class _FakeSizeZip:
    """Wraps a real `ZipFile` so `infolist()` returns members with rewritten
    `file_size` values. `_safe_extract` only reads metadata + path; it never
    actually extracts on the bomb path because it raises before that, so we
    don't need to fake decompression."""

    def __init__(self, zf: zipfile.ZipFile, size_overrides: dict[str, int]) -> None:
        self._zf = zf
        self._overrides = size_overrides

    def infolist(self) -> list[zipfile.ZipInfo]:
        out: list[zipfile.ZipInfo] = []
        for info in self._zf.infolist():
            if info.filename in self._overrides:
                # Rewrite in place — these ZipInfo objects are throw-away.
                info.file_size = self._overrides[info.filename]
            out.append(info)
        return out

    def extractall(self, *args, **kwargs):  # pragma: no cover — never reached
        raise AssertionError("extractall should not be called once caps trip")


def test_safe_extract_rejects_oversized_member(tmp_path: Path) -> None:
    """Single-member uncompressed-size cap (100 MB) must be enforced.

    We can't actually write 200 MB into the ZIP — instead we overwrite the
    in-memory `file_size` after read to simulate a header that lies about
    the decompressed size (the classic zip-bomb trick)."""
    zp = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("huge.bin", b"x")
    with zipfile.ZipFile(zp) as zf:
        fake = _FakeSizeZip(zf, {"huge.bin": 200 * 1024 * 1024})
        with pytest.raises(RuntimeError, match="per-file size cap"):
            w._safe_extract(fake, tmp_path / "out")  # type: ignore[arg-type]


def test_safe_extract_rejects_oversized_total(tmp_path: Path) -> None:
    """Total uncompressed-size cap (500 MB) must trip even on many small lies."""
    zp = tmp_path / "bomb_total.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for i in range(10):
            zf.writestr(f"chunk_{i}.bin", b"x")
    overrides = {f"chunk_{i}.bin": 80 * 1024 * 1024 for i in range(10)}
    with zipfile.ZipFile(zp) as zf:
        fake = _FakeSizeZip(zf, overrides)
        with pytest.raises(RuntimeError, match="total uncompressed size cap"):
            w._safe_extract(fake, tmp_path / "out")  # type: ignore[arg-type]


def test_safe_extract_rejects_symlink_member(tmp_path: Path) -> None:
    """Symlink entries are an extract-time escape hatch; refuse them."""
    zp = tmp_path / "symlink.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        info = zipfile.ZipInfo("link.txt")
        # Mark this entry as a Unix symlink (mode 0o120000 in the high half).
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
    with zipfile.ZipFile(zp) as zf:
        with pytest.raises(RuntimeError, match="symlink"):
            w._safe_extract(zf, tmp_path / "out")


def test_dispatch_receives_message_id_kwarg(tmp_path: Path) -> None:
    """The synthetic message_id must be passed as an explicit kwarg so the
    dispatcher dedupes on it (P0-1 regression — without this, re-imports
    duplicate every row)."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _make_export_zip(inbox / "export.zip")
    sink = _DispatchSink()
    w.sweep_once(inbox, dispatch=sink)
    assert sink.calls, "expected at least one dispatch call"
    for call in sink.calls:
        assert "message_id" in call, "dispatch must receive message_id as a kwarg"
        # And the kwarg must match the payload-embedded id.
        assert call["message_id"] == call["raw_payload"]["message_id"]
        assert call["message_id"].startswith("linkedin:")


# --------------------------------------------------------------------------- #
# Handler: contract + export short-circuit
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/posts/jane-doe_activity-12345",
        "https://www.linkedin.com/pulse/some-article-jane-doe",
        "https://www.linkedin.com/feed/update/urn:li:activity:9999",
        "https://linkedin.com/in/janedoe/recent-activity/all/",
        # Locale subdomains — handler must accept any *.linkedin.com host.
        "https://de.linkedin.com/posts/some-author_activity-1",
        "https://uk.linkedin.com/pulse/uk-article",
        "https://fr.linkedin.com/feed/update/urn:li:activity:1",
        "https://m.linkedin.com/posts/m-author_activity-1",
    ],
)
def test_handler_matches_linkedin_urls(url: str) -> None:
    assert LinkedInHandler().matches(url)


@pytest.mark.parametrize(
    "url",
    [
        # Look-alike host: must NOT be accepted. Suffix is `linkedin.com.evil`,
        # not `.linkedin.com`, and the apex is `evil`.
        "https://linkedin.com.evil/posts/abc",
        "https://notlinkedin.com/posts/abc",
    ],
)
def test_handler_rejects_lookalike_hosts(url: str) -> None:
    assert not LinkedInHandler().matches(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/posts/abc",
        "https://twitter.com/x/status/1",
        "https://www.linkedin.com/jobs/view/123",  # /jobs isn't ours
        "https://www.linkedin.com/in/janedoe/",  # bare profile, no recent-activity
        "ftp://linkedin.com/posts/abc",
    ],
)
def test_handler_does_not_match_other_urls(url: str) -> None:
    assert not LinkedInHandler().matches(url)


def test_strip_tracking_removes_known_params() -> None:
    url = (
        "https://www.linkedin.com/pulse/foo?trk=public_post&"
        "lipi=abc&trackingId=xyz&keep=me"
    )
    out = _strip_tracking(url)
    assert "trk=" not in out
    assert "lipi=" not in out
    assert "trackingId=" not in out
    assert "keep=me" in out


def _envelope(url: str, raw_payload: dict[str, Any] | None = None) -> InboundEnvelope:
    return InboundEnvelope(
        message_id="li-test-1",
        message_type=MessageType.url,
        url=AnyUrl(url),
        source=Source.linkedin,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload=raw_payload or {},
    )


def test_handler_uses_export_payload_when_present() -> None:
    """No network needed when raw_payload signals an export-sourced URL."""
    env = _envelope(
        "https://www.linkedin.com/pulse/why-staff-engineers-write-less-code?trk=share",
        raw_payload={
            "linkedin_export": True,
            "type": "saved",
            "title": "Why staff engineers write less code",
            "author": "Jane Doe",
        },
    )
    record = LinkedInHandler().handle(env)
    assert record.handler == "linkedin"
    assert record.title == "Why staff engineers write less code"
    assert record.raw_meta["linkedin"]["via"] == "export"
    assert record.raw_meta["linkedin"]["author"] == "Jane Doe"
    # Tracking params stripped on the stored URL.
    assert "trk=" not in record.url


def test_handler_falls_back_to_url_slug_when_export_has_no_title() -> None:
    env = _envelope(
        "https://www.linkedin.com/pulse/some-cool-article",
        raw_payload={"linkedin_export": True, "type": "saved"},
    )
    record = LinkedInHandler().handle(env)
    assert record.title.lower().replace(" ", "-") == "some-cool-article"
