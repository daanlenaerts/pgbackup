"""Telegram notification helper for pgbackup."""

import logging

import httpx

log = logging.getLogger("pgbackup")

API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send(token: str, chat_ids: list[str], message: str) -> None:
    """Send a message to multiple Telegram chats. Best-effort: logs warnings on failure."""
    url = API_BASE.format(token=token)
    for chat_id in chat_ids:
        try:
            resp = httpx.post(
                url,
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=15,
            )
            resp.raise_for_status()
            log.info("Telegram message sent to chat %s", chat_id)
        except Exception as exc:
            log.warning("Telegram notification failed for chat %s: %s", chat_id, exc)
