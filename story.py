from dataclasses import dataclass, field
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
    tags: list[str] = field(default_factory=list)

    def to_ai_dict(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "source": self.source,
            "title": self.title[:400],
            "published_at": self.published_at,
            "url": self.url,
            "content": (self.content or self.summary or self.title)[:6500],
            "source_tags": self.tags[:12],
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
