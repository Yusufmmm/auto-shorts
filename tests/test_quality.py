from datetime import datetime, timezone
import pytest
from autoshorts.quality import scene_plan, credit, fingerprint, near_duplicate_text, publication_slot, campaign_open, validate_short_package, validate_timings

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

def test_quran_ass_preserves_rtl_lines_and_ornament(tmp_path):
    from autoshorts.publisher_v3 import quran_ass
    p={'title':'سورة','verses':[{'number':12,'text':'هذه كلمات كثيرة لاختبار سطر عربي صحيح وواضح'}], 'verse_timings':[(0,5)]}
    output=tmp_path/'q.ass'
    assert quran_ass(p,tmp_path/'unused',5,output)=='reviewed_cues'
    text=output.read_text()
    assert '١٢' in text and r'\p1' in text and 'Amiri' in text
    assert r'\N' in text and r'\\N' not in text

def test_exhausted_quran_still_selects_unused_hadith(monkeypatch):
    from autoshorts import publisher_v3 as p
    monkeypatch.setattr(p,'publication_slot',lambda *_:'short:2026-09-20:0')
    monkeypatch.setattr(p.base,'RELIGIOUS',{'quran':[],'hadith':[{'topic':'new hadith','script':'reviewed'}]})
    assert p.choose({'published':[]},'short')[3]=='hadith'

def test_near_duplicate_topics_are_detected():
    assert near_duplicate_text(
        'how migrating birds navigate using Earth magnetic field',
        'how birds navigate with Earth magnetic field',
        .55,
    )
    assert not near_duplicate_text(
        'how migrating birds navigate',
        'why deep sea animals glow',
        .55,
    )

def test_growth_package_requires_short_hook_and_multiple_visuals():
    package = {
        'script': (
            'Octopuses can taste with their arms. '
            'Thousands of chemical sensors in their suckers help them inspect what they touch. '
            'That means an octopus can explore food and its surroundings without bringing every object to its mouth. '
            'These receptors help the animal distinguish useful chemical signals while it moves across rocks, shells, and prey. '
            'The arms also contain large networks of neurons, so much of this sensing is processed close to where contact happens. '
            'Researchers study these systems to understand how touch, chemistry, and movement work together in one distributed nervous system. '
            'It is a striking example of how evolution can spread sensing across an entire body instead of concentrating every task in one place. '
            'Subscribe for more surprising science stories every day.'
        ),
        'title': 'How Octopuses Taste With Their Arms',
        'description': 'Octopus arms can detect chemicals while touching objects.',
        'hashtags': ['#Science', '#Ocean', '#Shorts'],
        'media_queries': ['octopus arm closeup', 'octopus suckers', 'octopus reef', 'octopus feeding', 'octopus underwater', 'octopus movement'],
    }
    assert validate_short_package(package) is package
    bad = dict(package, title='You Won\'t Believe This SHOCKING TRUTH!!!')
    with pytest.raises(ValueError):
        validate_short_package(bad)

