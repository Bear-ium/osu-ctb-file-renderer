"""osu!catch object generation and judgement, ported from ppy/osu's osu.Game.Rulesets.Catch."""
import bisect
from dataclasses import dataclass, field

import numpy as np

from render.beatmap import Difficulty
from render.curves import SliderPath
from render.errors import BeatmapError

PLAYFIELD_WIDTH = 512
PLAYFIELD_HEIGHT = 384
CATCHER_BASE_SIZE = 106.75
ALLOWED_CATCH_RANGE = 0.8
BASE_DASH_SPEED = 1.0
BASE_SCORING_DISTANCE = 100
RNG_SEED = 1337
TAIL_LENIENCY = -36
MAX_SLIDER_LENGTH = 100000
MAX_OBJECTS = 100000

FRUIT = "fruit"
DROPLET = "droplet"
TINY_DROPLET = "tiny"
BANANA = "banana"


@dataclass
class CatchObject:
    kind: str
    time: float
    original_x: float
    colour_index: int = 0
    x_offset: float = 0.0
    hyper_target: "CatchObject | None" = None
    caught: bool = False

    @property
    def x(self):
        return min(max(self.original_x + self.x_offset, 0), PLAYFIELD_WIDTH)


class ObjectBudget:
    """Caps how many objects a beatmap may generate, so a hostile map can't exhaust memory."""

    def __init__(self, limit=MAX_OBJECTS):
        self.remaining = limit

    def Take(self, count=1):
        self.remaining -= count
        if self.remaining < 0:
            raise BeatmapError("The beatmap has too many objects to render.")


@dataclass
class ParentObject:
    kind: str
    time: float
    original_x: float
    nested: list = field(default_factory=list)
    path_end_x: float = 0.0


class LegacyRandom:
    """osu!stable's xorshift RNG, needed so HR offsets, tiny droplet jitter and banana positions match the game."""

    INT_TO_REAL = 1.0 / (2147483647 + 1.0)
    MASK = 0xFFFFFFFF

    def __init__(self, seed):
        self.x = seed & self.MASK
        self.y = 842502087
        self.z = 3579807591
        self.w = 273326509
        self.bit_buffer = 0
        self.bit_index = 32

    def NextUInt(self):
        t = (self.x ^ (self.x << 11)) & self.MASK
        self.x, self.y, self.z = self.y, self.z, self.w
        self.w = (self.w ^ (self.w >> 19) ^ t ^ (t >> 8)) & self.MASK
        return self.w

    def Next(self):
        return 0x7FFFFFFF & self.NextUInt()

    def NextDouble(self):
        return self.INT_TO_REAL * self.Next()

    def NextRange(self, lower, upper):
        return int(lower + self.NextDouble() * (upper - lower))

    def NextBool(self):
        if self.bit_index == 32:
            self.bit_buffer = self.NextUInt()
            self.bit_index = 1
            return (self.bit_buffer & 1) == 1
        self.bit_index += 1
        self.bit_buffer >>= 1
        return (self.bit_buffer & 1) == 1


def F32(value):
    return float(np.float32(value))


def DifficultyRange(difficulty, minimum, middle, maximum):
    if difficulty > 5:
        return middle + (maximum - middle) * (difficulty - 5) / 5
    if difficulty < 5:
        return middle + (middle - minimum) * (difficulty - 5) / 5
    return middle


def ApplyMods(difficulty, mods):
    circle_size = difficulty.circle_size
    approach_rate = difficulty.approach_rate
    if mods.hard_rock:
        circle_size = min(F32(circle_size * F32(1.3)), 10.0)
        approach_rate = min(F32(approach_rate * F32(1.4)), 10.0)
    if mods.easy:
        circle_size *= 0.5
        approach_rate *= 0.5
    return Difficulty(
        hp=difficulty.hp, circle_size=circle_size, overall=difficulty.overall, approach_rate=approach_rate,
        slider_multiplier=difficulty.slider_multiplier, slider_tick_rate=difficulty.slider_tick_rate,
    )


def ObjectScale(circle_size):
    return (1.0 - 0.7 * (circle_size - 5) / 5) / 2


def CatchWidth(circle_size):
    return CATCHER_BASE_SIZE * abs(ObjectScale(circle_size) * 2) * ALLOWED_CATCH_RANGE


