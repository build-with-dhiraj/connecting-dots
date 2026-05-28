"""WhatsApp outbound sender for daily digest.

Sends one interactive "button" message per digest item (k items → k messages).
Each message has 3 reply buttons encoding 👍/🤷/👎 reactions via encode_row_id.

Why button messages instead of a single list message:
  WhatsApp Cloud API caps interactive list messages at 10 rows TOTAL across all
  sections. With 5 items × 3 reactions = 15 rows, the list API returns:
    400 (#131009) "Total row count exceed max allowed count: 10"
  Reply-button messages support exactly 3 buttons — a perfect fit for the three
  reaction choices — and avoid the row-count limit entirely.

Meta interactive button message format:
    POST https://graph.facebook.com/v22.0/<PHONE_NUMBER_ID>/messages
    Authorization: Bearer <WA_ACCESS_TOKEN>
    Content-Type: application/json

Reaction mechanism: button reply.id encodes "<slug>__<short_reaction>". When the
user taps a button, Meta sends an interactive webhook with interactive.button_reply.id.
The inbound webhook reads button_reply.id (or list_reply.id for legacy messages) and
dispatches to labels.py.
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

# Short reaction codes embedded in button IDs (must be stable — changing breaks existing reactions)
_REACTION_CODES = {
    "thumbs_up": "up",
    "shrug": "shrug",
    "thumbs_down": "down",
}

# Button labels — each ≤ 20 chars (WA limit). Emoji counts as 2 chars on some clients;
# verified: "👍 Loved it" = 11 chars utf-8 display, safe. "🤷 Meh" = 6. "👎 Skip" = 7.
_REACTION_BUTTONS = [
    ("up",    "👍 Loved it"),
    ("shrug", "🤷 Meh"),
    ("down",  "👎 Skip"),
]
_MAX_BUTTON_TITLE = 20
_MAX_BODY = 1024
_MAX_BUTTON_ID = 200  # WA button reply ID limit (we use 200 per encode_row_id)


def _build_button_payload(
    item: DigestItem,
    index: int,
    total: int,
    to: str,
) -> dict[str, Any]:
    """Build the Meta interactive button message payload for a single digest item.

    Args:
        item: The digest item to render.
        index: 1-based position in today's digest.
        total: Total number of items in today's digest.
        to: Recipient WhatsApp phone number.

    Returns:
        A dict ready to POST to the Meta messages endpoint.
    """
    # Header: short context label
    header_text = f"Pick {index} of {total}"

    # Body: title + reason, truncated to WA limit
    reason_part = f"\n{item.reason}" if item.reason else ""
    body_text = f"*{item.title}*{reason_part}"
    if len(body_text) > _MAX_BODY:
        # Truncate reason first; keep title intact
        max_reason = _MAX_BODY - len(f"*{item.title}*\n") - 3  # "..." ellipsis
        if item.reason and max_reason > 0:
            body_text = f"*{item.title}*\n{item.reason[:max_reason]}..."
        else:
            body_text = f"*{item.title}*"[:_MAX_BODY]

    # Buttons: 3 reaction choices
    buttons = []
    for short_code, label in _REACTION_BUTTONS:
        btn_id = encode_row_id(item.slug, short_code)[:_MAX_BUTTON_ID]
        btn_title = label[:_MAX_BUTTON_TITLE]
        buttons.append({
            "type": "reply",
            "reply": {
                "id": btn_id,
                "title": btn_title,
            },
        })

    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "header": {
                "type": "text",
                "text": header_text,
            },
            "body": {
                "text": body_text,
            },
            "action": {
                "buttons": buttons,
            },
        },
    }


# Backward-compat shim: old name imported by external callers / legacy tests.
def _build_interactive_payload(
    items: list[DigestItem],
    to: str,
) -> dict[str, Any]:
    """Deprecated shim — returns the button payload for the first item only.

    Kept so that any code importing the old name doesn't crash. New code should
    use _build_button_payload directly or call send_digest.
    """
    if not items:
        raise ValueError("items list cannot be empty")
    return _build_button_payload(items[0], index=1, total=len(items), to=to)


def _post_one(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    max_retries: int,
    item_title: str,
) -> dict[str, Any]:
    """POST one message with retry-on-5xx. Fails fast on non-5xx errors."""
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            log.info("WA button message sent: %r, message_id=%s", item_title[:40], msg_id)
            return result
        except httpx.HTTPStatusError as e:
            last_error = e
            # Fail fast on non-5xx (client errors, auth, quota) — no point retrying
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
    raise RuntimeError("_post_one exhausted retries without raising") from last_error


def send_digest(
    items: list[DigestItem],
    to: str,
    access_token: str,
    phone_number_id: str,
    *,
    http_client: Optional[httpx.Client] = None,
    max_retries: int = _MAX_RETRIES,
) -> dict[str, Any]:
    """Send one interactive button message per digest item.

    Sends k messages (one per item) sequentially. Fails fast on the first
    non-5xx error — matches the behaviour of the old single-message path so
    the caller can treat any raise as "digest not delivered".

    Args:
        items: List of DigestItems (with reasons populated).
        to: Recipient's WhatsApp phone number (e.g. "918595087697").
        access_token: Meta permanent system user access token.
        phone_number_id: WhatsApp phone number ID from Meta.
        http_client: Optional httpx.Client for testing / connection reuse.
        max_retries: Number of retries on 5xx errors per message.

    Returns:
        dict with keys:
            "sent": int — number of successfully sent messages
            "message_ids": list[str] — WA message IDs
            "failed": list[str] — item slugs that failed (empty on full success)

    Raises:
        httpx.HTTPStatusError: on first non-2xx after retries (fail-fast).
        httpx.RequestError: on unrecoverable network errors.
        ValueError: if items list is empty.
    """
    if not items:
        raise ValueError("items list cannot be empty")

    url = f"{_WA_BASE}/{_WA_API_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    own_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)

    sent = 0
    message_ids: list[str] = []
    total = len(items)

    try:
        for i, item in enumerate(items, 1):
            payload = _build_button_payload(item, index=i, total=total, to=to)
            # _post_one raises on failure — fail-fast on first error
            result = _post_one(client, url, headers, payload, max_retries, item.title)
            msg_id = (result.get("messages") or [{}])[0].get("id", "")
            message_ids.append(msg_id)
            sent += 1
    finally:
        if own_client:
            client.close()

    return {"sent": sent, "message_ids": message_ids, "failed": []}


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
    "_build_button_payload",
    "_build_interactive_payload",  # backward-compat shim
]
