"""Three diverse Shorts/day and one original, chaptered weekly feature."""
from __future__ import annotations
import argparse
import asyncio
import copy
import hashlib
import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
import requests
from googleapiclient.http import MediaFileUpload
from . import publisher_v2 as previous
from .quality import LICENSE_URLS, campaign_open, credit, fingerprint, near_duplicate_text, publication_slot, scene_plan, validate_timings

base = previous.base
ROOT, BUILD = base.ROOT, base.BUILD
base.ALLOWED_LICENSES = set(LICENSE_URLS)

def save_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)

def checkpoint(state):
    save_json(base.STATE, state)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        subprocess.run(['git', 'add', 'state.json', 'logs/'], check=True)
        if subprocess.run(['git','diff','--cached','--quiet']).returncode:
            subprocess.run(['git','commit','-m','chore: checkpoint publishing state'], check=True)
            # Refuse to upload if the durable reservation cannot be written.
            subprocess.run(['git','push','origin','HEAD:main'], check=True)

def ask_json(prompt):
    key = os.environ['GEMINI_API_KEY']
    for model in base.runner._candidate_models(key):
        response = base.runner._request_package(key, model, prompt)
        if response.ok:
            data = json.loads(response.json()['candidates'][0]['content']['parts'][0]['text'])
            return data
    raise RuntimeError('No configured generation model succeeded')

def pick_topic(state):
    used = [x.get('topic','').strip() for x in state.get('published', []) if x.get('topic')]
    topic = previous.pick_global_topic(state)
    too_close = any(near_duplicate_text(topic, prior, .55) for prior in used)
    if topic.lower() in {x.lower() for x in used} or too_close or topic == 'a fascinating science fact most people do not know':
        result = ask_json(
            'Return JSON with topic: one specific original evergreen science, nature or history '
            'subject that is meaningfully different from all these prior subjects: ' + json.dumps(used[-60:])
        )
        topic = result['topic'].strip()
    if not topic or topic.lower() in {x.lower() for x in used}:
        raise ValueError('No new factual topic available')
    if any(near_duplicate_text(topic, prior, .55) for prior in used):
        raise ValueError('Generated topic is too similar to a previously published subject')
    return topic

def generate_feature(state):
    topic = pick_topic(state)
    outline = ask_json(f'''Write an original educational documentary outline on {topic}.
Not a compilation of shorts. One coherent narrative: introduction, six connected chapters,
and conclusion. Evergreen established facts only, no breaking news claims. Return JSON:
title, description, hashtags, media_queries (12 concrete visual queries), chapters (8 objects
with title and brief). No invented quotations, statistics or dates.''')
    chapters = outline['chapters']
    if len(chapters) != 8:
        raise ValueError('A feature requires an introduction, six chapters and a conclusion')
    for i, chapter in enumerate(chapters):
        result = ask_json(f'''Write chapter {i+1}/8 of this original documentary: {json.dumps(outline)}.
Current chapter: {json.dumps(chapter)}. Write 245-285 words of continuous English narration.
Avoid repeated hooks and recaps. Connect to the following chapter; only chapter 8 concludes.
Use well-established facts, explain uncertainty; no invented quotations. Return JSON with script.''')
        if not 210 <= len(result['script'].split()) <= 320:
            raise ValueError('Chapter word count failed')
        chapter['script'] = result['script']
    outline['script'] = '\n\n'.join(c['script'] for c in chapters)
    outline['topic'] = topic
    return topic, outline, 'en', 'fact'

def choose(state, kind, force_content=None):
    if kind == 'long':
        return generate_feature(state)
    used = {x.get('topic') for x in state.get('published', [])}
    today_types = set()
    day = publication_slot(state, kind).split(':')[1]
    from zoneinfo import ZoneInfo
    for item in state.get('published', []):
        if datetime.fromisoformat(item['created_at']).astimezone(ZoneInfo('Europe/Amsterdam')).date().isoformat() == day:
            today_types.add(item.get('content_type'))
    candidates = [force_content] if force_content else [x for x in ('quran','hadith','fact') if x not in today_types]
    # Quran/hadith text comes only from the reviewed catalogue, never from AI.
    for desired in candidates:
        if desired not in ('quran','hadith'):
            break
        for item in base.RELIGIOUS[desired]:
            if item['topic'] not in used:
                if desired == 'quran' and not item.get('human_audio_file'):
                    raise ValueError('Quran requires licensed human recitation')
                return item['topic'], copy.deepcopy(item), 'ar', desired
        print(f'No unused reviewed {desired} entry: try another category, never recycle scripture')
    topic = pick_topic(state)
    if topic in used:
        raise ValueError('Topic catalogue exhausted; refusing repetition')
    return topic, base.pipeline.generate_package(topic), 'en', 'fact'

