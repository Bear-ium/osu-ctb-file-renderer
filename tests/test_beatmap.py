import pytest

from render.beatmap import MAX_EVENT_LINES, ParseBeatmap, SafeFilename

MAP_WITH_EVENTS = """osu file format v14

[General]
AudioFilename: song.mp3
AudioLeadIn: 1500
Mode: 2

[Metadata]
Title:Test
BeatmapSetID:12345

[Difficulty]
CircleSize:4

[Events]
//Background and Video events
0,0,"bg image.jpg",0,0
Video,500,"clip.mp4"
//Break Periods
2,10000,15000
Break,20000,22500
//Storyboard Layer 0 (Background)
Sprite,Background,Centre,"sb/thing.png",320,240
 F,0,0,1000,0,1
_M,0,0,1000,0,0

[TimingPoints]
0,500,4,2,0,50,1,0

[HitObjects]
256,192,1000,1,0
"""


def test_reads_audio_background_breaks_and_set_id():
    beatmap = ParseBeatmap(MAP_WITH_EVENTS)
    assert beatmap.audio_filename == "song.mp3"
    assert beatmap.audio_lead_in == 1500
    assert beatmap.background_filename == "bg image.jpg"
    assert beatmap.breaks == [(10000, 15000), (20000, 22500)]
    assert beatmap.beatmapset_id == 12345


def test_old_format_offset_applies_to_breaks():
    beatmap = ParseBeatmap(MAP_WITH_EVENTS.replace("osu file format v14", "osu file format v4"))
    assert beatmap.breaks[0] == (10024, 15024)


def test_maps_without_events_still_parse():
    text = "osu file format v14\n\n[Difficulty]\nCircleSize:4\n\n[HitObjects]\n256,192,1000,1,0\n"
    beatmap = ParseBeatmap(text)
    assert (beatmap.audio_filename, beatmap.background_filename, beatmap.breaks, beatmap.beatmapset_id) == ("", "", [], 0)


@pytest.mark.parametrize("value", ['"../../etc/passwd"', "sb/bg.jpg", "C:\\x.jpg", "a\\b.jpg", '".."', '""', "c:bg.jpg"])
def test_unsafe_filenames_are_dropped(value):
    assert SafeFilename(value) == ""


def test_unsafe_background_is_ignored():
    beatmap = ParseBeatmap(MAP_WITH_EVENTS.replace('"bg image.jpg"', '"../secret.jpg"'))
    assert beatmap.background_filename == ""


def test_event_lines_are_capped():
    filler = "\n".join("Sprite,Background,Centre,\"x.png\",320,240" for _ in range(MAX_EVENT_LINES + 10))
    text = MAP_WITH_EVENTS.replace("//Break Periods", filler + "\n2,30000,31000")
    beatmap = ParseBeatmap(text)
    assert (30000, 31000) not in beatmap.breaks
    assert beatmap.background_filename == "bg image.jpg"
