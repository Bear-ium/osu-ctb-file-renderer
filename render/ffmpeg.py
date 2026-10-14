import os
import shutil
import subprocess


class FfmpegMissingError(RuntimeError):
    pass


def FfmpegPath():
    return os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")


def CheckFfmpeg():
    """Return ffmpeg's version line, or raise if it can't be run."""
    path = FfmpegPath()
    if not path:
        raise FfmpegMissingError("ffmpeg was not found on PATH. Install it (or set FFMPEG_PATH) before rendering.")
    try:
        result = subprocess.run([path, "-hide_banner", "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FfmpegMissingError(f"ffmpeg at {path} could not be run: {error}") from error
    if result.returncode != 0:
        raise FfmpegMissingError(f"ffmpeg at {path} exited with code {result.returncode}.")
    return result.stdout.splitlines()[0] if result.stdout else path
