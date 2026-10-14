import lzma
import struct

import pytest

from bot.config import Config
from bot.main import RenderQueue, SafeName
from render.catch import BuildCatchBeatmap
from render.errors import BeatmapError, ReplayError
from render.replay import MAX_REPLAY_DATA_BYTES, CompressedReplayData, ParseReplay, ReplayMods
from tests.test_catch import MAP_TEMPLATE
from render.beatmap import ParseBeatmap


def test_rejects_decompression_bomb(make_replay, tmp_path):
    data = make_replay().read_bytes()
    original = CompressedReplayData(data)
    start = data.index(original)
    bomb = lzma.compress(b"0|256|0|0," * (MAX_REPLAY_DATA_BYTES // 10 + 1000), format=lzma.FORMAT_ALONE)
    patched = data[:start - 4] + struct.pack("<i", len(bomb)) + bomb + data[start + len(original):]
    path = tmp_path / "bomb.osr"
    path.write_bytes(patched)
    with pytest.raises(ReplayError, match="far more movement data"):
        ParseReplay(str(path))


def test_huge_spinner_is_rejected():
    beatmap = ParseBeatmap(MAP_TEMPLATE.format(cs=4, ar=8, objects="256,192,1000,12,0,999999999"))
    with pytest.raises(BeatmapError, match="too many objects"):
        BuildCatchBeatmap(beatmap, ReplayMods())


def test_extreme_tick_rate_is_rejected():
    text = MAP_TEMPLATE.format(cs=4, ar=8, objects="100,192,1000,2,0,L|300:192,1,200").replace("SliderTickRate:1", "SliderTickRate:100000")
    with pytest.raises(BeatmapError):
        BuildCatchBeatmap(ParseBeatmap(text), ReplayMods())


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
