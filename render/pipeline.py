import logging
import time

from render.beatmap import ParseBeatmap
from render.catch import BuildCatchBeatmap, CatcherTrack, FindImpossibleMovement, JudgeReplay
from render.errors import BeatmapError, ReplayError
from render.lookup import FetchBeatmap, Md5
from render.replay import ParseReplay
from render.video import FrameRenderer, RenderVideo

log = logging.getLogger(__name__)


def LoadBeatmapData(replay, cache_dir, api, osu_file=None):
    """Return the .osu contents and its BeatmapInfo, which is None when the API wasn't used."""
    if osu_file is None:
        return FetchBeatmap(replay.beatmap_hash, cache_dir, api)
    with open(osu_file, "rb") as file:
        data = file.read()
    if Md5(data) != replay.beatmap_hash:
        raise BeatmapError("The .osu file given doesn't match the replay's beatmap hash.")
    return data, None


def RenderReplay(replay_path, output_path, cache_dir, api, max_bytes, osu_file=None):
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

    renderer = FrameRenderer(beatmap, catch_beatmap, replay, track, judgements)
    RenderVideo(renderer, output_path, max_bytes)
    log.info("Render finished in %.1fs", time.monotonic() - started)
    return renderer.duration_seconds
