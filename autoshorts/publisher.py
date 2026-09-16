from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from . import run as runner

pipeline = runner.pipeline
ROOT = pipeline.ROOT
BUILD = pipeline.BUILD
LOGS = pipeline.LOGS
STATE = pipeline.STATE
CONFIG = pipeline.CONFIG
RELIGIOUS = json.loads((ROOT / "religious_content.json").read_text(encoding="utf-8"))

ALLOWED_LICENSES = {
    "CC0",
    "Public domain",
    "CC BY 4.0",
    "CC BY-SA 4.0",
    "CC BY 3.0",
    "CC BY-SA 3.0",
}

GLOBAL_TREND_GEOS = ["US", "GB", "CA", "AU", "IN"]
FACT_KEYWORDS = (
    "science", "space", "technology", "tech", "ai", "robot", "ocean", "nature",
    "animal", "wildlife", "discovery", "archaeology", "history", "planet", "nasa",
    "research", "energy", "climate", "invention", "fossil", "earth", "moon", "mars",
)
FACT_FALLBACKS = [
    "a surprising deep ocean discovery",
    "the science of bioluminescence",
    "how migrating birds navigate",
    "an overlooked invention that changed daily life",
    "a counterintuitive fact about the solar system",
    "how waterfalls reshape landscapes",
    "why some lakes are naturally different colors",
    "the hidden engineering of spider silk",
    "how ancient people navigated before modern maps",
    "why lightning can strike far from a storm",
]

NATURE_QUERIES = [
    "mountain sunrise landscape video",
    "waterfall forest video",
    "river valley drone video",
    "ocean waves sunset video",
    "clouds mountains timelapse video",
    "forest stream nature video",
    "lake mountains drone video",
    "green valley landscape video",
]
NATURE_POSITIVE = {
    "mountain", "mountains", "waterfall", "river", "forest", "valley", "lake", "ocean",
    "sea", "waves", "cloud", "clouds", "sunrise", "sunset", "landscape", "nature",
    "canyon", "meadow", "coast", "beach", "stream", "drone", "snow", "fjord",
}
NATURE_NEGATIVE = {
    "ceres", "planet", "mars", "moon", "city", "town", "street", "building", "airport",
    "car", "train", "bus", "pika", "animal", "people", "person", "crowd", "museum",
}


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()


def _used_media_titles(state: dict) -> set[str]:
    used: set[str] = set()
    for item in state.get("published", []):
        media = item.get("media")
        if isinstance(media, dict) and media.get("title"):
            used.add(media["title"])
        elif isinstance(media, list):
            for entry in media:
                if isinstance(entry, dict) and entry.get("title"):
                    used.add(entry["title"])
    return used


def pick_global_topic(state: dict) -> str:
    """Pick a reusable factual topic from several major Google Trends feeds."""
    used = {str(x.get("topic", "")).lower() for x in state.get("published", [])}
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}

    for geo in GLOBAL_TREND_GEOS:
        try:
            feed = feedparser.parse(f"https://trends.google.com/trending/rss?geo={geo}")
            for rank, entry in enumerate(feed.entries[:30]):
                title = entry.title.strip()
                key = title.lower()
                if not title or key in used:
                    continue
                if not any(word in key for word in FACT_KEYWORDS):
                    continue
                # Cross-country repetition matters more, but high rank still helps.
                counts[key] += max(1, 35 - rank)
                display[key] = title
        except Exception as exc:
            print(f"Trend feed {geo} unavailable: {exc}")

    if counts:
        return display[counts.most_common(1)[0][0]]

    for topic in FACT_FALLBACKS:
        if topic.lower() not in used:
            return topic
    return "a fascinating science fact most people do not know"


