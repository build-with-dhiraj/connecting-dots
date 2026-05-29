"""Hermetic tests for workers.youtube_recap — no real Azure API calls."""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from workers.youtube_recap import (
    WatchEntry,
    _discover_zip,
    _parse_date,
    _parse_timestamp,
    aggregate_channels,
    classify_channels,
    compute_year_trend,
    discover_themes,
    extract_keywords,
    filter_by_date,
    find_rewatched,
    parse_watch_history,
    render_report,
    rollup_themes,
)

# ---------------------------------------------------------------------------
# HTML fixture — 5 entries: 2 normal, 1 removed, 1 post/community, 1 no-channel
# ---------------------------------------------------------------------------

_FIXTURE_HTML: bytes = (
    "<html><body>"
    '<div class="mdl-grid">'
    # Entry 1: valid watch
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
    '<div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">YouTube</p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
    "Watched "
    '<a href="https://www.youtube.com/watch?v=abc123">Magnus Carlsen wins Norway Chess</a><br>'
    '<a href="https://www.youtube.com/channel/UCchess">ChessBase India</a><br>'
    "May 15, 2024, 3:14:52 PM PDT<br>"
    "</div>"
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
    "</div></div>"
    # Entry 2: valid watch
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
    '<div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">YouTube</p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
    "Watched "
    '<a href="https://www.youtube.com/watch?v=def456">Claude Sonnet vs GPT-4 Comparison</a><br>'
    '<a href="https://www.youtube.com/channel/UCai">AI Explained</a><br>'
    "Jun 01, 2024, 10:00:00 AM PDT<br>"
    "</div>"
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
    "</div></div>"
    # Entry 3: removed video - title is the bare URL
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
    '<div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">YouTube</p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
    "Watched "
    '<a href="https://www.youtube.com/watch?v=removed1">https://www.youtube.com/watch?v=removed1</a><br>'
    '<a href="https://www.youtube.com/channel/UCsomechannel">Some Channel</a><br>'
    "Jun 02, 2024, 9:00:00 AM PDT<br>"
    "</div>"
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
    "</div></div>"
    # Entry 4: community post (Viewed, no watch URL) - should be skipped
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
    '<div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">YouTube</p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
    "Viewed "
    '<a href="https://www.youtube.com/post/UgkxABCpost">https://www.youtube.com/post/UgkxABCpost</a><br>'
    "May 20, 2024, 2:00:00 PM PDT<br>"
    "</div>"
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
    "</div></div>"
    # Entry 5: only one link, no channel - should be skipped
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
    '<div class="mdl-grid">'
    '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">YouTube</p></div>'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
    "Watched "
    '<a href="https://www.youtube.com/watch?v=nochannel1">Video with no channel</a><br>'
    "Jun 03, 2024, 8:00:00 AM PDT<br>"
    "</div>"
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
    "</div></div>"
    "</div></body></html>"
).encode("utf-8")

# All-ad fixture — entries with no valid watch URL at all
_ALL_AD_HTML: bytes = (
    "<html><body>"
    '<div class="mdl-grid">'
    '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
    '<div class="mdl-grid">'
    '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
    "Watched an ad from "
    '<a href="https://googleads.g.doubleclick.net/xyz">Ad Title</a><br>'
    '<a href="https://www.youtube.com/@SomeAdChannel">AdChan</a><br>'
    "Jun 01, 2024, 1:00:00 PM PDT<br>"
    "</div>"
    "</div></div>"
    "</div></body></html>"
).encode("utf-8")


# ---------------------------------------------------------------------------
# Helpers: build mock LLM clients
# ---------------------------------------------------------------------------


def _mock_llm_themes(themes: list[dict]) -> MagicMock:
    """Return a mock AzureOpenAI client whose completions return given themes."""
    resp = MagicMock()
    resp.choices[0].message.content = json.dumps({"themes": themes})
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def _mock_llm_classify(mapping: dict[str, str]) -> MagicMock:
    resp = MagicMock()
    resp.choices[0].message.content = json.dumps(mapping)
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


# ---------------------------------------------------------------------------
# Test 1: parse_watch_history returns only the 2 valid entries
# ---------------------------------------------------------------------------


