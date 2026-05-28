"""Daily digest builder.

Selects today's top-k vault items, generates why-reasons, writes:
  1. vault/digests/<date>.md  — human-readable Obsidian note
  2. data/digest_queue.jsonl  — WA sender picks this up
  3. data/digest_log.jsonl    — log of what was selected (for diversity penalty)

Then optionally sends via WhatsApp if --dry-run is not set and WA env vars exist.

Usage:
    python -m workers.digest_builder [--date YYYY-MM-DD] [--dry-run] [--k 5]

Cold-start note: on first run, digest_log.jsonl doesn't exist yet. The algorithm
falls back to pure recency-decay. After 7 days of digests + reactions, it ramps
to the full hybrid scoring.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from connecting_dots.digest.resurface import (
    DigestItem,
    load_vault_notes,
    select_digest_items,
)
from connecting_dots.digest.why_reason import generate_reasons

log = logging.getLogger("digest_builder")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _vault_root() -> Path:
    env = os.environ.get("VAULT_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "vault"


def _data_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data"


def _queue_path() -> Path:
    return _data_dir() / "digest_queue.jsonl"


def _log_path() -> Path:
    return _data_dir() / "digest_log.jsonl"


# --------------------------------------------------------------------------- #
# Markdown digest writer
# --------------------------------------------------------------------------- #

def _build_digest_markdown(items: list[DigestItem], digest_date: date) -> str:
    date_str = digest_date.isoformat()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fm = {
        "type": "digest",
        "date": date_str,
        "generated_at": now_str,
        "item_count": len(items),
    }
    fm_str = yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip()

    lines = [f"---\n{fm_str}\n---\n", f"# Daily Digest — {date_str}\n"]
    for i, item in enumerate(items, 1):
        lines.append(f"## {i}. {item.title}")
        if item.reason:
            lines.append(f"\n> {item.reason}\n")
        if item.url:
            lines.append(f"- Source: {item.url}")
        lines.append(f"- Note: [[{item.slug}]]")
        lines.append(f"- Score: {item.score:.3f}\n")

    return "\n".join(lines)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-digest-", suffix=path.suffix, dir=str(path.parent))
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


# --------------------------------------------------------------------------- #
# Queue + log writes
# --------------------------------------------------------------------------- #

def _write_queue(items: list[DigestItem], digest_date: date, queue_path: Path) -> None:
    """Write the digest payload to digest_queue.jsonl for WA delivery.

    Overwrites the queue (one line per item). The WA sender reads this file,
    sends the message, then archives/clears it.
    """
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for item in items:
        lines.append(json.dumps({
            "date": digest_date.isoformat(),
            "slug": item.slug,
            "title": item.title,
            "score": round(item.score, 4),
            "reason": item.reason,
            "url": item.url,
        }, ensure_ascii=False))
    content = "\n".join(lines) + "\n"
    _write_atomic(queue_path, content)
    log.info("Wrote %d items to queue at %s", len(items), queue_path)


def _append_log(items: list[DigestItem], digest_date: date, log_path: Path) -> None:
    """Append one log row per selected item to digest_log.jsonl."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log_path, "a", encoding="utf-8") as f:
        for item in items:
            row = {
                "selected_at": now_str,
                "date": digest_date.isoformat(),
                "slug": item.slug,
                "title": item.title,
                "score": round(item.score, 4),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info("Appended %d log rows to %s", len(items), log_path)


# --------------------------------------------------------------------------- #
# Main run function
# --------------------------------------------------------------------------- #

def run(
    *,
    digest_date: Optional[date] = None,
    k: int = 5,
    dry_run: bool = False,
    send_wa: bool = True,
    vault_root: Optional[Path] = None,
    queue_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Run the full digest pipeline.

    Returns:
        dict with keys: date, items (list of dicts), dry_run, sent_wa, digest_path
    """
    today = digest_date or datetime.now(timezone.utc).date()
    vault = vault_root or _vault_root()
    q_path = queue_path or _queue_path()
    l_path = log_path or _log_path()

    log.info("Building digest for %s (vault=%s, k=%d, dry_run=%s)", today, vault, k, dry_run)

    # Step 1: Select items
    items = select_digest_items(
        vault_root=vault,
        k=k,
        today=today,
        digest_log=l_path,
    )

    if not items:
        log.warning("No items selected — vault may be empty or all notes recently shown")
        return {"date": str(today), "items": [], "dry_run": dry_run, "sent_wa": False, "digest_path": None}

    # Step 2: Generate why-reasons
    log.info("Generating why-reasons for %d items...", len(items))
    vault_notes = load_vault_notes(vault)
    notes_by_slug = {n["slug"]: n for n in vault_notes}

    items_with_reasons = generate_reasons(items, notes_by_slug=notes_by_slug)

    # Step 3: Write markdown digest
    digest_dir = vault / "digests"
    digest_path = digest_dir / f"{today.isoformat()}.md"

    if not dry_run:
        content = _build_digest_markdown(items_with_reasons, today)
        _write_atomic(digest_path, content)
        log.info("Digest markdown written to %s", digest_path)

        # Step 4: Write queue for WA sender
        _write_queue(items_with_reasons, today, q_path)

        # Step 5: Append to digest log
        _append_log(items_with_reasons, today, l_path)
    else:
        log.info("[dry-run] Would write digest to %s", digest_path)
        log.info("[dry-run] Selected items:")
        for i, item in enumerate(items_with_reasons, 1):
            log.info("  %d. %s (score=%.3f) — %s", i, item.title, item.score, item.reason)

    # Step 6: Send WhatsApp (if env vars present and not dry-run)
    sent_wa = False
    if not dry_run and send_wa:
        wa_token = os.environ.get("WA_ACCESS_TOKEN")
        wa_phone_id = os.environ.get("WA_PHONE_NUMBER_ID")
        wa_owner = os.environ.get("WA_OWNER_NUMBER")
        if wa_token and wa_phone_id and wa_owner:
            try:
                from connecting_dots.digest.wa_send import send_digest
                send_digest(
                    items_with_reasons,
                    to=wa_owner,
                    access_token=wa_token,
                    phone_number_id=wa_phone_id,
                )
                sent_wa = True
                log.info("WhatsApp digest sent to %s", wa_owner)
            except Exception as e:
                log.error("WhatsApp send failed: %s", e)
        else:
            missing = [v for v in ("WA_ACCESS_TOKEN", "WA_PHONE_NUMBER_ID", "WA_OWNER_NUMBER") if not os.environ.get(v)]
            log.warning("WA send skipped — missing env vars: %s", missing)

    return {
        "date": str(today),
        "items": [
            {
                "slug": item.slug,
                "title": item.title,
                "score": item.score,
                "reason": item.reason,
                "url": item.url,
            }
            for item in items_with_reasons
        ],
        "dry_run": dry_run,
        "sent_wa": sent_wa,
        "digest_path": str(digest_path) if not dry_run else None,
    }


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="digest_builder",
        description="Build and send the daily Connecting Dots digest.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date to generate digest for (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing or sending.")
    parser.add_argument("--k", type=int, default=5, help="Number of items to select (default: 5).")
    parser.add_argument(
        "--no-wa",
        action="store_true",
        help="Skip WhatsApp send even if env vars are set.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    digest_date: Optional[date] = None
    if args.date:
        try:
            digest_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"Invalid date: {args.date!r}. Use YYYY-MM-DD.", file=sys.stderr)
            return 1

    result = run(
        digest_date=digest_date,
        k=args.k,
        dry_run=args.dry_run,
        send_wa=not args.no_wa,
    )

    if args.dry_run:
        print(f"\n[dry-run] Digest for {result['date']}:")
        for i, item in enumerate(result["items"], 1):
            print(f"  {i}. {item['title']} (score={item['score']:.3f})")
            if item["reason"]:
                print(f"     Reason: {item['reason']}")
    else:
        print(f"Digest for {result['date']}: {len(result['items'])} items")
        if result["digest_path"]:
            print(f"  Written to: {result['digest_path']}")
        if result["sent_wa"]:
            print("  WhatsApp: sent")
        else:
            print("  WhatsApp: not sent (check env vars or use --no-wa)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