def choose_content(state: dict) -> tuple[str, dict, str, str]:
    """Four-video cycle: fact, Quran, fact, hadith."""
    runs = int(state.get("runs", 0))
    slot = runs % 4

    if slot == 1:
        items = RELIGIOUS["quran"]
        item = dict(items[(runs // 4) % len(items)])
        return item["topic"], item, "ar", "quran"

    if slot == 3:
        items = RELIGIOUS["hadith"]
        item = dict(items[(runs // 4) % len(items)])
        return item["topic"], item, "ar", "hadith"

    topic = pick_global_topic(state)
    return topic, pipeline.generate_package(topic), "en", "fact"


def download_commons_audio(file_title: str, destination: Path, required_license: str = "CC0") -> dict:
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "titles": file_title,
        "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata",
        "format": "json",
        "formatversion": 2,
    }
    response = requests.get(api, params=params, headers=pipeline.COMMONS_HEADERS, timeout=30)
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", [])
    if not pages or not pages[0].get("imageinfo"):
        raise RuntimeError(f"Wikimedia Commons audio file not found: {file_title}")

    info = pages[0]["imageinfo"][0]
    mime = (info.get("mime") or "").lower()
    url = info.get("url") or ""
    is_audio = mime.startswith("audio/") or mime in {"application/ogg", "application/octet-stream"} or url.lower().endswith((".ogg", ".oga", ".mp3", ".wav", ".flac"))
    if not is_audio:
        raise RuntimeError(f"Commons file is not recognized as audio: {file_title} ({mime})")

    meta = info.get("extmetadata", {})
    license_name = meta.get("LicenseShortName", {}).get("value", "")
    if required_license and not license_name.upper().startswith(required_license.upper()):
        raise RuntimeError(f"Refusing audio license {license_name!r}; required {required_license!r}")

    if not url:
        raise RuntimeError(f"No downloadable URL for Commons audio: {file_title}")
    audio = requests.get(url, headers=pipeline.COMMONS_HEADERS, timeout=90)
    audio.raise_for_status()
    destination.write_bytes(audio.content)
    pipeline.probe_duration(destination)

    return {
        "title": pages[0].get("title", file_title),
        "source": info.get("descriptionurl"),
        "license": license_name,
        "artist": _strip_html(meta.get("Artist", {}).get("value", "Unknown")),
        "mime": mime,
    }


def _candidate_matches_nature(title: str) -> bool:
    words = set(re.findall(r"[a-z]+", title.lower()))
    if words & NATURE_NEGATIVE:
        return False
    return bool(words & NATURE_POSITIVE)


def _search_commons_video_candidates(query: str) -> list[dict]:
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"filetype:video {query}",
        "gsrnamespace": 6,
        "gsrlimit": 30,
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
        "format": "json",
        "formatversion": 2,
    }
    response = requests.get(api, params=params, headers=pipeline.COMMONS_HEADERS, timeout=35)
    response.raise_for_status()
    return response.json().get("query", {}).get("pages", [])


def download_commons_video(query: str, destination: Path, used_titles: set[str], strict_nature: bool) -> dict:
    candidates = _search_commons_video_candidates(query)

    # Prefer titles that clearly match the requested kind of scene.
    candidates.sort(key=lambda p: 0 if (not strict_nature or _candidate_matches_nature(p.get("title", ""))) else 1)

    for page in candidates:
        title = page.get("title", "")
        if title in used_titles:
            continue
        if strict_nature and not _candidate_matches_nature(title):
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
        if license_name not in ALLOWED_LICENSES:
            continue
        size = int(info.get("size") or 0)
        if size and size > 120 * 1024 * 1024:
            continue
        url = info.get("url")
        if not url:
            continue

        try:
            video = requests.get(url, headers=pipeline.COMMONS_HEADERS, timeout=120)
            video.raise_for_status()
            if len(video.content) > 125 * 1024 * 1024:
                continue
            destination.write_bytes(video.content)
            duration = pipeline.probe_duration(destination)
            if duration < 6.0:
                destination.unlink(missing_ok=True)
                continue
        except Exception:
            destination.unlink(missing_ok=True)
            continue

        return {
            "title": title,
            "source": info.get("descriptionurl"),
            "license": license_name,
            "artist": _strip_html(meta.get("Artist", {}).get("value", "Unknown")),
            "mime": mime,
            "duration": duration,
        }

    raise RuntimeError(f"No suitable licensed video found for {query!r}")


def download_video_set(package: dict, state: dict, duration: float, content_type: str) -> tuple[list[Path], list[dict]]:
    """Download enough unique clips so each scene lasts roughly 7-11 seconds, with no repeats."""
    strict_nature = content_type in {"quran", "hadith"}
    target_count = max(4, min(6, math.ceil(duration / 9.0)))
    used_titles = _used_media_titles(state)

    if strict_nature:
        queries = list(NATURE_QUERIES)
    else:
        queries = list(dict.fromkeys(package.get("media_queries", [])))
        queries += ["science nature video", "technology close up video", "ocean nature video", "mountain landscape video"]

    clips: list[Path] = []
    credits: list[dict] = []
    for index, query in enumerate(queries):
        if len(clips) >= target_count:
            break
        path = BUILD / f"licensed-video-{index}"
        try:
            info = download_commons_video(query, path, used_titles, strict_nature)
        except Exception as exc:
            print(f"Skipping {query!r}: {exc}")
            continue
        used_titles.add(info["title"])
        clips.append(path)
        credits.append(info)
        print(f"Selected clip {len(clips)}: {info['title']} ({info['license']})")

    if len(clips) < 3:
        raise RuntimeError(f"Only {len(clips)} unique licensed videos found; need at least 3")
    return clips, credits


def normalize_clip(source: Path, destination: Path, seconds: float) -> None:
    source_duration = pipeline.probe_duration(source)
    take = min(seconds, max(3.0, source_duration - 0.25))
    start = max(0.0, (source_duration - take) * 0.35)
    fade_out = max(0.0, take - 0.35)
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,fps=30,"
        "eq=saturation=1.04:contrast=1.02,"
        f"fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out:.3f}:d=0.25"
    )
    subprocess.run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{take:.3f}",
        "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", str(destination),
    ], check=True)


def render_montage(clips: list[Path], voice: Path, subtitles: Path, output: Path, duration: float) -> None:
    scene_length = duration / len(clips)
    normalized: list[Path] = []

    for i, clip in enumerate(clips):
        target = BUILD / f"scene-{i}.mp4"
        normalize_clip(clip, target, scene_length + 0.15)
        normalized.append(target)

    concat_file = BUILD / "concat-scenes.txt"
    lines = []
    for clip in normalized:
        escaped = str(clip.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    raw_montage = BUILD / "raw-montage.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(raw_montage),
    ], check=True)

    sub_path = str(subtitles).replace("'", "'\\''")
    vf = (
        "subtitles='" + sub_path +
        "':force_style='FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=250'"
    )
    subprocess.run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(raw_montage), "-i", str(voice),
        "-t", f"{duration:.3f}", "-vf", vf, "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac",
        "-b:a", "160k", "-pix_fmt", "yuv420p", "-shortest", str(output),
    ], check=True)