def TimePreempt(approach_rate):
    return int(DifficultyRange(approach_rate, 1800, 1200, 450))


def SliderEvents(start_time, span_duration, velocity, tick_distance, total_distance, span_count):
    length = min(MAX_SLIDER_LENGTH, total_distance)
    tick_distance = min(max(tick_distance, 0), length)
    if tick_distance > 0 and length / tick_distance > MAX_OBJECTS:
        raise BeatmapError("The beatmap has a juice stream with too many droplets to render.")
    min_distance_from_end = velocity * 10

    yield ("head", start_time, 0.0)
    for span in range(span_count):
        span_start_time = start_time + span * span_duration
        reversed_span = span % 2 == 1
        if tick_distance != 0:
            ticks = []
            d = tick_distance
            while d <= length:
                if d >= length - min_distance_from_end:
                    break
                path_progress = d / length
                time_progress = 1 - path_progress if reversed_span else path_progress
                ticks.append(("tick", span_start_time + time_progress * span_duration, path_progress))
                d += tick_distance
            if reversed_span:
                ticks.reverse()
            yield from ticks
        if span < span_count - 1:
            yield ("repeat", span_start_time + span_duration, float((span + 1) % 2))

    total_duration = span_count * span_duration
    final_span_start_time = start_time + (span_count - 1) * span_duration
    legacy_last_tick_time = max(start_time + total_duration / 2, (final_span_start_time + span_duration) + TAIL_LENIENCY)
    legacy_last_tick_progress = (legacy_last_tick_time - final_span_start_time) / span_duration
    if span_count % 2 == 0:
        legacy_last_tick_progress = 1 - legacy_last_tick_progress
    yield ("legacy_last_tick", legacy_last_tick_time, legacy_last_tick_progress)
    yield ("tail", start_time + total_duration, float(span_count % 2))


def BuildJuiceStream(beatmap, hit_object, colour_index, budget):
    timing_point = beatmap.TimingPointAt(hit_object.time)
    difficulty_point = beatmap.DifficultyPointAt(hit_object.time)
    slider_velocity = difficulty_point.slider_velocity
    tick_distance_multiplier = 1 / slider_velocity if beatmap.format_version < 8 else 1

    bpm_multiplier = min(max(F32(100 / slider_velocity), 10), 1000) / 100.0
    velocity = BASE_SCORING_DISTANCE * beatmap.difficulty.slider_multiplier / (timing_point.beat_length * bpm_multiplier)
    scoring_distance = velocity * timing_point.beat_length
    tick_distance = scoring_distance / beatmap.difficulty.slider_tick_rate * tick_distance_multiplier

    path = SliderPath(hit_object.control_points, hit_object.expected_distance)
    span_count = hit_object.repeat_count + 1
    head_x = min(max(hit_object.x, 0), PLAYFIELD_WIDTH)
    parent = ParentObject("juice_stream", hit_object.time, hit_object.x)
    parent.path_end_x = hit_object.x + (hit_object.control_points[-1][0][0] if hit_object.control_points else 0)

    def Add(obj):
        budget.Take()
        parent.nested.append(obj)

    if path.distance <= 0 or velocity <= 0:
        Add(CatchObject(FRUIT, hit_object.time, head_x, colour_index))
        return parent

    span_duration = path.distance / velocity
    last_event = None
    for event_type, event_time, progress in SliderEvents(hit_object.time, span_duration, velocity, tick_distance, path.distance, span_count):
        if last_event is not None:
            since_last_tick = int(event_time) - int(last_event[1])
            if since_last_tick > 80:
                time_between_tiny = since_last_tick
                while time_between_tiny > 100:
                    time_between_tiny /= 2
                t = time_between_tiny
                while t < since_last_tick:
                    tiny_progress = last_event[2] + (t / since_last_tick) * (progress - last_event[2])
                    Add(CatchObject(TINY_DROPLET, t + last_event[1], head_x + path.PositionAt(tiny_progress)[0], colour_index))
                    t += time_between_tiny
        last_event = (event_type, event_time, progress)

        if event_type == "tick":
            Add(CatchObject(DROPLET, event_time, head_x + path.PositionAt(progress)[0], colour_index))
        elif event_type in ("head", "tail", "repeat"):
            Add(CatchObject(FRUIT, event_time, head_x + path.PositionAt(progress)[0], colour_index))
    return parent


