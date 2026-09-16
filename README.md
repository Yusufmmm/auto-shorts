# Auto Shorts

Automated, license-aware pipeline that creates and uploads two original English YouTube Shorts per day for up to 30 days (60 videos).

## What it does

1. Reads Google Trends RSS and avoids topics already stored in `state.json`.
2. Uses Gemini to write an original 105–125 word script, hook, title, description and hashtags.
3. Downloads only an allowlisted Creative Commons/Public Domain image from Wikimedia Commons and records its attribution.
4. Produces English neural narration with Edge TTS, a 1080×1920 video, motion and burned-in subtitles.
5. Uploads through the official YouTube Data API.
6. Runs at 08:17 and 18:17 UTC in GitHub Actions. The workflow is serialized to prevent duplicate uploads.

Uploads default to **private** for safety. After reviewing initial results, change `privacy_status` in `config.json` to `public`.

## Required GitHub Secrets

- `GEMINI_API_KEY`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

No credential belongs in source code. A Pexels key is not needed; media comes from Wikimedia Commons with a strict license allowlist.

## One-time YouTube authorization

1. In Google Cloud, enable **YouTube Data API v3**.
2. Configure the OAuth consent screen and add your Google account as a test user if the app is in Testing.
3. Create an OAuth 2.0 Client ID of type **Desktop app** and download its JSON.
4. On a trusted computer, install requirements and run `python scripts/get_youtube_token.py`.
5. Paste the JSON when asked, approve your YouTube account, then add the three printed values as GitHub repository secrets.
6. Create a Gemini API key in Google AI Studio and add it as `GEMINI_API_KEY`.
7. In GitHub Actions, run **Create and upload YouTube Short** manually once. Check the private upload before making publishing public.

## Local tests

```bash
python -m pytest -q
```

## Safety and limits

- Scripts are original, but automated factual output must still be monitored.
- Media attribution and licenses are saved under `logs/`.
- YouTube API uploads cost quota; the default daily quota normally supports this schedule, but quota is controlled by Google.
- The campaign stops after 60 successful uploads. Failed runs do not increment the counter.
