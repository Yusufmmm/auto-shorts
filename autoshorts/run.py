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
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-3.6-flash",
        "gemini-3.8-flash",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
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
    target_seconds = int(os.environ.get("AUTOSHORTS_TARGET_SECONDS", "36"))
    prompt = f"""أنشئ مقطع YouTube Short عربي أصلي عن الموضوع التالي: {topic}.
أعد JSON فقط بالمفاتيح التالية بالضبط: script, title, description, hashtags, media_queries.

الهدف هو الاحتفاظ بالمشاهد والمشاركة، وليس المبالغة:
- اكتب بالعربية الفصحى السهلة، من 65 إلى 90 كلمة تقريباً، بحيث يناسب {target_seconds} ثانية.
- أول جملة من 4 إلى 10 كلمات وتدخل مباشرة في الفكرة؛ بدون سلام أو مقدمة.
- أعطِ أول معلومة مفيدة خلال أول 20 كلمة، ثم تفصيل جديد كل جملة أو جملتين.
- اختر حقائق ثابتة وقابلة للتحقق، ولا تستخدم أخباراً سياسية أو ادعاءات آنية.
- اجعل الموضوع بصرياً وقابلاً للمشاركة: ظاهرة غريبة، حقيقة يومية، طبيعة، تاريخ، علم أو تقنية.
- النهاية تكون مفاجأة أو خلاصة قصيرة، ثم CTA اختياري لا يتجاوز 3 كلمات مثل: تابع للمزيد.
- title: عنوان عربي واضح من 18 إلى 55 حرفاً، بدون تضليل أو مبالغة أو أحرف كبيرة مصطنعة.
- description: جملة عربية واحدة موجزة.
- hashtags: مصفوفة من 3 إلى 5 وسوم مركزة، ويمكن تضمين #Shorts.
- media_queries: مصفوفة من 6 إلى 8 عبارات بحث باللغة الإنجليزية فقط، قصيرة وملموسة، وكل عبارة تصف لقطة مختلفة مرتبطة مباشرة بالسرد.
لا تقلد أي صانع محتوى ولا تقتبس منه. لا تستخدم Markdown."""

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
