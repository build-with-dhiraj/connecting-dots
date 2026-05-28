"""Hybrid resurfacing algorithm for daily digest selection.

Formula (from docs/algorithm-reconciliation.md):

    score(item) = w_t * time_decay(item)
                + w_r * activity_relevance(item, recent_activity)
                + w_p * static_profile_match(item, profile)
                - lambda * diversity_penalty(item, already_selected_today)

Phase A defaults: w_t=0.3, w_r=0.4, w_p=0.3, lambda=0.7.

Cold-start behaviour
--------------------
When no labels exist (first 7 days of use), activity_relevance has nothing to
draw from. We detect this by checking digest_log.jsonl for the first entry's
timestamp and counting days since. Bootstrap period: days 0-7. During bootstrap:

    w_t=1.0, w_r=0.0, w_p=0.0

Ramp from days 8-14: linearly interpolate between bootstrap and hybrid defaults.
After day 14: full hybrid defaults.

This is read from data/digest_log.jsonl (first entry's `selected_at`). If the
file is missing, assume day 0 (pure recency).
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

import yaml

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #

class DigestItem(NamedTuple):
    slug: str
    title: str
    score: float
    reason: str  # populated later by why_reason.py; empty string at selection time
    url: Optional[str]


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_WEIGHTS = {
    "w_t": 0.3,
    "w_r": 0.4,
    "w_p": 0.3,
    "lambda": 0.7,
}

_BOOTSTRAP_WEIGHTS = {
    "w_t": 1.0,
    "w_r": 0.0,
    "w_p": 0.0,
    "lambda": 0.7,
}

_BOOTSTRAP_DAYS = 7
_RAMP_DAYS = 14  # by day 14, full hybrid defaults


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _default_vault_root() -> Path:
    """Resolve vault root from env or relative to this file."""
    env = os.environ.get("VAULT_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "vault"


def _default_labels_db() -> Path:
    env = os.environ.get("LABELS_DB")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "data" / "labels.jsonl"


def _default_digest_log() -> Path:
    env = os.environ.get("DIGEST_LOG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "data" / "digest_log.jsonl"


def _active_themes_path() -> Path:
    home = Path.home()
    return home / ".connecting_dots" / "active_themes.yaml"


# --------------------------------------------------------------------------- #
# Vault note loading
# --------------------------------------------------------------------------- #

_SKIP_DIR_PREFIXES = ("inbox/_failed/", "_failed/", ".trash/", "themes/", "digests/")
_SKIP_RELATIVE_PATHS = {"inbox/example.md"}


def _parse_frontmatter(text: str) -> Optional[dict[str, Any]]:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _parse_body(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + 5:]


def load_vault_notes(vault_root: Path) -> list[dict[str, Any]]:
    """Load all enriched notes from the vault. Returns list of note dicts.

    Each dict has: slug, title, topics, entities, captured_at, url, path.
    Only notes with a parseable frontmatter and a captured_at are included.
    """
    notes = []
    search_roots = [
        vault_root / "sources",
        vault_root / "inbox",
    ]
    for root in search_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(vault_root).as_posix()
            if rel in _SKIP_RELATIVE_PATHS:
                continue
            if any(rel.startswith(p) for p in _SKIP_DIR_PREFIXES):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(text)
            if not fm:
                continue
            captured_at = fm.get("captured_at")
            if not captured_at:
                continue
            # Parse captured_at
            try:
                if isinstance(captured_at, datetime):
                    captured_dt = captured_at.replace(tzinfo=timezone.utc) if captured_at.tzinfo is None else captured_at
                else:
                    captured_dt = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

            topics = fm.get("topics") or []
            if isinstance(topics, str):
                topics = [topics]

            # entities may be list of strings or list of dicts
            raw_entities = fm.get("entities") or []
            entities: list[str] = []
            for e in raw_entities:
                if isinstance(e, str):
                    entities.append(e)
                elif isinstance(e, dict):
                    name = e.get("name")
                    if name:
                        entities.append(str(name))

            notes.append({
                "slug": rel,
                "path": path,
                "title": str(fm.get("title") or path.stem),
                "topics": [str(t).lower().strip() for t in topics if t],
                "entities": [str(e).strip() for e in entities if e],
                "captured_at": captured_dt,
                "url": fm.get("url"),
            })
    return notes


# --------------------------------------------------------------------------- #
# Bootstrap / cold-start detection
# --------------------------------------------------------------------------- #

def _effective_weights(
    digest_log: Path,
    today: date,
    default_weights: dict,
) -> dict:
    """Return weights blended between bootstrap and hybrid based on days of use."""
    if not digest_log.exists():
        log.debug("No digest_log found — using cold-start weights (pure recency)")
        return dict(_BOOTSTRAP_WEIGHTS)

    # Read the first entry to determine start date
    first_selected_at: Optional[datetime] = None
    try:
        with open(digest_log, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    ts = row.get("selected_at") or row.get("timestamp")
                    if ts:
                        first_selected_at = datetime.fromisoformat(
                            str(ts).replace("Z", "+00:00")
                        )
                        break
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return dict(_BOOTSTRAP_WEIGHTS)

    if first_selected_at is None:
        return dict(_BOOTSTRAP_WEIGHTS)

    days_elapsed = (today - first_selected_at.date()).days

    if days_elapsed <= _BOOTSTRAP_DAYS:
        log.debug("Bootstrap period (day %d) — using cold-start weights", days_elapsed)
        return dict(_BOOTSTRAP_WEIGHTS)

    if days_elapsed >= _RAMP_DAYS:
        return dict(default_weights)

    # Linear interpolation between bootstrap and full hybrid over days 8-14
    t = (days_elapsed - _BOOTSTRAP_DAYS) / (_RAMP_DAYS - _BOOTSTRAP_DAYS)
    blended = {}
    for key in ("w_t", "w_r", "w_p", "lambda"):
        boot_val = _BOOTSTRAP_WEIGHTS[key]
        full_val = default_weights[key]
        blended[key] = boot_val + t * (full_val - boot_val)
    log.debug("Ramp period (day %d, t=%.2f) — interpolated weights: %s", days_elapsed, t, blended)
    return blended


# --------------------------------------------------------------------------- #
# Labels loading (for activity_relevance)
# --------------------------------------------------------------------------- #

def _load_recent_positive_slugs(labels_db: Path, days: int = 7) -> set[str]:
    """Return slugs of notes the user reacted positively to in the last `days` days."""
    if not labels_db.exists():
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    slugs: set[str] = set()
    try:
        with open(labels_db, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("reaction") != "thumbs_up":
                    continue
                ts_str = row.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts >= cutoff:
                    slugs.add(str(row.get("item_slug", "")))
    except OSError:
        pass
    return slugs


def _get_activity_entities(
    notes_by_slug: dict[str, dict],
    active_slugs: set[str],
) -> set[str]:
    """Return entity names from recently-liked notes (case-insensitive)."""
    entities: set[str] = set()
    for slug in active_slugs:
        note = notes_by_slug.get(slug)
        if note:
            for e in note.get("entities", []):
                entities.add(e.lower())
    return entities


# --------------------------------------------------------------------------- #
# Active themes profile
# --------------------------------------------------------------------------- #

def _load_active_themes(vault_notes: list[dict]) -> list[str]:
    """Load user's active themes from ~/.connecting_dots/active_themes.yaml.

    Falls back to the top-5 most-common topics in the vault.
    """
    themes_path = _active_themes_path()
    if themes_path.exists():
        try:
            with open(themes_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, list):
                themes = [str(t).lower().strip() for t in data if t][:5]
                if themes:
                    return themes
        except (OSError, yaml.YAMLError):
            pass

    # Fallback: top-5 most-common topics across vault
    topic_counts: dict[str, int] = defaultdict(int)
    for note in vault_notes:
        for topic in note.get("topics", []):
            topic_counts[topic] += 1
    if not topic_counts:
        return []
    top5 = sorted(topic_counts.keys(), key=lambda t: topic_counts[t], reverse=True)[:5]
    return top5


# --------------------------------------------------------------------------- #
# Previously-shown slugs (diversity penalty)
# --------------------------------------------------------------------------- #

def _load_recently_shown_slugs(digest_log: Path, days: int = 3) -> set[str]:
    """Return slugs shown in the last `days` digests (for surface-fatigue guard)."""
    if not digest_log.exists():
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    shown: set[str] = set()
    try:
        with open(digest_log, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = row.get("selected_at") or row.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts >= cutoff:
                    slug = row.get("slug") or row.get("item_slug")
                    if slug:
                        shown.add(str(slug))
    except OSError:
        pass
    return shown


# --------------------------------------------------------------------------- #
# Scoring components
# --------------------------------------------------------------------------- #

_DECAY_K = 0.01  # exp(-k * days_since_capture) — ~50% weight at 70 days


def _time_decay(note: dict, today: date) -> float:
    """Larger for older notes not recently resurfaced.

    Standard exponential: exp(-k * days_since_capture).
    Older notes get HIGHER scores to encourage resurfacing — hence we invert:
    1.0 - exp(-k * days) so the oldest notes score highest.
    Capped at [0, 1].
    """
    captured = note["captured_at"]
    days = max(0, (today - captured.date()).days)
    # We want old notes (not recently captured) to score higher.
    # exp(-k*days) is 1 for fresh notes and ~0 for very old ones.
    # Inverting: score = 1 - exp(-k * days) puts old notes up.
    # But very recent notes (days=0) would score 0, which is too harsh.
    # Blend: prefer notes in the sweet-spot of 30-180 days.
    # Use a "tent" function: peak at ~60 days.
    peak_days = 60.0
    if days <= peak_days:
        # Rising: 0 → 1 over first 60 days
        score = days / peak_days
    else:
        # Falling: exp decay after 60 days (still > 0.5 up to ~130 days)
        score = math.exp(-_DECAY_K * (days - peak_days))
    return min(1.0, max(0.0, score))


def _activity_relevance(note: dict, activity_entities: set[str]) -> float:
    """Entity overlap with recently-liked notes (Jaccard-style, [0,1])."""
    if not activity_entities:
        return 0.0
    note_entities = {e.lower() for e in note.get("entities", [])}
    if not note_entities:
        return 0.0
    overlap = len(note_entities & activity_entities)
    union = len(note_entities | activity_entities)
    if union == 0:
        return 0.0
    return overlap / union


def _static_profile_match(note: dict, active_themes: list[str]) -> float:
    """Jaccard similarity between note topics and user's active themes."""
    if not active_themes:
        return 0.0
    note_topics = set(note.get("topics", []))
    if not note_topics:
        return 0.0
    theme_set = set(active_themes)
    overlap = len(note_topics & theme_set)
    union = len(note_topics | theme_set)
    if union == 0:
        return 0.0
    return overlap / union


