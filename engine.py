"""One collection cycle: collect, dedupe, batch AI, reserve keys, then publish."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from datetime import datetime, timezone

from ai_router import generate_posts, AIError
from collectors import collect_sources, enrich_story
from telegram_publisher import send_post, TelegramError


def _norm_title(value: str) -> str:
    value = re.sub(r"[^a-z0-9 ]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", value).strip()


def _close_title(title: str, existing_title: str) -> bool:
    a, b = _norm_title(title), _norm_title(existing_title)
    if not a or not b:
        return False
    if a == b:
        return True
    # Titles must be sufficiently long for similarity-based dedupe to be useful.
    return min(len(a), len(b)) >= 35 and SequenceMatcher(None, a, b).ratio() >= 0.91


def _already_seen(story, state_store, already_current: list) -> bool:
    items = state_store.state.get("items", {})
    if story.key in items:
        return True
    for record in items.values():
        if _close_title(story.title, record.get("title", "")):
            try:
                old = datetime.fromisoformat(record.get("published_at", "").replace("Z", "+00:00"))
                if old.tzinfo is None:
                    old = old.replace(tzinfo=timezone.utc)
                age_hours = abs((datetime.fromtimestamp(story.published_ts, timezone.utc) - old).total_seconds()) / 3600
                if age_hours <= 48:
                    return True
            except (ValueError, TypeError):
                if _close_title(story.title, record.get("title", "")):
                    return True
    return any(_close_title(story.title, other.title) for other in already_current)


def _format_post(item: dict, story) -> str:
    headline = str(item.get("headline", "")).strip()[:220]
    summary = str(item.get("summary", "")).strip()[:1500]
    analysis = str(item.get("analysis", "")).strip()[:700]
    # The URL comes from the feed, not the model output.
    pieces = [f"🌍 {headline}", summary]
    if analysis:
        pieces.append("Why it matters: " + analysis)
    pieces.append(f"Source: {story.source}\n{story.url}")
    return "\n\n".join(piece for piece in pieces if piece).strip()


def run_cycle(settings, state_store) -> dict:
    all_stories = collect_sources(settings.website_rss_url, settings.youtube_channel_id,
                                  settings.max_item_age_hours)
    candidates = []
    seen_in_cycle = []
    for story in all_stories:
        if _already_seen(story, state_store, seen_in_cycle):
            continue
        seen_in_cycle.append(story)
        candidates.append(story)
        if len(candidates) >= settings.max_posts_per_cycle:
            break

    if not candidates:
        print("[cycle] No new eligible stories; AI request skipped.")
        return {"collected": len(all_stories), "new": 0, "posted": 0, "provider": None}

    # One AI call per batch, not one call per article. Limit prompt size in advance.
    candidates = candidates[:settings.max_posts_per_cycle]
    candidates = [enrich_story(story) for story in candidates]
    try:
        ai_items, provider = generate_posts(candidates, settings)
    except AIError as exc:
        print(f"[cycle] AI unavailable; will retry these stories in the next cycle: {exc}")
        return {"collected": len(all_stories), "new": len(candidates), "posted": 0, "provider": None}

    lookup = {story.key: story for story in candidates}
    approved: dict[str, dict] = {}
    for item in ai_items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id", ""))
        if key not in lookup or not item.get("relevant"):
            continue
        headline = str(item.get("headline", "")).strip()
        summary = str(item.get("summary", "")).strip()
        if not headline or not summary:
            continue
        approved[key] = item
        if len(approved) >= settings.max_posts_per_cycle:
            break

    # Fail closed: persist every evaluated item BEFORE sending so restarts do not repost.
    # A hard crash between reservation and send may skip a story, but will not duplicate it.
    post_keys = set(approved)
    state_store.reserve(candidates, post_keys)
    posted = 0
    for story in candidates:
        if story.key not in approved:
            continue
        text = _format_post(approved[story.key], story)
        try:
            message_id = send_post(settings.telegram_bot_token, settings.telegram_chat_id, text)
            state_store.set_status(story.key, "posted", message_id)
            posted += 1
            print(f"[cycle] Posted {story.key} via {provider}: {story.title}")
        except TelegramError as exc:
            state_store.set_status(story.key, "send_failed_no_retry", None)
            print(f"[cycle] Telegram send failed for {story.key}; will not auto-retry to avoid duplicates: {exc}")
    state_store.save()
    print(f"[cycle] Done. new={len(candidates)}, approved={len(approved)}, posted={posted}, provider={provider}")
    return {"collected": len(all_stories), "new": len(candidates), "posted": posted, "provider": provider}