def BuildBananaShower(hit_object, budget):
    parent = ParentObject("banana_shower", hit_object.time, 256)
    start_time = int(hit_object.time)
    end_time = int(hit_object.end_time)
    spacing = F32(hit_object.end_time - hit_object.time)
    while spacing > 100:
        spacing = F32(spacing / 2)
    if spacing <= 0:
        return parent
    budget.Take(int((end_time - start_time) / spacing) + 1)
    time = F32(start_time)
    while time <= end_time:
        parent.nested.append(CatchObject(BANANA, time, 0.0))
        time = F32(time + spacing)
    return parent


def ApplyHardRockOffset(fruit, state, rng):
    offset_position = F32(fruit.original_x)
    start_time = fruit.time
    if state["last_position"] is None or state["last_position"] == 0:
        state["last_position"] = offset_position
        state["last_start_time"] = start_time
        return

    position_diff = F32(offset_position - state["last_position"])
    time_diff = int(start_time - state["last_start_time"])
    if time_diff > 1000:
        state["last_position"] = offset_position
        state["last_start_time"] = start_time
        return

    if position_diff == 0:
        right = rng.NextBool()
        rand = min(20.0, float(rng.NextRange(0, max(0.0, time_diff / 4.0))))
        if right:
            offset_position = offset_position + rand if offset_position + rand <= PLAYFIELD_WIDTH else offset_position - rand
        else:
            offset_position = offset_position - rand if offset_position - rand >= 0 else offset_position + rand
        fruit.x_offset = offset_position - fruit.original_x
        return

    if abs(position_diff) < int(time_diff / 3):
        if position_diff > 0:
            if offset_position + position_diff < PLAYFIELD_WIDTH:
                offset_position += position_diff
        elif offset_position + position_diff > 0:
            offset_position += position_diff

    fruit.x_offset = offset_position - fruit.original_x
    state["last_position"] = offset_position
    state["last_start_time"] = start_time


def ApplyPositionOffsets(parents, hard_rock):
    rng = LegacyRandom(RNG_SEED)
    state = {"last_position": None, "last_start_time": 0.0}
    for parent in parents:
        if parent.kind == "fruit":
            if hard_rock:
                ApplyHardRockOffset(parent.nested[0], state, rng)
        elif parent.kind == "banana_shower":
            for banana in parent.nested:
                banana.x_offset = rng.NextDouble() * PLAYFIELD_WIDTH
                rng.Next()
                rng.Next()
                rng.Next()
        elif parent.kind == "juice_stream":
            state["last_position"] = F32(parent.path_end_x)
            state["last_start_time"] = parent.time
            for nested in parent.nested:
                nested.x_offset = 0
                if nested.kind == TINY_DROPLET:
                    nested.x_offset = min(max(rng.NextRange(-20, 20), -nested.original_x), PLAYFIELD_WIDTH - nested.original_x)
                elif nested.kind == DROPLET:
                    rng.Next()


def InitialiseHyperDash(objects, circle_size):
    palpable = [o for o in objects if o.kind in (FRUIT, DROPLET)]
    half_catcher_width = CatchWidth(circle_size) / 2 / ALLOWED_CATCH_RANGE
    last_direction = 0
    last_excess = half_catcher_width
    for current, following in zip(palpable, palpable[1:]):
        current.hyper_target = None
        this_direction = 1 if following.x > current.x else -1
        time_to_next = int(following.time) - int(current.time) - 1000 / 60 / 4
        distance_to_next = abs(following.x - current.x) - (last_excess if last_direction == this_direction else half_catcher_width)
        distance_to_hyper = F32(time_to_next * BASE_DASH_SPEED - distance_to_next)
        if distance_to_hyper < 0:
            current.hyper_target = following
            last_excess = half_catcher_width
        else:
            last_excess = min(max(distance_to_hyper, 0), half_catcher_width)
        last_direction = this_direction


@dataclass
class CatchBeatmap:
    objects: list
    difficulty: Difficulty
    catch_width: float
    object_scale: float
    preempt: int
    start_time: float
    end_time: float


