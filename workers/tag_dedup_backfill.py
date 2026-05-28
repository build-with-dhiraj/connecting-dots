"""Tag dedup backfill CLI.

Subcommands: build-map | apply | all (default)
Flags: --limit N, --dry-run, --reuse-map, --force-rebuild, --model, --map-path,
       --log-level, --embed-threshold, --no-embeddings, --embed-cache-path
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("tag_dedup_backfill")
DEFAULT_MAP_PATH = Path("data/tag_canonical_map.json")


def _resolve_vault_root() -> Path:
    env = os.environ.get("VAULT_ROOT", "")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in [here.parent.parent, here.parent.parent.parent]:
        candidate = parent / "vault"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("Cannot locate vault/. Set VAULT_ROOT env var.")


def _collect_tags(vault_root: Path) -> list[str]:
    unique: set[str] = set()
    for path in sorted(vault_root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---\n"):
            continue
        end = text.find("\n---\n", 4)
        if end == -1:
            continue
        try:
            fm = yaml.safe_load(text[4:end]) or {}
        except Exception:
            continue
        if not isinstance(fm, dict):
            continue
        tags = fm.get("tags")
        if not tags:
            continue
        items = tags.split() if isinstance(tags, str) else tags
        for t in items:
            if isinstance(t, str) and t.strip():
                unique.add(t.strip())
    return sorted(unique)


def cmd_build_map(
    map_path: Path,
    model: str,
    reuse_map: bool,
    force_rebuild: bool,
    dry_run: bool,
    embed_threshold: float = 0.85,
    no_embeddings: bool = False,
    embed_cache_path: Optional[Path] = None,
) -> dict[str, str]:
    from connecting_dots.enrichment.tag_dedup import phase_c, load_map

    if reuse_map and not force_rebuild and map_path.exists():
        log.info("Reusing cached map at %s", map_path)
        return load_map(map_path)

    vault_root = _resolve_vault_root()
    log.info("Collecting tags from %s …", vault_root)
    all_tags = _collect_tags(vault_root)
    log.info("Found %d unique tags", len(all_tags))

    mapping = phase_c(
        all_tags,
        cache_path=map_path,
        model=model,
        skip_llm=dry_run,
        embed_cache_path=embed_cache_path,
        embed_threshold=embed_threshold,
        no_embeddings=no_embeddings,
    )

    n_canonical = len(set(mapping.values()))
    n_mapped = sum(1 for k, v in mapping.items() if k != v)
    log.info("Map: %d raw → %d canonical (%d remapped)", len(all_tags), n_canonical, n_mapped)

    if dry_run:
        sample = [(k, v) for k, v in sorted(mapping.items()) if k != v][:20]
        print(f"\n[dry-run] Sample remapped tags ({len(sample)} shown):")
        for raw, canon in sample:
            print(f"  {raw!r:50s} → {canon!r}")
    return mapping


def cmd_apply(map_path: Path, vault_root: Path, limit: Optional[int], dry_run: bool) -> None:
    from connecting_dots.enrichment.tag_dedup import load_map, phase_d, _split_frontmatter, _rewrite_tags

    if not map_path.exists():
        log.error("Map not found: %s  Run build-map first.", map_path)
        sys.exit(1)

    canonical_map = load_map(map_path)
    log.info("Loaded %d mappings from %s", len(canonical_map), map_path)

    if limit is None:
        counts = phase_d(vault_root, canonical_map, dry_run=dry_run)
    else:
        counts = {"updated": 0, "skipped": 0, "no_tags": 0, "errors": 0}
        done = 0
        for path in sorted(vault_root.rglob("*.md")):
            if done >= limit:
                break
            done += 1
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

    prefix = "[dry-run] " if dry_run else ""
    log.info("%sApply: updated=%d skipped=%d no_tags=%d errors=%d",
             prefix, counts["updated"], counts["skipped"], counts["no_tags"], counts["errors"])


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tag_dedup_backfill", description="Collapse semantic-duplicate vault tags.")
    parser.add_argument("subcommand", nargs="?", default="all", choices=["build-map", "apply", "all"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reuse-map", action="store_true")
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--map-path", default=None)
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument("--embed-threshold", type=float, default=0.85,
                        help="Cosine similarity threshold for embedding-based candidate pairs (default: 0.85)")
    parser.add_argument("--no-embeddings", action="store_true",
                        help="Force lexical fallback candidate generation (skip embeddings)")
    parser.add_argument("--embed-cache-path", default=None,
                        help="Path to cache tag embeddings (default: data/tag_embeddings.json)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    model = args.model or os.environ.get("NER_MODEL", "gpt-4.1")
    map_path = Path(args.map_path) if args.map_path else DEFAULT_MAP_PATH
    embed_cache_path = (
        Path(args.embed_cache_path) if args.embed_cache_path
        else Path("data/tag_embeddings.json")
    )

    try:
        vault_root = _resolve_vault_root()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    if args.subcommand in ("build-map", "all"):
        cmd_build_map(
            map_path=map_path,
            model=model,
            reuse_map=args.reuse_map,
            force_rebuild=args.force_rebuild,
            dry_run=args.dry_run,
            embed_threshold=args.embed_threshold,
            no_embeddings=args.no_embeddings,
            embed_cache_path=embed_cache_path,
        )
    if args.subcommand in ("apply", "all"):
        cmd_apply(map_path=map_path, vault_root=vault_root, limit=args.limit, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