async def tts(script, output, language, subtitles):
    voice = base.CONFIG.get('arabic_voice', 'ar-SA-HamedNeural') if language == 'ar' else base.CONFIG['voice']
    events = []
    async for event in edge_tts.Communicate(script, voice, rate='-5%', boundary='WordBoundary').stream():
        if event['type'] == 'audio':
            with output.open('ab') as f:
                f.write(event['data'])
        elif event['type'] == 'WordBoundary':
            events.append(event)
    if not events:
        raise ValueError('Speech timing metadata unavailable')
    chunks = []
    for i in range(0,len(events),5):
        group = events[i:i+5]
        start = group[0]['offset']/1e7
        end = (group[-1]['offset']+group[-1]['duration'])/1e7
        chunks.append(f"{len(chunks)+1}\n{base.pipeline.srt_time(start)} --> {base.pipeline.srt_time(end)}\n{' '.join(x['text'] for x in group)}")
    subtitles.write_text('\n\n'.join(chunks), encoding='utf-8')

def quran_ass(package, voice, duration, output):
    # Validated cue sheets are preferred. Silence-assisted estimates are explicitly recorded.
    events = ([{'text':package['opening'], 'number':None}] if package.get('opening') else []) + package['verses']
    timings = package.get('verse_timings')
    mode = 'reviewed_cues' if timings else 'silence_assisted_estimate'
    if not timings:
        result = subprocess.run(['ffmpeg','-hide_banner','-i',str(voice),'-af','silencedetect=noise=-35dB:d=0.25','-f','null','-'],capture_output=True,text=True,check=True)
        silences = [float(x) for x in re.findall(r'silence_end: ([0-9.]+)',result.stderr)]
        weights = [max(3,len(e['text'].split())) for e in events]
        cursor, total, boundaries = 0, sum(weights), [0.0]
        for weight in weights[:-1]:
            cursor += weight
            expected = duration*cursor/total
            nearby = [s for s in silences if abs(s-expected)<1.7 and s>boundaries[-1]+.8 and s<duration-.8]
            boundaries.append(min(nearby,key=lambda s:abs(s-expected)) if nearby else max(boundaries[-1]+.01,expected))
        boundaries.append(duration)
        timings = list(zip(boundaries,boundaries[1:]))
    validate_timings(timings,len(events),duration)
    previous.create_quran_ass(package,duration,output)
    # Replace approximate dialogue intervals and preserve ASS linebreaks without double escaping.
    text = output.read_text(encoding='utf-8')
    text = text.replace('\\\\N','\\N').replace('Noto Naskh Arabic','Amiri')
    text = text.replace('Amiri,70,', 'Amiri,100,').replace('Amiri,66,', 'Amiri,90,').replace('Amiri,50,', 'Amiri,70,')
    lines, index = [], 0
    for line in text.splitlines():
        if line.startswith('Dialogue:') and ',SurahTitle,' not in line:
            fields = line.split(',',9)
            start,end = timings[index]
            fields[1],fields[2] = previous._ass_time(start),previous._ass_time(end)
            if fields[3] == 'VerseNo':
                number = previous._arabic_number(events[index]['number'])
                fields[9] = r'{\an5\pos(540,1135)\fs54\fad(150,150)}' + number
                points = []
                for j in range(32):
                    radius = 43 if j % 2 == 0 else 36
                    angle = math.pi*j/16
                    points.append((43+round(radius*math.cos(angle)),43+round(radius*math.sin(angle))))
                drawing = 'm '+str(points[0][0])+' '+str(points[0][1])+' l '+' '.join(f'{x} {y}' for x,y in points[1:]+points[:1])
                lines.append(f'Dialogue: 0,{fields[1]},{fields[2]},VerseNo,,0,0,0,,'+r'{\an5\pos(540,1135)\p1\bord2\shad0\1a&HFF&\3c&H00D7FF&\fad(150,150)}'+drawing+r'{\p0}')
            line = ','.join(fields)
            if fields[3] in ('Opening','VerseNo'):
                index += 1
        lines.append(line)
    output.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return mode

