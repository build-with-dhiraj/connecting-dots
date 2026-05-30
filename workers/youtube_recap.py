"""YouTube watch-history recap — parse Takeout HTML, aggregate channels, theme via LLM.

Usage:
    python -m workers.youtube_recap --since 2023-05-01 --until today
    python -m workers.youtube_recap --since 2024-01-01 --until 2025-01-01 --history-file PATH

Output: reports/youtube-recap_<since>_<until>.md
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from openai import AzureOpenAI

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1"
DEFAULT_API_VERSION = "2024-10-21"

STOPWORDS: frozenset[str] = frozenset(
    "the a an is in on at of for to and with by from new how why what you your "
    "i my me we our it its this that be are was were will can do does did has "
    "have had not no up out about get make like just s ft vs ep part official "
    "video shorts clip full watch youtube feat hd 720p 1080p 4k".split()
)

# ---------------------------------------------------------------------------
# Azure OpenAI client helpers
# ---------------------------------------------------------------------------


def _get_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION),
    )


def _get_model() -> str:
    return os.environ.get("AZURE_OPENAI_DEPLOYMENT", DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class WatchEntry(NamedTuple):
    timestamp: datetime
    title: str
    channel: str
    url: str
    video_id: str | None


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


def _extract_video_id(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc == "youtu.be":
        path = parsed.path.lstrip("/")
        return path or None
    ids = parse_qs(parsed.query).get("v")
    return ids[0] if ids else None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_TS_RE = re.compile(
    r"(\w+)\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2}):(\d{2})\s+(AM|PM)",
    re.IGNORECASE,
)


def _parse_timestamp(text: str) -> datetime | None:
    """Parse Google Takeout timestamp: 'May 29, 2026, 3:00:18 AM IST'."""
    # Normalise narrow no-break space (U+202F) and other unicode spaces to ASCII space
    text = text.replace(" ", " ").replace(" ", " ").strip()
    m = _TS_RE.search(text)
    if not m:
        return None
    month_s, day, year, hour, minute, second, ampm = m.groups()
    month = _MONTHS.get(month_s.lower()[:3])
    if month is None:
        return None
    h = int(hour)
    if ampm.upper() == "PM" and h != 12:
        h += 12
    elif ampm.upper() == "AM" and h == 12:
        h = 0
    try:
        return datetime(int(year), month, int(day), h, int(minute), int(second), tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_watch_history(html_bytes: bytes) -> list[WatchEntry]:
    """Parse watch-history HTML → list of valid WatchEntry records."""
    soup = BeautifulSoup(html_bytes, "lxml")
    entries: list[WatchEntry] = []

    for cell in soup.select(".outer-cell"):
        content = cell.select_one(".content-cell.mdl-cell--6-col")
        if content is None:
            continue

        text_raw = content.get_text(separator="\n", strip=True)

        # Skip community posts ("Viewed https://...post/...")
        if "Viewed" in text_raw and "watch?" not in text_raw and "youtu.be" not in text_raw:
            continue

        links = content.find_all("a", href=True)
        if len(links) < 2:
            continue

        video_link, channel_link = links[0], links[1]
        video_url: str = video_link["href"]
        title: str = video_link.get_text(strip=True)
        channel_url: str = channel_link["href"]
        channel: str = channel_link.get_text(strip=True)

        # Skip removed videos (link text = bare URL) or empty fields
        if title.startswith(("http", "www")) or not title or not channel:
            continue
        if "/post/" in video_url:
            continue
        if not any(
            p in channel_url
            for p in ("youtube.com/channel", "youtube.com/user", "youtube.com/@")
        ):
            continue

        # Must have a valid YouTube watch URL
        if "youtube.com/watch" not in video_url and "youtu.be/" not in video_url:
            continue

        ts = _parse_timestamp(text_raw)
        if ts is None:
            continue

        entries.append(WatchEntry(ts, title, channel, video_url, _extract_video_id(video_url)))

    return entries


# ---------------------------------------------------------------------------
# Auto-discovery + loading
# ---------------------------------------------------------------------------


def _discover_zip() -> str:
    repo_root = Path(__file__).parent.parent
    pattern = str(repo_root / "data" / "youtube-inbox" / ".processed" / "takeout-*.zip")
    zips = sorted(glob.glob(pattern))
    if not zips:
        raise FileNotFoundError(f"No takeout zip found at {pattern}")
    return zips[-1]


def _load_html(history_file: str | None) -> bytes:
    if history_file is None:
        history_file = _discover_zip()
    path = Path(history_file)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            html_name = next(
                (n for n in zf.namelist() if n.endswith("watch-history.html")), None
            )
            if html_name is None:
                raise FileNotFoundError("watch-history.html not found in zip")
            return zf.read(html_name)
    return path.read_bytes()


# ---------------------------------------------------------------------------
# Filtering + aggregation
# ---------------------------------------------------------------------------


def filter_by_date(entries: list[WatchEntry], since: date, until: date) -> list[WatchEntry]:
    since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    until_dt = datetime(until.year, until.month, until.day, 23, 59, 59, tzinfo=timezone.utc)
    return [e for e in entries if since_dt <= e.timestamp <= until_dt]


def aggregate_channels(entries: list[WatchEntry]) -> Counter:
    return Counter(e.channel for e in entries)


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]")
_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002700-\U000027BF\U0001F900-\U0001F9FF]+",
    flags=re.UNICODE,
)


def extract_keywords(titles: list[str], top_n: int = 20) -> list[tuple[str, int]]:
    freq: Counter = Counter()
    for title in titles:
        clean = _PUNCT_RE.sub(" ", _EMOJI_RE.sub(" ", title))
        for word in clean.lower().split():
            if word not in STOPWORDS and len(word) > 1:
                freq[word] += 1
    return freq.most_common(top_n)


# ---------------------------------------------------------------------------
# LLM: theme discovery + channel classification
# ---------------------------------------------------------------------------


def discover_themes(
    channels: list[tuple[str, int]],
    sample_titles: list[str],
    max_themes: int,
    client: AzureOpenAI,
    model: str,
) -> list[dict]:
    """Stage A — propose MECE themes from top channels + title sample."""
    channel_lines = "\n".join(f"- {ch} ({cnt} views)" for ch, cnt in channels[:300])
    title_sample = "\n".join(f"- {t}" for t in sample_titles[:200])
    system = (
        f"You are a YouTube watch-history analyst. Propose between 5 and {max_themes} "
        "MECE theme categories. HARD CONSTRAINT: produce AT MOST "
        f"{max_themes} themes; consolidate aggressively; this must be consumable at a glance. "
        'Return ONLY a JSON object: {"themes": [{"name": str, "description": str}, ...]}'
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Top channels:\n{channel_lines}\n\nSample titles:\n{title_sample}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=1024,
        temperature=0.2,
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = parsed.get("themes", list(parsed.values())[0] if parsed else [])
    return (parsed if isinstance(parsed, list) else [])[:max_themes]


def classify_channels(
    channels: list[str],
    themes: list[dict],
    client: AzureOpenAI,
    model: str,
    batch_size: int = 50,
) -> dict[str, str]:
    """Stage B — classify each channel into exactly one theme (batched)."""
    theme_names = [t["name"] for t in themes]
    theme_list = ", ".join(f'"{n}"' for n in theme_names)
    fallback = theme_names[0] if theme_names else "Other"
    mapping: dict[str, str] = {}

    for i in range(0, len(channels), batch_size):
        batch = channels[i : i + batch_size]
        channel_lines = "\n".join(f"- {ch}" for ch in batch)
        system = (
            f"Classify each YouTube channel into EXACTLY ONE of: {theme_list}. "
            'Return ONLY JSON: {"Channel": "Theme", ...}'
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"Channels:\n{channel_lines}"},
                ],
                response_format={"type": "json_object"},
                max_tokens=2048,
                temperature=0.0,
            )
            batch_map: dict = json.loads(resp.choices[0].message.content or "{}")
            for ch in batch:
                theme = batch_map.get(ch, fallback)
                mapping[ch] = theme if theme in theme_names else fallback
        except Exception as exc:  # noqa: BLE001
            log.warning("Classification batch %d failed: %s", i // batch_size, exc)
            for ch in batch:
                mapping[ch] = fallback

    return mapping


def rollup_themes(channel_counts: Counter, channel_to_theme: dict[str, str]) -> Counter:
    result: Counter = Counter()
    for ch, cnt in channel_counts.items():
        result[channel_to_theme.get(ch, "Other")] += cnt
    return result


# ---------------------------------------------------------------------------
# Trend + rewatched
# ---------------------------------------------------------------------------


def compute_year_trend(
    entries: list[WatchEntry], channel_to_theme: dict[str, str]
) -> dict[int, Counter]:
    by_year: dict[int, Counter] = defaultdict(Counter)
    for e in entries:
        by_year[e.timestamp.year][channel_to_theme.get(e.channel, "Other")] += 1
    return dict(by_year)


def find_rewatched(entries: list[WatchEntry], min_count: int = 3) -> list[dict]:
    id_entries: dict[str, WatchEntry] = {}
    id_counts: Counter = Counter()
    for e in entries:
        if e.video_id is None:
            continue
        id_counts[e.video_id] += 1
        id_entries[e.video_id] = e
    return [
        {
            "video_id": vid,
            "title": id_entries[vid].title,
            "channel": id_entries[vid].channel,
            "count": cnt,
        }
        for vid, cnt in id_counts.most_common()
        if cnt >= min_count
    ]


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _bar(pct: float, width: int = 8) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def render_report(
    *,
    entries: list[WatchEntry],
    channel_counts: Counter,
    themes: list[dict],
    theme_counts: Counter,
    channel_to_theme: dict[str, str],
    year_trend: dict[int, Counter],
    rewatched: list[dict],
    since: date,
    until: date,
    max_themes: int,
) -> str:
    total = sum(channel_counts.values())
    ts_list = [e.timestamp for e in entries]
    actual_start = min(ts_list).date() if ts_list else since
    actual_end = max(ts_list).date() if ts_list else until

    lines: list[str] = []
    lines += [
        f"# YouTube Watch Recap: {since} → {until}",
        "",
        "## Coverage",
        "",
        f"- **Events**: {total:,}",
        f"- **Actual date range**: {actual_start} → {actual_end}",
        f"- **Unique channels**: {len(channel_counts):,}",
        "",
    ]

    lines += [
        "## Top 15 Channels",
        "",
        "| # | Channel | Watches | Share |",
        "|---|---------|---------|-------|",
    ]
    for rank, (ch, cnt) in enumerate(channel_counts.most_common(15), 1):
        pct = cnt / total * 100 if total else 0
        lines.append(f"| {rank} | {ch} | {cnt:,} | {pct:.1f}% |")
    lines.append("")

    theme_total = sum(theme_counts.values())
    lines.append(f"## Themes (≤{max_themes})")
    lines.append("")
    for theme in themes:
        name = theme["name"]
        cnt = theme_counts.get(name, 0)
        pct = cnt / theme_total * 100 if theme_total else 0
        lines += [
            f"### {name}",
            f"{_bar(pct)} **{pct:.1f}%** ({cnt:,} watches)",
        ]
        if theme.get("description"):
            lines.append(f"*{theme['description']}*")
        theme_titles = [e.title for e in entries if channel_to_theme.get(e.channel) == name]
        kw = extract_keywords(theme_titles, top_n=5)
        if kw:
            lines.append(f"Keywords: {', '.join(w for w, _ in kw)}")
        lines.append("")

    years = sorted(year_trend.keys())
    if years:
        theme_names = [t["name"] for t in themes]
        lines += [
            "## Watch Trend by Year",
            "",
            "| Year | Total | " + " | ".join(theme_names) + " |",
            "|------|-------|" + "|".join(["-------"] * len(theme_names)) + "|",
        ]
        for yr in years:
            yr_counts = year_trend[yr]
            yr_total = sum(yr_counts.values())
            cells = [
                f"{yr_counts.get(tn, 0) / yr_total * 100:.0f}%" if yr_total else "0%"
                for tn in theme_names
            ]
            lines.append(f"| {yr} | {yr_total:,} | " + " | ".join(cells) + " |")
        lines.append("")

    if rewatched:
        lines += [
            "## Most-Rewatched Videos (≥3×)",
            "",
            "| Count | Title | Channel |",
            "|-------|-------|---------|",
        ]
        for item in rewatched:
            lines.append(f"| {item['count']} | {item['title']} | {item['channel']} |")
        lines.append("")

    lines += [
        "## Caveats",
        "",
        "- History only covers periods when YouTube Watch History was enabled.",
        "- Counts represent videos **opened**, not necessarily finished.",
        "- Shorts and autoplay may inflate watch counts for certain channels.",
        "- Timezone: timestamps reflect the Takeout export (usually local time).",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_recap(
    since: date,
    until: date,
    history_file: str | None = None,
    max_themes: int = 8,
) -> Path:
    html_bytes = _load_html(history_file)
    log.info("Parsing watch history HTML (%d bytes)…", len(html_bytes))
    all_entries = parse_watch_history(html_bytes)
    log.info("Parsed %d total entries", len(all_entries))

    entries = filter_by_date(all_entries, since, until)
    log.info("Filtered to %d entries in [%s, %s]", len(entries), since, until)
    if not entries:
        print("No watch entries found in the specified date range.")
        return Path()

    channel_counts = aggregate_channels(entries)
    client = _get_client()
    model = _get_model()

    print("Discovering themes via LLM…")
    themes = discover_themes(
        channel_counts.most_common(300),
        [e.title for e in entries[:500]],
        max_themes,
        client,
        model,
    )
    if not themes:
        themes = [{"name": "General", "description": "All content"}]
    themes = themes[:max_themes]

    print(f"Classifying {len(channel_counts)} channels into {len(themes)} themes…")
    channel_to_theme = classify_channels(list(channel_counts.keys()), themes, client, model)
    theme_counts = rollup_themes(channel_counts, channel_to_theme)

    log.info("Top 20 keywords: %s", extract_keywords([e.title for e in entries], top_n=20))

    year_trend = compute_year_trend(entries, channel_to_theme)
    rewatched = find_rewatched(entries, min_count=3)

    report_text = render_report(
        entries=entries,
        channel_counts=channel_counts,
        themes=themes,
        theme_counts=theme_counts,
        channel_to_theme=channel_to_theme,
        year_trend=year_trend,
        rewatched=rewatched,
        since=since,
        until=until,
        max_themes=max_themes,
    )

    reports_dir = Path(__file__).parent.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    out_path = reports_dir / f"youtube-recap_{since}_{until}.md"
    out_path.write_text(report_text, encoding="utf-8")

    print(f"\nReport written to: {out_path}")
    top_themes = sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    top_theme_str = ", ".join(f"{n} ({c:,})" for n, c in top_themes)
    print(f"Entries: {len(entries):,}  |  Channels: {len(channel_counts):,}  |  Themes: {len(themes)}")
    print(f"Top themes: {top_theme_str}")
    if rewatched:
        print(f"Rewatched (>=3x): {len(rewatched)} videos")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(s: str) -> date:
    if s.lower() == "today":
        return date.today()
    return date.fromisoformat(s)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="YouTube watch-history recap")
    parser.add_argument("--since", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="End date YYYY-MM-DD or 'today'")
    parser.add_argument("--history-file", default=None, help="Path to .html or .zip")
    parser.add_argument("--max-themes", type=int, default=8)
    args = parser.parse_args()
    run_recap(
        _parse_date(args.since),
        _parse_date(args.until),
        history_file=args.history_file,
        max_themes=args.max_themes,
    )


if __name__ == "__main__":
    main()
