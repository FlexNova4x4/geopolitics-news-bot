"""One collection cycle: collect, dedupe, batch AI, reserve keys, then publish."""
from __future__ import annotations

import os
import re
import time
from difflib import SequenceMatcher
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


def _tag_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", str(value or "").title())
    return cleaned[:32]


def _format_post(item: dict, story) -> str:
    headline = str(item.get("headline", "")).strip()[:220]
    summary = str(item.get("summary", "")).strip()[:1500]
    analysis = str(item.get("analysis", "")).strip()[:700]

    try:
        local_dt = datetime.fromtimestamp(story.published_ts, ZoneInfo("Asia/Kolkata"))
        date_line = local_dt.strftime("%d %b %Y, %I:%M %p IST")
    except (ValueError, OSError, OverflowError):
        date_line = story.published_at or "Date unavailable"

    region = str(item.get("region", "")).strip()[:60]
    topics_value = item.get("topics", [])
    if isinstance(topics_value, str):
        topics = [t.strip() for t in re.split(r"[,;|]", topics_value) if t.strip()]
    elif isinstance(topics_value, list):
        topics = [str(t).strip() for t in topics_value if str(t).strip()]
    else:
        topics = []
    if not topics and getattr(story, "tags", None):
        topics = list(story.tags[:3])
    if not region:
        region = "Global"
    topics = topics[:3]

    # Canonical region/topic labels are in English for consistent searchable tags;
    # the story itself remains in Hindi.
    hashtag_values = []
    for value in [region, *topics]:
        slug = _tag_slug(value)
        if slug and slug.lower() not in {x.lower() for x in hashtag_values}:
            hashtag_values.append(slug)
    tag_line = " ".join("#" + value for value in hashtag_values[:4])

    pieces = [
        f"🌍 {headline}",
        f"📅 प्रकाशित: {date_line}",
        f"🗺️ क्षेत्र / Region: {region}",
        ("🏷️ विषय / Topics: " + " · ".join(topics)) if topics else "",
        summary,
        ("🇮🇳 भारत के लिए महत्व: " + analysis) if analysis else "",
        tag_line,
        f"स्रोत: {story.source}\n{story.url}",
    ]
    return "\n\n".join(piece for piece in pieces if piece).strip()


def run_cycle(settings, state_store) -> dict:
    all_stories = collect_sources(
        settings.website_rss_urls,
        settings.youtube_channel_id,
        settings.max_item_age_hours,
    )

    # No per-cycle story cap: process all unseen stories dated today in Asia/Kolkata.
    candidates = []
    seen_in_cycle = []
    for story in all_stories:
        if _already_seen(story, state_store, seen_in_cycle):
            continue
        seen_in_cycle.append(story)
        candidates.append(story)

    if not candidates:
        print("[cycle] No new eligible stories; AI request skipped.")
        return {"collected": len(all_stories), "new": 0, "posted": 0, "provider": None}

    try:
        batch_size = max(1, min(8, int(os.getenv("AI_BATCH_SIZE", "4"))))
    except (TypeError, ValueError):
        batch_size = 4

    posted_total = 0
    evaluated_total = 0
    unresolved_total = 0
    failed_batches = 0
    providers_used = set()
    print(
        f"[cycle] Eligible today={len(all_stories)}; unseen={len(candidates)}; "
        f"AI batch={batch_size}; no fixed per-cycle story/post cap."
    )

    for offset in range(0, len(candidates), batch_size):
        batch = [enrich_story(story) for story in candidates[offset:offset + batch_size]]
        try:
            ai_items, provider = generate_posts(batch, settings)
        except AIError as exc:
            failed_batches += 1
            unresolved_total += len(candidates) - offset
            print(
                f"[cycle] AI unavailable for batch {offset // batch_size + 1}; "
                f"this and later stories remain unreserved for the next cycle: {exc}"
            )
            break

        providers_used.add(provider)
        lookup = {story.key: story for story in batch}
        approved = {}
        resolved_keys = set()
        for item in ai_items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id", ""))
            if key not in lookup or not isinstance(item.get("relevant"), bool):
                continue
            if item["relevant"]:
                headline = str(item.get("headline", "")).strip()
                summary = str(item.get("summary", "")).strip()
                if not headline or not summary:
                    continue  # malformed AI result remains unreserved and can be retried next cycle
                approved[key] = item
            resolved_keys.add(key)  # explicit relevant=true/false result received

        resolved_batch = [story for story in batch if story.key in resolved_keys]
        unresolved_total += len(batch) - len(resolved_batch)
        if resolved_batch:
            # Reserve each resolved story BEFORE its send, so restarts do not repost a sent/reserved item.
            state_store.reserve(resolved_batch, set(approved))
            for story in resolved_batch:
                if story.key not in approved:
                    continue  # AI explicitly marked irrelevant
                text = _format_post(approved[story.key], story)
                try:
                    message_id = send_post(settings.telegram_bot_token, settings.telegram_chat_id, text)
                except TelegramError as exc:
                    state_store.set_status(story.key, "send_failed_no_retry", None)
                    print(f"[cycle] Telegram send not confirmed for {story.key}; not retrying ambiguous delivery: {exc}")
                    continue

                state_store.set_status(story.key, "posted", message_id)
                posted_total += 1
                print(f"[cycle] Posted {story.key} via {provider}: {story.title}")
                # Telegram recommends no more than 20 messages/minute in a group; 3.2s leaves a margin.
                time.sleep(3.2)

            state_store.save()
            evaluated_total += len(resolved_batch)

    provider_summary = ",".join(sorted(providers_used)) if providers_used else "none"
    print(
        f"[cycle] Done. collected={len(all_stories)}, candidates={len(candidates)}, "
        f"evaluated={evaluated_total}, unresolved_for_retry={unresolved_total}, posted={posted_total}, "
        f"failed_batches={failed_batches}, providers={provider_summary}"
    )
    return {
        "collected": len(all_stories), "new": evaluated_total, "posted": posted_total,
        "provider": provider_summary, "failed_batches": failed_batches,
        "unresolved_for_retry": unresolved_total,
    }

