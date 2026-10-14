import logging
import time
from dataclasses import dataclass, field

from render.assets import AssetSettings, FetchSetAssets, SetAssets
from render.beatmap import ParseBeatmap
from render.catch import BuildCatchBeatmap, CatcherTrack, FindImpossibleMovement, JudgeReplay
from render.errors import BeatmapError, ReplayError
from render.lookup import FetchBeatmap, Md5
from render.replay import ParseReplay
from render.video import FrameRenderer, RenderVideo

log = logging.getLogger(__name__)


@dataclass
class RenderOptions:
    asset_settings: AssetSettings | None = None
    dim_play: float = 0.7
    dim_rest: float = 0.4
    skin_dir: str = ""


@dataclass
class RenderResult:
    duration_seconds: float
    notes: list = field(default_factory=list)


def LoadBeatmapData(replay, cache_dir, api, osu_file=None):
    """Return the .osu contents and its BeatmapInfo, which is None when the API wasn't used."""
    if osu_file is None:
        return FetchBeatmap(replay.beatmap_hash, cache_dir, api)
    with open(osu_file, "rb") as file:
        data = file.read()
    if Md5(data) != replay.beatmap_hash:
        raise BeatmapError("The .osu file given doesn't match the replay's beatmap hash.")
    return data, None


def RenderReplay(replay_path, output_path, cache_dir, api, max_bytes, osu_file=None, options=None):
    """Render a replay to output_path. Song and background are only fetched when options.asset_settings is set."""
    options = options or RenderOptions()
    started = time.monotonic()
    replay = ParseReplay(replay_path)
    data, info = LoadBeatmapData(replay, cache_dir, api, osu_file)
    beatmap = ParseBeatmap(data.decode("utf-8-sig", errors="replace"))

    catch_beatmap = BuildCatchBeatmap(beatmap, replay.mods)
    impossible_at = FindImpossibleMovement(catch_beatmap, replay.frames)
    if impossible_at is not None:
        log.warning("Rejected replay by %s: impossible movement at %.0f ms", replay.username, impossible_at)
        raise ReplayError("This replay moves the catcher faster than osu!catch allows, so it looks edited. It can't be rendered.")
    track = CatcherTrack(replay.frames)
    judgements = JudgeReplay(catch_beatmap, track)
    log.info("%s - %s [%s] played by %s: %d objects, %d misses, max combo %d (replay says %d misses, %dx)",
             beatmap.artist, beatmap.title, beatmap.version, replay.username, len(catch_beatmap.objects),
             judgements.misses, judgements.max_combo, replay.count_miss, replay.max_combo)

    if options.asset_settings is not None:
        assets = FetchSetAssets(beatmap, replay.beatmap_hash, info, cache_dir, options.asset_settings, osu_size=len(data))
    else:
        assets = SetAssets()
    try:
        renderer = FrameRenderer(beatmap, catch_beatmap, replay, track, judgements, background_path=assets.background_path,
                                 dim_play=options.dim_play, dim_rest=options.dim_rest)
        notes = assets.notes + RenderVideo(renderer, output_path, max_bytes, assets.audio_path)
    finally:
        assets.Cleanup()
    log.info("Render finished in %.1fs", time.monotonic() - started)
    return RenderResult(renderer.duration_seconds, notes)
