"""Polite RSS collectors. Website article bodies are fetched only when robots.txt allows it.

YouTube is read from its channel RSS feed (metadata only); this code does not download videos
or scrape transcripts.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import html
import os
import re
import time
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import feedparser
import requests
import trafilatura

from story import Story

USER_AGENT = "GeopoliticsNewsBot/1.0 (+https://github.com; RSS reader)"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/html, */*"})
_ROBOTS: dict[str, RobotFileParser | None] = {}
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return url.strip()
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS]
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query), ""))
    except Exception:
        return url.strip()


def story_key(url: str, title: str) -> str:
    normalized = normalize_url(url)
    material = normalized if normalized else re.sub(r"\s+", " ", title.casefold()).strip()
    return hashlib.sha256(material.encode("utf-8", "ignore")).hexdigest()[:24]


def clean_html(value: str) -> str:
    text = trafilatura.extract(value or "", include_links=False, include_comments=False) or ""
    if not text:
        text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _published(entry) -> tuple[str, float]:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        ts = calendar.timegm(parsed)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat(), ts

    raw = str(entry.get("published") or entry.get("updated") or "").strip()
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), ts
        except (TypeError, ValueError, OverflowError):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), ts
            except (TypeError, ValueError, OverflowError):
                pass

    # Never pretend an undated item is new; that can replay old articles after restarts.
    return raw, 0.0


def _today_start_ts() -> float:
    """Today's midnight in NEWS_TIMEZONE (Asia/Kolkata by default), as UTC epoch."""
    zone_name = os.getenv("NEWS_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        zone = ZoneInfo("Asia/Kolkata")
    now_local = datetime.now(zone)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).timestamp()