def BuildCatchBeatmap(beatmap, mods):
    difficulty = ApplyMods(beatmap.difficulty, mods)
    budget = ObjectBudget()
    parents = []
    combo_index = 0
    last_kind = None
    for hit_object in beatmap.hit_objects:
        if hit_object.kind != "spinner" and (hit_object.new_combo or last_kind in (None, "spinner")):
            combo_index += hit_object.combo_skip + 1
        last_kind = hit_object.kind
        colour_index = combo_index % len(beatmap.combo_colours)
        if hit_object.kind == "circle":
            parent = ParentObject("fruit", hit_object.time, hit_object.x)
            budget.Take()
            parent.nested.append(CatchObject(FRUIT, hit_object.time, hit_object.x, colour_index))
        elif hit_object.kind == "slider":
            parent = BuildJuiceStream(beatmap, hit_object, colour_index, budget)
        else:
            parent = BuildBananaShower(hit_object, budget)
        parents.append(parent)

    ApplyPositionOffsets(parents, mods.hard_rock)

    objects = [nested for parent in parents for nested in parent.nested]
    objects.sort(key=lambda o: o.time)
    InitialiseHyperDash(objects, difficulty.circle_size)

    if not objects:
        raise BeatmapError("The beatmap has nothing to catch.")
    return CatchBeatmap(
        objects=objects,
        difficulty=difficulty,
        catch_width=CatchWidth(difficulty.circle_size),
        object_scale=ObjectScale(difficulty.circle_size),
        preempt=TimePreempt(difficulty.approach_rate),
        start_time=objects[0].time,
        end_time=objects[-1].time,
    )


class CatcherTrack:
    """Catcher position over time, linearly interpolated between replay frames like osu!'s replay input handler."""

    def __init__(self, frames):
        self.times = [frame.time for frame in frames]
        self.frames = frames

    def IndexAt(self, time):
        return bisect.bisect_right(self.times, time) - 1

    def XAt(self, time):
        index = self.IndexAt(time)
        if index < 0:
            return self.frames[0].x
        if index >= len(self.frames) - 1:
            return self.frames[-1].x
        start, end = self.frames[index], self.frames[index + 1]
        if end.time == start.time:
            return end.x
        w = (time - start.time) / (end.time - start.time)
        return start.x + (end.x - start.x) * w

    def DashingAt(self, time):
        index = self.IndexAt(time)
        return self.frames[max(index, 0)].dashing


@dataclass
class Judgements:
    combo_at: list
    accuracy_at: list
    judgement_times: list
    hyper_intervals: list
    fruits_caught: int = 0
    droplets_caught: int = 0
    tiny_caught: int = 0
    tiny_missed: int = 0
    misses: int = 0
    max_combo: int = 0

    def StateAt(self, time):
        index = bisect.bisect_right(self.judgement_times, time) - 1
        if index < 0:
            return 0, 1.0
        return self.combo_at[index], self.accuracy_at[index]


def JudgeReplay(catch_beatmap, track):
    half_width = catch_beatmap.catch_width / 2
    judgements = Judgements(combo_at=[], accuracy_at=[], judgement_times=[], hyper_intervals=[])
    combo = 0
    hits = 0
    total = 0
    for obj in catch_beatmap.objects:
        catcher_x = track.XAt(obj.time)
        obj.caught = catcher_x - half_width <= obj.x <= catcher_x + half_width
        if obj.kind == BANANA:
            continue
        total += 1
        if obj.caught:
            hits += 1
        if obj.kind == FRUIT:
            judgements.fruits_caught += obj.caught
        elif obj.kind == DROPLET:
            judgements.droplets_caught += obj.caught
        else:
            judgements.tiny_caught += obj.caught
            judgements.tiny_missed += not obj.caught

        if obj.kind in (FRUIT, DROPLET):
            if obj.caught:
                combo += 1
                if obj.hyper_target is not None:
                    judgements.hyper_intervals.append((obj.time, obj.hyper_target.time))
            else:
                combo = 0
                judgements.misses += 1
        judgements.max_combo = max(judgements.max_combo, combo)
        judgements.judgement_times.append(obj.time)
        judgements.combo_at.append(combo)
        judgements.accuracy_at.append(hits / total)
    return judgements
