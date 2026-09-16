from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import run as runner

pipeline = runner.pipeline
ROOT = pipeline.ROOT
BUILD = pipeline.BUILD
LOGS = pipeline.LOGS
STATE = pipeline.STATE
CONFIG = pipeline.CONFIG
RELIGIOUS = json.loads((ROOT / "religious_content.json").read_text(encoding="utf-8"))

ALLOWED_VISUAL_LICENSES = {
    "CC0",
    "Public domain",
    "CC BY 4.0",
    "CC BY-SA 4.0",
    "CC BY 3.0",
    "CC BY-SA 3.0",
}


def choose_content(state: dict) -> tuple[str, dict, str]:
    runs = state.get("runs", 0)

    # One immediate nature-video Quran test after the first human-recitation test.
    if runs == 3:
        item = dict(RELIGIOUS["quran"][1])
        return item["topic"], item, "ar"

    slot = runs % 4
    if slot == 1:
        items = RELIGIOUS["quran"]
        item = dict(items[(runs // 4) % len(items)])
        return item["topic"], item, "ar"
    if slot == 3:
        items = RELIGIOUS["hadith"]
        item = dict(items[(runs // 4) % len(items)])
        return item["topic"], item, "ar"

    topic = pipeline.pick_topic(state)
    return topic, pipeline.generate_package(topic), "en"


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
    mime = info.get("mime", "")
    if not mime.startswith("audio/"):
        raise RuntimeError(f"Commons file is not audio: {file_title}")

    meta = info.get("extmetadata", {})
    license_name = meta.get("LicenseShortName", {}).get("value", "")
    if required_license and not license_name.upper().startswith(required_license.upper()):
        raise RuntimeError(
            f"Refusing audio because license is {license_name!r}, required {required_license!r}"
        )

    audio_url = info.get("url")
    if not audio_url:
        raise RuntimeError(f"No downloadable URL for Commons audio: {file_title}")

    audio = requests.get(audio_url, headers=pipeline.COMMONS_HEADERS, timeout=60)
    audio.raise_for_status()
    destination.write_bytes(audio.content)

    artist = re.sub("<[^>]+>", "", meta.get("Artist", {}).get("value", "Unknown"))
    return {
        "title": pages[0].get("title", file_title),
        "source": info.get("descriptionurl"),
        "license": license_name,
        "artist": artist,
        "mime": mime,
    }


def _clean_artist(meta: dict) -> str:
    return re.sub("<[^>]+>", "", meta.get("Artist", {}).get("value", "Unknown"))


def download_commons_video(query: str, destination: Path) -> dict:
    """Find and download one reusable Commons nature video, refusing unclear licenses."""
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"filetype:video {query}",
        "gsrnamespace": 6,
        "gsrlimit": 20,
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
        "format": "json",
        "formatversion": 2,
    }
    response = requests.get(api, params=params, headers=pipeline.COMMONS_HEADERS, timeout=30)
    response.raise_for_status()

    for page in response.json().get("query", {}).get("pages", []):
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        mime = info.get("mime", "")
        if not mime.startswith("video/"):
            continue
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "")
        if license_name not in ALLOWED_VISUAL_LICENSES:
            continue
        size = int(info.get("size") or 0)
        if size and size > 90 * 1024 * 1024:
            continue
        url = info.get("url")
        if not url:
            continue

        try:
            video = requests.get(url, headers=pipeline.COMMONS_HEADERS, timeout=90)
            video.raise_for_status()
            if len(video.content) > 95 * 1024 * 1024:
                continue
            destination.write_bytes(video.content)
            # Verify ffmpeg can read it before accepting it.
            pipeline.probe_duration(destination)
        except Exception:
            destination.unlink(missing_ok=True)
            continue

        return {
            "title": page.get("title", "Wikimedia Commons video"),
            "source": info.get("descriptionurl"),
            "license": license_name,
            "artist": _clean_artist(meta),
            "mime": mime,
        }

    raise RuntimeError(f"No suitably licensed Commons video found for {query!r}")


def download_nature_videos(package: dict) -> tuple[list[Path], list[dict]]:
    """Download 2-4 distinct licensed motion clips suitable for a Short."""
    queries = list(dict.fromkeys(package.get("media_queries", [])))
    extras = ["mountain landscape", "waterfall nature", "river forest", "clouds timelapse"]
    queries.extend(q for q in extras if q not in queries)

    clips: list[Path] = []
    licenses: list[dict] = []
    seen_titles: set[str] = set()

    for index, query in enumerate(queries):
        if len(clips) >= 4:
            break
        raw = BUILD / f"nature-video-{index}"
        try:
            info = download_commons_video(query, raw)
            if info["title"] in seen_titles:
                raw.unlink(missing_ok=True)
                continue
            seen_titles.add(info["title"])
            clips.append(raw)
            licenses.append(info)
            print(f"Nature clip: {info['title']} ({info['license']})")
        except Exception as exc:
            print(f"Skipping video query {query!r}: {exc}")

    if not clips:
        raise RuntimeError("No licensed nature video clips could be downloaded")
    return clips, licenses


def _normalize_clip(source: Path, destination: Path, seconds: float = 5.0) -> None:
    """Turn an arbitrary Commons video into a silent 1080x1920 H.264 segment."""
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,fps=30,"
        "eq=saturation=1.05:contrast=1.02"
    )
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(source),
            "-t", f"{seconds:.2f}",
            "-vf", vf,
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            str(destination),
        ],
        check=True,
    )


def render_video_montage(clips: list[Path], voice: Path, subtitles: Path, output: Path, duration: float) -> None:
    """Cut together nature footage, loop the montage as needed, then add recitation and captions."""
    normalized: list[Path] = []
    for i, clip in enumerate(clips):
        target = BUILD / f"normalized-{i}.mp4"
        _normalize_clip(clip, target, 5.0)
        normalized.append(target)

    concat_list = BUILD / "video-concat.txt"
    # Repeat the set enough times to cover the audio duration.
    repeats = max(1, int(duration // (5.0 * len(normalized))) + 2)
    lines = []
    for _ in range(repeats):
        for clip in normalized:
            escaped = str(clip.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
    concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

    montage = BUILD / "nature-montage.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-t", f"{duration:.3f}",
            "-c", "copy",
            str(montage),
        ],
        check=True,
    )

    sub_path = str(subtitles).replace("'", "'\\''")
    vf = (
        "subtitles='" + sub_path +
        "':force_style='FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=250'"
    )
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(montage),
            "-i", str(voice),
            "-vf", vf,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "21",
            "-c:a", "aac",
            "-b:a", "160k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            str(output),
        ],
        check=True,
    )


