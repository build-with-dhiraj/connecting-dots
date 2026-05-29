"""Connecting Dots — channel-agnostic URL capture pipeline."""

# Load repo-root .env as early as possible so every submodule and every worker
# that imports from this package sees credentials without manual `source .env`.
# override=False ensures real env vars (CI, shell exports) always take priority.
from connecting_dots._env_bootstrap import load_once as _load_env_once

_load_env_once()
