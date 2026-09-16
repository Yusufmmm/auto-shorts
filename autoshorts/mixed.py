from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

import requests

from . import run as runner

pipeline = runner.pipeline
ROOT = pipeline.ROOT
BUILD = pipeline.BUILD
LOGS = pipeline.LOGS
STATE = pipeline.STATE
CONFIG = pipeline.CONFIG
RELIGIOUS = json.loads((ROOT / "religious_content.json").read_text(encoding="utf-8"))


def choose_content(state: dict) -> tuple[str, dict, str]:
    runs = state.get("runs", 0)

    # One-time immediate human-recitation Quran test after the first two uploads.
    # If the test fails, state remains at 2 and the next retry stays on Quran.
    if runs == 2:
        item = dict(RELIGIOUS["quran"][0])
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


def download_commons_audio(file_title: str, destination, required_license: str = "CC0") -> dict:
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


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    state = pipeline.load_state()

    if len(state["published"]) >= CONFIG["max_videos"]:
        print("30-day campaign complete")
        return

    topic, package, language = choose_content(state)
    raw_image = BUILD / "source-image"
    license_info = pipeline.download_commons_image(package["media_queries"][0], raw_image)

    background = BUILD / "background.jpg"
    tts_voice = BUILD / "voice.mp3"
    human_voice = BUILD / "human-recitation"
    subtitles = BUILD / "captions.srt"
    video = BUILD / "short.mp4"

    pipeline.make_background(raw_image, background)

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
    pipeline.render(background, voice, subtitles, video)
    video_id = pipeline.upload(video, package)

    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "video_id": video_id,
        "title": package["title"],
        "language": language,
        "media": license_info,
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
