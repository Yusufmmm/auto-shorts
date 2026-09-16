from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
import feedparser
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
LOGS = ROOT / "logs"
STATE = ROOT / "state.json"
CONFIG = json.loads((ROOT / "config.json").read_text())


def load_state() -> dict:
    return json.loads(STATE.read_text())


def pick_topic(state: dict) -> str:
    feed = feedparser.parse("https://trends.google.com/trending/rss?geo=US")
    used = {x["topic"].lower() for x in state["published"]}
    niche = ("science", "space", "technology", "history", "nature", "ocean", "animal")
    titles = [e.title.strip() for e in feed.entries if e.title.strip().lower() not in used]
    preferred = [t for t in titles if any(word in t.lower() for word in niche)]
    if preferred:
        return preferred[0]
    fallbacks = [
        "a surprising deep ocean discovery", "the science of bioluminescence",
        "an overlooked invention that changed daily life", "how migrating birds navigate",
        "a counterintuitive fact about the solar system"
    ]
    for topic in fallbacks:
        if topic.lower() not in used:
            return topic
    raise RuntimeError("No unused topic is available")


def generate_package(topic: str) -> dict:
    key = os.environ["GEMINI_API_KEY"]
    prompt = f"""Create an original English YouTube Short about: {topic}.
Return strict JSON with keys: script, title, description, hashtags, media_queries.
The script must be 105-125 words, factual, self-contained, no unsupported breaking-news
claims, with a strong first-sentence hook and a satisfying ending. Do not imitate or quote
another creator. title <= 70 chars. hashtags is an array of 4-6 strings. media_queries is
an array of 3 simple visual search phrases. No markdown."""
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    response = requests.post(url, params={"key": key}, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8}}, timeout=60)
    response.raise_for_status()
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(raw)
    if not 80 <= len(data["script"].split()) <= 145:
        raise ValueError("Generated script length is outside safety bounds")
    return data


def download_commons_image(query: str, destination: Path) -> dict:
    api = "https://commons.wikimedia.org/w/api.php"
    params = {"action": "query", "generator": "search", "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": 6, "gsrlimit": 10, "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 1200, "format": "json", "origin": "*"}
    data = requests.get(api, params=params, timeout=30).json()
    allowed = {"CC0", "Public domain", "CC BY 4.0", "CC BY-SA 4.0", "CC BY 3.0", "CC BY-SA 3.0"}
    for page in data.get("query", {}).get("pages", {}).values():
        info = page.get("imageinfo", [{}])[0]
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "")
        if license_name not in allowed:
            continue
        image_url = info.get("thumburl") or info.get("url")
        blob = requests.get(image_url, timeout=45).content
        destination.write_bytes(blob)
        try:
            with Image.open(destination) as im:
                im.verify()
        except Exception:
            destination.unlink(missing_ok=True)
            continue
        return {"title": page["title"], "source": info.get("descriptionurl"), "license": license_name, "artist": re.sub("<[^>]+>", "", meta.get("Artist", {}).get("value", "Unknown"))}
    raise RuntimeError(f"No suitably licensed Wikimedia image found for {query!r}")


async def create_voice(text: str, output: Path) -> None:
    await edge_tts.Communicate(text, CONFIG["voice"], rate="+5%").save(str(output))


def make_background(image_path: Path, output: Path) -> None:
    with Image.open(image_path).convert("RGB") as image:
        ratio = max(1080 / image.width, 1920 / image.height)
        image = image.resize((int(image.width * ratio), int(image.height * ratio)))
        left, top = (image.width - 1080) // 2, (image.height - 1920) // 2
        image.crop((left, top, left + 1080, top + 1920)).save(output, quality=92)


def srt_time(seconds: float) -> str:
    ms = int(seconds * 1000)
    return f"{ms//3600000:02}:{(ms//60000)%60:02}:{(ms//1000)%60:02},{ms%1000:03}"


def create_subtitles(script: str, duration: float, output: Path) -> None:
    words = script.split()
    chunks = [" ".join(words[i:i + 5]) for i in range(0, len(words), 5)]
    step = duration / len(chunks)
    output.write_text("\n\n".join(f"{i+1}\n{srt_time(i*step)} --> {srt_time((i+1)*step)}\n{chunk}" for i, chunk in enumerate(chunks)), encoding="utf-8")


def probe_duration(path: Path) -> float:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def render(image: Path, voice: Path, subtitles: Path, output: Path) -> None:
    vf = "scale=1080:1920,zoompan=z='min(zoom+0.0008,1.10)':d=9999:s=1080x1920:fps=30,subtitles=" + str(subtitles).replace("'", "'\\''") + ":force_style='FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=250'"
    subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", str(image), "-i", str(voice), "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "160k", "-pix_fmt", "yuv420p", "-shortest", str(output)], check=True)


def upload(video: Path, package: dict) -> str:
    credentials = Credentials(None, refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"], token_uri="https://oauth2.googleapis.com/token", client_id=os.environ["YOUTUBE_CLIENT_ID"], client_secret=os.environ["YOUTUBE_CLIENT_SECRET"], scopes=["https://www.googleapis.com/auth/youtube.upload"])
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    title = package["title"]
    description = package["description"] + "\n\n" + " ".join(package["hashtags"]) + " #Shorts"
    request = youtube.videos().insert(part="snippet,status", body={"snippet": {"title": title, "description": description, "categoryId": "27", "defaultLanguage": "en"}, "status": {"privacyStatus": CONFIG["privacy_status"], "selfDeclaredMadeForKids": False}}, media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True))
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    state = load_state()
    if len(state["published"]) >= CONFIG["max_videos"]:
        print("30-day / 60-video campaign complete")
        return
    topic = pick_topic(state)
    package = generate_package(topic)
    raw_image = BUILD / "source-image"
    license_info = download_commons_image(package["media_queries"][0], raw_image)
    background, voice, subtitles, video = BUILD / "background.jpg", BUILD / "voice.mp3", BUILD / "captions.srt", BUILD / "short.mp4"
    make_background(raw_image, background)
    asyncio.run(create_voice(package["script"], voice))
    duration = probe_duration(voice)
    create_subtitles(package["script"], duration, subtitles)
    render(background, voice, subtitles, video)
    video_id = upload(video, package)
    record = {"created_at": datetime.now(timezone.utc).isoformat(), "topic": topic, "video_id": video_id, "title": package["title"], "media": license_info}
    state["published"].append(record)
    state["runs"] += 1
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    (LOGS / f"{datetime.now(timezone.utc):%Y-%m-%dT%H-%M-%SZ}.json").write_text(json.dumps({**record, "script": package["script"]}, indent=2, ensure_ascii=False) + "\n")
    print(f"Uploaded https://youtube.com/shorts/{video_id}")


if __name__ == "__main__":
    main()