def _robots_allows(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _ROBOTS:
        rp = RobotFileParser()
        robots_url = origin + "/robots.txt"
        try:
            response = _get_with_retries(robots_url, timeout=20, attempts=2)
            if response.status_code == 404:
                _ROBOTS[origin] = None  # no robots file: default allow
            elif response.ok:
                rp.set_url(robots_url)
                rp.parse(response.text.splitlines())
                _ROBOTS[origin] = rp
            else:
                _ROBOTS[origin] = False  # if access rules can't be checked, don't fetch article body
        except requests.RequestException:
            _ROBOTS[origin] = False
    policy = _ROBOTS[origin]
    if policy is False:
        return False
    if policy is None:
        return True
    return bool(policy.can_fetch(USER_AGENT, url))


def _article_text(url: str) -> str:
    if not _robots_allows(url):
        return ""
    try:
        response = _get_with_retries(url, timeout=25, attempts=2)
        if not response.ok or "text/html" not in response.headers.get("content-type", "text/html"):
            return ""
        extracted = trafilatura.extract(response.text, include_comments=False, include_tables=False,
                                        include_links=False, favor_precision=True)
        return (extracted or "")[:6500]
    except requests.RequestException:
        return ""



def _get_with_retries(url: str, *, timeout: int = 25, attempts: int = 2, headers: dict | None = None):
    """Retry transient network/read-timeout failures, but do not retry hard 403/404 responses."""
    attempts = max(1, attempts)
    last_exc = None
    for attempt in range(attempts):
        try:
            response = HTTP.get(url, timeout=timeout, allow_redirects=True, headers=headers)
            if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait_seconds = max(1, min(8, int(retry_after)))
                except (TypeError, ValueError):
                    wait_seconds = 2 * (attempt + 1)
                print(f"[collector] Temporary HTTP {response.status_code} for {urlparse(url).hostname}; retrying in {wait_seconds}s.")
                time.sleep(wait_seconds)
                continue
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                raise
            wait_seconds = 2 * (attempt + 1)
            print(f"[collector] Temporary network error for {urlparse(url).hostname}: {exc}; retrying in {wait_seconds}s.")
            time.sleep(wait_seconds)
    if last_exc:
        raise last_exc
    raise requests.RequestException(f"Could not fetch {url}")


def _yt_text(value) -> str:
    """Extract readable text from YouTube's small text/run objects."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"].strip()
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict)).strip()
    return ""


def _balanced_json_object(text: str, from_index: int) -> dict | None:
    """Read one balanced JSON object starting at/after from_index, respecting quoted braces."""
    start = text.find("{", from_index)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:index + 1])
                    return obj if isinstance(obj, dict) else None
                except (ValueError, TypeError):
                    return None
    return None


def _extract_yt_initial_data(page_html: str) -> dict | None:
    """Extract ytInitialData without requiring an API key or video playlist feed."""
    markers = (
        "ytInitialData =",
        'window["ytInitialData"] =',
        "window['ytInitialData'] =",
        '"ytInitialData":',
    )
    for marker in markers:
        pos = page_html.find(marker)
        if pos >= 0:
            data = _balanced_json_object(page_html, pos + len(marker))
            if data is not None:
                return data
    return None


def _walk_youtube_videos(node):
    """Yield video-card-shaped dictionaries anywhere inside YouTube's page data."""
    if isinstance(node, dict):
        title = node.get("title")
        if node.get("videoId") and isinstance(title, (dict, str)) and node.get("publishedTimeText"):
            yield node
        for value in node.values():
            yield from _walk_youtube_videos(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_youtube_videos(value)


def _yt_published_ts(label: str, now_ts: float) -> float:
    """Convert YouTube's English relative upload label to an approximate epoch timestamp."""
    value = re.sub(r"\s+", " ", (label or "").strip()).lower()
    match = re.search(r"(?:(?:streamed|premiered|published|uploaded)\s+)?(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", value)
    if match:
        count = int(match.group(1))
        unit = match.group(2)
        seconds = {
            "second": 1, "minute": 60, "hour": 3600, "day": 86400,
            "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400,
        }[unit] * count
        # YouTube rounds relative day/week labels. Add half a unit to avoid falsely treating
        # a video labelled "1 day ago" as definitely published today.
        if unit == "day":
            seconds += 12 * 3600
        elif unit == "week":
            seconds += 3.5 * 86400
        elif unit == "month":
            seconds += 15 * 86400
        elif unit == "year":
            seconds += 182 * 86400
        return now_ts - seconds

    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=datetime.now(ZoneInfo("Asia/Kolkata")).year)
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            timestamp = parsed.timestamp()
            if timestamp > now_ts + 12 * 3600:
                parsed = parsed.replace(year=parsed.year - 1)
                timestamp = parsed.timestamp()
            return timestamp
        except ValueError:
            continue
    return 0.0


def _parse_youtube_videos_section(channel_id: str, max_age_hours: int) -> list[Story]:
    """Read recent uploads from the channel's actual /videos section (not its uploads playlist)."""
    channel_id = (channel_id or "").strip()
    if not channel_id.startswith("UC"):
        print("[collector] YOUTUBE_CHANNEL_ID must be a channel ID starting with UC; skipping YouTube.")
        return []

    urls = [
        f"https://www.youtube.com/channel/{channel_id}/videos?hl=en&gl=US",
        f"https://m.youtube.com/channel/{channel_id}/videos?hl=en&gl=US",
    ]
    page_data = None
    last_error = None
    browser_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    for page_url in urls:
        try:
            response = _get_with_retries(page_url, timeout=25, attempts=2, headers=browser_headers)
            response.raise_for_status()
            page_data = _extract_yt_initial_data(response.text)
            if page_data:
                break
            last_error = "YouTube page loaded, but ytInitialData was not found"
        except requests.RequestException as exc:
            last_error = str(exc)
            print(f"[collector] YouTube Videos section request failed for {urlparse(page_url).hostname}: {exc}")

    if not page_data:
        print(f"[collector] YouTube Videos section could not be parsed; no YouTube items this cycle. {last_error or ''}")
        return []

    now = time.time()
    today_start_ts = _today_start_ts()
    stories: list[Story] = []
    seen_ids: set[str] = set()
    cards_seen = 0
    for card in _walk_youtube_videos(page_data):
        video_id = str(card.get("videoId", "")).strip()
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        cards_seen += 1
        title = clean_html(_yt_text(card.get("title")))
        if not title:
            continue
        published_label = _yt_text(card.get("publishedTimeText"))
        published_ts = _yt_published_ts(published_label, now)
        if published_ts <= 0 or published_ts < today_start_ts:
            continue
        if published_ts > now + 15 * 60 or now - published_ts > max_age_hours * 3600:
            continue
        description = clean_html(_yt_text(card.get("descriptionSnippet")))[:2500]
        published_at = datetime.fromtimestamp(published_ts, timezone.utc).isoformat()
        url = f"https://www.youtube.com/watch?v={video_id}"
        key = story_key(url, title)
        content = description or title
        stories.append(Story(
            key, "YouTube", title, url, published_at, published_ts,
            description, content, [],
        ))

    print(f"[collector] YouTube Videos section cards={cards_seen}; eligible_today={len(stories)}.")
    return sorted(stories, key=lambda story: story.published_ts)


def _parse_feed(url: str, source: str, max_age_hours: int) -> list[Story]:
    try:
        response = _get_with_retries(url, timeout=25, attempts=2)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception as exc:
        response_obj = getattr(exc, "response", None)
        status_code = getattr(response_obj, "status_code", None)
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        fallback_url = None
        fallback_source = None
        if status_code == 403 and host == "mea.gov.in" and "indexed fallback" not in source.lower():
            fallback_url = "https://news.google.com/rss/search?q=site%3Amea.gov.in&hl=en-IN&gl=IN&ceid=IN%3Aen"
            fallback_source = "MEA (indexed fallback)"
        elif status_code == 403 and host == "pib.gov.in" and "indexed fallback" not in source.lower():
            fallback_url = "https://news.google.com/rss/search?q=site%3Apib.gov.in&hl=en-IN&gl=IN&ceid=IN%3Aen"
            fallback_source = "PIB (indexed fallback)"
        if fallback_url:
            print(f"[collector] {source} official feed returned 403; using a domain-scoped search-index fallback.")
            return _parse_feed(fallback_url, fallback_source, max_age_hours)
        print(f"[collector] Feed error ({source}): {exc}")
        return []
    if getattr(parsed, "bozo", False) and not parsed.entries:
        print(f"[collector] Invalid/unavailable feed ({source}): {getattr(parsed, 'bozo_exception', 'unknown error')}")
        return []

    now = time.time()
    today_start_ts = _today_start_ts()
    stories: list[Story] = []
    for entry in parsed.entries:
        link = str(entry.get("link", "")).strip()
        title = clean_html(str(entry.get("title", "")))
        if not link or not title:
            continue

        published_at, published_ts = _published(entry)
        # Today's India-date only: do not publish 23 October or other previous-day items.
        if published_ts <= 0 or published_ts < today_start_ts:
            continue
        if published_ts > now + 15 * 60:
            continue
        if now - published_ts > max_age_hours * 3600:
            continue

        summary = clean_html(str(entry.get("summary", "") or entry.get("description", "")))[:2500]
        source_tags = []
        for tag in entry.get("tags", []) or []:
            if isinstance(tag, dict):
                value = clean_html(str(tag.get("term") or tag.get("label") or ""))
                if value and value.lower() not in {x.lower() for x in source_tags}:
                    source_tags.append(value[:80])
        key = story_key(link, title)
        stories.append(Story(
            key, source, title, normalize_url(link), published_at, published_ts,
            summary, summary, source_tags[:12],
        ))
    # Oldest-first ensures all newly eligible articles are processed in publication order.
    return sorted(stories, key=lambda story: story.published_ts)


def _source_label(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host.endswith("thediplomat.com"):
        return "The Diplomat"
    if host.endswith("indianexpress.com"):
        return "The Indian Express"
    if host.endswith("mea.gov.in"):
        return "MEA"
    if host.endswith("pib.gov.in"):
        return "PIB"
    return host or "Website"


def collect_sources(website_rss_urls: list[str] | str, youtube_channel_id: str, max_age_hours: int) -> list[Story]:
    all_stories: list[Story] = []
    if isinstance(website_rss_urls, str):
        website_urls = [url.strip() for url in website_rss_urls.split(",") if url.strip()]
    else:
        website_urls = [str(url).strip() for url in website_rss_urls if str(url).strip()]

    for feed_url in website_urls:
        all_stories.extend(_parse_feed(feed_url, _source_label(feed_url), max_age_hours))

    if youtube_channel_id:
        # User requested the channel's Videos section, not an uploads-playlist feed.
        all_stories.extend(_parse_youtube_videos_section(youtube_channel_id, max_age_hours))
    # Dedupe within this collection by normalized URL/key.
    unique: dict[str, Story] = {}
    for story in all_stories:
        unique.setdefault(story.key, story)
    # Oldest-first lets each 30-minute cycle process every newly eligible item without a fixed post cap.
    return sorted(unique.values(), key=lambda story: story.published_ts)


def enrich_story(story: Story) -> Story:
    """Fetch allowed article content; resolve indexed-feed links when possible."""
    if story.source.endswith("(indexed fallback)") and (urlparse(story.url).hostname or "").lower() == "news.google.com":
        try:
            response = HTTP.get(story.url, timeout=10, allow_redirects=True, stream=True)
            final_url = response.url
            response.close()
            final_host = (urlparse(final_url).hostname or "").lower()
            if final_host and final_host != "news.google.com":
                story.url = normalize_url(final_url)
        except requests.RequestException as exc:
            print(f"[collector] Could not resolve indexed article link; retaining source redirect: {exc}")
        if (urlparse(story.url).hostname or "").lower() == "news.google.com":
            return story
    if story.source != "YouTube" and story.url.startswith(("https://", "http://")):
        body = _article_text(story.url)
        if body:
            story.content = body
    # YouTube is metadata-only in this version; no video/transcript scraping.
    return story
