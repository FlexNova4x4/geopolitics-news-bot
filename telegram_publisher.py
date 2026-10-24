"""Telegram publisher with duplicate-conscious retry handling."""
import time
import requests


class TelegramError(RuntimeError):
    pass


def send_post(bot_token: str, chat_id: str, text: str) -> int:
    if len(text) > 3900:
        text = text[:3850].rsplit(" ", 1)[0] + "…"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=25)
        except requests.RequestException as exc:
            raise TelegramError(
                "Network timeout; delivery is uncertain. Not retrying."
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(
                "Unrecognized Telegram response; not retrying."
            ) from exc

        if response.ok and data.get("ok"):
            return int(data["result"]["message_id"])

        code = int(data.get("error_code", response.status_code))
        description = str(data.get("description", response.text[:250]))

        # An explicit 429 means Telegram rejected the request for rate limits.
        if code == 429 and attempt < 2:
            wait = int(data.get("parameters", {}).get("retry_after", 5))
            time.sleep(min(max(wait, 1) + 1, 60))
            continue

        # Do not retry ambiguous server errors: Telegram might have accepted it.
        if code >= 500:
            raise TelegramError(
                f"HTTP/API {code}; delivery may be uncertain. "
                f"Not retrying: {description}"
            )

        raise TelegramError(f"Telegram API {code}: {description}")

    raise TelegramError("Telegram rate limit persisted after retries.")
