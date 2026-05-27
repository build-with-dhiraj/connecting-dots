"""LinkedIn data-export ZIP watcher.

LinkedIn does not expose a "saved items" API. The user manually requests a
data export once a month (Settings & Privacy → Data Privacy → Get a copy of
your data → pick "Saved Articles" + "Activity" + "Reactions" → 24h SLA),
downloads the resulting ZIP, and drops it into a watched folder. This worker:

1. Polls `LINKEDIN_INBOX_DIR` (default `data/linkedin-inbox`) for new `.zip`s.
2. Verifies the archive looks like a LinkedIn export (top-level CSVs with the
   expected names — column names read at runtime to survive minor schema drift).
3. Extracts to `<inbox>/.unpacked/<utc-timestamp>/`.
4. Parses `Saved Articles.csv` (intent saves) and `Reactions.csv` (likes — a
   softer signal). Column headers are detected at runtime; both files use
   different conventions between exports.
5. Builds one `dispatch_url` call per row, with a synthetic deterministic
   `message_id = "linkedin:<sha256(url|captured_at)>"` so re-importing the
   same ZIP is idempotent (the stream consumer's `seen_message_ids` dedupe
   table absorbs the replay).
6. Moves processed ZIPs to `<inbox>/.processed/`.

Polling not inotify: kqueue/FSEvents on macOS has notification quirks for
moved-in files and the cadence here is monthly, so a 60-second `os.scandir`
beat is both simpler and easier to test.

Subcommands (mirrors `workers.mailto_poller`):
    python -m workers.linkedin_zip_watcher          # daemon mode (default 60s)
    python -m workers.linkedin_zip_watcher once     # process whatever's there, exit
    python -m workers.linkedin_zip_watcher run      # alias of daemon mode

Env vars:
    LINKEDIN_INBOX_DIR       default: data/linkedin-inbox
    LINKEDIN_POLL_INTERVAL_S default: 60
    LOG_LEVEL                default: INFO
"""
from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import shutil
import signal
import sys
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from connecting_dots.dispatcher import dispatch_url

logger = logging.getLogger(__name__)


# --- env / paths -------------------------------------------------------------

DEFAULT_INBOX = Path("data/linkedin-inbox")
DEFAULT_POLL_INTERVAL_S = 60


def _inbox_dir() -> Path:
    return Path(os.environ.get("LINKEDIN_INBOX_DIR", str(DEFAULT_INBOX)))


def _poll_interval() -> int:
    raw = os.environ.get("LINKEDIN_POLL_INTERVAL_S", str(DEFAULT_POLL_INTERVAL_S))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_POLL_INTERVAL_S


# --- LinkedIn export structure (verified against the 2024–2025 format) -------

# Filenames LinkedIn uses inside the export. Names have drifted historically
# ("Saved_Articles.csv" vs "Saved Articles.csv"); we match case- and
# separator-insensitively. The truthy presence of *any* of these signals the
# archive is a LinkedIn export.
_LINKEDIN_FILE_HINTS: tuple[str, ...] = (
    "saved articles",
    "saved_articles",
    "shares",
    "reactions",
    "comments",
    "following companies",
    "following_companies",
    "messages",
    "votes",
)

# Map normalized basenames -> row parser. We only ingest "intent" (saves) and
# "interest" (reactions). The other CSVs are ignored here — they're handled
# elsewhere in the pipeline (e.g. Comments → a future engagement signal).
SAVED_BASENAMES: frozenset[str] = frozenset({"saved articles", "saved_articles"})
REACTION_BASENAMES: frozenset[str] = frozenset({"reactions"})


def _normalize_filename(name: str) -> str:
    """Lowercase + strip extension for tolerant matching."""
    stem = Path(name).stem
    return stem.lower().strip()


def _is_linkedin_export(zf: zipfile.ZipFile) -> bool:
    norms = {_normalize_filename(n) for n in zf.namelist() if n.lower().endswith(".csv")}
    return any(hint in norms for hint in _LINKEDIN_FILE_HINTS)


# --- column resolution -------------------------------------------------------

