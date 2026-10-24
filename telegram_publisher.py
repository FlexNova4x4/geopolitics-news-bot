"""Telegram Bot API publisher."""
import time
import requests


class TelegramError(RuntimeError):
    pass


def send_post(bot_token: str, chat_id: str, text: str) -> int:
    if len(text) > 3900:
        text = text[:3850].rsplit(" ", 1)[0] + "…"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=25)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
            time.sleep(1 + attempt * 2)
            continue
        if response.ok and data.get("ok"):
            return int(data["result"]["message_id"])
        code = int(data.get("error_code", response.status_code))
        description = str(data.get("description", response.text[:250]))
        if code == 429:
            retry_after = int(data.get("parameters", {}).get("retry_after", 5))
            time.sleep(min(retry_after + 1, 60))
            last_error = description
            continue
        if code >= 500:
            last_error = description
            time.sleep(1 + attempt * 2)
            continue
        raise TelegramError(f"Telegram API {code}: {description}")
    raise TelegramError(f"Telegram send failed after retries: {last_error or 'unknown error'}")
