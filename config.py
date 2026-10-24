"""Environment-backed configuration for the Geopolitics News Bot."""
from dataclasses import dataclass, field
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default



def _rss_urls() -> list[str]:
    default = (
        "https://thediplomat.com/feed/,"
        "https://indianexpress.com/section/world/feed/,"
        "https://indianexpress.com/section/explained/explained-global/feed/,"
        "https://indianexpress.com/section/india/feed/"
    )
    configured = os.getenv("WEBSITE_RSS_URLS", "").strip()
    if not configured:
        configured = os.getenv("WEBSITE_RSS_URL", default).strip()
    return [url.strip() for url in configured.split(",") if url.strip()]


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    groq_model: str = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b").strip()
    website_rss_urls: list[str] = field(default_factory=_rss_urls)
    youtube_channel_id: str = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()
    cycle_minutes: int = _int("CYCLE_MINUTES", 30, 5)
    service_minutes: int = _int("SERVICE_MINUTES", 350, 1)
    max_item_age_hours: int = _int("MAX_ITEM_AGE_HOURS", 36, 1)
    max_state_items: int = _int("MAX_STATE_ITEMS", 5000, 500)
    github_repository: str = os.getenv("GITHUB_REPOSITORY", "").strip()
    github_token: str = os.getenv("GITHUB_TOKEN", "").strip()
    state_branch: str = os.getenv("STATE_BRANCH", "bot-state").strip() or "bot-state"

    def validate(self, require_telegram: bool = True) -> None:
        if require_telegram and (not self.telegram_bot_token or not self.telegram_chat_id):
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Actions Secrets or local .env.")
        if not self.gemini_api_key and not self.groq_api_key:
            raise ValueError("Set at least one AI key: GEMINI_API_KEY or GROQ_API_KEY.")
        if not self.website_rss_urls and not self.youtube_channel_id:
            raise ValueError("Configure at least one source: WEBSITE_RSS_URL or YOUTUBE_CHANNEL_ID.")
