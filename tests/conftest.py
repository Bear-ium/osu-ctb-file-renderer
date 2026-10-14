import os
import sys
from datetime import datetime, timezone

import pytest
from osrparse import GameMode, Mod, Replay, ReplayEventCatch, ReplayEventOsu
from osrparse.utils import Key

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLE_HASH = "0123456789abcdef0123456789abcdef"


def MakeReplay(path, mode=GameMode.CTB, mods=Mod.NoMod, events=None):
    if events is None:
        if mode == GameMode.CTB:
            events = [ReplayEventCatch(0, 256.0, False), ReplayEventCatch(16, 250.0, False), ReplayEventCatch(17, 240.0, True)]
        else:
            events = [ReplayEventOsu(0, 256.0, 192.0, Key(0)), ReplayEventOsu(16, 250.0, 190.0, Key(0))]
    replay = Replay(
        mode=mode, game_version=20240101, beatmap_hash=SAMPLE_HASH, username="Tester",
        replay_hash="f" * 32, count_300=1, count_100=0, count_50=0, count_geki=0, count_katu=0, count_miss=0,
        score=1000, max_combo=1, perfect=True, mods=mods, life_bar_graph=None,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc), replay_data=events, replay_id=0, rng_seed=None,
    )
    replay.write_path(str(path))
    return path


@pytest.fixture
def make_replay(tmp_path):
    def Factory(name="replay.osr", **kwargs):
        return MakeReplay(tmp_path / name, **kwargs)
    return Factory
