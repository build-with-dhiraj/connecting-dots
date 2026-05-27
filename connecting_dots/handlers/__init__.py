"""Per-domain URL handlers.

Each handler module exposes a single object that implements the `Handler`
Protocol from `connecting_dots.handlers.base`. Handlers are listed by import
path in `connecting_dots.dispatcher.HANDLER_MODULES` — the dispatcher
resolves them lazily so the registry boots even when an individual handler
module is missing.
"""
