"""YouTube URL handler — transcript + metadata extractor.

Resolves a captured YouTube URL into a `NoteRecord` containing:
    - title from the video metadata (via yt-dlp; falls back to "YouTube video <id>"
      if metadata fetch fails)
    - text = transcript joined into readable paragraphs
    - raw_meta = {channel, duration_s, language, view_count, upload_date, video_id}

When the transcript is unavailable (disabled, none-in-requested-language, age-restricted,
IP-blocked) the handler degrades gracefully: `text=""` and `raw_meta["transcript_unavailable"]=True`
with a `reason` string. The dispatcher still gets a usable NoteRecord.

Language preference:
    1. Channel language hint from yt-dlp metadata (if present)
    2. English ("en")
    3. Any available transcript (via list() + first available)
    Auto-translated transcripts are accepted.

Out of scope: NER, embedding, vault writing.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from connecting_dots.generated.inbound_envelope import InboundEnvelope
from connecting_dots.types import NoteRecord

logger = logging.getLogger(__name__)

# Accept every YouTube URL flavor we've seen in capture:
#   - youtube.com/watch?v=ID
#   - youtu.be/ID
#   - youtube.com/shorts/ID
#   - youtube.com/embed/ID
#   - youtube.com/v/ID
#   - m.youtube.com/... (mobile)
#   - music.youtube.com/...
# Host match deliberately broad; ID extraction is the strict gate.
_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

# 11-character YouTube video ID (letters, digits, _, -)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Paths under youtube.com that carry a video ID as the FIRST path segment
# after the keyword (e.g. /shorts/<id>, /embed/<id>, /v/<id>, /live/<id>).
_PATH_KEYWORDS = ("shorts", "embed", "v", "live")


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char video ID out of any YouTube URL flavor.

    Returns None if `url` is not a YouTube URL or has no extractable ID.
    """
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return None

    host = (parsed.hostname or "").lower()
    if host not in _YT_HOSTS:
        return None

    # youtu.be/<id>
    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    # /watch?v=<id>
    if parsed.path in ("/watch", "/watch/"):
        v = parse_qs(parsed.query).get("v", [""])[0]
        return v if _VIDEO_ID_RE.match(v) else None

    # /shorts/<id>, /embed/<id>, /v/<id>, /live/<id>
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) >= 2 and segments[0] in _PATH_KEYWORDS:
        candidate = segments[1]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    return None


def _join_snippets_into_paragraphs(snippets: list[dict[str, Any]], max_chars: int = 600) -> str:
    """Merge transcript snippets into readable paragraphs.

    Breaks paragraphs on sentence-ending punctuation when the accumulated
    paragraph exceeds `max_chars`, so downstream readers/embedders see
    natural prose instead of one-line-per-caption.
    """
    paragraphs: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for snip in snippets:
        text = (snip.get("text") or "").strip()
        if not text:
            continue
        buf.append(text)
        buf_len += len(text) + 1
        if buf_len >= max_chars and text.endswith((".", "?", "!", "…", "。", "？", "！")):
            paragraphs.append(" ".join(buf))
            buf = []
            buf_len = 0
    if buf:
        paragraphs.append(" ".join(buf))
    return "\n\n".join(paragraphs)


def _snippets_from_fetched(fetched: Any) -> list[dict[str, Any]]:
    """Normalize a `FetchedTranscript` (youtube-transcript-api 1.x) into a list of
    plain dicts. Also tolerates the legacy 0.x list-of-dicts shape for fixture tests.
    """
    # youtube-transcript-api >= 1.0 returns FetchedTranscript with .snippets
    if hasattr(fetched, "snippets"):
        return [
            {
                "text": s.text,
                "start": float(getattr(s, "start", 0.0)),
                "duration": float(getattr(s, "duration", 0.0)),
            }
            for s in fetched.snippets
        ]
    # Legacy / fixture: already a list of dicts
    if isinstance(fetched, list):
        return [
            {
                "text": s.get("text", ""),
                "start": float(s.get("start", 0.0)),
                "duration": float(s.get("duration", 0.0)),
            }
            for s in fetched
        ]
    raise TypeError(f"Unrecognized transcript shape: {type(fetched)!r}")


def _fetched_language(fetched: Any, fallback: str = "en") -> str:
    """Extract the language code from a FetchedTranscript (1.x) or default."""
    return getattr(fetched, "language_code", None) or fallback