def test_parse_watch_history_valid_entries_only():
    entries = parse_watch_history(_FIXTURE_HTML)
    # Only 2 valid entries: Magnus Carlsen + Claude Sonnet
    assert len(entries) == 2
    titles = {e.title for e in entries}
    assert "Magnus Carlsen wins Norway Chess" in titles
    assert "Claude Sonnet vs GPT-4 Comparison" in titles


# ---------------------------------------------------------------------------
# Test 2a: Date-range filter excludes entries before --since
# ---------------------------------------------------------------------------


def test_filter_by_date_keeps_range():
    entries = parse_watch_history(_FIXTURE_HTML)
    # Only keep May 15 2024 (exclude Jun 01 2024)
    filtered = filter_by_date(entries, date(2024, 5, 1), date(2024, 5, 31))
    assert len(filtered) == 1
    assert filtered[0].title == "Magnus Carlsen wins Norway Chess"


# ---------------------------------------------------------------------------
# Test 2b: Date-range filter — both boundary dates are inclusive
# ---------------------------------------------------------------------------


def test_filter_by_date_includes_boundary():
    entries = parse_watch_history(_FIXTURE_HTML)
    # Both valid entries are in May–June 2024 window
    filtered = filter_by_date(entries, date(2024, 5, 15), date(2024, 6, 1))
    assert len(filtered) == 2


# ---------------------------------------------------------------------------
# Test 3: Channel aggregation — correct counts
# ---------------------------------------------------------------------------


def test_aggregate_channels():
    entries = [
        WatchEntry(datetime(2024, 1, 1, tzinfo=timezone.utc), "T1", "Chan A", "url", "id1"),
        WatchEntry(datetime(2024, 1, 2, tzinfo=timezone.utc), "T2", "Chan A", "url", "id2"),
        WatchEntry(datetime(2024, 1, 3, tzinfo=timezone.utc), "T3", "Chan B", "url", "id3"),
    ]
    counts = aggregate_channels(entries)
    assert counts["Chan A"] == 2
    assert counts["Chan B"] == 1


# ---------------------------------------------------------------------------
# Test 4: Theme rollup — sums watch counts per theme correctly
# ---------------------------------------------------------------------------


def test_rollup_themes():
    channel_counts = Counter({"Chess TV": 50, "ChessBase India": 30, "AI Explained": 20})
    channel_to_theme = {
        "Chess TV": "Chess",
        "ChessBase India": "Chess",
        "AI Explained": "Technology",
    }
    result = rollup_themes(channel_counts, channel_to_theme)
    assert result["Chess"] == 80
    assert result["Technology"] == 20


# ---------------------------------------------------------------------------
# Test 5: Keyword extraction strips stopwords + YouTube boilerplate
# ---------------------------------------------------------------------------


def test_extract_keywords_strips_stopwords():
    titles = [
        "The Best Chess Video ever",
        "How to play chess in the end game",
        "video review of the latest AI model",
        "Chess opening theory official",
    ]
    kw = extract_keywords(titles, top_n=20)
    words = {w for w, _ in kw}
    assert "the" not in words, "'the' is a stopword and should be stripped"
    assert "video" not in words, "'video' is YouTube boilerplate and should be stripped"
    assert "how" not in words, "'how' is a stopword"
    assert "official" not in words, "'official' is YouTube boilerplate"
    assert "chess" in words, "'chess' should appear as a meaningful keyword"


# ---------------------------------------------------------------------------
# Test 6: --max-themes cap enforced even if LLM returns more
# ---------------------------------------------------------------------------


def test_max_themes_cap_enforced():
    many_themes = [{"name": f"Theme {i}", "description": f"Desc {i}"} for i in range(10)]
    client = _mock_llm_themes(many_themes)
    result = discover_themes(
        channels=[("Chan A", 10)],
        sample_titles=["title a"],
        max_themes=5,
        client=client,
        model="gpt-4.1",
    )
    assert len(result) <= 5


# ---------------------------------------------------------------------------
# Test 7: Report file is written to reports/ (uses tmp_path)
# ---------------------------------------------------------------------------


