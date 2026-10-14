"""Reads .osr files directly, with every size bounded, rather than trusting a general-purpose parser with uploads."""
import lzma
import math
import os
import struct
from dataclasses import dataclass
from typing import NamedTuple

from render.errors import ReplayError

MAX_REPLAY_BYTES = 5 * 1024 * 1024
MAX_REPLAY_DATA_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 200_000
MAX_ULEB128_BYTES = 5
PLAYFIELD_WIDTH = 512
SEED_FRAME_DELTA = -12345

MODE_CATCH = 2
MODE_NAMES = {0: "osu!standard", 1: "osu!taiko", 3: "osu!mania"}

MOD_EASY = 1 << 1
MOD_HIDDEN = 1 << 3
MOD_HARD_ROCK = 1 << 4
MOD_DOUBLE_TIME = 1 << 6
MOD_HALF_TIME = 1 << 8
MOD_NIGHTCORE = 1 << 9
MOD_FLASHLIGHT = 1 << 10

CORRUPT_MESSAGE = "The replay file is corrupt or not a valid `.osr` file."


class RawFrame(NamedTuple):
    time_delta: int
    x: float
    dashing: bool
    visible: bool = True


@dataclass
class CatchFrame:
    time: float
    x: float
    dashing: bool


@dataclass
class ReplayMods:
    easy: bool = False
    hard_rock: bool = False
    double_time: bool = False
    half_time: bool = False
    hidden: bool = False
    flashlight: bool = False

    @property
    def rate(self):
        if self.double_time:
            return 1.5
        if self.half_time:
            return 0.75
        return 1.0

    def Acronyms(self):
        names = [("EZ", self.easy), ("HD", self.hidden), ("HR", self.hard_rock),
                 ("DT", self.double_time), ("HT", self.half_time), ("FL", self.flashlight)]
        return [name for name, enabled in names if enabled]


@dataclass
class CatchReplay:
    username: str
    beatmap_hash: str
    mods: ReplayMods
    frames: list
    count_300: int
    count_100: int
    count_50: int
    count_katu: int
    count_miss: int
    max_combo: int
    score: int
    is_legacy_format: bool


class Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def Unpack(self, specifier):
        try:
            (value,) = struct.unpack_from(specifier, self.data, self.offset)
        except struct.error as error:
            raise ReplayError(CORRUPT_MESSAGE) from error
        self.offset += struct.calcsize(specifier)
        return value

    def Byte(self):
        return self.Unpack("<B")

    def Short(self):
        return self.Unpack("<H")

    def Int(self):
        return self.Unpack("<I")

    def Long(self):
        return self.Unpack("<q")

    def Uleb128(self):
        result = 0
        for index in range(MAX_ULEB128_BYTES):
            byte = self.Byte()
            result |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                return result
        raise ReplayError(CORRUPT_MESSAGE)

    def Bytes(self, length):
        if length > len(self.data) - self.offset:
            raise ReplayError(CORRUPT_MESSAGE)
        chunk = self.data[self.offset:self.offset + length]
        self.offset += length
        return chunk

    def String(self):
        marker = self.Byte()
        if marker == 0x00:
            return ""
        if marker != 0x0B:
            raise ReplayError(CORRUPT_MESSAGE)
        try:
            return self.Bytes(self.Uleb128()).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReplayError(CORRUPT_MESSAGE) from error


def ValidateReplayFile(path, max_bytes=MAX_REPLAY_BYTES):
    if not path.lower().endswith(".osr"):
        raise ReplayError("That isn't an `.osr` file.")
    size = os.path.getsize(path)
    if size == 0:
        raise ReplayError("The replay file is empty.")
    if size > max_bytes:
        raise ReplayError(f"The replay file is too large (limit is {max_bytes // (1024 * 1024)} MB).")


def ExtractMods(mods):
    mods = int(mods)
    return ReplayMods(
        easy=bool(mods & MOD_EASY),
        hard_rock=bool(mods & MOD_HARD_ROCK),
        double_time=bool(mods & (MOD_DOUBLE_TIME | MOD_NIGHTCORE)),
        half_time=bool(mods & MOD_HALF_TIME),
        hidden=bool(mods & MOD_HIDDEN),
        flashlight=bool(mods & MOD_FLASHLIGHT),
    )