def download_fallback_photos(package: dict) -> tuple[list[Path], list[dict]]:
    backgrounds: list[Path] = []
    credits: list[dict] = []
    queries = list(dict.fromkeys(package.get("media_queries", [])))[:4] or ["peaceful nature"]
    for i, query in enumerate(queries):
        raw = BUILD / f"fallback-source-{i}"
        bg = BUILD / f"fallback-bg-{i}.jpg"
        try:
            info = pipeline.download_commons_image(query, raw)
            pipeline.make_background(raw, bg)
        except Exception as exc:
            print(f"Fallback image skipped: {exc}")
            continue
        backgrounds.append(bg)
        credits.append(info)
    if not backgrounds:
        raise RuntimeError("No fallback visuals available")
    return backgrounds, credits


def render_photo_fallback(backgrounds: list[Path], voice: Path, subtitles: Path, output: Path, duration: float) -> None:
    count = len(backgrounds)
    segment = duration / count
    cmd = ["ffmpeg", "-y"]
    for image in backgrounds:
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{segment:.3f}", "-i", str(image)]
    cmd += ["-i", str(voice)]

    filters = []
    for i in range(count):
        filters.append(
            f"[{i}:v]scale=1200:2134:force_original_aspect_ratio=increase,crop=1080:1920,"
            f"zoompan=z='min(zoom+0.00065,1.07)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s=1080x1920:fps=30,trim=duration={segment:.3f},setpts=PTS-STARTPTS[v{i}]"
        )
    joined = "".join(f"[v{i}]" for i in range(count))
    filters.append(f"{joined}concat=n={count}:v=1:a=0[base]")
    sub_path = str(subtitles).replace("'", "'\\''")
    filters.append(
        "[base]subtitles='" + sub_path +
        "':force_style='FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=250'[vout]"
    )
    cmd += [
        "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", f"{count}:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac",
        "-b:a", "160k", "-pix_fmt", "yuv420p", "-shortest", str(output),
    ]
    subprocess.run(cmd, check=True)


