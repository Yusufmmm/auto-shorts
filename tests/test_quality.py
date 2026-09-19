from datetime import datetime, timezone
import pytest
from autoshorts.quality import scene_plan, credit, fingerprint, publication_slot, campaign_open, validate_timings

def test_cannot_loop_insufficient_footage():
    with pytest.raises(ValueError): scene_plan([12,12],60)

def test_long_clip_can_cover_narration_without_repeats():
    p=scene_plan([15,40,20],60)
    assert sum(p)==pytest.approx(60)
    assert all(x<=d-.2 for x,d in zip(p,[15,40,20]))

def test_attribution_requires_supported_license_and_author():
    m={'title':'Nature','artist':'A','source':'https://example.org/source','license':'CC BY 4.0'}
    assert 'https://creativecommons.org/licenses/by/4.0/' in credit(m)
    assert 'Changes:' in credit(m)
    for license in ['CC BY-NC 4.0','CC BY-ND 4.0','CC BY-SA 4.0','']:
        with pytest.raises(ValueError):credit(dict(m,license=license))

def test_duplicate_scripts_ignore_punctuation_and_case():
    assert fingerprint('Hello, World!')==fingerprint('hello world')

def test_daily_limit_in_amsterdam_counts_legacy_uploads():
    now=datetime(2026,9,19,22,30,tzinfo=timezone.utc)
    state={'published':[{'created_at':now.isoformat()}]*3}
    assert publication_slot(state,'short',now) is None

def test_weekly_limit_and_campaign_end():
    now=datetime(2026,9,19,tzinfo=timezone.utc)
    state={'published':[{'format':'long','week':'2026-W38'}],'campaign_v3_start':'2026-08-01T00:00:00+00:00'}
    assert publication_slot(state,'long',now) is None
    assert not campaign_open(state,{'max_days':30},now)

def test_invalid_verse_cues_are_rejected():
    with pytest.raises(ValueError):validate_timings([(0,3),(2,6)],2,6)
    with pytest.raises(ValueError):validate_timings([(0,7)],1,6)
