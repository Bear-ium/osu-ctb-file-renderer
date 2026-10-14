"""Render an osu!catch replay to MP4: python render.py replay.osr out.mp4"""
import argparse
import logging
import sys

from bot.config import LoadConfig
from render.errors import RenderError
from render.ffmpeg import CheckFfmpeg, FfmpegMissingError
from render.lookup import OsuApi
from render.pipeline import RenderOptions, RenderReplay

EXIT_USER_ERROR = 2
USER_ERROR_PREFIX = "USER_ERROR: "
NOTE_PREFIX = "NOTE: "


def PeakMemoryMb():
    try:
        import resource
    except ImportError:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def Main():
    parser = argparse.ArgumentParser(description="Render an osu!catch replay to an MP4.")
    parser.add_argument("replay", help="path to the .osr file")
    parser.add_argument("output", help="path for the .mp4 output")
    parser.add_argument("--beatmap", help="use this local .osu file instead of downloading it")
    parser.add_argument("--max-mb", type=float, default=None, help="target maximum file size in MB")
    parser.add_argument("--no-assets", action="store_true", help="don't download the song or background")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = LoadConfig(require_discord=False)
    max_bytes = int(args.max_mb * 1024 * 1024) if args.max_mb else config.max_upload_bytes

    try:
        CheckFfmpeg()
        api = OsuApi(config.osu_client_id, config.osu_client_secret) if config.osu_client_id and config.osu_client_secret else None
        options = RenderOptions(
            asset_settings=None if args.no_assets else config.AssetSettings(),
            dim_play=config.background_dim_play,
            dim_rest=config.background_dim_rest,
            skin_dir=config.skin_dir,
        )
        result = RenderReplay(args.replay, args.output, config.cache_dir, api, max_bytes, osu_file=args.beatmap, options=options)
    except RenderError as error:
        print(USER_ERROR_PREFIX + str(error), file=sys.stderr)
        return EXIT_USER_ERROR
    except FfmpegMissingError as error:
        logging.error("%s", error)
        return 1

    for note in result.notes:
        print(NOTE_PREFIX + note, file=sys.stderr)
    peak = PeakMemoryMb()
    if peak is not None:
        logging.info("Peak memory: %.0f MB", peak)
    return 0


if __name__ == "__main__":
    sys.exit(Main())
