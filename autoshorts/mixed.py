from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from . import run as runner

pipeline = runner.pipeline
ROOT = pipeline.ROOT
BUILD = pipeline.BUILD
LOGS = pipeline.LOGS
STATE = pipeline.STATE
CONFIG = pipeline.CONFIG
RELIGIOUS = json.loads((ROOT / "religious_content.json").read_text(encoding="utf-8"))


def choose_content(state: dict) -> tuple[str, dict, str]:
    slot = state.get("runs", 0) % 4
    if slot == 1:
        items = RELIGIOUS["quran"]
        item = dict(items[(state.get("runs", 0) // 4) % len(items)])
        return item["topic"], item, "ar"
    if slot == 3:
        items = RELIGIOUS["hadith"]
        item = dict(items[(state.get("runs", 0) // 4) % len(items)])
        return item["topic"], item, "ar"

    topic = pipeline.pick_topic(state)
    return topic, pipeline.generate_package(topic), "en"


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
    voice = BUILD / "voice.mp3"
    subtitles = BUILD / "captions.srt"
    video = BUILD / "short.mp4"

    pipeline.make_background(raw_image, background)

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