def _visual_query_variants(query):
    """Broaden over-specific generated searches while keeping the subject recognizable."""
    cleaned = re.sub(r'\b(video|videos|footage|clip|clips|drone|aerial|view|close|closeup|landscape|cinematic)\b', ' ', query, flags=re.I)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    words = [w for w in re.findall(r"[A-Za-z0-9'-]+", cleaned) if len(w) >= 4]
    variants = [query.strip(), cleaned]
    if len(words) >= 3:
        variants.append(' '.join(words[:3]))
    if len(words) >= 2:
        variants.append(' '.join(words[:2]))
    # Long distinctive words often map best to Commons titles (e.g. waterfall, bioluminescence).
    variants.extend(sorted(set(words), key=len, reverse=True)[:3])
    return [v for v in dict.fromkeys(variants) if v]

def download_visuals(package,state,duration,content_type):
    strict = content_type in ('quran','hadith')
    source_queries = list(base.NATURE_QUERIES) if strict else list(package.get('media_queries',[]))
    queries = []
    for query in source_queries:
        queries.extend(_visual_query_variants(query))
    used = base._used_media_titles(state)
    seen_hashes = {m.get('sha256') for r in state.get('published',[]) for m in (r.get('media',[]) if isinstance(r.get('media'),list) else [r.get('media',{})])}
    paths, credits, available = [], [], 0
    for i,q in enumerate(dict.fromkeys(queries)):
        if available >= duration+.5:
            break
        path=BUILD/f'clip-{i}'
        try:
            info=previous.download_commons_video(q,path,used,strict)
            credit(info)
            checksum=hashlib.sha256(path.read_bytes()).hexdigest()
            if checksum in seen_hashes:
                continue
            info['sha256']=checksum
            used.add(info['title']); seen_hashes.add(checksum)
            paths.append(path); credits.append(info)
            available += info['duration']-.2
        except (RuntimeError,ValueError,requests.RequestException) as exc:
            print(f'Skip visual query {q}: {type(exc).__name__}')
    # Faster visual changes improve mobile retention without looping or reusing clips.
    plan=scene_plan([m['duration'] for m in credits],duration,20 if content_type=='quran' else 6)
    return paths[:len(plan)],credits[:len(plan)],plan

def render(paths,plan,voice,subs,output,kind,quran=False):
    width,height=(1920,1080) if kind=='long' else (1080,1920)
    scenes=[]
    for i,(path,seconds) in enumerate(zip(paths,plan)):
        scene=BUILD/f'scene-v3-{i}.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(path),'-t',f'{seconds:.3f}',
            '-vf',f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30',
            '-an','-c:v','libx264','-preset','fast','-crf','19','-pix_fmt','yuv420p',str(scene)],check=True)
        scenes.append(scene)
    concat=BUILD/'scenes-v3.txt'
    concat.write_text(''.join(f"file '{p.resolve()}'\n" for p in scenes))
    vf=f"subtitles='{subs}'"
    if not quran:
        vf += ":force_style='FontName=Amiri,FontSize=20,Outline=2,Shadow=1,MarginV=45'"
    subprocess.run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',str(concat),'-i',str(voice),
        '-vf',vf,'-af','loudnorm=I=-16:TP=-1.5:LRA=11','-map','0:v:0','-map','1:a:0',
        '-c:v','libx264','-preset','fast','-crf','19','-c:a','aac','-b:a','192k','-movflags','+faststart','-shortest',str(output)],check=True)
    duration=base.pipeline.probe_duration(voice)
    if abs(base.pipeline.probe_duration(output)-duration)>.3:
        raise ValueError('Rendered video does not cover all narration')

