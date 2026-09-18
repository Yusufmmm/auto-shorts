from __future__ import annotations

import math
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

import feedparser
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from . import publisher as base

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

CURRENT_CONTEXT: dict = {}
ORIGINAL_CHOOSE_CONTENT = base.choose_content
ORIGINAL_CREATE_SUBTITLES = base.pipeline.create_subtitles
ORIGINAL_RENDER_MONTAGE = base.render_montage
ORIGINAL_RENDER_PHOTO_FALLBACK = base.render_photo_fallback


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
        topic_queries = list(dict.fromkeys(package.get("media_queries", [])))
        try_queries(topic_queries, False)
        if len(clips) < target_count:
            try_queries(list(base.NATURE_QUERIES), True)

    if len(clips) < target_count:
        raise RuntimeError(f"Only {len(clips)} suitable unique clips found; need {target_count}")
    return clips, credits


def hide_bad_test_video() -> None:
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


def choose_content(state: dict):
    trigger_path = base.ROOT / ".github" / "auto-shorts-trigger"
    trigger_text = trigger_path.read_text(encoding="utf-8") if trigger_path.exists() else ""

    if "QURAN_STYLE_TEST" in trigger_text:
        items = base.RELIGIOUS["quran"]
        item = dict(items[1 % len(items)])
        result = (item["topic"], item, "ar", "quran")
    else:
        result = ORIGINAL_CHOOSE_CONTENT(state)

    topic, package, language, content_type = result
    CURRENT_CONTEXT.clear()
    CURRENT_CONTEXT.update({
        "topic": topic,
        "package": package,
        "language": language,
        "content_type": content_type,
    })
    return result


def _ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h = cs // 360000
    m = (cs // 6000) % 60
    s = (cs // 100) % 60
    c = cs % 100
    return f"{h}:{m:02}:{s:02}.{c:02}"


def _arabic_number(number: int) -> str:
    digits = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
    return str(number).translate(digits)


def _escape_ass(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _wrap_quran_text(text: str, words_per_line: int = 6) -> str:
    """Keep long verses readable on a vertical phone screen."""
    words = text.split()
    if len(words) <= words_per_line:
        return text
    lines = []
    for i in range(0, len(words), words_per_line):
        lines.append(" ".join(words[i:i + words_per_line]))
    return r"\N".join(lines)


def create_quran_ass(package: dict, duration: float, destination: Path) -> None:
    verses = list(package.get("verses") or [])
    opening = (package.get("opening") or "").strip()
    surah_name = package.get("surah_name") or package.get("title", "القرآن الكريم").split("|")[0].strip()

    events: list[tuple[str, int | None]] = []
    if opening:
        events.append((opening, None))
    for verse in verses:
        events.append((str(verse["text"]).strip(), int(verse["number"])))

    if not events:
        ORIGINAL_CREATE_SUBTITLES(package["script"], duration, destination)
        return

    weights = [max(3, len(re.findall(r"\S+", text))) for text, _ in events]
    total_weight = sum(weights)
    cursor = 0.0
    event_lines = []

    for (text, number), weight in zip(events, weights):
        span = duration * weight / total_weight
        start = cursor
        end = min(duration, cursor + span)
        cursor = end

        if number is None:
            display = _wrap_quran_text(text, 5)
            style = "Opening"
            event_lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},{style},,0,0,0,,"
                f"{{\\fad(260,260)}}{_escape_ass(display)}"
            )
        else:
            display = _wrap_quran_text(text, 6)
            event_lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Verse,,0,0,0,,"
                f"{{\\fad(260,260)}}{_escape_ass(display)}"
            )
            # Put the verse number on its own line. Using a single Qur'anic end-of-ayah
            # ornament avoids the RTL mirroring problem of paired ornate brackets.
            marker = f"۝ {_arabic_number(number)}"
            event_lines.append(
                f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},VerseNo,,0,0,0,,"
                f"{{\\fad(260,260)}}{_escape_ass(marker)}"
            )

    title_line = (
        f"Dialogue: 0,{_ass_time(0)},{_ass_time(duration)},SurahTitle,,0,0,0,,"
        f"{{\\fad(450,450)}}{_escape_ass(surah_name)}"
    )

    ass = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: SurahTitle,Noto Naskh Arabic,50,&H0000D7FF,&H0000D7FF,&H00111111,&H55000000,-1,0,0,0,100,100,0,0,1,2,1,8,80,80,95,1
