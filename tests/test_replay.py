import pytest
from osrparse import GameMode, Mod, ReplayEventCatch

from render.errors import ReplayError
from render.replay import AbsoluteFrames, ExtractMods, ParseReplay


def test_parses_catch_replay(make_replay):
    replay = ParseReplay(str(make_replay()))
    assert replay.username == "Tester"
    assert replay.beatmap_hash == "0123456789abcdef0123456789abcdef"
    assert [f.time for f in replay.frames] == [0, 16, 33]
    assert replay.frames[-1].dashing


@pytest.mark.parametrize("mode, name", [(GameMode.STD, "osu!standard"), (GameMode.MANIA, "osu!mania"), (GameMode.TAIKO, "osu!taiko")])
def test_rejects_other_modes(make_replay, mode, name):
    with pytest.raises(ReplayError, match="Only osu!catch replays are supported") as error:
        ParseReplay(str(make_replay(mode=mode)))
    assert name in str(error.value)


def test_rejects_corrupt_file(tmp_path):
    path = tmp_path / "broken.osr"
    path.write_bytes(b"\x02\x00\x00\x00not really a replay at all")
    with pytest.raises(ReplayError, match="corrupt"):
        ParseReplay(str(path))


def test_rejects_truncated_file(make_replay, tmp_path):
    data = make_replay().read_bytes()
    path = tmp_path / "truncated.osr"
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(ReplayError):
        ParseReplay(str(path))


def test_rejects_empty_file(tmp_path):
    path = tmp_path / "empty.osr"
    path.write_bytes(b"")
    with pytest.raises(ReplayError, match="empty"):
        ParseReplay(str(path))


def test_rejects_huge_file(tmp_path):
    path = tmp_path / "huge.osr"
    path.write_bytes(b"\0" * 1024)
    with pytest.raises(ReplayError, match="too large"):
        ParseReplay(str(path), max_bytes=512)


def test_rejects_wrong_extension(tmp_path):
    path = tmp_path / "replay.txt"
    path.write_bytes(b"data")
    with pytest.raises(ReplayError, match=".osr"):
        ParseReplay(str(path))


def test_relative_times_become_absolute():
    events = [ReplayEventCatch(-1940, 256, False), ReplayEventCatch(17, 256, False), ReplayEventCatch(8, 250, True),
              ReplayEventCatch(0, 240, True), ReplayEventCatch(18, 230, False)]
    frames = AbsoluteFrames(events)
    assert [f.time for f in frames] == [-1923, -1915, -1915, -1897]
    assert [f.x for f in frames] == [256, 250, 240, 230]


def test_negative_delta_advances_clock_but_is_dropped():
    events = [ReplayEventCatch(100, 1, False), ReplayEventCatch(-20, 2, False), ReplayEventCatch(50, 3, False)]
    frames = AbsoluteFrames(events)
    assert [(f.time, f.x) for f in frames] == [(100, 1), (130, 3)]


def test_positions_are_clamped_to_playfield():
    frames = AbsoluteFrames([ReplayEventCatch(0, -10, False), ReplayEventCatch(16, 600, False)])
    assert [f.x for f in frames] == [0, 512]


def test_extracts_mods():
    mods = ExtractMods(Mod.HardRock | Mod.DoubleTime)
    assert mods.hard_rock and mods.double_time and mods.rate == 1.5
    assert ExtractMods(Mod.Nightcore | Mod.DoubleTime).rate == 1.5
    assert ExtractMods(Mod.HalfTime).rate == 0.75
    assert ExtractMods(Mod.Easy).Acronyms() == ["EZ"]
    assert ExtractMods(Mod.NoMod).rate == 1.0
