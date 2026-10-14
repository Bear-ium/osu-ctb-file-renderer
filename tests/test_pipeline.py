import json
import math
import os
import shutil
import struct
import subprocess
import wave

import pytest
from osrparse import GameMode, ReplayEventCatch
from PIL import Image

import render.assets as assets
from render.assets import AssetSettings
from render.lookup import Md5
from render.pipeline import RenderOptions, RenderReplay
from tests.conftest import MakeReplay

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

MAP_TEXT = """osu file format v14

[General]
AudioFilename: song.wav

[Metadata]
Title:Pipeline Test
Artist:Tester
Version:Normal
BeatmapSetID:777

[Difficulty]
CircleSize:4
ApproachRate:9

[Events]
0,0,"bg.png",0,0

[TimingPoints]
0,500,4,2,0,50,1,0

[HitObjects]
256,192,800,1,0
256,192,1300,1,0
"""


def Streams(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                            capture_output=True, text=True, check=True)
    return result.stdout.split()


def MakeSong(path, seconds=4):
    with wave.open(str(path), "wb") as song:
        song.setnchannels(1)
        song.setsampwidth(2)
        song.setframerate(44100)
        song.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 20))) for i in range(44100 * seconds)))


@pytest.fixture
def scene(tmp_path):
    osu_path = tmp_path / "map.osu"
    osu_path.write_bytes(MAP_TEXT.encode())
    beatmap_hash = Md5(MAP_TEXT.encode())
    events = [ReplayEventCatch(16, 256.0, False) for _ in range(120)]
    replay_path = MakeReplay(tmp_path / "replay.osr", mode=GameMode.CTB, events=events)
    data = bytearray(replay_path.read_bytes())
    old_hash = b"0123456789abcdef0123456789abcdef"
    data[data.index(old_hash):data.index(old_hash) + 32] = beatmap_hash.encode()
    replay_path.write_bytes(bytes(data))
    return tmp_path, str(replay_path), str(osu_path), beatmap_hash


def test_render_completes_when_mirrors_are_unreachable(scene, monkeypatch):
    tmp_path, replay_path, osu_path, _ = scene

    def Unreachable(host):
        raise OSError("no network in tests")

    monkeypatch.setattr(assets, "ResolveHost", Unreachable)
    output = str(tmp_path / "out.mp4")
    options = RenderOptions(asset_settings=AssetSettings(mirrors=["https://mirror.example/d/{set_id}"]))
    result = RenderReplay(replay_path, output, str(tmp_path / "cache"), None, 10 * 1024 * 1024, osu_file=osu_path, options=options)
    assert os.path.getsize(output) > 0
    assert Streams(output) == ["video"]
    assert result.notes == ["No music or map background: the beatmap set couldn't be downloaded."]


def test_render_includes_cached_song_and_background(scene):
    tmp_path, replay_path, osu_path, beatmap_hash = scene
    entry = tmp_path / "cache" / "assets" / beatmap_hash
    entry.mkdir(parents=True)
    MakeSong(entry / "audio.wav")
    Image.new("RGB", (1280, 720), (200, 60, 120)).save(entry / "background.png")
    (entry / "meta.json").write_text(json.dumps({"background": "background.png", "audio": "audio.wav", "notes": []}))

    output = str(tmp_path / "out.mp4")
    options = RenderOptions(asset_settings=AssetSettings(mirrors=[]))
    result = RenderReplay(replay_path, output, str(tmp_path / "cache"), None, 10 * 1024 * 1024, osu_file=osu_path, options=options)
    assert sorted(Streams(output)) == ["audio", "video"]
    assert result.notes == []


def test_unreadable_song_still_renders_silently(scene):
    tmp_path, replay_path, osu_path, beatmap_hash = scene
    entry = tmp_path / "cache" / "assets" / beatmap_hash
    entry.mkdir(parents=True)
    (entry / "audio.wav").write_bytes(b"RIFF this is not really audio")
    (entry / "meta.json").write_text(json.dumps({"background": None, "audio": "audio.wav", "notes": []}))

    output = str(tmp_path / "out.mp4")
    options = RenderOptions(asset_settings=AssetSettings(mirrors=[]))
    result = RenderReplay(replay_path, output, str(tmp_path / "cache"), None, 10 * 1024 * 1024, osu_file=osu_path, options=options)
    assert Streams(output) == ["video"]
    assert result.notes == ["No music: the song file couldn't be read."]
