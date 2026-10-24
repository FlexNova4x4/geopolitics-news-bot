"""Polite RSS collectors. Website article bodies are fetched only when robots.txt allows it.

YouTube is read from its channel RSS feed (metadata only); this code does not download videos
or scrape transcripts.
"""
from __future__ import annotations

import calendar
import hashlib
import html
import re
import time
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
    raw = entry.get("published") or entry.get("updated") or ""
    return str(raw), time.time()


def _robots_allows(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _ROBOTS:
        rp = RobotFileParser()
        robots_url = origin + "/robots.txt"
        try:
            response = HTTP.get(robots_url, timeout=8)
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
        response = HTTP.get(url, timeout=12, allow_redirects=True)
        if not response.ok or "text/html" not in response.headers.get("content-type", "text/html"):
            return ""
        extracted = trafilatura.extract(response.text, include_comments=False, include_tables=False,
                                        include_links=False, favor_precision=True)
        return (extracted or "")[:6500]
    except requests.RequestException:
        return ""


def _parse_feed(url: str, source: str, max_age_hours: int) -> list[Story]:
    try:
        response = HTTP.get(url, timeout=15, allow_redirects=True)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception as exc:
        print(f"[collector] Feed error ({source}): {exc}")
        return []
    if getattr(parsed, "bozo", False) and not parsed.entries:
        print(f"[collector] Invalid/unavailable feed ({source}): {getattr(parsed, 'bozo_exception', 'unknown error')}")
        return []

    now = time.time()
    stories: list[Story] = []
    for entry in parsed.entries[:30]:
        link = str(entry.get("link", "")).strip()
        title = clean_html(str(entry.get("title", "")))
        if not link or not title:
            continue
        published_at, published_ts = _published(entry)
        if now - published_ts > max_age_hours * 3600:
            continue
        summary = clean_html(str(entry.get("summary", "") or entry.get("description", "")))[:2500]
        # Fetch only feed metadata at collection time. The engine enriches only the
        # few new candidates selected for this cycle, avoiding repeated article reads.
        key = story_key(link, title)
        stories.append(Story(key, source, title, normalize_url(link), published_at,
                             published_ts, summary, summary))
    return sorted(stories, key=lambda story: story.published_ts, reverse=True)


def collect_sources(website_rss_url: str, youtube_channel_id: str, max_age_hours: int) -> list[Story]:
    all_stories: list[Story] = []
    if website_rss_url:
        all_stories.extend(_parse_feed(website_rss_url, "Website", max_age_hours))
    if youtube_channel_id:
        channel_id = youtube_channel_id.strip()
        if channel_id.startswith("UC"):
            yt_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            all_stories.extend(_parse_feed(yt_url, "YouTube", max_age_hours))
        else:
            print("[collector] YOUTUBE_CHANNEL_ID must be the channel ID (normally starts with UC). Skipping YouTube for this cycle.")
    # Dedupe within this collection by normalized URL/key.
    unique: dict[str, Story] = {}
    for story in all_stories:
        unique.setdefault(story.key, story)
    return sorted(unique.values(), key=lambda story: story.published_ts, reverse=True)


def enrich_story(story: Story) -> Story:
    """Fetch a website article body only for a newly selected story, respecting robots.txt."""
    if story.source == "Website" and story.url.startswith(("https://", "http://")):
        body = _article_text(story.url)
        if body:
            story.content = body
    # YouTube is metadata-only in this version; no video/transcript scraping.
    return story
