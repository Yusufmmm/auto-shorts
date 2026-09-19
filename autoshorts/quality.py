"""Pure quality gates shared by scheduled and manual publishing."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LICENSE_URLS = {
    'CC0': 'https://creativecommons.org/publicdomain/zero/1.0/',
    'Public domain': 'https://creativecommons.org/publicdomain/mark/1.0/',
    'CC BY 4.0': 'https://creativecommons.org/licenses/by/4.0/',
    'CC BY 3.0': 'https://creativecommons.org/licenses/by/3.0/',
}

def fingerprint(text):
    return hashlib.sha256(' '.join(re.findall(r'\w+', text.lower())).encode()).hexdigest()

def credit(item):
    if item.get('license') not in LICENSE_URLS or not item.get('source') or not item.get('artist') or item['artist'] == 'Unknown':
        raise ValueError('Incomplete or unsupported media license')
    return f"{item['title']} — {item['artist']}\n{item['source']}\n{LICENSE_URLS[item['license']]}\nChanges: trimmed, cropped, color adjusted; captions and narration added."

def scene_plan(durations, target, preferred=16):
    """Use each source once, at normal speed. Never loop or freeze the last frame."""
    available = [max(0, d - .2) for d in durations]
    if sum(available) < target:
        raise ValueError('Not enough unique footage to cover the complete narration')
    remaining = target
    plan = []
    for i, capacity in enumerate(available):
        if remaining <= .01:
            break
        take = min(capacity, max(min(preferred, remaining), remaining-sum(available[i+1:])))
        plan.append(take)
        remaining -= take
    if remaining > .05:
        raise ValueError('Incomplete scene coverage')
    return plan

def publication_slot(state, kind, now=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo('Europe/Amsterdam'))
    published = state.get('published', [])
    if kind == 'long':
        week = local.strftime('%G-W%V')
        if any(x.get('week') == week and x.get('format') == 'long' for x in published):
            return None
        return f'long:{week}'
    day = local.date().isoformat()
    today = [x for x in published if x.get('format') != 'long' and datetime.fromisoformat(x['created_at']).astimezone(ZoneInfo('Europe/Amsterdam')).date().isoformat() == day]
    if len(today) >= 3:
        return None
    return f'short:{day}:{len(today)}'

def campaign_open(state, config, now=None):
    now = now or datetime.now(timezone.utc)
    start = datetime.fromisoformat(state.get('campaign_v3_start', now.isoformat()))
    return now < start + timedelta(days=config.get('max_days', 30))

def validate_timings(timings, count, duration):
    if len(timings) != count:
        raise ValueError('Verse timing count does not match text')
    previous = 0
    for start, end in timings:
        if not 0 <= start < end <= duration+.05 or start < previous:
            raise ValueError('Invalid or overlapping verse timing')
        previous = end
    return timings
