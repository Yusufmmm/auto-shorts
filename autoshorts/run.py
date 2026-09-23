from __future__ import annotations

import json
import os
import time

import requests

from . import main as pipeline
from .quality import validate_short_package


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


def _candidate_models(key: str) -> list[str]:
    models = _available_models(key)
    preferred = [
        "gemini-2.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash",
        "gemini-flash-latest",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.8-flash",
    ]
    ordered: list[str] = []
    for model in preferred:
        if model in models and model not in ordered:
            ordered.append(model)
    stable_flash = [
        m for m in models
        if "flash" in m and "preview" not in m and "exp" not in m and m not in ordered
    ]
    ordered.extend(stable_flash)
    ordered.extend(m for m in models if m not in ordered)
    if not ordered:
        raise RuntimeError("This Gemini API key has no model available for generateContent")
    return ordered


def _request_package(key: str, model: str, prompt: str) -> requests.Response:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    return requests.post(
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


def generate_package(topic: str) -> dict:
    key = os.environ["GEMINI_API_KEY"]
    prompt = f"""Create an original English YouTube Short about: {topic}.
Return strict JSON with exactly these keys: script, title, description, hashtags, media_queries.

Optimize for real viewer retention and subscriptions without misleading clickbait:
- 105-125 words, factual, self-contained, and evergreen unless the fact is firmly established.
- Sentence 1 is a 5-12 word hook that creates a specific curiosity gap; no greeting or intro.
- Deliver a concrete payoff within the first 25 words, then add one new detail every 1-2 sentences.
- Use short spoken sentences and natural rhythm. No filler, repeated setup, or invented claims.
- End with a useful takeaway followed by one brief CTA of at most 10 words inviting viewers
  to subscribe for more science/history/nature stories. Do not beg for likes.
- title: 35-65 characters, specific and searchable, with one curiosity gap; no ALL CAPS,
  fake urgency, or more than one ! or ?.
- description: 1-2 concise sentences; first sentence naturally contains the main topic phrase.
- hashtags: array of 3-5 focused strings, including #Shorts only if useful.
- media_queries: array of 6-8 distinct, concrete visual search phrases matching different
  moments in the narration so the edit can change scenes frequently.
Do not imitate or quote another creator. No markdown."""

    transient_statuses = {429, 500, 502, 503, 504}
    errors: list[str] = []

    for model in _candidate_models(key):
        for attempt in range(1, 4):
            response = _request_package(key, model, prompt)
            if response.ok:
                try:
                    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                    data = json.loads(raw)
                    validate_short_package(data)
                    print(f"Using Gemini model: {model}")
                    return data
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    summary = f"{model} attempt {attempt}: quality retry: {exc}"
                    errors.append(summary)
                    print(summary)
                    if attempt < 3:
                        continue
                    break

            summary = f"{model} attempt {attempt}: HTTP {response.status_code} {response.text[:240]}"
            errors.append(summary)
            print(summary)

            if response.status_code not in transient_statuses:
                break
            if attempt < 3:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = max(2, min(30, int(retry_after))) if retry_after else 5 * attempt
                except ValueError:
                    wait_seconds = 5 * attempt
                print(f"Temporary Gemini error; retrying in {wait_seconds}s")
                time.sleep(wait_seconds)

        print(f"Trying another Gemini model after {model} was unavailable")

    raise RuntimeError("All available Gemini models failed. " + " | ".join(errors[-8:]))


pipeline.generate_package = generate_package

if __name__ == "__main__":
    pipeline.main()
