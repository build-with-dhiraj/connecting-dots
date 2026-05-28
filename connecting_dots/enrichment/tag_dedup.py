"""Tag deduplication — phases A, B, C, D.

Phase A: deterministic slug merge (case/punct/word-order variants).
Phase B: Azure gpt-4.1 judges ambiguous pairs (e.g. india ↔ nseindia).
Phase C: combine A+B → cached mapping at data/tag_canonical_map.json.
Phase D: atomic frontmatter rewrite across the vault (tags + entities arrays).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
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
EMBED_BATCH_SIZE = 200
DEFAULT_EMBED_THRESHOLD = 0.85
DEFAULT_EMBED_DEPLOYMENT = "text-embedding-3-small"

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
        left, right = m.group(1), m.group(2)
        return (left + right) if (len(left) == 1 and len(right) == 1) else (left + " " + right)

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


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors (fast pure-Python)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_texts(texts: list[str], deployment: str) -> list[list[float]]:
    """Batch-embed texts using Azure OpenAI embeddings. Returns list of vectors."""
    client = _get_client()
    results: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        resp = client.embeddings.create(model=deployment, input=batch)
        results.extend([item.embedding for item in resp.data])
    return results


def _is_acronym(short: str, long_words: list[str]) -> bool:
    """True if `short` is an acronym of the words in `long_words`."""
    if len(short) < 2:
        return False
    initials = [w[0] for w in long_words if w]
    return short.lower() == "".join(initials).lower()


def _lexical_fallback_candidates(
    ns_canonicals: dict[str, list[str]],
) -> list[tuple[str, str]]:
    """Broadened lexical candidate pairs when embeddings unavailable."""
    pairs: list[tuple[str, str]] = []
    seen_pairs: set[frozenset] = set()
    for ns, canon_list in ns_canonicals.items():
        sorted_list = sorted(canon_list)
        for i, a in enumerate(sorted_list):
            sa = _slug(a)
            words_a = [w for w in re.split(r"[-_\s]+", sa.lower()) if len(w) >= 2]
            for b in sorted_list[i + 1 :]:
                key: frozenset = frozenset([a, b])
                if key in seen_pairs:
                    continue
                sb = _slug(b)
                words_b = [w for w in re.split(r"[-_\s]+", sb.lower()) if len(w) >= 2]
                set_a, set_b = set(words_a), set(words_b)
                shared = set_a & set_b
                # Any shared word
                if shared:
                    seen_pairs.add(key)
                    pairs.append((a, b))
                    continue
                # Acronym matching
                if (sa == sb) or _is_acronym(sa, words_b) or _is_acronym(sb, words_a):
                    seen_pairs.add(key)
                    pairs.append((a, b))
                    continue
                # Substring containment (only if the short one is ≤4 chars)
                shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
                if len(shorter) <= 4 and shorter in longer:
                    seen_pairs.add(key)
                    pairs.append((a, b))
    return pairs


def _embedding_candidates(
    a_map: dict[str, str],
    embed_cache_path: Path | None = None,
    threshold: float = DEFAULT_EMBED_THRESHOLD,
    deployment: str | None = None,
    no_embeddings: bool = False,
) -> list[tuple[str, str]]:
    """Generate candidate pairs using embedding cosine similarity within each namespace.

    Falls back to broadened lexical matching if embeddings are unavailable.
    """
    # Collect unique canonicals per namespace
    ns_canonicals: dict[str, list[str]] = {}
    seen: set[str] = set()
    for canon in a_map.values():
        if canon not in seen:
            seen.add(canon)
            ns_canonicals.setdefault(_namespace(canon), []).append(canon)

    # Try embeddings unless explicitly disabled
    if not no_embeddings:
        embed_deployment = deployment or os.environ.get(
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", DEFAULT_EMBED_DEPLOYMENT
        )
        # Try to get embeddings
        try:
            return _embedding_candidates_with_model(
                ns_canonicals, embed_deployment, embed_cache_path, threshold
            )
        except Exception as exc:
            log.warning(
                "Embeddings unavailable (%s); falling back to lexical candidates.", exc
            )

    # Fallback: broadened lexical
    log.info("Using lexical fallback candidate generation.")
    return _lexical_fallback_candidates(ns_canonicals)


def _embedding_candidates_with_model(
    ns_canonicals: dict[str, list[str]],
    deployment: str,
    embed_cache_path: Path | None,
    threshold: float,
) -> list[tuple[str, str]]:
    """Core embedding logic. Raises on error so caller can fall back."""
    # Gather all canonicals and their slugs
    all_canonicals: list[str] = []
    for canon_list in ns_canonicals.values():
        all_canonicals.extend(canon_list)

    slugs = [_slug(c) for c in all_canonicals]

    # Load or compute embeddings
    cached: dict[str, list[float]] = {}
    if embed_cache_path and embed_cache_path.exists():
        cached = json.loads(embed_cache_path.read_text(encoding="utf-8"))

    to_embed = [s for s in slugs if s not in cached]
    if to_embed:
        log.info("Embedding %d new tag slugs via %s …", len(to_embed), deployment)
        new_vecs = _embed_texts(to_embed, deployment)
        for slug, vec in zip(to_embed, new_vecs):
            cached[slug] = vec
        if embed_cache_path:
            embed_cache_path.parent.mkdir(parents=True, exist_ok=True)
            embed_cache_path.write_text(
                json.dumps(cached, separators=(",", ":")), encoding="utf-8"
            )
            log.info("Saved embedding cache → %s", embed_cache_path)

    # Build pairs per namespace
    pairs: list[tuple[str, str]] = []
    seen_pairs: set[frozenset] = set()
    for ns, canon_list in ns_canonicals.items():
        sorted_list = sorted(canon_list)
        for i, a in enumerate(sorted_list):
            vec_a = cached.get(_slug(a))
            if vec_a is None:
                continue
            for b in sorted_list[i + 1 :]:
                key: frozenset = frozenset([a, b])
                if key in seen_pairs:
                    continue
                vec_b = cached.get(_slug(b))
                if vec_b is None:
                    continue
                sim = _cosine(vec_a, vec_b)
                if sim >= threshold:
                    seen_pairs.add(key)
                    pairs.append((a, b))

    log.info("Embedding candidates: %d pairs above threshold=%.2f", len(pairs), threshold)
    return pairs


def _candidate_pairs(
    tags: list[str],
    a_map: dict[str, str],
    embed_cache_path: Path | None = None,
    embed_threshold: float = DEFAULT_EMBED_THRESHOLD,
    no_embeddings: bool = False,
) -> list[tuple[str, str]]:
    """Generate candidate pairs for phase B. Uses embeddings when available."""
    return _embedding_candidates(
        a_map,
        embed_cache_path=embed_cache_path,
        threshold=embed_threshold,
        no_embeddings=no_embeddings,
    )


def phase_b(
    tags: list[str],
    a_map: dict[str, str],
    model: str = DEFAULT_MODEL,
    embed_cache_path: Path | None = None,
    embed_threshold: float = DEFAULT_EMBED_THRESHOLD,
    no_embeddings: bool = False,
) -> dict[str, str]:
    """LLM-judge ambiguous pairs; return extra non-canonical→canonical mappings."""
    pairs = _candidate_pairs(
        tags,
        a_map,
        embed_cache_path=embed_cache_path,
        embed_threshold=embed_threshold,
        no_embeddings=no_embeddings,
    )
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
def phase_c(
    tags: list[str],
    cache_path: Path,
    model: str = DEFAULT_MODEL,
    skip_llm: bool = False,
    embed_cache_path: Path | None = None,
    embed_threshold: float = DEFAULT_EMBED_THRESHOLD,
    no_embeddings: bool = False,
) -> dict[str, str]:
    """Build canonical mapping (Phase A + optional B), save to cache_path."""
    a_map = phase_a(tags)
    b_map = (
        {}
        if skip_llm
        else phase_b(
            tags,
            a_map,
            model=model,
            embed_cache_path=embed_cache_path,
            embed_threshold=embed_threshold,
            no_embeddings=no_embeddings,
        )
    )

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


def _build_entity_canonical_map(tag_canonical_map: dict[str, str]) -> dict[str, str]:
    """Derive entity string → canonical entity string from the tag canonical map.

    E.g. if #entity/artificial-intelligence → #entity/ai, then:
      "artificial-intelligence" → "ai"  and  "Artificial Intelligence" → "ai"
    """
    entity_map: dict[str, str] = {}
    for raw_tag, canon_tag in tag_canonical_map.items():
        if _namespace(raw_tag) != "entity":
            continue
        raw_slug = _slug(raw_tag)
        canon_slug = _slug(canon_tag)
        if raw_slug == canon_slug:
            continue
        # Map both slug form and display (title-case words) forms
        for variant in _entity_display_variants(raw_slug):
            entity_map[variant] = canon_slug
    return entity_map


def _entity_display_variants(slug: str) -> list[str]:
    """Return likely display forms for an entity slug."""
    variants = [slug]
    # dash/underscore → space
    spaced = re.sub(r"[-_]+", " ", slug)
    if spaced != slug:
        variants.append(spaced)
    # Title case
    titled = spaced.title()
    variants.append(titled)
    # All caps abbreviation for short slugs
    if len(slug) <= 4:
        variants.append(slug.upper())
    return list(dict.fromkeys(variants))  # dedupe, preserve order


def _rewrite_entities(
    fm: dict,
    entity_map: dict[str, str],
    map_hash: str,
) -> tuple[dict, bool]:
    """Rewrite entities: array using entity_map. Idempotent via map_hash stamp."""
    existing_stamp = fm.get("raw_meta", {}) if isinstance(fm.get("raw_meta"), dict) else {}
    if isinstance(existing_stamp, dict) and existing_stamp.get("entities_canonicalized_at") == map_hash:
        return fm, False

    entities = fm.get("entities")
    if not entities or not isinstance(entities, list):
        return fm, False

    seen: set[str] = set()
    new_entities: list[str] = []
    changed = False
    for e in entities:
        e_str = str(e).strip()
        if not e_str:
            continue
        canonical = entity_map.get(e_str, entity_map.get(e_str.lower(), e_str))
        if canonical != e_str:
            changed = True
        if canonical not in seen:
            seen.add(canonical)
            new_entities.append(canonical)

    if not changed:
        return fm, False

    new_fm = {**fm, "entities": new_entities}
    raw_meta = dict(fm.get("raw_meta") or {}) if isinstance(fm.get("raw_meta"), dict) else {}
    raw_meta["entities_canonicalized_at"] = map_hash
    new_fm["raw_meta"] = raw_meta
    return new_fm, True


def _map_hash(canonical_map: dict[str, str]) -> str:
    """Short hash of the canonical map for idempotency stamping."""
    payload = json.dumps(canonical_map, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def phase_d(
    vault_root: Path,
    canonical_map: dict[str, str],
    *,
    dry_run: bool = False,
    rewrite_entities: bool = True,
) -> dict[str, int]:
    """Rewrite tags (and optionally entities) in every .md under vault_root."""
    import yaml

    entity_map = _build_entity_canonical_map(canonical_map) if rewrite_entities else {}
    m_hash = _map_hash(canonical_map)
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
        if not fm.get("tags") and not fm.get("entities"):
            counts["no_tags"] += 1
            continue

        new_fm, tags_changed = _rewrite_tags(fm, canonical_map)
        if rewrite_entities and entity_map:
            new_fm, ents_changed = _rewrite_entities(new_fm, entity_map, m_hash)
        else:
            ents_changed = False

        changed = tags_changed or ents_changed

        if not fm.get("tags") and not changed:
            counts["no_tags"] += 1
            continue
        if not changed:
            counts["skipped"] += 1
            continue
        if dry_run:
            counts["updated"] += 1
            continue
        try:
            serialized = yaml.safe_dump(
                new_fm, sort_keys=False, allow_unicode=True, default_flow_style=False
            ).rstrip()
            content = f"---\n{serialized}\n---\n{body}"
            if not content.endswith("\n"):
                content += "\n"
            fd, tmp = tempfile.mkstemp(
                prefix=".tmp-tagdedup-", suffix=".md", dir=str(path.parent)
            )
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
