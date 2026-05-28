"""Initialize the LanceDB sidecar index at vault/.lancedb/.

Creates a single `items` table mirroring the Obsidian vault. Idempotent: re-running
on an existing DB is a no-op unless --force is passed.

Usage:
    python scripts/init_lancedb.py [--db vault/.lancedb] [--dim 384] [--force]

Dependencies (pin in requirements.txt when the project gets one):
    lancedb >= 0.13
    pyarrow >= 15
"""
from __future__ import annotations

import argparse
from pathlib import Path

import lancedb
import pyarrow as pa

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "vault" / ".lancedb"
DEFAULT_DIM = 384  # bge-small / all-MiniLM-L6-v2 class; revisit in component #9

TABLE_NAME = "items"


def items_schema(dim: int) -> pa.Schema:
    """Schema for the canonical `items` table.

    Columns mirror the vault frontmatter so the LanceDB row and the markdown note
    are recoverable from each other. `vault_path` is the relative path from the
    vault root (e.g. `inbox/lancedb-landing.md`).
    """
    return pa.schema(
        [
            pa.field("id", pa.string()),            # stable hash of vault_path
            pa.field("vector", pa.list_(pa.float32(), dim)),
            pa.field("text", pa.string()),          # note body, no frontmatter
            pa.field("source", pa.string()),
            pa.field("captured_at", pa.timestamp("us", tz="UTC")),
            pa.field("url", pa.string()),
            pa.field("vault_path", pa.string()),
        ]
    )


def init(db_path: Path, dim: int, force: bool) -> None:
    db_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(db_path))

    # `list_tables()` replaces the deprecated `table_names()`. Newer lancedb
    # releases (0.20+) return a `ListTablesResponse` object exposing `.tables`,
    # while older releases returned a plain `list[str]`. Handle both shapes
    # so the script stays portable across version bumps.
    tables = db.list_tables()
    table_names = getattr(tables, "tables", tables)
    existing = TABLE_NAME in table_names
    if existing and not force:
        print(f"[init_lancedb] table '{TABLE_NAME}' already exists at {db_path} — skipping (use --force to recreate)")
        return

    if existing and force:
        db.drop_table(TABLE_NAME)
        print(f"[init_lancedb] dropped existing '{TABLE_NAME}'")

    db.create_table(TABLE_NAME, schema=items_schema(dim))
    print(f"[init_lancedb] created '{TABLE_NAME}' (dim={dim}) at {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize LanceDB sidecar for Connecting Dots vault")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="LanceDB directory")
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM, help="Embedding dimension")
    parser.add_argument("--force", action="store_true", help="Drop and recreate the table")
    args = parser.parse_args()
    init(args.db, args.dim, args.force)


if __name__ == "__main__":
    main()