def download_visuals(package: dict) -> tuple[list[Path], list[dict]]:
    """Fallback: download up to three licensed Commons still images."""
    backgrounds: list[Path] = []
    licenses: list[dict] = []
    queries = list(dict.fromkeys(package.get("media_queries", [])))[:3] or ["peaceful nature"]

    for index, query in enumerate(queries):
        raw = BUILD / f"source-image-{index}"
        background = BUILD / f"background-{index}.jpg"
        try:
            info = pipeline.download_commons_image(query, raw)
            pipeline.make_background(raw, background)
            backgrounds.append(background)
            licenses.append(info)
        except Exception as exc:
            print(f"Skipping visual {index + 1} ({query!r}): {exc}")

    if not backgrounds:
        raise RuntimeError("No licensed visuals could be downloaded")
    return backgrounds, licenses


def render_dynamic(backgrounds: list[Path], voice: Path, subtitles: Path, output: Path, duration: float) -> None:
    """Fallback renderer: moving vertical scenes from licensed photos."""
    count = len(backgrounds)
    segment = max(duration / count, 1.0)

    cmd = ["ffmpeg", "-y"]
    for image in backgrounds:
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{segment:.3f}", "-i", str(image)]
    cmd += ["-i", str(voice)]

    filters = []
    for i in range(count):
        motion = (
            "z='min(zoom+0.0009,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            if i % 2 == 0
            else "z='min(zoom+0.0007,1.08)':x='max(0,iw-iw/zoom)':y='ih/2-(ih/zoom/2)'"
        )
        filters.append(
            f"[{i}:v]scale=1200:2134:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,zoompan={motion}:d=1:s=1080x1920:fps=30,"
            f"trim=duration={segment:.3f},setpts=PTS-STARTPTS[v{i}]"
        )

    joined = "".join(f"[v{i}]" for i in range(count))
    filters.append(f"{joined}concat=n={count}:v=1:a=0[base]")
    sub_path = str(subtitles).replace("'", "'\\''")
    filters.append(
        "[base]subtitles='" + sub_path
        + "':force_style='FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=250'[vout]"
    )

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]",
        "-map", f"{count}:a:0",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "21",
        "-c:a", "aac",
        "-b:a", "160k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(output),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    state = pipeline.load_state()

    if len(state["published"]) >= CONFIG["max_videos"]:
        print("30-day campaign complete")
        return

    topic, package, language = choose_content(state)

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
        print(
            f"Using human Quran recitation: {audio_info['title']} "
            f"({audio_info['license']})"
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

    media_info: list[dict]
    visual_mode = "licensed_nature_video"
    try:
        nature_clips, media_info = download_nature_videos(package)
        package["description"] += "\n\nمشاهد الفيديو المرخصة:"
        for item in media_info:
            package["description"] += (
                f"\n- {item['artist']} | {item['license']} | {item['source']}"
            )
        render_video_montage(nature_clips, voice, subtitles, video, duration)
    except Exception as exc:
        print(f"Nature-video montage unavailable; falling back to moving photos: {exc}")
        visual_mode = "moving_licensed_photos"
        backgrounds, media_info = download_visuals(package)
        render_dynamic(backgrounds, voice, subtitles, video, duration)

    video_id = pipeline.upload(video, package)

    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "video_id": video_id,
        "title": package["title"],
        "language": language,
        "visual_mode": visual_mode,
        "media": media_info,
    }
    if audio_info:
        record["audio"] = audio_info

    state["published"].append(record)
    state["runs"] += 1
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (LOGS / f"{datetime.now(timezone.utc):%Y-%m-%dT%H-%M-%SZ}.json").write_text(
        json.dumps({**record, "script": package["script"]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Uploaded https://youtube.com/shorts/{video_id}")


if __name__ == "__main__":
    main()