def upload_localized(video: Path, package: dict, language: str) -> str:
    credentials = Credentials(
        None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    description = package["description"] + "\n\n" + " ".join(package["hashtags"]) + " #Shorts"
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": package["title"],
                "description": description,
                "categoryId": "27",
                "defaultLanguage": language,
            },
            "status": {
                "privacyStatus": CONFIG["privacy_status"],
                "selfDeclaredMadeForKids": False,
            },
        },
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    state = pipeline.load_state()

    if len(state.get("published", [])) >= CONFIG["max_videos"]:
        print("Campaign complete")
        return

    topic, package, language, content_type = choose_content(state)
    tts_voice = BUILD / "voice.mp3"
    human_voice = BUILD / "human-recitation"
    subtitles = BUILD / "captions.srt"
    video = BUILD / "short.mp4"

    audio_info = None
    human_audio_file = package.get("human_audio_file")
    if human_audio_file:
        audio_info = download_commons_audio(
            human_audio_file,
            human_voice,
            package.get("required_audio_license", "CC0"),
        )
        voice = human_voice
        package["description"] += (
            f"\n\nالتلاوة البشرية: {audio_info['artist']}"
            f"\nالمصدر: {audio_info['source']}"
            f"\nالترخيص: {audio_info['license']}"
        )
    else:
        voice = tts_voice
        original_voice = CONFIG["voice"]
        try:
            if language == "ar":
                CONFIG["voice"] = CONFIG.get("arabic_voice", "ar-SA-HamedNeural")
            asyncio.run(pipeline.create_voice(package["script"], voice))
        finally:
            CONFIG["voice"] = original_voice

    duration = pipeline.probe_duration(voice)
    pipeline.create_subtitles(package["script"], duration, subtitles)

    visual_mode = "licensed_video_montage"
    try:
        clips, media_info = download_video_set(package, state, duration, content_type)
        package["description"] += "\n\nمشاهد مرخصة مستخدمة:"
        for item in media_info:
            package["description"] += f"\n- {item['artist']} | {item['license']} | {item['source']}"
        render_montage(clips, voice, subtitles, video, duration)
    except Exception as exc:
        print(f"Video montage unavailable, using moving photos: {exc}")
        visual_mode = "moving_licensed_photos"
        backgrounds, media_info = download_fallback_photos(package)
        render_photo_fallback(backgrounds, voice, subtitles, video, duration)

    video_id = upload_localized(video, package, language)
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "video_id": video_id,
        "title": package["title"],
        "language": language,
        "content_type": content_type,
        "visual_mode": visual_mode,
        "media": media_info,
    }
    if audio_info:
        record["audio"] = audio_info

    state["published"].append(record)
    state["runs"] = int(state.get("runs", 0)) + 1
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (LOGS / f"{datetime.now(timezone.utc):%Y-%m-%dT%H-%M-%SZ}.json").write_text(
        json.dumps({**record, "script": package["script"]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Uploaded https://youtube.com/shorts/{video_id}")


if __name__ == "__main__":
    main()
