"""Tag deduplication — phases A, B, C, D.

Phase A: deterministic slug merge (case/punct/word-order variants).
Phase B: Azure gpt-4.1 judges ambiguous pairs (e.g. india ↔ nseindia).
Phase C: combine A+B → cached mapping at data/tag_canonical_map.json.
Phase D: atomic frontmatter rewrite across the vault.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from openai import AzureOpenAI

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1"
DEFAULT_API_VERSION = "2024-10-21"
BATCH_SIZE = 30
MAX_BATCHES_WARN = 100

_WS_RE = re.compile(r"\s+")
_client_cache: dict[str, AzureOpenAI] = {}


def _get_client() -> AzureOpenAI:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION)
    key = f"{endpoint}|{api_key}|{api_version}"
    if key not in _client_cache:
        _client_cache[key] = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
    return _client_cache[key]


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def normalize_key(tag: str) -> str:
    """Lowercase, collapse abbreviations/punct, sort words → grouping key.

    Examples:
        "#entity/AI"  → "ai"    "#entity/a-i"  → "ai"
        "#topic/AI Engineering"  → "ai engineering"
        "#topic/ai-engineering"  → "ai engineering"
    """
    raw = tag.lstrip("#")
    if "/" in raw:
        raw = raw.split("/", 1)[1]
    raw = raw.lower()
    raw = re.sub(r"[\s_]+", " ", raw)

    def _hyphens(m: re.Match) -> str:
        l, r = m.group(1), m.group(2)
        return (l + r) if (len(l) == 1 and len(r) == 1) else (l + " " + r)

    raw = re.sub(r"([a-z0-9]+)-([a-z0-9]+)", _hyphens, raw)
    raw = re.sub(r"[^a-z0-9\s]", "", raw)
    raw = _WS_RE.sub(" ", raw).strip()
    return " ".join(sorted(raw.split())) if raw else ""


def _namespace(tag: str) -> str:
    raw = tag.lstrip("#")
    return raw.split("/", 1)[0] if "/" in raw else ""


def _slug(tag: str) -> str:
    raw = tag.lstrip("#")
    return raw.split("/", 1)[1] if "/" in raw else raw


# --------------------------------------------------------------------------- #
# Phase A
# --------------------------------------------------------------------------- #
def phase_a(tags: list[str]) -> dict[str, str]:
    """Group by (namespace, normalized_key); pick shortest+lex-smallest as canonical."""
    groups: dict[tuple[str, str], list[str]] = {}
    for tag in tags:
        groups.setdefault((_namespace(tag), normalize_key(tag)), []).append(tag)
    mapping: dict[str, str] = {}
    for group in groups.values():
        canonical = min(group, key=lambda t: (len(t), t))
        for tag in group:
            mapping[tag] = canonical
    return mapping


# --------------------------------------------------------------------------- #
# Phase B — LLM judge
# --------------------------------------------------------------------------- #
_JUDGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_decisions",
        "description": "Record duplicate decisions for tag pairs.",
        "parameters": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pair_index": {"type": "integer"},
                            "duplicate": {"type": "boolean"},
                            "canonical": {"type": "string"},
                        },
                        "required": ["pair_index", "duplicate", "canonical"],
                    },
                }
            },
            "required": ["decisions"],
        },
    },
}

_JUDGE_SYSTEM = """\
You are a tag-deduplication judge for a personal knowledge vault.
For each tag pair decide: semantic duplicate? If yes, which is canonical?
Rules: prefer shorter/more-common form. "india" ↔ "indian" = NOT duplicate.
"anthropic" ↔ "anthropic-pbc" = duplicate → "anthropic".
"nse" ↔ "nseindia" = duplicate → "nse".
Call record_decisions exactly once with ALL pairs answered.
"""


def _judge_batch(pairs: list[tuple[str, str]], model: str) -> list[dict[str, Any]]:
    client = _get_client()
    lines = [f"{i+1}. {a} ↔ {b}" for i, (a, b) in enumerate(pairs)]
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": "Pairs:\n" + "\n".join(lines)},
            ],
            tools=[_JUDGE_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_decisions"}},
            max_tokens=1024,
        )
        calls = resp.choices[0].message.tool_calls
        if not calls:
            return []
        return json.loads(calls[0].function.arguments).get("decisions", [])
    except Exception as exc:
        log.warning("LLM judge batch failed: %s", exc)
        return []


def _candidate_pairs(tags: list[str], a_map: dict[str, str]) -> list[tuple[str, str]]:
    """Pairs of distinct canonicals that share a word prefix (potential merge)."""
    ns_canonicals: dict[str, list[str]] = {}
    seen: set[str] = set()
    for raw, canon in a_map.items():
        if canon not in seen:
            seen.add(canon)
            ns_canonicals.setdefault(_namespace(canon), []).append(canon)

    pairs: list[tuple[str, str]] = []
    seen_pairs: set[frozenset] = set()
    for canon_list in ns_canonicals.values():
        for i, a in enumerate(sorted(canon_list)):
            for b in sorted(canon_list)[i+1:]:
                key = frozenset([a, b])
                if key in seen_pairs:
                    continue
                sa, sb = _slug(a), _slug(b)
                words_a = set(re.split(r"[-_\s]+", sa.lower()))
                words_b = set(re.split(r"[-_\s]+", sb.lower()))
                words_a = {w for w in words_a if len(w) >= 2}
                words_b = {w for w in words_b if len(w) >= 2}
                if (words_a & words_b) and (sa in sb or sb in sa or sa.startswith(sb[:4]) or sb.startswith(sa[:4])):
                    seen_pairs.add(key)
                    pairs.append((a, b))
    return pairs


def phase_b(tags: list[str], a_map: dict[str, str], model: str = DEFAULT_MODEL) -> dict[str, str]:
    """LLM-judge ambiguous pairs; return extra non-canonical→canonical mappings."""
    pairs = _candidate_pairs(tags, a_map)
    if not pairs:
        return {}
    n_batches = (len(pairs) + BATCH_SIZE - 1) // BATCH_SIZE
    if n_batches > MAX_BATCHES_WARN:
        log.warning("Phase B: %d pairs → %d batches; cost may exceed $2.", len(pairs), n_batches)
    log.info("Phase B: %d ambiguous pairs → %d batch(es)", len(pairs), n_batches)

    extra: dict[str, str] = {}
    for start in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[start:start + BATCH_SIZE]
        for dec in _judge_batch(batch, model):
            idx = dec.get("pair_index", 0) - 1
            if not (0 <= idx < len(batch)) or not dec.get("duplicate"):
                continue
            tag_a, tag_b = batch[idx]
            chosen = dec.get("canonical", "")
            sa, sb = _slug(tag_a), _slug(tag_b)
            if chosen in (sa, tag_a):
                extra[tag_b] = tag_a
            elif chosen in (sb, tag_b):
                extra[tag_a] = tag_b
            else:
                extra[tag_b if len(tag_a) <= len(tag_b) else tag_a] = (
                    tag_a if len(tag_a) <= len(tag_b) else tag_b
                )
    return extra


# --------------------------------------------------------------------------- #
# Phase C — build + cache
# --------------------------------------------------------------------------- #
def phase_c(tags: list[str], cache_path: Path, model: str = DEFAULT_MODEL, skip_llm: bool = False) -> dict[str, str]:
    """Build canonical mapping (Phase A + optional B), save to cache_path."""
    a_map = phase_a(tags)
    b_map = {} if skip_llm else phase_b(tags, a_map, model=model)

    combined = dict(a_map)
    for tag, canon in b_map.items():
        combined[tag] = a_map.get(canon, canon)

    # Transitive resolution
    for _ in range(10):
        changed = False
        for tag in list(combined):
            deeper = combined.get(combined[tag], combined[tag])
            if deeper != combined[tag]:
                combined[tag] = deeper
                changed = True
        if not changed:
            break

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(combined, indent=2, sort_keys=True), encoding="utf-8")
    log.info("Cached %d mappings → %s", len(combined), cache_path)
    return combined


def load_map(cache_path: Path) -> dict[str, str]:
    return json.loads(cache_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Phase D — vault frontmatter rewrite
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    import yaml
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except Exception:
        return None, text
    return (fm if isinstance(fm, dict) else None), text[end + 5:]


def _rewrite_tags(fm: dict, canonical_map: dict[str, str]) -> tuple[dict, bool]:
    existing = fm.get("tags")
    if not existing:
        return fm, False
    if isinstance(existing, str):
        tag_list = [t.strip() for t in existing.split() if t.strip()]
    elif isinstance(existing, list):
        tag_list = [str(t).strip() for t in existing if str(t).strip()]
    else:
        return fm, False
    new_tags = sorted({canonical_map.get(t, t) for t in tag_list})
    if new_tags == sorted(set(tag_list)):
        return fm, False
    return {**fm, "tags": new_tags}, True


def phase_d(vault_root: Path, canonical_map: dict[str, str], *, dry_run: bool = False) -> dict[str, int]:
    """Rewrite tags in every .md under vault_root. Returns counts dict."""
    import yaml
    counts = {"updated": 0, "skipped": 0, "no_tags": 0, "errors": 0}
    for path in sorted(vault_root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot read %s: %s", path, exc)
            counts["errors"] += 1
            continue
        fm, body = _split_frontmatter(text)
        if fm is None:
            counts["skipped"] += 1
            continue
        if not fm.get("tags"):
            counts["no_tags"] += 1
            continue
        new_fm, changed = _rewrite_tags(fm, canonical_map)
        if not changed:
            counts["skipped"] += 1
            continue
        if dry_run:
            counts["updated"] += 1
            continue
        try:
            serialized = yaml.safe_dump(new_fm, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()
            content = f"---\n{serialized}\n---\n{body}"
            if not content.endswith("\n"):
                content += "\n"
            fd, tmp = tempfile.mkstemp(prefix=".tmp-tagdedup-", suffix=".md", dir=str(path.parent))
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
            counts["updated"] += 1
        except Exception as exc:
            log.warning("Cannot write %s: %s", path, exc)
            counts["errors"] += 1
    return counts