# Each canonical field maps to a tuple of acceptable column-header aliases
# (case-insensitive, whitespace-tolerant). LinkedIn has shipped at least three
# header variants across years; resolving at runtime keeps us robust to drift.
_SAVED_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "captured_at": ("savedat", "saved at", "date", "saveddate"),
    "title": ("articletitle", "title", "article title", "name"),
    "url": ("articleurl", "url", "article url", "link"),
    "author": ("articleauthor", "author", "article author", "byline"),
}

_REACTION_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "captured_at": ("date", "reactedat", "reacted at"),
    "type": ("type", "reactiontype", "reaction"),
    "url": ("link", "url", "reactedonurl"),
}


def _resolve_columns(
    fieldnames: list[str] | None, aliases: dict[str, tuple[str, ...]]
) -> dict[str, str]:
    """Map canonical name -> actual header in the CSV. Missing keys omitted."""
    if not fieldnames:
        return {}
    norm = {h.strip().lower().replace("_", "").replace(" ", ""): h for h in fieldnames}
    resolved: dict[str, str] = {}
    for canonical, candidates in aliases.items():
        for cand in candidates:
            key = cand.replace("_", "").replace(" ", "").lower()
            if key in norm:
                resolved[canonical] = norm[key]
                break
    return resolved


# --- captured_at parsing -----------------------------------------------------