def youtube_client():
    credentials=previous.Credentials(None,refresh_token=os.environ['YOUTUBE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',client_id=os.environ['YOUTUBE_CLIENT_ID'],
        client_secret=os.environ['YOUTUBE_CLIENT_SECRET'],scopes=['https://www.googleapis.com/auth/youtube.upload'])
    return previous.build('youtube','v3',credentials=credentials,cache_discovery=False)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--kind',choices=['short','long'],default='short')
    parser.add_argument('--content',choices=['fact','quran','hadith'])
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    BUILD.mkdir(exist_ok=True); base.LOGS.mkdir(exist_ok=True)
    state=base.pipeline.load_state()
    now=datetime.now(timezone.utc)
    state.setdefault('campaign_v3_start',now.isoformat())
    if not campaign_open(state,base.CONFIG,now):
        print('30-day campaign finished'); return
    if state.get('pending_upload'):
        raise RuntimeError('Unresolved prior upload: reconcile YouTube and state before any new upload')
    slot=publication_slot(state,args.kind,now)
    if not slot:
        print('Daily/weekly publication quota already satisfied'); return
    if not args.dry_run:
        checkpoint(state)
    topic,package,language,content_type=choose(state,args.kind,args.content)
    script_hash=fingerprint(package['script'])
    if any(r.get('script_hash')==script_hash for r in state.get('published',[])):
        raise ValueError('Duplicate narration refused')
    voice,subs,video=BUILD/'voice-v3.mp3',BUILD/'captions-v3.srt',BUILD/'video-v3.mp4'
    voice.unlink(missing_ok=True)
    audio_info=None; timing_mode='speech_word_boundaries'
    if content_type=='quran':
        audio_info=base.download_commons_audio(package['human_audio_file'],voice,package.get('required_audio_license','CC0'))
        credit(audio_info)
    else:
        asyncio.run(tts(package['script'],voice,language,subs))
    duration=base.pipeline.probe_duration(voice)
    if args.kind=='long' and not 720<=duration<=1200:
        raise ValueError(f'Feature duration {duration:.1f}s outside 12–20 minutes; never pad or loop')
    if args.kind=='short' and not 15<=duration<=180:
        raise ValueError('Short duration outside 15–180 seconds')
    if content_type=='quran':
        subs=BUILD/'quran-v3.ass'
        timing_mode=quran_ass(package,voice,duration,subs)
    paths,media,plan=download_visuals(package,state,duration,content_type)
    render(paths,plan,voice,subs,video,args.kind,content_type=='quran')
    credits='\n\n'.join(credit(m) for m in ([audio_info] if audio_info else [])+media)
    description=package['description']+'\n\n'+credits+'\n\n'+' '.join(h for h in package.get('hashtags',[]) if args.kind=='short' or h.lower()!='#shorts')
    if args.kind=='short': description+=' #Shorts'
    if content_type!='quran': description+='\nOriginal script; synthetic narration.'
    if len(description)>5000:
        raise ValueError('Attribution does not fit YouTube description; refusing to drop credits')
    record=dict(created_at=now.isoformat(),topic=topic,title=package['title'],language=language,content_type=content_type,
        format=args.kind,slot=slot,week=now.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Amsterdam')).strftime('%G-W%V'),
        media=media,audio=audio_info,script_hash=script_hash,script=package['script'],duration=duration,timing_mode=timing_mode,
        chapters=package.get('chapters',[]),visual_mode='unique_licensed_video_no_loop')
    save_json(BUILD/'manifest.json',dict(record,description=description,scene_durations=plan))
    if args.dry_run:
        print('Dry run ready: '+str(video)); return
    state['pending_upload']={'slot':slot,'script_hash':script_hash,'started_at':now.isoformat()}
    checkpoint(state)
    request=youtube_client().videos().insert(part='snippet,status',body={
        'snippet':{'title':package['title'][:100],'description':description,'categoryId':'27','defaultLanguage':language},
        'status':{'privacyStatus':base.CONFIG['privacy_status'],'selfDeclaredMadeForKids':False}},
        media_body=MediaFileUpload(str(video),mimetype='video/mp4',resumable=True))
    response=None
    while response is None:
        _,response=request.next_chunk(num_retries=3)
    record['video_id']=response['id']
    save_json(BUILD/'upload-receipt.json',record)
    state.setdefault('published',[]).append(record); state['runs']=state.get('runs',0)+1
    state.pop('pending_upload',None)
    save_json(base.LOGS/f'{now:%Y-%m-%dT%H-%M-%SZ}.json',record)
    checkpoint(state)
    url='https://www.youtube.com/watch?v='+record['video_id']
    print('Uploaded '+url)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write(f'Published: {url}\n')

if __name__=='__main__':main()
