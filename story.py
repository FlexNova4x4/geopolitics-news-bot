from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class Story:
    key: str
    source: str
    title: str
    url: str
    published_at: str
    published_ts: float
    summary: str
    content: str = ""

    def to_ai_dict(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "source": self.source,
            "title": self.title[:400],
            "published_at": self.published_at,
            "url": self.url,
            "content": (self.content or self.summary or self.title)[:6500],
        }

    def to_state_dict(self, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "source": self.source,
            "title": self.title[:400],
            "url": self.url,
            "published_at": self.published_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
