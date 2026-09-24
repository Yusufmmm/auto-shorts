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

def test_growth_experiment_keeps_one_audience_and_language(monkeypatch):
    from autoshorts import publisher_v3 as p
    monkeypatch.setattr(p,'pick_topic',lambda *_:'why the sky changes color')
    monkeypatch.setattr(p.base.pipeline,'generate_package',lambda topic:{
        'script':'هذه حقيقة قصيرة ومفيدة تبدأ مباشرة بالفكرة وتشرح سبب تغير لون السماء بطريقة بسيطة وواضحة للمشاهد العربي مع تفاصيل مرئية متتابعة تساعد على إبقاء الانتباه حتى النهاية وتقدم معلومة جديدة كل عدة ثوان من دون مقدمة طويلة أو تكرار غير ضروري ثم تنتهي بخلاصة سهلة التذكر ومناسبة للمشاركة مع الآخرين لأن الفكرة واضحة ومثيرة للاهتمام.',
        'title':'لماذا يتغير لون السماء؟',
        'description':'شرح عربي قصير لسبب تغير لون السماء.',
        'hashtags':['#علوم','#حقائق','#Shorts'],
        'media_queries':['blue sky clouds','sunset sky','atmosphere sunlight','red sunset','sun rays clouds','earth atmosphere']
    })
    _,_,language,content_type = p.choose({'published':[]},'short')
    assert language=='ar' and content_type=='fact'

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
            'الأخطبوط يتذوق بأذرعه فعلاً. '
            'تحتوي الممصات على مستقبلات كيميائية تساعده على فحص ما يلمسه مباشرة. '
            'لهذا يستطيع استكشاف الطعام والصخور من دون نقل كل شيء إلى فمه. '
            'كما أن أذرعه تحمل شبكة كبيرة من الخلايا العصبية فتتم معالجة جزء من المعلومات قريباً من مكان اللمس. '
            'هذه الطريقة تجعل جسمه كله تقريباً جزءاً من نظام الإحساس. '
            'والنتيجة أن حركة واحدة تكشف له الطعم والملمس معاً. تابع للمزيد.'
        ),
        'title': 'كيف يتذوق الأخطبوط بأذرعه؟',
        'description': 'حقيقة قصيرة عن طريقة إحساس الأخطبوط.',
        'hashtags': ['#علوم', '#بحر', '#Shorts'],
        'media_queries': ['octopus arm closeup', 'octopus suckers', 'octopus reef', 'octopus feeding', 'octopus underwater', 'octopus movement'],
    }
    assert validate_short_package(package) is package
    bad = dict(package, title='لن تصدق الحقيقة الصادمة عن الأخطبوط!')
    with pytest.raises(ValueError):
        validate_short_package(bad)

def test_visual_query_variants_broaden_specific_searches():
    from autoshorts.publisher_v3 import _visual_query_variants
    variants = _visual_query_variants('Niagara Falls rock edge erosion aerial view')
    assert 'Niagara Falls' in variants
    assert any(v.lower() == 'erosion' for v in variants)

def test_scheduled_publish_uses_fixed_netherlands_slots():
    from autoshorts.publisher_v3 import scheduled_publish_at
    now = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)  # 10:00 Europe/Amsterdam
    state = {'published': []}
    assert scheduled_publish_at(state, 'short', now) == '2026-09-24T10:30:00Z'

    state = {'published': [{'scheduled_publish_at': '2026-09-24T10:30:00Z'}]}
    assert scheduled_publish_at(state, 'short', now) == '2026-09-24T18:30:00Z'

def test_scheduled_publish_skips_too_close_slot():
    from autoshorts.publisher_v3 import scheduled_publish_at
    now = datetime(2026, 9, 24, 10, 10, tzinfo=timezone.utc)  # 12:10 Europe/Amsterdam
    assert scheduled_publish_at({'published': []}, 'short', now) == '2026-09-24T18:30:00Z'

