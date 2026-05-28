"""WhatsApp outbound sender for daily digest.

Sends a Meta "interactive list" message with 5 digest items. Each row encodes
the slug and a reaction short-code in its ID so the inbound webhook can parse
reactions when the user taps a row.

Meta interactive list message format:
    POST https://graph.facebook.com/v22.0/<PHONE_NUMBER_ID>/messages
    Authorization: Bearer <WA_ACCESS_TOKEN>
    Content-Type: application/json

The list supports up to 10 sections with up to 10 rows each. We use 1 section
with up to 5 rows (one per digest item), plus a footer prompting reactions.

Reaction mechanism: the interactive list rows each have a distinct ID. When the
user taps a row, Meta sends an interactive reply webhook. The row ID encodes
"<slug>__<short_reaction>". The inbound webhook dispatches this to labels.py.

Note: WhatsApp interactive lists require exactly one button label (the "CTA"
that opens the list). We use "See Today's Picks".
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

from .labels import encode_row_id
from .resurface import DigestItem

log = logging.getLogger(__name__)

_WA_API_VERSION = "v22.0"
_WA_BASE = "https://graph.facebook.com"
_MAX_RETRIES = 2
_RETRY_DELAY_S = 2.0

# Short reaction codes embedded in row IDs (must be stable — changing breaks existing reactions)
_REACTION_CODES = {
    "thumbs_up": "up",
    "shrug": "shrug",
    "thumbs_down": "down",
}

# Row description template (shown under each item title in the list)
_ROW_DESCRIPTION = "Tap to react: 👍 up  🤷 shrug  👎 down — then reply with reaction"


def _build_interactive_payload(
    items: list[DigestItem],
    to: str,
) -> dict[str, Any]:
    """Build the Meta interactive list message payload.

    Each digest item becomes 3 rows: one for each reaction (👍 / 🤷 / 👎).
    Max rows per section = 10. With 5 items × 1 row (single "view" action)
    plus a compact reaction-reply instruction, we stay within limits.

    Simplified approach: one row per item for viewing; reactions are collected
    via a follow-up text reply instruction in the footer. This avoids the 10-row
    limit when k=5 (3 rows × 5 = 15 > 10).

    Row ID encodes the slug only; user replies with 👍/🤷/👎 text separately.
    Actually, the cleanest WA pattern for 5 items is:

      Section "Today's Digest (tap a reaction below each)"
        Row 1: item 1
        ...
        Row 5: item 5

    And the body text instructs: "Reply 1👍, 1🤷, or 1👎 to rate item 1."
    But interactive list rows don't support inline reply. The correct WA
    pattern is to use the row ID itself to encode the reaction selection.

    We use 1 section per item (up to 5 sections) each with 3 rows (reactions).
    This gives 5 × 3 = 15 rows total — but WA supports max 10 rows per section
    and max 10 sections. We use 5 sections × 3 rows = fine.
    """
    sections = []
    for i, item in enumerate(items, 1):
        # Truncate title to WA row title limit (24 chars)
        title = item.title[:24] if len(item.title) > 24 else item.title
        reason_short = item.reason[:72] if item.reason and len(item.reason) > 72 else (item.reason or "")

        rows = []
        for reaction_label, short_code in [("👍 Loved it", "up"), ("🤷 Meh", "shrug"), ("👎 Skip", "down")]:
            row_id = encode_row_id(item.slug, short_code)[:200]
            rows.append({
                "id": row_id,
                "title": reaction_label,
                "description": title[:72] if len(title) > 72 else title,
            })

        section_title = f"{i}. {title}"[:24]
        if reason_short:
            section_title = f"{i}. {title}"[:20]

        sections.append({
            "title": section_title,
            "rows": rows,
        })

    # Body text: numbered list of items with reasons
    lines = ["*Today's 5 picks from your vault:*\n"]
    for i, item in enumerate(items, 1):
        reason_txt = f" — {item.reason}" if item.reason else ""
        lines.append(f"{i}. *{item.title}*{reason_txt}")
    lines.append("\n_Tap a section below to react to each item._")
    body_text = "\n".join(lines)[:1024]  # WA body limit

    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {
                "type": "text",
                "text": "Your Daily Digest",
            },
            "body": {
                "text": body_text,
            },
            "footer": {
                "text": "Connecting Dots — react to train your digest",
            },
            "action": {
                "button": "React to Items",
                "sections": sections,
            },
        },
    }


def send_digest(
    items: list[DigestItem],
    to: str,
    access_token: str,
    phone_number_id: str,
    *,
    http_client: Optional[httpx.Client] = None,
    max_retries: int = _MAX_RETRIES,
) -> dict[str, Any]:
    """POST a WhatsApp interactive list message with digest items.

    Args:
        items: List of DigestItems (with reasons populated).
        to: Recipient's WhatsApp phone number (e.g. "918595087697").
        access_token: Meta permanent system user access token.
        phone_number_id: WhatsApp phone number ID from Meta.
        http_client: Optional httpx.Client for testing / connection reuse.
        max_retries: Number of retries on 5xx errors.

    Returns:
        Meta API response dict (contains messages[0].id on success).

    Raises:
        httpx.HTTPStatusError: if the API returns a non-2xx after retries.
        ValueError: if items list is empty.
    """
    if not items:
        raise ValueError("items list cannot be empty")

    url = f"{_WA_BASE}/{_WA_API_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = _build_interactive_payload(items, to)

    own_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)

    last_error: Optional[Exception] = None
    try:
        for attempt in range(max_retries + 1):
            try:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
                log.info(
                    "WhatsApp digest sent to %s, message_id=%s",
                    to,
                    (result.get("messages") or [{}])[0].get("id", "?"),
                )
                return result
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code < 500 or attempt >= max_retries:
                    log.error(
                        "WA send failed (attempt %d/%d): %s — %s",
                        attempt + 1,
                        max_retries + 1,
                        e.response.status_code,
                        e.response.text[:200],
                    )
                    raise
                log.warning(
                    "WA send 5xx (attempt %d/%d), retrying in %.1fs...",
                    attempt + 1,
                    max_retries + 1,
                    _RETRY_DELAY_S,
                )
                time.sleep(_RETRY_DELAY_S)
            except httpx.RequestError as e:
                last_error = e
                if attempt >= max_retries:
                    log.error("WA send request error after %d attempts: %s", attempt + 1, e)
                    raise
                log.warning("WA send network error, retrying: %s", e)
                time.sleep(_RETRY_DELAY_S)
    finally:
        if own_client:
            client.close()

    # Should not reach here, but appease type checker
    raise RuntimeError("send_digest exhausted retries without raising") from last_error


def send_digest_from_env(items: list[DigestItem]) -> dict[str, Any]:
    """Convenience wrapper that reads credentials from environment variables.

    Required env vars:
        WA_ACCESS_TOKEN
        WA_PHONE_NUMBER_ID
        WA_OWNER_NUMBER
    """
    access_token = os.environ["WA_ACCESS_TOKEN"]
    phone_number_id = os.environ["WA_PHONE_NUMBER_ID"]
    to = os.environ["WA_OWNER_NUMBER"]
    return send_digest(items, to=to, access_token=access_token, phone_number_id=phone_number_id)


__all__ = [
    "send_digest",
    "send_digest_from_env",
    "_build_interactive_payload",
]
