"""Auto-load the repo-root .env file exactly once at package import time.

Rules:
- override=False  →  real environment variables always win over .env values.
- Missing .env    →  silent no-op (safe in CI and test environments).
- Idempotent      →  repeated imports are harmless (module-level flag guards).
- Path resolution →  relative to *this file*, not cwd, so workers invoked from
                     any directory still find the correct .env.
"""

from __future__ import annotations

from pathlib import Path

_loaded: bool = False


def load_once() -> None:
    """Load <repo-root>/.env if not already loaded.  Called by package __init__."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    # This file lives at connecting_dots/_env_bootstrap.py, so two parents up is
    # the repo root regardless of the working directory.
    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"

    # python-dotenv is a runtime dependency; import here to keep the module
    # importable even before the venv is fully set up (ImportError is re-raised).
    from dotenv import load_dotenv  # noqa: PLC0415

    load_dotenv(dotenv_path=env_path, override=False)
