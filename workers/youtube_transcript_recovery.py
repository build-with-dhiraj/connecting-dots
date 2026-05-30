"""YouTube transcript recovery worker.

Triage notes whose `raw_meta.transcript_unavailable == true`, check caption
availability via the YouTube Data API, then attempt to recover the transcript
with youtube-transcript-api.  Recovered transcripts are written back to the
note body; a one-sentence Azure OpenAI tldr is appended to the frontmatter.

Commands:
    triage   — scan vault, batch-check caption flag, stamp checked_at
    recover  — try to fetch transcripts for triaged candidates
    run      — daemon: triage once, recover until exhausted

Flags (all commands):
    --dry-run          write nothing
    --limit N          stop after N recoveries (recover / run only)
    --max-per-run N    synonym for --limit
    --delay F          override YT_TRANSCRIPT_DELAY_S
    --vault VAULT_DIR  override VAULT_ROOT env / default

Env:
    VAULT_ROOT                default vault directory
    YT_TRANSCRIPT_DELAY_S     base sleep between transcript fetches (default 4)
    YT_TRANSCRIPT_JITTER_S    max random jitter on top (default 2)
    AZURE_OPENAI_ENDPOINT     required for tldr generation
    AZURE_OPENAI_API_KEY      required for tldr generation
    AZURE_OPENAI_API_VERSION  optional (defaults to 2024-10-21)
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_TOKEN_PATH = Path.home() / ".youtube-mcp" / "token.json"
_SECRET_PATH = Path.home() / ".youtube-mcp" / "client_secret.json"

_DEFAULT_VAULT = Path(os.environ.get("VAULT_ROOT", Path(__file__).parent.parent / "vault"))
_DEFAULT_DELAY = float(os.environ.get("YT_TRANSCRIPT_DELAY_S", "4"))
_DEFAULT_JITTER = float(os.environ.get("YT_TRANSCRIPT_JITTER_S", "2"))

_PREFERRED_LANGS: tuple[str, ...] = ("en",)

# Batch size for videos.list — YouTube API cap is 50 per request.
_CAPTION_BATCH_SIZE = 50

# How many consecutive NoTranscript / block-like errors on caption-true notes
# before we enter exponential backoff (and pause further attempts).
_BLOCK_THRESHOLD = 3
_BACKOFF_BASE_S = 30.0
_BACKOFF_MAX_S = 600.0


# ---------------------------------------------------------------------------
# Stable frontmatter key order — must match lib/vault_writer/writer.py
# ---------------------------------------------------------------------------
_FRONTMATTER_ORDER = (
    "source",
    "handler",
    "captured_at",
    "url",
    "title",
    "tags",
    "entities",
    "topics",
    "labels",
    "raw_meta",
)


# ---------------------------------------------------------------------------
# Auth / YouTube API client (reuse pattern from youtube_api_sync.py)
# ---------------------------------------------------------------------------

class InsufficientScopesError(RuntimeError):
    """Raised when the stored token lacks the required OAuth scopes."""


def _load_credentials():
    """Load and (if needed) refresh OAuth2 credentials."""
    from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]
    from google.auth.transport.requests import Request  # type: ignore[import-not-found]

    if not _TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Token not found at {_TOKEN_PATH}. "
            "Run the YouTube MCP OAuth flow to generate a token first."
        )
    if not _SECRET_PATH.exists():
        raise FileNotFoundError(
            f"Client secret not found at {_SECRET_PATH}. "
            "Download it from Google Cloud Console (Desktop OAuth app)."
        )

    token_data = __import__("json").loads(_TOKEN_PATH.read_text())
    secret_data = __import__("json").loads(_SECRET_PATH.read_text())
    installed = secret_data.get("installed") or secret_data.get("web") or {}

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id") or installed.get("client_id"),
        client_secret=token_data.get("client_secret") or installed.get("client_secret"),
        scopes=token_data.get("scopes"),
    )

    stored_scopes = token_data.get("scopes") or []
    if stored_scopes and not any("youtube" in s for s in stored_scopes):
        raise InsufficientScopesError(
            f"Stored token scopes {stored_scopes!r} do not include youtube.readonly."
        )

    if creds.expired and creds.refresh_token:
        logger.info("[transcript-recovery] refreshing expired token")
        creds.refresh(Request())
        updated = {**token_data, "token": creds.token}
        _TOKEN_PATH.write_text(__import__("json").dumps(updated, indent=2))

    return creds


def _build_client(creds):
    from googleapiclient.discovery import build  # type: ignore[import-not-found]
    return build("youtube", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Frontmatter helpers (reuse pattern from ner_backfill.py)
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return (frontmatter_dict, body).  Returns (None, text) if malformed."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    raw_fm = text[4:end]
    body = text[end + 5:]
    try:
        fm = yaml.safe_load(raw_fm) or {}
    except yaml.YAMLError:
        return None, text
    if not isinstance(fm, dict):
        return None, text
    return fm, body


def _ordered(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _FRONTMATTER_ORDER:
        if k in meta:
            out[k] = meta[k]
    for k in sorted(meta):
        if k not in out:
            out[k] = meta[k]
    return out


def _write_note_atomic(path: Path, fm: dict[str, Any], body: str) -> None:
    """Atomic frontmatter+body rewrite using tmp+rename."""
    serialized_fm = yaml.safe_dump(
        _ordered(fm),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    content = f"---\n{serialized_fm}\n---\n{body}"
    if not content.endswith("\n"):
        content += "\n"

    parent = path.parent
    fd, tmp = tempfile.mkstemp(prefix=".tmp-transcript-recovery-", suffix=".md", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.rename(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Vault walking
# ---------------------------------------------------------------------------

def _iter_youtube_notes(vault_root: Path):
    """Yield every .md note under vault/sources/youtube/ in stable order."""
    yt_dir = vault_root / "sources" / "youtube"
    if not yt_dir.exists():
        logger.warning("[transcript-recovery] youtube sources dir not found: %s", yt_dir)
        return
    for path in sorted(yt_dir.rglob("*.md")):
        yield path


# ---------------------------------------------------------------------------
# transcript-api helpers (reuse from connecting_dots/handlers/youtube.py)
# ---------------------------------------------------------------------------

def _fetch_transcript(
    video_id: str,
    preferred_languages: tuple[str, ...] = _PREFERRED_LANGS,
) -> tuple[list[dict[str, Any]], str] | None:
    """Try preferred languages then fall back to any available transcript.

    Returns (snippets, language_code) or None on total failure.
    Propagates TranscriptsDisabled to distinguish hard absence from IP blocks.
    """
    from youtube_transcript_api import (  # type: ignore[import-not-found]
        NoTranscriptFound,
        TranscriptsDisabled,
        YouTubeTranscriptApi,
    )

    def _snippets_from_fetched(fetched: Any) -> list[dict[str, Any]]:
        if hasattr(fetched, "snippets"):
            return [
                {
                    "text": s.text,
                    "start": float(getattr(s, "start", 0.0)),
                    "duration": float(getattr(s, "duration", 0.0)),
                }
                for s in fetched.snippets
            ]
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
        return getattr(fetched, "language_code", None) or fallback

    api = YouTubeTranscriptApi()

    try:
        fetched = api.fetch(video_id, languages=preferred_languages)
        return _snippets_from_fetched(fetched), _fetched_language(fetched)
    except NoTranscriptFound:
        pass
    except TranscriptsDisabled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transcript-recovery] primary fetch failed for %s: %s", video_id, exc)

    try:
        transcript_list = api.list(video_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transcript-recovery] transcript listing failed for %s: %s", video_id, exc)
        return None

    for transcript in transcript_list:
        try:
            fetched = transcript.fetch()
            return _snippets_from_fetched(fetched), getattr(transcript, "language_code", "unknown")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[transcript-recovery] variant fetch failed: %s", exc)
            continue
    return None


def _join_snippets(snippets: list[dict[str, Any]], max_chars: int = 600) -> str:
    """Merge snippets into readable paragraphs (mirrors handler logic)."""
    paragraphs: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for snip in snippets:
        text = (snip.get("text") or "").strip()
        if not text:
            continue
        buf.append(text)
        buf_len += len(text) + 1
        if buf_len >= max_chars and text.endswith((".", "?", "!", "…")):
            paragraphs.append(" ".join(buf))
            buf = []
            buf_len = 0
    if buf:
        paragraphs.append(" ".join(buf))
    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# Azure OpenAI tldr (reuse _get_client from ner.py)
# ---------------------------------------------------------------------------

def _get_azure_client():
    """Build (or reuse) an AzureOpenAI client from env."""
    from openai import AzureOpenAI  # type: ignore[import-not-found]

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    return AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)


def _generate_tldr(title: str, body: str, client=None) -> str | None:
    """Generate a one-sentence tldr via gpt-4.1.  Returns None on failure."""
    try:
        api = client or _get_azure_client()
        model = (
            os.environ.get("NER_MODEL")
            or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
            or "gpt-4.1"
        )
        truncated = body[:4000]
        response = api.chat.completions.create(
            model=model,
            max_tokens=100,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise summarizer. Given a video title and transcript, "
                        "produce exactly ONE sentence (under 30 words) summarising the core idea."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Title: {title or '(untitled)'}\nTranscript: {truncated}",
                },
            ],
        )
        choices = getattr(response, "choices", None) or []
        if choices:
            message = getattr(choices[0], "message", None)
            if message:
                return (getattr(message, "content", None) or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transcript-recovery] tldr generation failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Phase A — Triage
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def triage(
    vault_root: Path,
    *,
    client=None,
    dry_run: bool = False,
) -> tuple[list[tuple[Path, dict, str]], list[tuple[Path, dict, str]]]:
    """Scan vault, batch-check caption flag, partition into (caption_true, caption_false).

    Returns:
        (caption_true_list, caption_false_list) where each entry is
        (path, frontmatter, video_id).
    """
    # Collect candidates
    candidates: list[tuple[Path, dict, str]] = []
    for path in _iter_youtube_notes(vault_root):
        text = path.read_text(encoding="utf-8")
        fm, _body = _split_frontmatter(text)
        if fm is None:
            continue
        raw_meta = fm.get("raw_meta") or {}
        if not isinstance(raw_meta, dict):
            continue
        if not raw_meta.get("transcript_unavailable"):
            continue
        video_id = raw_meta.get("video_id") or ""
        if not video_id:
            logger.debug("[triage] skipping note with no video_id: %s", path.name)
            continue
        candidates.append((path, fm, video_id))

    logger.info("[triage] found %d transcript-unavailable notes", len(candidates))

    if not candidates:
        return [], []

    # Build a client only if we have something to check
    if client is None:
        try:
            creds = _load_credentials()
            client = _build_client(creds)
        except Exception as exc:
            logger.error("[triage] cannot build YouTube client: %s", exc)
            return [], []

    # Batch-check caption availability (≤50 IDs per call)
    caption_map: dict[str, bool] = {}
    all_ids = [vid for _, _, vid in candidates]

    for i in range(0, len(all_ids), _CAPTION_BATCH_SIZE):
        batch = all_ids[i: i + _CAPTION_BATCH_SIZE]
        id_param = ",".join(batch)
        try:
            resp = client.videos().list(
                part="contentDetails", id=id_param, maxResults=50
            ).execute()
            for item in resp.get("items") or []:
                vid = item.get("id") or ""
                cd = (item.get("contentDetails") or {})
                caption_flag = str(cd.get("caption", "false")).lower() == "true"
                caption_map[vid] = caption_flag
        except Exception as exc:  # noqa: BLE001
            logger.warning("[triage] videos.list batch failed: %s", exc)

    caption_true: list[tuple[Path, dict, str]] = []
    caption_false: list[tuple[Path, dict, str]] = []
    now = _now_iso()

    for path, fm, video_id in candidates:
        has_captions = caption_map.get(video_id)  # None = API didn't return this video

        if has_captions is True:
            caption_true.append((path, fm, video_id))
        else:
            # caption=false or video not found — low priority
            caption_false.append((path, fm, video_id))

        if not dry_run:
            raw_meta = fm.get("raw_meta") or {}
            raw_meta["transcript_recovery_checked_at"] = now
            fm["raw_meta"] = raw_meta
            text = path.read_text(encoding="utf-8")
            _fm, body = _split_frontmatter(text)
            _write_note_atomic(path, fm, body)

    logger.info(
        "[triage] caption-true=%d  caption-false=%d  (dry_run=%s)",
        len(caption_true),
        len(caption_false),
        dry_run,
    )
    return caption_true, caption_false


# ---------------------------------------------------------------------------
# Phase B — Recover
# ---------------------------------------------------------------------------

def recover(
    candidates: list[tuple[Path, dict, str]],
    *,
    caption_true: bool = True,
    dry_run: bool = False,
    delay_s: float = _DEFAULT_DELAY,
    jitter_s: float = _DEFAULT_JITTER,
    limit: int | None = None,
    azure_client=None,
) -> int:
    """Try to fetch and write transcripts for candidate notes.

    Args:
        candidates: list of (path, frontmatter, video_id).
        caption_true: whether these are caption-true candidates (affects backoff logic).
        dry_run: skip all writes.
        delay_s: base sleep between requests.
        jitter_s: max random jitter added to delay.
        limit: max recoveries; None = unlimited.
        azure_client: injected AzureOpenAI client for tests.

    Returns:
        Number of successfully recovered transcripts.
    """
    from youtube_transcript_api import (  # type: ignore[import-not-found]
        NoTranscriptFound,
        TranscriptsDisabled,
    )

    recovered = 0
    consecutive_blocks = 0
    backoff_s = _BACKOFF_BASE_S

    for path, _fm, video_id in candidates:
        if limit is not None and recovered >= limit:
            break

        # Re-read note fresh (a previous run may have updated it)
        text = path.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(text)
        if fm is None:
            continue

        raw_meta = fm.get("raw_meta") or {}
        if not isinstance(raw_meta, dict):
            continue

        # Idempotency guards
        if raw_meta.get("transcript_recovered_at"):
            logger.debug("[recover] already recovered, skipping: %s", path.name)
            continue
        if raw_meta.get("truly_no_captions"):
            logger.debug("[recover] marked truly_no_captions, skipping: %s", path.name)
            continue
        if not raw_meta.get("video_id"):
            logger.debug("[recover] no video_id, skipping: %s", path.name)
            continue

        # Throttle
        sleep_s = delay_s + random.uniform(0, jitter_s)
        logger.debug("[recover] sleeping %.1fs before fetch", sleep_s)
        time.sleep(sleep_s)

        try:
            result = _fetch_transcript(video_id)
        except TranscriptsDisabled:
            logger.info("[recover] TranscriptsDisabled for %s", video_id)
            result = None
            if not caption_true:
                # Hard disabled on caption-false → stamp truly_no_captions
                if not dry_run:
                    raw_meta["truly_no_captions"] = True
                    fm["raw_meta"] = raw_meta
                    _write_note_atomic(path, fm, body)
            # For caption-true: don't stamp; treat like a block
            if caption_true:
                consecutive_blocks += 1
        except NoTranscriptFound:
            logger.info("[recover] NoTranscriptFound for %s", video_id)
            result = None
            if caption_true:
                consecutive_blocks += 1
            else:
                # caption-false: confirmed no transcript
                if not dry_run:
                    raw_meta["truly_no_captions"] = True
                    fm["raw_meta"] = raw_meta
                    _write_note_atomic(path, fm, body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[recover] unexpected error for %s: %s", video_id, exc)
            result = None
            if caption_true:
                consecutive_blocks += 1

        else:
            if result is None:
                if caption_true:
                    consecutive_blocks += 1
                else:
                    # caption-false with no result → truly no captions
                    if not dry_run:
                        raw_meta["truly_no_captions"] = True
                        fm["raw_meta"] = raw_meta
                        _write_note_atomic(path, fm, body)
            else:
                consecutive_blocks = 0
                backoff_s = _BACKOFF_BASE_S  # reset after success
                snippets, _lang = result

                transcript_text = _join_snippets(snippets)
                new_body = transcript_text + "\n"

                now = _now_iso()
                raw_meta.pop("transcript_unavailable", None)
                raw_meta.pop("reason", None)
                raw_meta["transcript_recovered_at"] = now
                fm["raw_meta"] = raw_meta

                # Generate tldr
                title = fm.get("title") or ""
                tldr = _generate_tldr(title, transcript_text, client=azure_client)
                if tldr:
                    fm["tldr"] = tldr
                    raw_meta["tldr_generated_at"] = now

                if not dry_run:
                    _write_note_atomic(path, fm, new_body)
                    logger.info("[recover] recovered transcript for %s (%s)", video_id, path.name)
                else:
                    logger.info("[recover] [dry-run] would recover %s", video_id)

                recovered += 1

        # Exponential backoff if we're seeing block-like behaviour on caption-true
        if caption_true and consecutive_blocks >= _BLOCK_THRESHOLD:
            logger.warning(
                "[recover] %d consecutive failures on caption-true notes — "
                "backing off %.0fs (possible IP block)",
                consecutive_blocks,
                backoff_s,
            )
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, _BACKOFF_MAX_S)
            # Do NOT reset consecutive_blocks here — we keep tracking across the pause.

    return recovered


# ---------------------------------------------------------------------------
# Phase C is embedded in recover() above (tldr generated per recovered note)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="YouTube transcript recovery worker")
    p.add_argument(
        "command",
        choices=["triage", "recover", "run"],
        help="triage: scan+check captions; recover: fetch transcripts; run: both",
    )
    p.add_argument("--dry-run", action="store_true", help="Write nothing")
    p.add_argument("--limit", "--max-per-run", type=int, default=None, dest="limit",
                   help="Stop after N recoveries")
    p.add_argument("--delay", type=float, default=None,
                   help="Base sleep between transcript fetches (seconds)")
    p.add_argument("--vault", type=Path, default=None,
                   help="Override vault root directory")
    return p.parse_args(argv)


def _run_triage(args, vault_root: Path, client=None):
    ct, cf = triage(vault_root, client=client, dry_run=args.dry_run)
    print(
        f"[triage] caption-true candidates: {len(ct)}, "
        f"caption-false candidates: {len(cf)}"
    )
    return ct, cf


def _run_recover(args, caption_true, caption_false):
    delay_s = args.delay if args.delay is not None else _DEFAULT_DELAY
    n = 0
    if caption_true:
        n += recover(
            caption_true,
            caption_true=True,
            dry_run=args.dry_run,
            delay_s=delay_s,
            limit=args.limit,
        )
    remaining_limit = None if args.limit is None else max(0, args.limit - n)
    if caption_false and (remaining_limit is None or remaining_limit > 0):
        n += recover(
            caption_false,
            caption_true=False,
            dry_run=args.dry_run,
            delay_s=delay_s,
            limit=remaining_limit,
        )
    print(f"[recover] recovered {n} transcripts")
    return n


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    args = _parse_args(argv)
    vault_root = args.vault or _DEFAULT_VAULT

    if args.command == "triage":
        _run_triage(args, vault_root)

    elif args.command == "recover":
        # Re-triage to get fresh candidate lists; then recover
        ct, cf = _run_triage(args, vault_root)
        _run_recover(args, ct, cf)

    elif args.command == "run":
        # Daemon: triage once, recover in batches until exhausted
        ct, cf = _run_triage(args, vault_root)
        while ct or cf:
            n = _run_recover(args, ct, cf)
            if n == 0:
                logger.info("[run] no progress, exiting")
                break
            # Re-triage to pick up any newly-checkable notes
            ct, cf = _run_triage(args, vault_root)


if __name__ == "__main__":
    main()