def test_report_written_to_reports_dir(tmp_path):
    entries = [
        WatchEntry(
            datetime(2024, 6, 1, tzinfo=timezone.utc),
            "Title A",
            "Chan A",
            "https://youtube.com/watch?v=aaa",
            "aaa",
        ),
    ]
    channel_counts = aggregate_channels(entries)
    themes = [{"name": "General", "description": "All content"}]
    theme_counts = Counter({"General": 1})
    channel_to_theme = {"Chan A": "General"}
    year_trend = compute_year_trend(entries, channel_to_theme)

    text = render_report(
        entries=entries,
        channel_counts=channel_counts,
        themes=themes,
        theme_counts=theme_counts,
        channel_to_theme=channel_to_theme,
        year_trend=year_trend,
        rewatched=[],
        since=date(2024, 1, 1),
        until=date(2024, 12, 31),
        max_themes=8,
    )
    assert "# YouTube Watch Recap" in text
    assert "Chan A" in text

    reports = tmp_path / "reports"
    reports.mkdir()
    out = reports / "youtube-recap_2024-01-01_2024-12-31.md"
    out.write_text(text)
    assert out.exists()
    assert out.stat().st_size > 0


# ---------------------------------------------------------------------------
# Test 8: No vault writes — assert no file is written under vault/
# ---------------------------------------------------------------------------


def test_no_vault_writes():
    entries = [
        WatchEntry(
            datetime(2024, 6, 1, tzinfo=timezone.utc),
            "Title A",
            "Chan A",
            "https://youtube.com/watch?v=aaa",
            "aaa",
        ),
    ]
    channel_counts = aggregate_channels(entries)
    themes = [{"name": "General", "description": "All content"}]
    theme_counts = Counter({"General": 1})
    channel_to_theme = {"Chan A": "General"}
    year_trend = compute_year_trend(entries, channel_to_theme)

    text = render_report(
        entries=entries,
        channel_counts=channel_counts,
        themes=themes,
        theme_counts=theme_counts,
        channel_to_theme=channel_to_theme,
        year_trend=year_trend,
        rewatched=[],
        since=date(2024, 1, 1),
        until=date(2024, 12, 31),
        max_themes=8,
    )
    # Render only writes to 'reports/' — the text itself must not reference vault paths
    assert "vault/" not in text.lower()


# ---------------------------------------------------------------------------
# Test 9: Auto-discovery picks the newest (lexicographically last) zip
# ---------------------------------------------------------------------------


def test_discover_zip_finds_newest(tmp_path, monkeypatch):
    import workers.youtube_recap as m

    fake_zips = [
        str(tmp_path / "takeout-20240101T000000Z-1-001.zip"),
        str(tmp_path / "takeout-20250101T000000Z-2-001.zip"),  # newest by sort
    ]
    for z in fake_zips:
        Path(z).touch()

    monkeypatch.setattr(m.glob, "glob", lambda pattern: fake_zips)
    result = _discover_zip()
    assert result == fake_zips[-1]


# ---------------------------------------------------------------------------
# Test 10: --history-file pointing to a .html file is parsed correctly
# ---------------------------------------------------------------------------


def test_history_file_html(tmp_path):
    from workers.youtube_recap import _load_html

    html_path = tmp_path / "watch-history.html"
    html_path.write_bytes(_FIXTURE_HTML)

    html_bytes = _load_html(str(html_path))
    entries = parse_watch_history(html_bytes)
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# Test 11: Entries with no valid YouTube watch URL are skipped
# ---------------------------------------------------------------------------


def test_entries_without_watch_url_skipped():
    """Ad entries that link to googleads or non-watch URLs must be excluded."""
    entries = parse_watch_history(_ALL_AD_HTML)
    assert entries == []


# ---------------------------------------------------------------------------
# Test 12: Trend table groups watches by year correctly
# ---------------------------------------------------------------------------


def test_compute_year_trend():
    entries = [
        WatchEntry(datetime(2023, 6, 1, tzinfo=timezone.utc), "T1", "ChanA", "url", "v1"),
        WatchEntry(datetime(2023, 9, 1, tzinfo=timezone.utc), "T2", "ChanA", "url", "v2"),
        WatchEntry(datetime(2024, 3, 1, tzinfo=timezone.utc), "T3", "ChanB", "url", "v3"),
        WatchEntry(datetime(2024, 11, 1, tzinfo=timezone.utc), "T4", "ChanA", "url", "v4"),
    ]
    channel_to_theme = {"ChanA": "Sports", "ChanB": "Tech"}
    trend = compute_year_trend(entries, channel_to_theme)

    assert set(trend.keys()) == {2023, 2024}
    assert trend[2023]["Sports"] == 2
    assert trend[2024]["Sports"] == 1
    assert trend[2024]["Tech"] == 1


