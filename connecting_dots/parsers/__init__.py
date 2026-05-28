"""Pure-function parsers for ingest channels that produce plain-text dumps.

The watchers in `workers/` are thin glue around these parsers. Keeping the
parsing logic separate makes it cheap to unit-test (no filesystem, no zip,
no daemon loop) and easy to reuse from other ingest channels later.
"""
