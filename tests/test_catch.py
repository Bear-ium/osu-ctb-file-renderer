import os

import pytest

from render.beatmap import ParseBeatmap
from render.catch import (BANANA, DROPLET, FRUIT, TINY_DROPLET, ApplyMods, BuildCatchBeatmap, CatcherTrack, CatchWidth,
                          FindImpossibleMovement, JudgeReplay, LegacyRandom, TimePreempt)
from render.replay import CatchFrame, ParseReplay, ReplayMods

MAP_TEMPLATE = """osu file format v14

[General]
Mode: 2

[Metadata]
Title:Test
Artist:Tester
Creator:Tester
Version:Normal

[Difficulty]
HPDrainRate:5
CircleSize:{cs}
OverallDifficulty:5
ApproachRate:{ar}
SliderMultiplier:1.4
SliderTickRate:1

[TimingPoints]
0,500,4,2,0,50,1,0

[HitObjects]
{objects}
"""


def MakeMap(objects, cs=4, ar=8):
    return ParseBeatmap(MAP_TEMPLATE.format(cs=cs, ar=ar, objects="\n".join(objects)))


@pytest.mark.parametrize("cs, expected", [(5, 85.4), (4, 97.356), (0, 145.18), (10, 25.62)])
def test_catcher_width(cs, expected):
    assert CatchWidth(cs) == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize("ar, expected", [(5, 1200), (9, 600), (10, 450), (0, 1800), (8.5, 675)])
def test_time_preempt(ar, expected):
    assert TimePreempt(ar) == expected


def test_hard_rock_and_easy_adjust_difficulty():
    beatmap = MakeMap(["256,192,1000,1,0"], cs=4, ar=8)
    hard_rock = ApplyMods(beatmap.difficulty, ReplayMods(hard_rock=True))
    assert hard_rock.circle_size == pytest.approx(5.2, abs=1e-4)
    assert hard_rock.approach_rate == pytest.approx(10.0)
    easy = ApplyMods(beatmap.difficulty, ReplayMods(easy=True))
    assert (easy.circle_size, easy.approach_rate) == (2, 4)


def test_legacy_random_is_deterministic():
    first, second = LegacyRandom(1337), LegacyRandom(1337)
    assert [first.Next() for _ in range(5)] == [second.Next() for _ in range(5)]
    assert all(0 <= LegacyRandom(1).NextDouble() < 1 for _ in range(10))


def test_juice_stream_generates_fruits_droplets_and_tiny_droplets():
    beatmap = MakeMap(["100,192,1000,2,0,L|300:192,1,200"])
    objects = BuildCatchBeatmap(beatmap, ReplayMods()).objects
    kinds = [o.kind for o in objects]
    assert kinds.count(FRUIT) == 2
    assert kinds.count(DROPLET) == 1
    assert kinds.count(TINY_DROPLET) > 0
    assert objects[0].x == 100 and objects[0].time == 1000
    tail = [o for o in objects if o.kind == FRUIT][-1]
    assert tail.x == pytest.approx(300, abs=0.01)
    assert tail.time == pytest.approx(1000 + 200 / (100 * 1.4 / 500))


def test_repeating_juice_stream_returns_to_start():
    beatmap = MakeMap(["100,192,1000,2,0,L|300:192,2,200"])
    fruits = [o for o in BuildCatchBeatmap(beatmap, ReplayMods()).objects if o.kind == FRUIT]
    assert [round(f.x) for f in fruits] == [100, 300, 100]


def test_spinner_becomes_bananas():
    beatmap = MakeMap(["256,192,1000,12,0,2000"])
    bananas = BuildCatchBeatmap(beatmap, ReplayMods()).objects
    assert all(o.kind == BANANA for o in bananas)
    assert len(bananas) == 17
    assert all(0 <= b.x <= 512 for b in bananas)


def test_judgement_follows_catcher_position():
    beatmap = MakeMap(["100,192,1000,1,0", "400,192,2000,1,0"])
    catch_beatmap = BuildCatchBeatmap(beatmap, ReplayMods())
    track = CatcherTrack([CatchFrame(0, 100, False), CatchFrame(1500, 100, False), CatchFrame(2500, 100, False)])
    judgements = JudgeReplay(catch_beatmap, track)
    assert [o.caught for o in catch_beatmap.objects] == [True, False]
    assert judgements.misses == 1
    assert judgements.StateAt(1500) == (1, 1.0)
    assert judgements.StateAt(2500) == (0, 0.5)


def test_catcher_position_interpolates_between_frames():
    track = CatcherTrack([CatchFrame(0, 100, False), CatchFrame(100, 200, True)])
    assert track.XAt(50) == 150
    assert track.XAt(-10) == 100
    assert track.XAt(500) == 200
    assert track.DashingAt(100)


SAMPLE_OSR = os.environ.get("CTB_SAMPLE_OSR")
SAMPLE_OSU = os.environ.get("CTB_SAMPLE_OSU")


@pytest.mark.skipif(not (SAMPLE_OSR and SAMPLE_OSU), reason="set CTB_SAMPLE_OSR and CTB_SAMPLE_OSU to a real replay and its .osu")
def test_real_replay_matches_its_own_judgement_counts():
    replay = ParseReplay(SAMPLE_OSR)
    with open(SAMPLE_OSU, encoding="utf-8-sig") as file:
        beatmap = ParseBeatmap(file.read())
    catch_beatmap = BuildCatchBeatmap(beatmap, replay.mods)
    judgements = JudgeReplay(catch_beatmap, CatcherTrack(replay.frames))
    kinds = [o.kind for o in catch_beatmap.objects]
    assert kinds.count(FRUIT) + kinds.count(DROPLET) == replay.count_300 + replay.count_100 + replay.count_miss
    assert kinds.count(TINY_DROPLET) == replay.count_50 + replay.count_katu
    assert judgements.misses == replay.count_miss
    assert judgements.tiny_missed == replay.count_katu
    assert judgements.max_combo == replay.max_combo
    assert FindImpossibleMovement(catch_beatmap, replay.frames) is None

    edited = list(replay.frames)
    middle = len(edited) // 2
    far_side = 0 if edited[middle].x > 256 else 512
    edited[middle] = CatchFrame(edited[middle].time, far_side, edited[middle].dashing)
    assert FindImpossibleMovement(catch_beatmap, edited) is not None


def test_normal_movement_is_allowed():
    catch_beatmap = BuildCatchBeatmap(MakeMap(["100,192,1000,1,0", "400,192,2000,1,0"]), ReplayMods())
    frames = [CatchFrame(t, 100 + min(max(t - 1000, 0), 300) * 0.3, False) for t in range(0, 2500, 16)]
    assert FindImpossibleMovement(catch_beatmap, frames) is None


def test_teleporting_catcher_is_flagged():
    catch_beatmap = BuildCatchBeatmap(MakeMap(["100,192,1000,1,0", "400,192,3000,1,0"]), ReplayMods())
    frames = [CatchFrame(t, 100 if t < 1500 else 450, True) for t in range(0, 2500, 16)]
    assert FindImpossibleMovement(catch_beatmap, frames) is not None


def test_hyperdash_speed_is_allowed():
    catch_beatmap = BuildCatchBeatmap(MakeMap(["0,192,1000,1,0", "512,192,1200,1,0"]), ReplayMods())
    assert catch_beatmap.objects[0].hyper_target is not None
    frames = [CatchFrame(t, min(max((t - 1000) * 2.56, 0), 512), True) for t in range(0, 1600, 16)]
    assert FindImpossibleMovement(catch_beatmap, frames) is None