Style: Opening,Noto Naskh Arabic,66,&H00FFFFFF,&H00FFFFFF,&H00101010,&H70000000,-1,0,0,0,100,100,0,0,3,2,0,5,100,100,0,1
Style: Verse,Noto Naskh Arabic,70,&H00FFFFFF,&H00FFFFFF,&H00101010,&H70000000,-1,0,0,0,100,100,0,0,3,2,0,5,95,95,30,1
Style: VerseNo,Noto Naskh Arabic,42,&H0000D7FF,&H0000D7FF,&H00101010,&H55000000,-1,0,0,0,100,100,0,0,1,2,1,2,0,0,330,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    ass += title_line + "\n" + "\n".join(event_lines) + "\n"
    destination.write_text(ass, encoding="utf-8")


def create_subtitles(script: str, duration: float, output: Path) -> None:
    if CURRENT_CONTEXT.get("content_type") == "quran":
        package = CURRENT_CONTEXT.get("package") or {}
        quran_ass = base.BUILD / "quran-captions.ass"
        create_quran_ass(package, duration, quran_ass)
        output.write_text("", encoding="utf-8")
        return
    ORIGINAL_CREATE_SUBTITLES(script, duration, output)


def _quran_ass_filter() -> str:
    quran_ass = base.BUILD / "quran-captions.ass"
    escaped = str(quran_ass).replace("'", "'\\''")
    return f"subtitles='{escaped}'"


def render_montage(clips: list[Path], voice: Path, subtitles: Path, output: Path, duration: float) -> None:
    if CURRENT_CONTEXT.get("content_type") != "quran":
        return ORIGINAL_RENDER_MONTAGE(clips, voice, subtitles, output, duration)

    scene_length = duration / len(clips)
    normalized: list[Path] = []
    for i, clip in enumerate(clips):
        target = base.BUILD / f"scene-{i}.mp4"
        base.normalize_clip(clip, target, scene_length + 0.15)
        normalized.append(target)

    concat_file = base.BUILD / "concat-scenes.txt"
    lines = []
    for clip in normalized:
        escaped = str(clip.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    raw_montage = base.BUILD / "raw-montage.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(raw_montage),
    ], check=True)

    subprocess.run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(raw_montage), "-i", str(voice),
        "-t", f"{duration:.3f}", "-vf", _quran_ass_filter(),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "medium",
        "-crf", "21", "-c:a", "aac", "-b:a", "160k", "-pix_fmt", "yuv420p",
        "-shortest", str(output),
    ], check=True)


def render_photo_fallback(backgrounds: list[Path], voice: Path, subtitles: Path, output: Path, duration: float) -> None:
    if CURRENT_CONTEXT.get("content_type") != "quran":
        return ORIGINAL_RENDER_PHOTO_FALLBACK(backgrounds, voice, subtitles, output, duration)

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
    filters.append(f"{joined}concat=n={count}:v=1:a=0[basev]")
    filters.append(f"[basev]{_quran_ass_filter()}[vout]")
    cmd += [
        "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", f"{count}:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac",
        "-b:a", "160k", "-pix_fmt", "yuv420p", "-shortest", str(output),
    ]
    subprocess.run(cmd, check=True)


base.pick_global_topic = pick_global_topic
base.download_commons_video = download_commons_video
base.download_video_set = download_video_set
base.choose_content = choose_content
base.pipeline.create_subtitles = create_subtitles
base.render_montage = render_montage
base.render_photo_fallback = render_photo_fallback


def main() -> None:
    hide_bad_test_video()
    base.main()


if __name__ == "__main__":
    main()
