"""One collection cycle: collect, dedupe, batch AI, reserve keys, then publish."""
from __future__ import annotations

import os
import re
from html import escape
import time
from difflib import SequenceMatcher
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

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
    """Render a compact, readable Hindi news post with a clickable source name."""
    headline = str(item.get("headline", "")).strip()[:180]
    summary = str(item.get("summary", "")).strip()[:1500]
    analysis = str(item.get("analysis", "")).strip()[:500]

    # Hide boilerplate/no-impact claims instead of posting an empty India-impact section.
    no_impact_patterns = (
        r"\bno (?:clear |direct )?(?:impact|effect|relevance)\b",
        r"\bdoes not directly affect india\b",
        r"भारत पर (?:कोई )?(?:स्पष्ट )?(?:सीधा )?(?:असर|प्रभाव) (?:नहीं|स्पष्ट नहीं)",
        r"भारत पर इसका (?:कोई )?(?:स्पष्ट )?(?:सीधा )?(?:असर|प्रभाव) (?:नहीं|स्पष्ट नहीं)",
        r"सीधा असर उपलब्ध जानकारी से स्पष्ट नहीं",
        r"भारत से (?:कोई )?(?:सीधा )?संबंध नहीं",
    )
    if analysis and any(re.search(pattern, analysis, flags=re.IGNORECASE) for pattern in no_impact_patterns):
        analysis = ""

    try:
        local_dt = datetime.fromtimestamp(story.published_ts, ZoneInfo("Asia/Kolkata"))
        date_line = f"{local_dt.strftime('%d %b')} · {local_dt.strftime('%I %p').lstrip('0')} IST"
    except (ValueError, OSError, OverflowError):
        date_line = "Date/time unavailable"

    topics_value = item.get("topics", [])
    if isinstance(topics_value, str):
        topics = [part.strip() for part in re.split(r"[,;|]", topics_value) if part.strip()]
    elif isinstance(topics_value, list):
        topics = [str(part).strip() for part in topics_value if str(part).strip()]
    else:
        topics = []
    topics = topics[:3] or ["World News"]

    pieces = [
        f"🌍 <b>{escape(headline)}</b>",
        f"🕒 {escape(date_line)}",
        "🏷️ " + escape(" · ".join(topics)),
    ]
    if summary:
        # Preserve paragraph breaks while escaping model-generated text for Telegram HTML.
        pieces.append(escape(summary))

    # Only show an India response when the source-supported position is actually India's.
    raw_positions = item.get("positions", [])
    if isinstance(raw_positions, list):
        for entry in raw_positions[:3]:
            if not isinstance(entry, dict):
                continue
            side = str(entry.get("side", "")).strip().lower()
            reaction = str(entry.get("position", "")).strip()[:500]
            india_markers = ("🇮🇳", "india", "indian", "भारत", "भारतीय")
            if reaction and any(marker in side for marker in india_markers):
                # The flag is the only label; no 'related response' heading.
                pieces.append("🇮🇳 " + escape(reaction))
                break

    if analysis:
        pieces.append("🇮🇳 <b>भारत पर असर:</b> " + escape(analysis))

    # Prefer a friendly publisher name, inferred from the source label or article host.
    source_raw = str(getattr(story, "source", "Source") or "Source").strip()
    source_key = source_raw.lower().removeprefix("www.")
    host = (urlparse(str(getattr(story, "url", ""))).hostname or "").lower().removeprefix("www.")
    source_names = {
        "thediplomat.com": "The Diplomat",
        "indianexpress.com": "The Indian Express",
        "feeds.bbci.co.uk": "BBC News",
        "bbc.co.uk": "BBC News",
        "bbc.com": "BBC News",
        "aljazeera.com": "Al Jazeera",
        "thehindu.com": "The Hindu",
        "hindustantimes.com": "Hindustan Times",
        "indiatoday.in": "India Today",
        "dawn.com": "Dawn",
        "pib.gov.in": "PIB India",
        "mea.gov.in": "MEA India",
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
    }
    label = source_names.get(source_key) or source_names.get(host)
    if not label:
        label = re.sub(r"\s*\(indexed fallback\)", "", source_raw, flags=re.IGNORECASE).strip()
        label = label.removeprefix("www.") or "Source"
    article_url = str(getattr(story, "url", "")).strip()
    if article_url.startswith(("https://", "http://")):
        source_line = f"🗞️ स्रोत: <a href=\"{escape(article_url, quote=True)}\">{escape(label)}</a>"
    else:
        source_line = "🗞️ स्रोत: " + escape(label)
    pieces.append(source_line)
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
        ai_dicts = [item for item in ai_items if isinstance(item, dict)]
        matched_by_id = {}
        for item in ai_dicts:
            key = str(item.get("id", "")).strip()
            if key in lookup and key not in matched_by_id:
                matched_by_id[key] = item

        # If IDs are all wrong/omitted, map only a complete one-response-per-input batch by order.
        used_order_fallback = False
        if not matched_by_id and len(ai_dicts) == len(batch):
            matched_by_id = {story.key: item for story, item in zip(batch, ai_dicts)}
            used_order_fallback = True
            print("[cycle] AI IDs did not match; mapping complete response by input order.")

        approved = {}
        resolved_keys = set()
        missing_headline_fallbacks = 0
        missing_summary_fallbacks = 0
        for story in batch:
            item = matched_by_id.get(story.key)
            if not isinstance(item, dict):
                continue

            # Accept common schema variants from models, not just the preferred field names.
            headline_value = next((item.get(name) for name in (
                "headline", "title", "headline_en", "title_en", "news_title"
            ) if isinstance(item.get(name), str) and item.get(name).strip()), "")
            summary_value = next((item.get(name) for name in (
                "summary", "summary_hi", "summary_hindi", "hindi_summary", "brief", "news_summary", "description"
            ) if isinstance(item.get(name), str) and item.get(name).strip()), "")

            headline = str(headline_value or "").strip()
            if not headline:
                headline = str(story.title or "World News").strip()
                missing_headline_fallbacks += 1

            # Body should be Hindi. If the model supplied no usable summary, keep the item
            # and state plainly that the feed has limited detail rather than inventing facts.
            summary = str(summary_value or "").strip()
            if summary and not any("\u0900" <= char <= "\u097f" for char in summary):
                summary = ""
            if not summary:
                summary = "स्रोत के उपलब्ध विवरण में अतिरिक्त जानकारी सीमित है। पूरी खबर के लिए नीचे दिया गया मूल लिंक देखें।"
                missing_summary_fallbacks += 1

            item["id"] = story.key
            item["headline"] = headline[:180]
            item["summary"] = summary[:1200]
            item["relevant"] = True
            item["analysis"] = str(
                item.get("analysis") or item.get("india_impact") or item.get("impact") or ""
            ).strip()

            if not str(item.get("region", "")).strip():
                item["region"] = "Global"
            topics = item.get("topics", item.get("categories", item.get("category", [])))
            if isinstance(topics, str):
                topics = [part.strip() for part in topics.replace(",", "|").replace(";", "|").split("|") if part.strip()]
            elif not isinstance(topics, list):
                topics = []
            if not topics and getattr(story, "tags", None):
                topics = list(story.tags[:3])
            item["topics"] = [str(topic).strip() for topic in topics if str(topic).strip()][:3] or ["World News"]

            positions = item.get("positions", item.get("sides", []))
            if not isinstance(positions, list):
                positions = []
            normalized_positions = []
            for side_item in positions[:3]:
                if not isinstance(side_item, dict):
                    continue
                side = str(side_item.get("side") or side_item.get("actor") or "").strip()
                position = str(side_item.get("position") or side_item.get("stance") or side_item.get("view") or "").strip()
                if side and position:
                    normalized_positions.append({"side": side[:90], "position": position[:650]})
            item["positions"] = normalized_positions

            approved[story.key] = item
            resolved_keys.add(story.key)

        print(
            f"[cycle] AI response diagnostic: returned={len(ai_items)}, dict_posts={len(ai_dicts)}, "
            f"matched_ids={len(matched_by_id)}, accepted_posts={len(approved)}, "
            f"headline_fallbacks={missing_headline_fallbacks}, summary_fallbacks={missing_summary_fallbacks}, "
            f"order_fallback={used_order_fallback}."
        )

        # A valid AI response is published irrespective of topic/relevance labels.
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

