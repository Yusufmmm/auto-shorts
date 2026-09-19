# Auto Shorts v3

Production entrypoint: `python -m autoshorts.publisher_v3`.

## Schedule

GitHub Actions runs Shorts at **07:17, 12:17, 18:17 UTC**, and an original long feature on **Sunday 09:37 UTC**. Amsterdam times in summer: 09:17 / 14:17 / 20:17 and Sunday 11:37. Winter times are one hour earlier. GitHub may delay scheduled jobs. The previous ChatGPT four-times-daily publisher must remain disabled to avoid competing triggers.

A maximum of three Shorts per Amsterdam calendar day and one feature per ISO week includes manual runs and legacy uploads. The campaign stops 30 days after the first v3 run. Content generation is best effort: no eligible media or failed quality gates means no upload, never a low-quality or unlicensed substitute.

## Editorial and licensing

- Quran uses only human recordings whose current Commons metadata satisfies the required CC0 license. Never TTS for Quran.
- Reviewed Quran/hadith catalogue is used without AI rewriting. Exhausted entries fall back to a new factual Short; extending the catalogue is necessary to sustain daily religious variety.
- Facts use filtered worldwide trend feeds with evergreen fallbacks; a fallback is not labelled trending.
- Weekly features have an introduction, six connected chapters and a conclusion, generated as a coherent original narrative. Actual audio must be 12–20 minutes: no loops, padding or stitched Shorts.
- Media accepts CC0, public domain and CC BY 3.0/4.0 with author, source, license link and change notice. NC, ND, unknown and share-alike material are excluded. Metadata is checked per run; it does not guarantee an uploader owns every right.
- Clip titles and downloaded SHA-256 checksums are checked against history. Each selected source is used once, at normal speed. Footage must cover the full narration. No image fallback or repeated-video fallback.
- Output: 1080×1920 Shorts; 1920×1080 features; H.264 CRF19, AAC192k, normalized audio.
- Quran captions use Amiri, shaped RTL via libass, Arabic ayah numbers centered inside a separately drawn ornament. Manually reviewed verse cues (`verse_timings`, seconds per opening/verse) take priority. Without them the system logs `silence_assisted_estimate`: approximate, not word-perfect synchronization.
- Non-Quran narration uses the existing Edge TTS service with actual word boundary captions; narration is disclosed as synthetic. No cloning of real speakers.

## Safety against duplicate uploads

One concurrency group serializes publishing. Before uploading, a `pending_upload` reservation is committed and pushed. A failed push prevents upload. An unresolved reservation blocks subsequent uploads until the channel and upload receipt are reconciled. This sacrifices automatic retries when the remote outcome is ambiguous to avoid duplicates. Do not clear a reservation without checking YouTube.

The workflow preserves the rendered video, caption files, manifest and upload receipt as artifacts for 14 days, including on failure. Successful uploads write a canonical video link in the run summary and `logs/`.

## Run and test

```
pip install -r requirements.txt
python -m pytest -q
python -m autoshorts.publisher_v3 --dry-run
python -m autoshorts.publisher_v3 --kind long --dry-run
```

Install FFmpeg/libass and `fonts-hosny-amiri`. Required GitHub secrets are unchanged: `GEMINI_API_KEY`, `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`. Never commit credentials or print tokens.

Manual workflow dispatch can select `short` or `long` and `dry_run`. Updating `.github/auto-shorts-trigger` triggers one Short, subject to the same quota and license gates. Trigger text no longer overrides content selection. `publisher_v2` and earlier modules remain only as reusable helpers and historical code; do not schedule them.
