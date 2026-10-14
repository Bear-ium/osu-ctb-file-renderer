import lzma
import os
import struct
from dataclasses import dataclass

from osrparse import GameMode, Mod, Replay

from render.errors import ReplayError

MAX_REPLAY_BYTES = 5 * 1024 * 1024
PLAYFIELD_WIDTH = 512


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


def ValidateReplayFile(path, max_bytes=MAX_REPLAY_BYTES):
    if not path.lower().endswith(".osr"):
        raise ReplayError("That isn't an `.osr` file.")
    size = os.path.getsize(path)
    if size == 0:
        raise ReplayError("The replay file is empty.")
    if size > max_bytes:
        raise ReplayError(f"The replay file is too large (limit is {max_bytes // (1024 * 1024)} MB).")


def ExtractMods(mods):
    return ReplayMods(
        easy=Mod.Easy in mods,
        hard_rock=Mod.HardRock in mods,
        double_time=Mod.DoubleTime in mods or Mod.Nightcore in mods,
        half_time=Mod.HalfTime in mods,
        hidden=Mod.Hidden in mods,
        flashlight=Mod.Flashlight in mods,
    )


def AbsoluteFrames(events):
    """Turn osrparse's relative time deltas into absolute times.

    Frames with a negative delta still advance the clock but are dropped, matching how osu! plays replays back.
    """
    frames = []
    current_time = 0
    for event in events:
        current_time += event.time_delta
        if event.time_delta < 0:
            continue
        x = min(max(event.x, 0), PLAYFIELD_WIDTH)
        frames.append(CatchFrame(time=current_time, x=x, dashing=event.dashing))
    return frames


def RejectOtherModes(path):
    with open(path, "rb") as file:
        mode = file.read(1)[0]
    if mode == GameMode.CTB.value:
        return
    mode_names = {GameMode.STD.value: "osu!standard", GameMode.TAIKO.value: "osu!taiko", GameMode.MANIA.value: "osu!mania"}
    if mode not in mode_names:
        raise ReplayError("The replay file is corrupt or not a valid `.osr` file.")
    raise ReplayError(f"Only osu!catch replays are supported (this one is {mode_names[mode]}).")


def ParseReplay(path, max_bytes=MAX_REPLAY_BYTES):
    ValidateReplayFile(path, max_bytes)
    RejectOtherModes(path)
    try:
        replay = Replay.from_path(path)
    except (ValueError, struct.error, lzma.LZMAError, UnicodeDecodeError, IndexError, EOFError) as error:
        raise ReplayError("The replay file is corrupt or not a valid `.osr` file.") from error

    if replay.mode != GameMode.CTB:
        raise ReplayError("Only osu!catch replays are supported.")
    if not replay.beatmap_hash:
        raise ReplayError("The replay doesn't say which beatmap it was played on.")

    frames = AbsoluteFrames(replay.replay_data)
    if len(frames) < 2:
        raise ReplayError("The replay has no movement data.")

    return CatchReplay(
        username=replay.username or "Unknown player",
        beatmap_hash=replay.beatmap_hash.lower(),
        mods=ExtractMods(replay.mods),
        frames=frames,
        count_300=replay.count_300,
        count_100=replay.count_100,
        count_50=replay.count_50,
        count_katu=replay.count_katu,
        count_miss=replay.count_miss,
        max_combo=replay.max_combo,
        score=replay.score,
        is_legacy_format=replay.game_version < 30000000,
    )
