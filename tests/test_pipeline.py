from autoshorts.main import create_subtitles, srt_time


def test_srt_time():
    assert srt_time(61.234) == "00:01:01,234"


def test_subtitles(tmp_path):
    path = tmp_path / "captions.srt"
    create_subtitles("one two three four five six", 6, path)
    text = path.read_text()
    assert "00:00:00,000 --> 00:00:03,000" in text
    assert "one two three four five" in text

