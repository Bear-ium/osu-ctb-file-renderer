"""Reads the parts of a .osu file that osu!catch needs, following osu!'s LegacyBeatmapDecoder and ConvertHitObjectParser."""
import bisect
import math
from dataclasses import dataclass, field

from render.errors import BeatmapError

FIRST_LAZER_VERSION = 128
EARLY_VERSION_TIMING_OFFSET = 24
DEFAULT_COMBO_COLOURS = [(255, 192, 0), (0, 202, 0), (18, 124, 255), (242, 24, 57)]
MAX_PARSE_VALUE = 2**31 - 1
MAX_COORDINATE_VALUE = 131072
MAX_HIT_OBJECTS = 100_000
MAX_CONTROL_POINTS = 2048


def ParseNumber(text, limit=MAX_PARSE_VALUE, allow_nan=False):
    """Parse a number the way osu!'s Parsing class does: bounded, and NaN only where allowed."""
    value = float(text)
    if math.isnan(value):
        if allow_nan:
            return value
        raise ValueError("not a number")
    if abs(value) > limit:
        raise ValueError("number out of range")
    return value


@dataclass
class Difficulty:
    hp: float = 5
    circle_size: float = 5
    overall: float = 5
    approach_rate: float | None = None
    slider_multiplier: float = 1.4
    slider_tick_rate: float = 1


@dataclass
class TimingPoint:
    time: float
    beat_length: float


@dataclass
class DifficultyPoint:
    time: float
    slider_velocity: float
    generate_ticks: bool


@dataclass
class HitObject:
    kind: str
    time: float
    x: float
    new_combo: bool
    combo_skip: int
    control_points: list = field(default_factory=list)
    expected_distance: float | None = None
    repeat_count: int = 0
    end_time: float = 0


@dataclass
class Beatmap:
    format_version: int
    mode: int
    title: str
    artist: str
    version: str
    creator: str
    difficulty: Difficulty
    timing_points: list
    difficulty_points: list
    hit_objects: list
    combo_colours: list

    def TimingPointAt(self, time):
        if not self.timing_points:
            return TimingPoint(0, 1000)
        times = [point.time for point in self.timing_points]
        index = bisect.bisect_right(times, time) - 1
        return self.timing_points[max(index, 0)]

    def DifficultyPointAt(self, time):
        times = [point.time for point in self.difficulty_points]
        index = bisect.bisect_right(times, time) - 1
        if index < 0:
            return DifficultyPoint(0, 1.0, True)
        return self.difficulty_points[index]


def ConvertPathType(token):
    letter = token[0]
    if letter == "B":
        return "B"
    if letter in "LP":
        return letter
    return "C"


def IsLinear(p0, p1, p2):
    return abs((p1[1] - p0[1]) * (p2[0] - p0[0]) - (p1[0] - p0[0]) * (p2[1] - p0[1])) <= 1e-3


def ConvertPoints(path_type, points, end_point, format_version):
    vertices = [[point, None] for point in points]
    if path_type == "P":
        end_point_length = 0 if end_point is None else 1
        if format_version < FIRST_LAZER_VERSION:
            if len(vertices) + end_point_length != 3:
                path_type = "B"
            elif IsLinear(points[0], points[1], end_point if end_point is not None else points[2]):
                path_type = "L"
        elif len(vertices) + end_point_length > 3:
            path_type = "B"

    vertices[0][1] = path_type
    segments = []
    start_index = 0
    end_index = 0
    while True:
        end_index += 1
        if end_index >= len(vertices):
            break
        if vertices[end_index][0] != vertices[end_index - 1][0]:
            continue
        if path_type == "C" and end_index > 1 and format_version < FIRST_LAZER_VERSION:
            continue
        if end_index == len(vertices) - 1:
            continue
        vertices[end_index - 1][1] = path_type
        segments.append(vertices[start_index:end_index])
        start_index = end_index + 1
    if start_index < end_index:
        segments.append(vertices[start_index:end_index])
    return segments


def ConvertPathString(point_string, head, format_version):
    points = []
    segment_starts = []
    for token in point_string.split("|"):
        if not token:
            continue
        if token[0].isalpha():
            segment_starts.append((ConvertPathType(token), len(points)))
            if not points:
                points.append((0.0, 0.0))
            continue
        x_text, y_text = token.split(":")[:2]
        x, y = ParseNumber(x_text, MAX_COORDINATE_VALUE), ParseNumber(y_text, MAX_COORDINATE_VALUE)
        if format_version < FIRST_LAZER_VERSION:
            x, y = float(int(x)), float(int(y))
        points.append((x - head[0], y - head[1]))
        if len(points) > MAX_CONTROL_POINTS:
            raise BeatmapError("The beatmap has a slider with too many points to render.")

    control_points = []
    for i, (path_type, start) in enumerate(segment_starts):
        if i < len(segment_starts) - 1:
            end = segment_starts[i + 1][1]
            segments = ConvertPoints(path_type, points[start:end], points[end] if end < len(points) else None, format_version)
        else:
            segments = ConvertPoints(path_type, points[start:], None, format_version)
        for segment in segments:
            control_points.extend((tuple(position), point_type) for position, point_type in segment)
    return control_points


