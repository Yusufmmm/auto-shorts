from __future__ import annotations

import math
import os
import re
from collections import Counter
from pathlib import Path

import feedparser
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from . import publisher as base

# Keep the channel inside its intended science / nature / technology / history niche.
TREND_KEYWORDS = {
    "science", "space", "technology", "robot", "robotics", "ocean", "nature",
    "animal", "wildlife", "discovery", "archaeology", "history", "planet", "nasa",
    "research", "energy", "climate", "invention", "fossil", "earth", "moon", "mars",
    "telescope", "asteroid", "volcano", "earthquake", "dinosaur", "biology", "physics",
}
TREND_PHRASES = {"artificial intelligence", "machine learning", "deep sea", "solar system"}
EXCLUDED_NEWS_WORDS = {
    "war", "election", "president", "minister", "military", "attack", "missile", "ceasefire",
    "ukraine", "russia", "russian", "israel", "gaza", "iran", "trump", "putin", "netanyahu",
    "congress", "senate", "parliament", "sanctions", "nato", "campaign", "vote", "voting",
}
QUERY_STOPWORDS = {
    "video", "videos", "footage", "clip", "clips", "nature", "science", "technology",
    "close", "up", "beautiful", "cinematic", "short", "vertical", "stock", "drone",
}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def pick_global_topic(state: dict) -> str:
    used = {str(x.get("topic", "")).lower() for x in state.get("published", [])}
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}

    for geo in base.GLOBAL_TREND_GEOS:
        try:
            feed = feedparser.parse(f"https://trends.google.com/trending/rss?geo={geo}")
            for rank, entry in enumerate(feed.entries[:35]):
                title = entry.title.strip()
                key = title.lower()
                if not title or key in used:
                    continue
                words = _tokens(title)
                if words & EXCLUDED_NEWS_WORDS:
                    continue
                relevant = bool(words & TREND_KEYWORDS) or any(phrase in key for phrase in TREND_PHRASES)
                if not relevant:
                    continue
                counts[key] += max(1, 40 - rank)
                display[key] = title
        except Exception as exc:
            print(f"Trend feed {geo} unavailable: {exc}")

    if counts:
        return display[counts.most_common(1)[0][0]]

    for topic in base.FACT_FALLBACKS:
        if topic.lower() not in used:
            return topic
    return "a fascinating science fact most people do not know"


def _meaningful_query_words(query: str) -> set[str]:
    return {w for w in _tokens(query) if len(w) >= 4 and w not in QUERY_STOPWORDS}


def _candidate_relevant(title: str, query: str, strict_nature: bool) -> bool:
    if strict_nature:
        return base._candidate_matches_nature(title)
    wanted = _meaningful_query_words(query)
    if not wanted:
        return base._candidate_matches_nature(title)
    title_words = _tokens(title)
    return bool(title_words & wanted)


def download_commons_video(query: str, destination: Path, used_titles: set[str], strict_nature: bool) -> dict:
    candidates = base._search_commons_video_candidates(query)
    candidates.sort(key=lambda p: 0 if _candidate_relevant(p.get("title", ""), query, strict_nature) else 1)

    for page in candidates:
        title = page.get("title", "")
        if not title or title in used_titles:
            continue
        if not _candidate_relevant(title, query, strict_nature):
            continue

        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        mime = (info.get("mime") or "").lower()
        if not mime.startswith("video/"):
            continue
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "")
        if license_name not in base.ALLOWED_LICENSES:
            continue
        size = int(info.get("size") or 0)
        if size and size > 70 * 1024 * 1024:
            continue
        url = info.get("url")
        if not url:
            continue

        try:
            response = requests.get(url, headers=base.pipeline.COMMONS_HEADERS, timeout=100)
            response.raise_for_status()
            if len(response.content) > 75 * 1024 * 1024:
                continue
            destination.write_bytes(response.content)
            duration = base.pipeline.probe_duration(destination)
            # Longer source clips let us keep each scene on screen long enough to feel intentional.
            if duration < 12.0:
                destination.unlink(missing_ok=True)
                continue
        except Exception:
            destination.unlink(missing_ok=True)
            continue

        return {
            "title": title,
            "source": info.get("descriptionurl"),
            "license": license_name,
            "artist": base._strip_html(meta.get("Artist", {}).get("value", "Unknown")),
            "mime": mime,
            "duration": duration,
        }

    raise RuntimeError(f"No relevant licensed video found for {query!r}")


def download_video_set(package: dict, state: dict, duration: float, content_type: str):
    """Use 3-5 unique clips, around 9-14 seconds each, and never recycle older channel clips."""
    target_count = max(3, min(5, math.ceil(duration / 11.0)))
    used_titles = base._used_media_titles(state)
    clips: list[Path] = []
    credits: list[dict] = []

    def try_queries(queries: list[str], strict_nature: bool) -> None:
        for index, query in enumerate(queries):
            if len(clips) >= target_count:
                return
            path = base.BUILD / f"v2-licensed-{len(clips)}-{index}"
            try:
                info = download_commons_video(query, path, used_titles, strict_nature)
            except Exception as exc:
                print(f"Skipping {query!r}: {exc}")
                continue
            used_titles.add(info["title"])
            clips.append(path)
            credits.append(info)
            print(f"Selected clip {len(clips)}: {info['title']} ({info['license']})")

    if content_type in {"quran", "hadith"}:
        try_queries(list(base.NATURE_QUERIES), True)
    else:
        # First try visuals that actually match the generated topic.
        topic_queries = list(dict.fromkeys(package.get("media_queries", [])))
        try_queries(topic_queries, False)
        # If Commons does not have enough relevant footage, fill the rest with clean nature scenes.
        if len(clips) < target_count:
            try_queries(list(base.NATURE_QUERIES), True)

    if len(clips) < target_count:
        raise RuntimeError(f"Only {len(clips)} suitable unique clips found; need {target_count}")
    return clips, credits


def hide_bad_test_video() -> None:
    """Make the one off-niche test upload private once the corrected publisher starts."""
    video_id = "rAcvnNqqKpY"
    try:
        credentials = Credentials(
            None,
            refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["YOUTUBE_CLIENT_ID"],
            client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
        )
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        current = youtube.videos().list(part="status", id=video_id).execute().get("items", [])
        if not current:
            return
        status = current[0].get("status", {})
        if status.get("privacyStatus") == "private":
            return
        status["privacyStatus"] = "private"
        youtube.videos().update(part="status", body={"id": video_id, "status": status}).execute()
        print(f"Made unsuitable test video private: {video_id}")
    except Exception as exc:
        print(f"Could not hide unsuitable test video: {exc}")


# Patch the proven publisher with stricter selection rules.
base.pick_global_topic = pick_global_topic
base.download_commons_video = download_commons_video
base.download_video_set = download_video_set


def main() -> None:
    hide_bad_test_video()
    base.main()


if __name__ == "__main__":
    main()
