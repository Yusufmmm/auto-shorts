from __future__ import annotations

import json
import os

import requests

from . import main as pipeline


def _available_models(key: str) -> list[str]:
    response = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": key, "pageSize": 100},
        timeout=30,
    )
    response.raise_for_status()
    models: list[str] = []
    for item in response.json().get("models", []):
        methods = item.get("supportedGenerationMethods", [])
        name = item.get("name", "").removeprefix("models/")
        if "generateContent" not in methods or not name.startswith("gemini-"):
            continue
        lowered = name.lower()
        if any(tag in lowered for tag in ("image", "tts", "embedding", "robotics", "live")):
            continue
        models.append(name)
    return models


def _choose_model(key: str) -> str:
    models = _available_models(key)
    preferred = [
        "gemini-flash-latest",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.8-flash",
    ]
    for model in preferred:
        if model in models:
            return model
    stable_flash = [m for m in models if "flash" in m and "preview" not in m and "exp" not in m]
    if stable_flash:
        return stable_flash[0]
    if models:
        return models[0]
    raise RuntimeError("This Gemini API key has no model available for generateContent")


def generate_package(topic: str) -> dict:
    key = os.environ["GEMINI_API_KEY"]
    model = _choose_model(key)
    prompt = f"""Create an original English YouTube Short about: {topic}.
Return strict JSON with keys: script, title, description, hashtags, media_queries.
The script must be 105-125 words, factual, self-contained, no unsupported breaking-news
claims, with a strong first-sentence hook and a satisfying ending. Do not imitate or quote
another creator. title <= 70 chars. hashtags is an array of 4-6 strings. media_queries is
an array of 3 simple visual search phrases. No markdown."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(
        url,
        params={"key": key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.8,
            },
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Gemini request failed for {model}: {response.status_code} {response.text[:500]}")
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(raw)
    if not 80 <= len(data["script"].split()) <= 145:
        raise ValueError("Generated script length is outside safety bounds")
    print(f"Using Gemini model: {model}")
    return data


pipeline.generate_package = generate_package

if __name__ == "__main__":
    pipeline.main()
