"""Round-trip verifier: load a JSON file containing one envelope and parse it
through the codegenned pydantic model. Exits non-zero on validation failure.

    python -m scripts.parse_envelope /tmp/_envelope.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from connecting_dots.inbound_envelope import InboundEnvelope


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m scripts.parse_envelope <path-to-envelope.json>", file=sys.stderr)
        return 2
    raw = Path(argv[1]).read_text(encoding="utf-8")
    data = json.loads(raw)
    env = InboundEnvelope.model_validate(data)
    print(
        f"OK message_id={env.message_id} type={env.message_type.value} "
        f"source={env.source.value} url={env.url} "
        f"captured_at={env.captured_at.isoformat()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