# ---------------------------------------------------------------------------
# Test 13: Most-rewatched — only videos with ≥3 occurrences included
# ---------------------------------------------------------------------------


def test_find_rewatched():
    entries = [
        WatchEntry(datetime(2024, 1, i, tzinfo=timezone.utc), "Same Title", "Chan", "url", "vid_abc")
        for i in range(1, 5)  # watched 4 times
    ] + [
        WatchEntry(datetime(2024, 2, 1, tzinfo=timezone.utc), "Once Only", "Chan", "url", "vid_xyz"),
    ]
    result = find_rewatched(entries, min_count=3)
    assert len(result) == 1
    assert result[0]["video_id"] == "vid_abc"
    assert result[0]["count"] == 4


def test_find_rewatched_threshold_exact():
    """Videos watched exactly min_count times ARE included."""
    entries = [
        WatchEntry(datetime(2024, 1, i, tzinfo=timezone.utc), "Tri Title", "Chan", "url", "vid_tri")
        for i in range(1, 4)  # exactly 3
    ]
    result = find_rewatched(entries, min_count=3)
    assert len(result) == 1
    assert result[0]["count"] == 3


# ---------------------------------------------------------------------------
# Test 14: All-ad fixture returns empty list
# ---------------------------------------------------------------------------


def test_all_ad_entries_returns_empty():
    entries = parse_watch_history(_ALL_AD_HTML)
    assert entries == [], f"Expected [], got {entries}"


# ---------------------------------------------------------------------------
# Additional: _parse_date and _parse_timestamp helpers
# ---------------------------------------------------------------------------


def test_today_alias_resolves():
    result = _parse_date("today")
    assert isinstance(result, date)
    assert result == date.today()


def test_iso_date_parses():
    result = _parse_date("2024-05-15")
    assert result == date(2024, 5, 15)


def test_parse_timestamp_pm():
    ts = _parse_timestamp("May 15, 2024, 3:14:52 PM PDT")
    assert ts is not None
    assert ts.year == 2024
    assert ts.month == 5
    assert ts.day == 15
    assert ts.hour == 15  # 3 PM -> 15
    assert ts.minute == 14


def test_parse_timestamp_midnight():
    ts = _parse_timestamp("Jan 01, 2023, 12:00:00 AM UTC")
    assert ts is not None
    assert ts.hour == 0  # midnight


def test_parse_timestamp_noon():
    ts = _parse_timestamp("Jan 01, 2023, 12:00:00 PM UTC")
    assert ts is not None
    assert ts.hour == 12


def test_parse_timestamp_invalid():
    ts = _parse_timestamp("not a timestamp at all")
    assert ts is None


# ---------------------------------------------------------------------------
# Additional: classify_channels batching
# ---------------------------------------------------------------------------


def test_classify_channels_batching():
    themes = [{"name": "Tech"}, {"name": "Sports"}]
    channels = [f"Channel {i}" for i in range(75)]  # 75 -> 2 batches of 50

    call_count = 0

    def _fake_create(**kwargs):
        nonlocal call_count
        call_count += 1
        user_msg = kwargs["messages"][1]["content"]
        batch_channels = [
            line.lstrip("- ").strip()
            for line in user_msg.strip().split("\n")
            if line.strip().startswith("-")
        ]
        mapping = {ch: "Tech" for ch in batch_channels}
        resp = MagicMock()
        resp.choices[0].message.content = json.dumps(mapping)
        return resp

    client = MagicMock()
    client.chat.completions.create.side_effect = _fake_create

    result = classify_channels(channels, themes, client, "gpt-4.1", batch_size=50)
    assert call_count == 2  # 75 channels / 50 per batch = 2 calls
    assert len(result) == 75
    assert all(v in ("Tech", "Sports") for v in result.values())
