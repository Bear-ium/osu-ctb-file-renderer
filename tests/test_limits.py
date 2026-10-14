import lzma
import struct
import time

import pytest

from bot.config import Config
from bot.main import RenderQueue, SafeName
from render.beatmap import ParseBeatmap
from render.catch import BuildCatchBeatmap
from render.errors import BeatmapError, ReplayError
from render.curves import PathBudget, SliderPath
from render.replay import MAX_FRAMES, MAX_REPLAY_DATA_BYTES, ParseReplay, ReplayMods
from tests.test_catch import MAP_TEMPLATE

FRAMES = b"0|256|-500|0,-1|256|-500|0,16|250|0|0,17|240|0|1,"


def OsrString(text):
    data = text.encode()
    return b"\x0b" + bytes([len(data)]) + data


def BuildOsr(compressed, length=None, mode=2):
    header = bytes([mode]) + struct.pack("<I", 20240101)
    header += OsrString("0123456789abcdef0123456789abcdef") + OsrString("Tester") + OsrString("f" * 32)
    header += struct.pack("<6H", 1, 0, 0, 0, 0, 0) + struct.pack("<I", 1000) + struct.pack("<H", 1) + b"\x01"
    header += struct.pack("<I", 0) + b"\x00" + struct.pack("<q", 0)
    header += struct.pack("<I", len(compressed) if length is None else length)
    return header + compressed + struct.pack("<q", 0)


def Write(tmp_path, data, name="replay.osr"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_hand_built_replay_parses(tmp_path):
    replay = ParseReplay(Write(tmp_path, BuildOsr(lzma.compress(FRAMES, format=lzma.FORMAT_ALONE))))
    assert [(f.time, f.x, f.dashing) for f in replay.frames] == [(15, 250, False), (32, 240, True)]


def test_rejects_decompression_bomb(tmp_path):
    bomb = lzma.compress(b"0|256|0|0," * (MAX_REPLAY_DATA_BYTES // 10 + 1000), format=lzma.FORMAT_ALONE)
    with pytest.raises(ReplayError, match="far more movement data"):
        ParseReplay(Write(tmp_path, BuildOsr(bomb)))


def test_oversized_length_field_is_rejected(tmp_path):
    compressed = lzma.compress(FRAMES, format=lzma.FORMAT_ALONE)
    for length in (0x80000000, 0xFFFFFFF0):
        with pytest.raises(ReplayError, match="corrupt"):
            ParseReplay(Write(tmp_path, BuildOsr(compressed, length=length)))


def test_streams_after_the_first_are_ignored(tmp_path):
    bomb = lzma.compress(b"0|256|0|0," * (MAX_REPLAY_DATA_BYTES // 10 + 1000), format=lzma.FORMAT_XZ)
    compressed = lzma.compress(FRAMES, format=lzma.FORMAT_XZ) + bomb
    replay = ParseReplay(Write(tmp_path, BuildOsr(compressed)))
    assert len(replay.frames) == 2


def test_long_uleb128_is_rejected_quickly(tmp_path):
    data = bytes([2]) + struct.pack("<I", 1) + b"\x0b" + b"\xff" * 4_000_000
    started = time.monotonic()
    with pytest.raises(ReplayError, match="corrupt"):
        ParseReplay(Write(tmp_path, data))
    assert time.monotonic() - started < 1


def test_non_finite_positions_are_rejected(tmp_path):
    frames = b"16|nan|0|0,16|250|0|0,"
    with pytest.raises(ReplayError, match="corrupt"):
        ParseReplay(Write(tmp_path, BuildOsr(lzma.compress(frames, format=lzma.FORMAT_ALONE))))


def test_huge_spinner_is_rejected():
    beatmap = ParseBeatmap(MAP_TEMPLATE.format(cs=4, ar=8, objects="256,192,1000,12,0,2000000000"))
    with pytest.raises(BeatmapError, match="too many objects"):
        BuildCatchBeatmap(beatmap, ReplayMods())


def test_late_spinner_does_not_loop_forever():
    beatmap = ParseBeatmap(MAP_TEMPLATE.format(cs=4, ar=8, objects="256,192,2000000000,12,0,2000000050"))
    with pytest.raises(BeatmapError, match="too late"):
        BuildCatchBeatmap(beatmap, ReplayMods())


def test_difficulty_values_are_clamped_like_osu():
    text = MAP_TEMPLATE.format(cs=40, ar=-3, objects="256,192,1000,1,0").replace("SliderTickRate:1", "SliderTickRate:100000")
    difficulty = ParseBeatmap(text).difficulty
    assert (difficulty.circle_size, difficulty.approach_rate, difficulty.slider_tick_rate) == (10, 0, 8)


@pytest.mark.parametrize("line", [
    "nan,192,1000,1,0",
    "256,192,inf,1,0",
    "256,192,99999999999,1,0",
    "100,192,1000,2,0,B|999999:192,1,200",
])
def test_out_of_range_numbers_are_rejected(line):
    with pytest.raises(BeatmapError):
        ParseBeatmap(MAP_TEMPLATE.format(cs=4, ar=8, objects=line))


def test_slider_with_too_many_points_is_rejected():
    points = "|".join(f"{i % 500}:{i % 300}" for i in range(3000))
    with pytest.raises(BeatmapError, match="too many points"):
        ParseBeatmap(MAP_TEMPLATE.format(cs=4, ar=8, objects=f"100,192,1000,2,0,B|{points},1,200"))


def test_many_repeats_hit_the_object_cap():
    text = MAP_TEMPLATE.format(cs=4, ar=8, objects="100,192,1000,2,0,L|300:192,9000,200").replace("SliderTickRate:1", "SliderTickRate:8")
    with pytest.raises(BeatmapError, match="too many objects"):
        BuildCatchBeatmap(ParseBeatmap(text), ReplayMods())


def test_usernames_cannot_mention_or_format():
    name = SafeName("@everyone **bold** <@123456789012345678>")
    assert "@everyone" not in name
    assert "<@123456789012345678>" not in name
    assert "**" not in name


def MakeConfig(**overrides):
    values = dict(discord_token="", osu_client_id="", osu_client_secret="", cache_dir="", max_upload_bytes=1,
                  max_concurrent_renders=1, render_timeout_seconds=1, user_cooldown_seconds=0, max_queue_length=2, dev_guild_id=None)
    values.update(overrides)
    return Config(**values)


def test_queue_reservation_counts_immediately():
    queue = RenderQueue(MakeConfig())
    assert queue.TryReserve(1) is None
    assert queue.TryReserve(1) is not None
    assert queue.TryReserve(2) is None
    assert "full" in queue.TryReserve(3)


def test_cooldown_starts_at_request_time():
    queue = RenderQueue(MakeConfig(user_cooldown_seconds=60))
    assert queue.TryReserve(1) is None
    queue.active_users.discard(1)
    queue.pending -= 1
    assert "Slow down" in queue.TryReserve(1)


def test_frame_count_is_checked_before_splitting(tmp_path):
    frames = b"16|250|0|0," * (MAX_FRAMES + 10)
    with pytest.raises(ReplayError, match="far more movement data"):
        ParseReplay(Write(tmp_path, BuildOsr(lzma.compress(frames, format=lzma.FORMAT_ALONE))))


def test_path_budget_is_shared_across_sliders():
    budget = PathBudget(max_work=10_000)
    wiggly = [(((i * 7919) % 500, (i * 104729) % 380), "B" if i == 0 else None) for i in range(40)]
    with pytest.raises(BeatmapError, match="too complex"):
        for _ in range(50):
            SliderPath(wiggly, None, budget)