def _diversity_penalty(note: dict, selected: list[DigestItem], notes_by_slug: dict) -> float:
    """MMR-style penalty: fraction of entities shared with already-selected items."""
    if not selected:
        return 0.0
    note_entities = {e.lower() for e in note.get("entities", [])}
    if not note_entities:
        return 0.0

    max_overlap = 0.0
    for item in selected:
        sel_note = notes_by_slug.get(item.slug)
        if not sel_note:
            continue
        sel_entities = {e.lower() for e in sel_note.get("entities", [])}
        if not sel_entities:
            continue
        overlap = len(note_entities & sel_entities)
        union = len(note_entities | sel_entities)
        if union > 0:
            max_overlap = max(max_overlap, overlap / union)
    return max_overlap


# --------------------------------------------------------------------------- #
# Main selector
# --------------------------------------------------------------------------- #

def select_digest_items(
    vault_root: Optional[Path] = None,
    labels_db: Optional[Path] = None,
    *,
    k: int = 5,
    weights: Optional[dict] = None,
    today: Optional[date] = None,
    digest_log: Optional[Path] = None,
) -> list[DigestItem]:
    """Return top-k items for today's digest using the hybrid resurfacing algorithm.

    Args:
        vault_root: Path to vault directory. Defaults to env VAULT_ROOT or repo vault/.
        labels_db: Path to labels.jsonl. Defaults to data/labels.jsonl.
        k: Number of items to select.
        weights: Override default algorithm weights.
        today: Date to score against (default: today UTC).
        digest_log: Path to digest_log.jsonl for cold-start detection + surface-fatigue.

    Returns:
        List of DigestItem namedtuples (reason field is empty; populated by why_reason.py).
    """
    vault_root = vault_root or _default_vault_root()
    labels_db = labels_db or _default_labels_db()
    digest_log_path = digest_log or _default_digest_log()
    today = today or datetime.now(timezone.utc).date()
    base_weights = weights or DEFAULT_WEIGHTS

    # Determine effective weights (cold-start ramp)
    eff_weights = _effective_weights(digest_log_path, today, base_weights)

    # Load vault notes
    vault_notes = load_vault_notes(vault_root)
    if not vault_notes:
        log.warning("No vault notes found at %s", vault_root)
        return []

    notes_by_slug = {n["slug"]: n for n in vault_notes}

    # Activity signal: recently liked slugs → their entities
    active_slugs = _load_recent_positive_slugs(labels_db, days=7)
    activity_entities = _get_activity_entities(notes_by_slug, active_slugs)

    # Cold-start fallback: if no label-based activity, use recently captured notes
    if not activity_entities and eff_weights.get("w_r", 0) > 0:
        # Use entities from the 10 most recently captured notes
        recent_notes = sorted(vault_notes, key=lambda n: n["captured_at"], reverse=True)[:10]
        for n in recent_notes:
            for e in n.get("entities", []):
                activity_entities.add(e.lower())

    # Static profile
    active_themes = _load_active_themes(vault_notes)

    # Surface-fatigue: skip notes shown in last 3 days
    recently_shown = _load_recently_shown_slugs(digest_log_path, days=3)

    # Score all candidates (excluding recently shown)
    candidates = [n for n in vault_notes if n["slug"] not in recently_shown]
    if not candidates:
        # If fatigue filter removed everything, relax it
        candidates = vault_notes
        log.info("Surface-fatigue filter removed all candidates; relaxing constraint")

    # Greedy MMR selection
    selected: list[DigestItem] = []
    scored_candidates: list[tuple[float, dict]] = []

    for note in candidates:
        t_score = _time_decay(note, today)
        r_score = _activity_relevance(note, activity_entities)
        p_score = _static_profile_match(note, active_themes)

        base_score = (
            eff_weights["w_t"] * t_score
            + eff_weights.get("w_r", 0) * r_score
            + eff_weights.get("w_p", 0) * p_score
        )
        scored_candidates.append((base_score, note))

    # Sort by base score descending
    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    # Greedy selection with diversity penalty applied iteratively
    selected_slugs: set[str] = set()
    remaining = list(scored_candidates)

    while len(selected) < k and remaining:
        best_score = -1.0
        best_idx = 0
        best_note = None

        for i, (base_score, note) in enumerate(remaining):
            div_pen = _diversity_penalty(note, selected, notes_by_slug)
            final_score = base_score - eff_weights.get("lambda", 0.7) * div_pen
            if final_score > best_score:
                best_score = final_score
                best_idx = i
                best_note = note

        if best_note is None:
            break

        remaining.pop(best_idx)
        if best_note["slug"] in selected_slugs:
            continue

        selected_slugs.add(best_note["slug"])
        selected.append(DigestItem(
            slug=best_note["slug"],
            title=best_note["title"],
            score=best_score,
            reason="",  # populated later by why_reason.py
            url=best_note.get("url"),
        ))

    log.info(
        "Selected %d items from %d candidates (vault_size=%d, activity_entities=%d, themes=%s)",
        len(selected),
        len(candidates),
        len(vault_notes),
        len(activity_entities),
        active_themes,
    )
    return selected


__all__ = [
    "DigestItem",
    "DEFAULT_WEIGHTS",
    "select_digest_items",
    "load_vault_notes",
]