def _fetch_metadata(video_id: str) -> dict[str, Any]:
    """Pull title/channel/duration/etc via yt-dlp (no download).

    Returns an empty dict on failure — the handler degrades to a synthetic title
    rather than crashing.
    """
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("yt-dlp not installed; metadata will be empty")
        return {}

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception as exc:  # noqa: BLE001 — yt-dlp raises a wide variety of errors
        logger.warning("yt-dlp metadata fetch failed for %s: %s", video_id, exc)
        return {}

    if not info:
        return {}

    return {
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration_s": info.get("duration"),
        "language": info.get("language") or "",
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
    }


def _fetch_transcript(
    video_id: str,
    preferred_languages: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str] | None:
    """Try the preferred languages in order, then fall back to any available
    transcript. Returns (snippets, language_code) or None on total failure.
    """
    # Local import so tests can monkeypatch the module-level YouTubeTranscriptApi
    # without paying the import cost when fixtures stub _fetch_transcript directly.
    from youtube_transcript_api import (  # type: ignore[import-not-found]
        NoTranscriptFound,
        TranscriptsDisabled,
        YouTubeTranscriptApi,
    )

    api = YouTubeTranscriptApi()

    # First try the preferred-language path
    try:
        fetched = api.fetch(video_id, languages=preferred_languages)
        return _snippets_from_fetched(fetched), _fetched_language(fetched)
    except NoTranscriptFound:
        pass
    except TranscriptsDisabled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("primary transcript fetch failed for %s: %s", video_id, exc)

    # Fall back to listing + first translatable/auto-generated
    try:
        transcript_list = api.list(video_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("transcript listing failed for %s: %s", video_id, exc)
        return None

    for transcript in transcript_list:
        try:
            fetched = transcript.fetch()
            return _snippets_from_fetched(fetched), getattr(transcript, "language_code", "unknown")
        except Exception as exc:  # noqa: BLE001
            logger.debug("transcript variant fetch failed: %s", exc)
            continue
    return None


class YouTubeHandler:
    """Handler for any YouTube video URL flavor.

    See module docstring for behavior, language preference, and degradation rules.
    """

    name = "youtube"

    def matches(self, url: str) -> bool:
        return extract_video_id(url) is not None

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        url = str(envelope.url)
        video_id = extract_video_id(url)
        if video_id is None:
            # matches() should have gated this — fail loud rather than silently degrade.
            raise ValueError(f"YouTubeHandler.handle called with non-YouTube URL: {url}")

        meta = _fetch_metadata(video_id)
        channel_lang = (meta.get("language") or "").split("-")[0].strip().lower() or None

        # Build the language preference list: channel language → English → done.
        preferred: list[str] = []
        if channel_lang and channel_lang != "en":
            preferred.append(channel_lang)
        preferred.append("en")

        title = meta.get("title") or f"YouTube video {video_id}"
        raw_meta: dict[str, Any] = {
            "video_id": video_id,
            "channel": meta.get("channel", ""),
            "duration_s": meta.get("duration_s"),
            "language": meta.get("language", ""),
            "view_count": meta.get("view_count"),
            "upload_date": meta.get("upload_date"),
        }

        try:
            result = _fetch_transcript(video_id, tuple(preferred))
        except Exception as exc:  # noqa: BLE001 — captures TranscriptsDisabled, IpBlocked, etc.
            logger.info("transcript unavailable for %s: %s", video_id, exc)
            raw_meta["transcript_unavailable"] = True
            raw_meta["reason"] = type(exc).__name__
            return NoteRecord(
                source=envelope.source.value,
                handler=self.name,
                url=url,
                title=title,
                text="",
                captured_at=envelope.captured_at,
                raw_meta=raw_meta,
            )

        if result is None:
            raw_meta["transcript_unavailable"] = True
            raw_meta["reason"] = "NoTranscriptFound"
            return NoteRecord(
                source=envelope.source.value,
                handler=self.name,
                url=url,
                title=title,
                text="",
                captured_at=envelope.captured_at,
                raw_meta=raw_meta,
            )

        snippets, language_code = result
        text = _join_snippets_into_paragraphs(snippets)
        # Prefer the actually-fetched language over yt-dlp's guess (more accurate
        # for auto-translated transcripts).
        if language_code:
            raw_meta["language"] = language_code

        return NoteRecord(
            source=envelope.source.value,
            handler=self.name,
            url=url,
            title=title,
            text=text,
            captured_at=envelope.captured_at,
            raw_meta=raw_meta,
        )


# Module-level singleton — handlers are stateless, dispatcher imports this.
youtube_handler = YouTubeHandler()
