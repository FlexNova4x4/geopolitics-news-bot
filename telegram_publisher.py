"""Telegram Bot API publisher with FloodWait-aware pacing and duplicate-conscious retries."""
import time
import requests


class TelegramError(RuntimeError):
    pass


def send_post(bot_token: str, chat_id: str, text: str) -> int:
    if len(text) > 3900:
        text = text[:3850].rsplit(" ", 1)[0] + "…"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}

    while True:
        try:
            response = requests.post(url, json=payload, timeout=25)
        except requests.RequestException as exc:
            # Outcome is ambiguous: Telegram may have accepted the message. Do not retry.
            raise TelegramError(
                "Network error/timeout; delivery is uncertain, so automatic retry was skipped to avoid duplicates."
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Unreadable Telegram response (HTTP {response.status_code}); delivery is uncertain."
            ) from exc

        if response.ok and data.get("ok"):
            return int(data["result"]["message_id"])

        code = int(data.get("error_code", response.status_code))
        description = str(data.get("description", response.text[:250]))

        if code == 429:
            # Telegram explicitly rejected this request; retrying after retry_after is safe.
            retry_after = int(data.get("parameters", {}).get("retry_after", 5))
            wait_seconds = max(1, retry_after) + 1
            print(f"[telegram] FloodWait/429. Sleeping {wait_seconds}s before retry. {description}")
            time.sleep(wait_seconds)
            continue

        if code >= 500:
            # Ambiguous delivery; do not retry automatically to avoid duplicate messages.
            raise TelegramError(
                f"Telegram API {code}; delivery may be uncertain, so not retrying: {description}"
            )

        raise TelegramError(f"Telegram API {code}: {description}")