# LinkedIn timestamps appear in three flavours across exports:
#   "2024-03-14 10:42:11 UTC"   (saved articles)
#   "2024-03-14"                (some older reactions)
#   "Mar 14, 2024, 10:42 AM"    (locale-formatted, rare)
_ISO_DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_linkedin_datetime(value: str) -> datetime:
    """Parse a LinkedIn export timestamp; fall back to now() on garbage."""
    s = (value or "").strip()
    if not s:
        return datetime.now(timezone.utc)
    m = _ISO_DT_RE.match(s)
    if m:
        y, mo, d, h, mi, se = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, h, mi, se, tzinfo=timezone.utc)
        except ValueError:
            pass
    m = _ISO_DATE_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            pass
    # Last resort: locale parser. Don't crash on the row.
    for fmt in ("%b %d, %Y, %I:%M %p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.debug("[linkedin-zip] unparseable timestamp %r — falling back to now()", s)
    return datetime.now(timezone.utc)


# --- envelope construction ---------------------------------------------------


def _synthetic_message_id(url: str, captured_at: datetime) -> str:
    """Deterministic id so re-imports collapse via the existing dedupe DB."""
    h = hashlib.sha256()
    h.update(url.encode("utf-8"))
    h.update(b"|")
    h.update(captured_at.isoformat().encode("utf-8"))
    return f"linkedin:{h.hexdigest()}"


@dataclass
class _Row:
    url: str
    captured_at: datetime
    raw_payload: dict[str, Any]


def _emit(row: _Row, *, dispatch: Callable[..., None]) -> None:
    # Pass message_id explicitly so the dispatcher's dedupe table uses our
    # deterministic synthetic id (not a `hash(url)` fallback that would let
    # re-imports duplicate every row).
    dispatch(
        url=row.url,
        source="linkedin",
        captured_at=row.captured_at,
        raw_payload=row.raw_payload,
        message_id=row.raw_payload["message_id"],
    )


# --- CSV row iterators -------------------------------------------------------


def _iter_saved_articles(csv_path: Path) -> Iterator[_Row]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = _resolve_columns(reader.fieldnames, _SAVED_COLUMN_ALIASES)
        if "url" not in cols:
            logger.warning(
                "[linkedin-zip] %s: no URL column found (headers=%s) — skipping file",
                csv_path.name,
                reader.fieldnames,
            )
            return
        for row in reader:
            url = (row.get(cols["url"], "") or "").strip()
            if not url:
                continue
            captured_raw = row.get(cols.get("captured_at", ""), "") if cols.get("captured_at") else ""
            captured_at = _parse_linkedin_datetime(captured_raw)
            title = (row.get(cols.get("title", ""), "") or "").strip() if cols.get("title") else ""
            author = (row.get(cols.get("author", ""), "") or "").strip() if cols.get("author") else ""
            mid = _synthetic_message_id(url, captured_at)
            yield _Row(
                url=url,
                captured_at=captured_at,
                raw_payload={
                    "message_id": mid,
                    "linkedin_export": True,
                    "type": "saved",
                    "title": title,
                    "author": author,
                    "source_file": csv_path.name,
                },
            )


def _iter_reactions(csv_path: Path) -> Iterator[_Row]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = _resolve_columns(reader.fieldnames, _REACTION_COLUMN_ALIASES)
        if "url" not in cols:
            logger.warning(
                "[linkedin-zip] %s: no URL/link column found (headers=%s) — skipping file",
                csv_path.name,
                reader.fieldnames,
            )
            return
        for row in reader:
            url = (row.get(cols["url"], "") or "").strip()
            if not url:
                continue
            captured_raw = row.get(cols.get("captured_at", ""), "") if cols.get("captured_at") else ""
            captured_at = _parse_linkedin_datetime(captured_raw)
            rxn_type = (row.get(cols.get("type", ""), "") or "").strip() if cols.get("type") else ""
            mid = _synthetic_message_id(url, captured_at)
            yield _Row(
                url=url,
                captured_at=captured_at,
                raw_payload={
                    "message_id": mid,
                    "linkedin_export": True,
                    "type": "reaction",
                    "reaction": rxn_type,
                    "source_file": csv_path.name,
                },
            )


# --- ZIP processing ----------------------------------------------------------


# Zip-bomb caps. LinkedIn's real exports are well under 100 MB even for
# 10-year accounts; 500 MB total + 100 MB per-member is generous.
_MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
_MAX_PER_MEMBER_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
# Unix symlink mode bits live in the high half of `external_attr`.
_SYMLINK_MODE_BITS = 0o120000 << 16


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract `zf` to `dest`, refusing unsafe members.

    Refuses:
    - Path-traversal / absolute paths (zip-slip + sibling-directory bypass:
      `dest_evil/...` when dest is `dest` used to slip past a `startswith`
      check on stringified paths).
    - Symlink entries (LinkedIn exports never use them, and they're a
      well-known escape hatch around extract-time path checks).
    - Archives whose declared uncompressed size exceeds our caps (zip-bomb).
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    total_size = 0
    for member in zf.infolist():
        # Reject symlinks outright.
        if (member.external_attr & _SYMLINK_MODE_BITS) == _SYMLINK_MODE_BITS:
            raise RuntimeError(f"symlink entries are not allowed: {member.filename!r}")

        # Zip-bomb caps based on the archive's declared sizes.
        if member.file_size > _MAX_PER_MEMBER_UNCOMPRESSED_BYTES:
            raise RuntimeError(
                f"member exceeds per-file size cap: {member.filename!r} "
                f"({member.file_size} bytes)"
            )
        total_size += member.file_size
        if total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise RuntimeError(
                f"archive exceeds total uncompressed size cap "
                f"({total_size} bytes > {_MAX_TOTAL_UNCOMPRESSED_BYTES})"
            )

        # Reject absolute paths, parent-dir traversal, and sibling-directory
        # bypass (`dest_evil/...` when dest is `dest`). `is_relative_to` is
        # path-aware where `str.startswith` was not.
        member_path = (dest / member.filename).resolve()
        if not member_path.is_relative_to(dest):
            raise RuntimeError(f"unsafe path in archive: {member.filename!r}")

    zf.extractall(dest)


class UnprocessableZip(Exception):
    """Raised when a ZIP should be left in the inbox for inspection.

    Used for: malformed ZIPs, non-LinkedIn ZIPs, and zip-slip-style attempts.
    `sweep_once` catches this and skips the move-to-`.processed` step so the
    file stays visible to the user.
    """


def process_zip(
    zip_path: Path,
    *,
    unpacked_root: Path,
    dispatch: Callable[..., None] = dispatch_url,
) -> int:
    """Extract `zip_path` and dispatch every saved/reaction row.

    Returns the count of dispatched URLs.

    Raises `UnprocessableZip` when the file isn't a real LinkedIn export (or
    looks adversarial) so the sweep loop knows to leave it in place rather
    than archive it.
    """
    if not zipfile.is_zipfile(zip_path):
        logger.warning("[linkedin-zip] %s is not a valid ZIP — leaving in place", zip_path)
        raise UnprocessableZip(f"not a zip file: {zip_path}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = unpacked_root / f"{ts}_{zip_path.stem}"

    with zipfile.ZipFile(zip_path) as zf:
        if not _is_linkedin_export(zf):
            logger.warning(
                "[linkedin-zip] %s does not look like a LinkedIn export — leaving in place",
                zip_path.name,
            )
            raise UnprocessableZip(f"not a LinkedIn export: {zip_path.name}")
        try:
            _safe_extract(zf, dest)
        except RuntimeError as exc:
            logger.error("[linkedin-zip] refusing to extract %s: %s", zip_path.name, exc)
            raise UnprocessableZip(str(exc)) from exc

    dispatched = 0
    for csv_file in sorted(dest.rglob("*.csv")):
        base = _normalize_filename(csv_file.name)
        try:
            if base in SAVED_BASENAMES:
                iterator = _iter_saved_articles(csv_file)
            elif base in REACTION_BASENAMES:
                iterator = _iter_reactions(csv_file)
            else:
                continue
            for row in iterator:
                try:
                    _emit(row, dispatch=dispatch)
                    dispatched += 1
                except Exception:  # noqa: BLE001 — one bad row mustn't drop the batch
                    logger.exception(
                        "[linkedin-zip] dispatch failed for url=%s (file=%s)",
                        row.url,
                        csv_file.name,
                    )
        except Exception:  # noqa: BLE001 — log + move on to next CSV
            logger.exception("[linkedin-zip] failed parsing %s", csv_file.name)

    logger.info("[linkedin-zip] %s → dispatched=%d", zip_path.name, dispatched)
    return dispatched


# --- inbox sweep -------------------------------------------------------------


def _ensure_dirs(inbox: Path) -> tuple[Path, Path]:
    inbox.mkdir(parents=True, exist_ok=True)
    unpacked = inbox / ".unpacked"
    processed = inbox / ".processed"
    unpacked.mkdir(exist_ok=True)
    processed.mkdir(exist_ok=True)
    return unpacked, processed


def _list_pending_zips(inbox: Path) -> list[Path]:
    out: list[Path] = []
    for entry in os.scandir(inbox):
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith("."):
            continue
        if not name.lower().endswith(".zip"):
            continue
        out.append(Path(entry.path))
    out.sort()
    return out


def sweep_once(
    inbox: Path | None = None,
    *,
    dispatch: Callable[..., None] = dispatch_url,
) -> int:
    """Process every pending ZIP in the inbox. Returns total URLs dispatched."""
    inbox = inbox or _inbox_dir()
    unpacked, processed = _ensure_dirs(inbox)

    total = 0
    for zip_path in _list_pending_zips(inbox):
        try:
            total += process_zip(zip_path, unpacked_root=unpacked, dispatch=dispatch)
        except UnprocessableZip:
            # Logged inside process_zip; leave the file for the user to inspect.
            continue
        except Exception:  # noqa: BLE001 — keep sweeping even if one ZIP blew up
            logger.exception("[linkedin-zip] unhandled error on %s", zip_path.name)
            continue
        # Move to .processed on success (collision-safe).
        target = processed / zip_path.name
        if target.exists():
            target = processed / f"{zip_path.stem}.{int(time.time())}{zip_path.suffix}"
        shutil.move(str(zip_path), str(target))
        logger.info("[linkedin-zip] moved %s -> %s", zip_path.name, target)
    return total


# --- daemon loop -------------------------------------------------------------


def run_forever() -> None:
    interval = _poll_interval()
    inbox = _inbox_dir()
    logger.info("[linkedin-zip] watching %s (interval=%ss)", inbox, interval)

    stop = False

    def _shutdown(_signum: int, _frame: Any) -> None:
        nonlocal stop
        logger.info("[linkedin-zip] shutdown signal received; finishing current cycle")
        stop = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop:
        try:
            n = sweep_once(inbox)
            if n:
                logger.info("[linkedin-zip] sweep done; dispatched=%d", n)
        except Exception:  # noqa: BLE001
            logger.exception("[linkedin-zip] sweep failed; will retry next interval")
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)
    logger.info("[linkedin-zip] exited cleanly")


# --- entrypoint --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cmd = argv[0] if argv else "run"
    if cmd == "once":
        n = sweep_once()
        print(f"dispatched={n}")
        return 0
    if cmd in ("run", "daemon"):
        run_forever()
        return 0
    print(f"unknown command: {cmd!r}; expected one of: once, run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