def ParseHitObject(line, offset, format_version):
    split = line.split(",")
    head = (float(int(ParseNumber(split[0], MAX_COORDINATE_VALUE))), float(int(ParseNumber(split[1], MAX_COORDINATE_VALUE))))
    time = ParseNumber(split[2]) + offset
    type_flags = int(split[3])
    new_combo = bool(type_flags & 4)
    combo_skip = (type_flags >> 4) & 7

    if type_flags & 2:
        repeat_count = int(split[6])
        if repeat_count > 9000:
            raise BeatmapError("The beatmap has a slider with an impossible repeat count.")
        expected_distance = None
        if len(split) > 7:
            expected_distance = max(0.0, ParseNumber(split[7], MAX_COORDINATE_VALUE))
            if expected_distance == 0:
                expected_distance = None
        return HitObject(
            kind="slider", time=time, x=head[0], new_combo=new_combo, combo_skip=combo_skip,
            control_points=ConvertPathString(split[5], head, format_version),
            expected_distance=expected_distance, repeat_count=max(0, repeat_count - 1),
        )
    if type_flags & 8:
        end_time = max(time, ParseNumber(split[5]) + offset)
        return HitObject(kind="spinner", time=time, x=256, new_combo=new_combo, combo_skip=combo_skip, end_time=end_time)
    if type_flags & 1:
        return HitObject(kind="circle", time=time, x=head[0], new_combo=new_combo, combo_skip=combo_skip)
    return None


def ParseColour(value):
    parts = [int(part.strip()) for part in value.split(",")[:3]]
    return tuple(parts) if len(parts) == 3 else None


def ParseBeatmap(text):
    lines = text.splitlines()
    if not lines or "osu file format" not in lines[0]:
        raise BeatmapError("The beatmap file is not a valid `.osu` file.")
    try:
        format_version = int(lines[0].strip().lstrip("﻿").split("v")[-1])
    except ValueError:
        format_version = 14
    offset = EARLY_VERSION_TIMING_OFFSET if format_version < 5 else 0

    section = None
    values = {}
    timing_lines = []
    hit_object_lines = []
    colours = {}
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "TimingPoints":
            timing_lines.append(line)
        elif section == "HitObjects":
            hit_object_lines.append(line)
        elif section in ("General", "Metadata", "Difficulty", "Colours") and ":" in line:
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if section == "Colours":
                if key.startswith("Combo"):
                    colour = ParseColour(value)
                    if colour:
                        colours[int(key[5:] or 0)] = colour
            else:
                values[(section, key)] = value

    def Number(section_name, key, default, low, high):
        try:
            value = ParseNumber(values[(section_name, key)])
        except (KeyError, ValueError):
            value = default
        return min(max(value, low), high)

    overall = Number("Difficulty", "OverallDifficulty", 5, 0, 10)
    difficulty = Difficulty(
        hp=Number("Difficulty", "HPDrainRate", 5, 0, 10),
        circle_size=Number("Difficulty", "CircleSize", 5, 0, 10),
        overall=overall,
        approach_rate=Number("Difficulty", "ApproachRate", overall, 0, 10),
        slider_multiplier=Number("Difficulty", "SliderMultiplier", 1.4, 0.4, 3.6),
        slider_tick_rate=Number("Difficulty", "SliderTickRate", 1, 0.5, 8),
    )

    timing_points, difficulty_points = ParseTimingPoints(timing_lines, offset)

    if len(hit_object_lines) > MAX_HIT_OBJECTS:
        raise BeatmapError("The beatmap has too many objects to render.")
    hit_objects = []
    for line in hit_object_lines:
        try:
            hit_object = ParseHitObject(line, offset, format_version)
        except (ValueError, IndexError) as error:
            raise BeatmapError("The beatmap has a hit object I couldn't read.") from error
        if hit_object is not None:
            hit_objects.append(hit_object)
    hit_objects.sort(key=lambda h: h.time)
    if not hit_objects:
        raise BeatmapError("The beatmap has no hit objects.")

    return Beatmap(
        format_version=format_version,
        mode=int(Number("General", "Mode", 0, 0, 3)),
        title=values.get(("Metadata", "Title"), "Unknown title"),
        artist=values.get(("Metadata", "Artist"), "Unknown artist"),
        version=values.get(("Metadata", "Version"), ""),
        creator=values.get(("Metadata", "Creator"), ""),
        difficulty=difficulty,
        timing_points=timing_points,
        difficulty_points=difficulty_points,
        hit_objects=hit_objects,
        combo_colours=[colours[k] for k in sorted(colours)] or list(DEFAULT_COMBO_COLOURS),
    )


def ParseTimingPoints(lines, offset):
    timing_points = []
    raw_difficulty_points = []
    for order, line in enumerate(lines):
        split = line.split(",")
        try:
            time = ParseNumber(split[0]) + offset
            beat_length = ParseNumber(split[1], allow_nan=True)
        except (ValueError, IndexError):
            continue
        timing_change = len(split) < 7 or split[6].strip()[:1] == "1"
        speed_multiplier = 100.0 / -beat_length if beat_length < 0 else 1.0
        if timing_change:
            if math.isnan(beat_length):
                continue
            timing_points.append((time, order, TimingPoint(time, min(max(beat_length, 6), 60000))))
        raw_difficulty_points.append((time, 0 if timing_change else 1, order, DifficultyPoint(
            time, min(max(speed_multiplier, 0.1), 10), not math.isnan(beat_length))))

    timing_points.sort(key=lambda item: (item[0], item[1]))
    raw_difficulty_points.sort(key=lambda item: (item[0], item[1], item[2]))
    difficulty_points = []
    for time, _, _, point in raw_difficulty_points:
        if difficulty_points and difficulty_points[-1].time == time:
            difficulty_points[-1] = point
        else:
            difficulty_points.append(point)
    return [point for _, _, point in timing_points], difficulty_points
