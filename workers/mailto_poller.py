"""IMAP-based mailto fallback poller.

Polls a Gmail account over IMAP for unread messages under a specific label
(default: `connecting-dots`), extracts the first URL from each message body
(or subject if body has none), dispatches the URL via the channel-agnostic
dispatcher, and marks the message as seen.

Designed as a hot-spare ingest channel: runs on a 5-minute cron / loop so
capture latency is bounded. Safe to run alongside the WhatsApp channel.

Env vars (see .env.example):
    IMAP_HOST              default: imap.gmail.com
    IMAP_PORT              default: 993
    IMAP_USER              Gmail address
    IMAP_APP_PASSWORD      Gmail App Password (NOT account password)
    IMAP_LABEL             default: connecting-dots
    IMAP_POLL_INTERVAL_S   default: 300 (5 minutes)
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from email.message import Message
from typing import Iterator

from connecting_dots.dispatcher import dispatch_url

logger = logging.getLogger(__name__)

# RFC-3986-ish URL extraction. Greedy enough to grab tracking params, conservative
# enough not to swallow trailing punctuation in plain prose.
_URL_RE = re.compile(
    r"https?://[^\s<>\"'\])}]+",
    re.IGNORECASE,
)


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val or ""


def _extract_body_text(msg: Message) -> str:
    """Return the plain-text body (falling back to stripped HTML)."""
    if msg.is_multipart():
        text_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp.lower():
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:  # noqa: BLE001
                continue
            if ctype == "text/plain":
                text_parts.append(decoded)
            elif ctype == "text/html":
                html_parts.append(decoded)
        if text_parts:
            return "\n".join(text_parts)
        if html_parts:
            # crude HTML strip — good enough since we only need URL regex hits
            return re.sub(r"<[^>]+>", " ", "\n".join(html_parts))
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _first_url(text: str) -> str | None:
    if not text:
        return None
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(0)
    # strip common trailing punctuation that the regex's negative class missed
    return url.rstrip(".,);:!?")


def _connect(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    return conn


def _select_label(conn: imaplib.IMAP4_SSL, label: str) -> None:
    # Gmail exposes labels as IMAP mailboxes. Quote it to be safe with hyphens/spaces.
    typ, _ = conn.select(f'"{label}"')
    if typ != "OK":
        raise RuntimeError(f"Could not select IMAP label/mailbox: {label}")


def _iter_unseen(conn: imaplib.IMAP4_SSL) -> Iterator[tuple[bytes, Message]]:
    typ, data = conn.search(None, "UNSEEN")
    if typ != "OK" or not data or not data[0]:
        return
    for uid in data[0].split():
        typ, msg_data = conn.fetch(uid, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            logger.warning("Failed to fetch UID %s", uid)
            continue
        raw = msg_data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            continue
        yield uid, email.message_from_bytes(raw)


def _mark_seen(conn: imaplib.IMAP4_SSL, uid: bytes) -> None:
    conn.store(uid, "+FLAGS", "\\Seen")


def poll_once() -> int:
    """One IMAP poll cycle. Returns count of URLs dispatched."""
    host = _env("IMAP_HOST", "imap.gmail.com")
    port = int(_env("IMAP_PORT", "993"))
    user = _env("IMAP_USER", required=True)
    password = _env("IMAP_APP_PASSWORD", required=True)
    label = _env("IMAP_LABEL", "connecting-dots")

    dispatched = 0
    conn = _connect(host, port, user, password)
    try:
        _select_label(conn, label)
        for uid, msg in _iter_unseen(conn):
            subject = str(msg.get("Subject", "") or "")
            body = _extract_body_text(msg)
            url = _first_url(body) or _first_url(subject)
            if not url:
                logger.info("UID %s: no URL found; skipping (leaving unread)", uid.decode())
                continue
            captured_at = datetime.now(timezone.utc)
            raw_payload = {
                "message_id": str(msg.get("Message-ID", "")),
                "from": str(msg.get("From", "")),
                "to": str(msg.get("To", "")),
                "subject": subject,
                "date": str(msg.get("Date", "")),
                "imap_uid": uid.decode(),
                "imap_label": label,
            }
            try:
                dispatch_url(url=url, source="mailto", captured_at=captured_at, raw_payload=raw_payload)
            except Exception:  # noqa: BLE001
                logger.exception("Dispatch failed for UID %s; leaving unread for retry", uid.decode())
                continue
            _mark_seen(conn, uid)
            dispatched += 1
            logger.info("Dispatched url=%s from UID %s", url, uid.decode())
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn.logout()
    return dispatched


def run_forever() -> None:
    interval = int(_env("IMAP_POLL_INTERVAL_S", "300"))
    logger.info("mailto_poller starting; interval=%ss", interval)

    stop = False

    def _shutdown(_signum, _frame):
        nonlocal stop
        logger.info("Shutdown signal received; finishing current cycle")
        stop = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop:
        try:
            n = poll_once()
            logger.info("cycle complete; dispatched=%d", n)
        except Exception:  # noqa: BLE001
            logger.exception("Poll cycle failed; will retry next interval")
        # sleep in 1s ticks so SIGTERM is responsive
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)
    logger.info("mailto_poller exited cleanly")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        n = poll_once()
        print(f"dispatched={n}")
    else:
        run_forever()