def DecompressReplayData(compressed):
    """Decompress the first LZMA stream, refusing to produce more than MAX_REPLAY_DATA_BYTES."""
    decompressor = lzma.LZMADecompressor()
    try:
        data = decompressor.decompress(compressed, max_length=MAX_REPLAY_DATA_BYTES)
    except lzma.LZMAError as error:
        raise ReplayError(CORRUPT_MESSAGE) from error
    if not decompressor.eof:
        if decompressor.needs_input:
            raise ReplayError(CORRUPT_MESSAGE)
        raise ReplayError("The replay contains far more movement data than any real play.")
    return data


def ParseFrameText(text):
    if text.count(",") > MAX_FRAMES + 1:
        raise ReplayError("The replay contains far more movement data than any real play.")
    events = text.rstrip(",").split(",")
    frames = []
    for index, event in enumerate(events):
        parts = event.split("|")
        if len(parts) != 4:
            raise ReplayError(CORRUPT_MESSAGE)
        try:
            time_delta = int(parts[0])
            x, y = float(parts[1]), float(parts[2])
            keys = int(parts[3])
        except ValueError as error:
            raise ReplayError(CORRUPT_MESSAGE) from error
        if not (math.isfinite(x) and math.isfinite(y)) or abs(time_delta) > 2**31:
            raise ReplayError(CORRUPT_MESSAGE)
        if time_delta == SEED_FRAME_DELTA and index == len(events) - 1:
            continue
        frames.append(RawFrame(time_delta, x, keys == 1, visible=not (index < 2 and x == 256 and y == -500)))
    return frames


def AbsoluteFrames(events):
    """Turn relative time deltas into absolute times.

    Stable's two leading placeholder frames and frames with a negative delta still advance the clock but are
    dropped, matching osu!'s LegacyScoreDecoder.
    """
    frames = []
    current_time = 0
    for event in events:
        current_time += event.time_delta
        if event.time_delta < 0 or not getattr(event, "visible", True):
            continue
        x = min(max(event.x, 0), PLAYFIELD_WIDTH)
        frames.append(CatchFrame(time=current_time, x=x, dashing=event.dashing))
    return frames


def ParseReplay(path, max_bytes=MAX_REPLAY_BYTES):
    ValidateReplayFile(path, max_bytes)
    with open(path, "rb") as file:
        reader = Reader(file.read())

    mode = reader.Byte()
    if mode != MODE_CATCH:
        if mode in MODE_NAMES:
            raise ReplayError(f"Only osu!catch replays are supported (this one is {MODE_NAMES[mode]}).")
        raise ReplayError(CORRUPT_MESSAGE)

    game_version = reader.Int()
    beatmap_hash = reader.String()
    username = reader.String()
    reader.String()
    count_300, count_100, count_50, _count_geki, count_katu, count_miss = (reader.Short() for _ in range(6))
    score = reader.Int()
    max_combo = reader.Short()
    reader.Byte()
    mods = reader.Int()
    reader.String()
    reader.Long()
    compressed = reader.Bytes(reader.Int())

    if not beatmap_hash:
        raise ReplayError("The replay doesn't say which beatmap it was played on.")
    try:
        text = DecompressReplayData(compressed).decode("ascii")
    except UnicodeDecodeError as error:
        raise ReplayError(CORRUPT_MESSAGE) from error

    frames = AbsoluteFrames(ParseFrameText(text))
    if len(frames) < 2:
        raise ReplayError("The replay has no movement data.")

    return CatchReplay(
        username=username or "Unknown player",
        beatmap_hash=beatmap_hash.lower(),
        mods=ExtractMods(mods),
        frames=frames,
        count_300=count_300,
        count_100=count_100,
        count_50=count_50,
        count_katu=count_katu,
        count_miss=count_miss,
        max_combo=max_combo,
        score=score,
        is_legacy_format=game_version < 30000000,
    )
