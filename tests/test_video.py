import pytest

from render.replay import ExtractMods
from render.video import AUDIO_KBPS, MIN_BITRATE_KBPS, AudioFilter, TargetBitrateKbps, Truncate, LoadFont


def test_audio_filter_trims_when_video_starts_after_song():
    assert AudioFilter(2500, 1.0, False, 60) == (
        "aresample=44100,atrim=start=2.500,asetpts=PTS-STARTPTS,afade=t=out:st=58.500:d=1.5,apad"
    )


def test_audio_filter_delays_when_video_starts_before_song():
    assert AudioFilter(-1939, 1.0, False, 62.8) == "aresample=44100,adelay=1939:all=1,afade=t=out:st=61.300:d=1.5,apad"


@pytest.mark.parametrize("rate, expected", [(1.5, "atempo=1.5"), (0.75, "atempo=0.75")])
def test_audio_filter_changes_tempo_for_dt_and_ht(rate, expected):
    audio_filter = AudioFilter(1000, rate, False, 40)
    assert expected in audio_filter
    assert "asetrate" not in audio_filter


def test_audio_filter_raises_pitch_for_nightcore():
    audio_filter = AudioFilter(1000, 1.5, True, 40)
    assert "asetrate=44100*1.5,aresample=44100" in audio_filter
    assert "atempo" not in audio_filter


def test_audio_filter_never_fades_before_zero():
    assert "afade=t=out:st=0.000:d=1.5" in AudioFilter(0, 1.0, False, 1.0)


def test_bitrate_budget_subtracts_audio():
    without_audio = TargetBitrateKbps(10 * 1024 * 1024, 60)
    with_audio = TargetBitrateKbps(10 * 1024 * 1024, 60, AUDIO_KBPS)
    assert without_audio - with_audio == pytest.approx(AUDIO_KBPS * 1.05, abs=1)


def test_bitrate_budget_has_a_floor():
    assert TargetBitrateKbps(1024 * 1024, 600, AUDIO_KBPS) == MIN_BITRATE_KBPS


def test_nightcore_is_told_apart_from_double_time():
    nightcore = ExtractMods((1 << 9) | (1 << 6))
    assert nightcore.nightcore and nightcore.double_time and nightcore.rate == 1.5
    assert nightcore.Acronyms() == ["NC"]
    double_time = ExtractMods(1 << 6)
    assert not double_time.nightcore and double_time.Acronyms() == ["DT"]


def test_long_text_is_truncated_with_an_ellipsis():
    font = LoadFont(16)
    text = Truncate("Three feat. Kagamine Len and a great many other featured artists", font, 200)
    assert text.endswith("…") and font.getlength(text) <= 200
    assert Truncate("Short", font, 200) == "Short"
