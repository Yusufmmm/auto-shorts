from __future__ import annotations

import re
from pathlib import Path

import requests

from . import mixed

_original_choose_content = mixed.choose_content


def choose_content(state: dict):
    runs = state.get("runs", 0)
    if runs == 3:
        item = dict(mixed.RELIGIOUS["quran"][0])
        return item["topic"], item, "ar"
    return _original_choose_content(state)


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
    response = requests.get(api, params=params, headers=mixed.pipeline.COMMONS_HEADERS, timeout=30)
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", [])
    if not pages or not pages[0].get("imageinfo"):
        raise RuntimeError(f"Wikimedia Commons audio file not found: {file_title}")

    info = pages[0]["imageinfo"][0]
    mime = info.get("mime", "")
    accepted_audio = mime.startswith("audio/") or mime in {"application/ogg", "application/x-ogg"}
    if not accepted_audio:
        raise RuntimeError(f"Commons file is not audio: {file_title} ({mime})")

    meta = info.get("extmetadata", {})
    license_name = meta.get("LicenseShortName", {}).get("value", "")
    if required_license and not license_name.upper().startswith(required_license.upper()):
        raise RuntimeError(
            f"Refusing audio because license is {license_name!r}, required {required_license!r}"
        )

    audio_url = info.get("url")
    if not audio_url:
        raise RuntimeError(f"No downloadable URL for Commons audio: {file_title}")

    audio = requests.get(audio_url, headers=mixed.pipeline.COMMONS_HEADERS, timeout=60)
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


mixed.choose_content = choose_content
mixed.download_commons_audio = download_commons_audio

if __name__ == "__main__":
    mixed.main()
