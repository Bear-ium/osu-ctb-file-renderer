import io
import os
import zipfile

import pytest
from PIL import Image

import render.assets as assets
from render.assets import AssetSettings, ExtractSetAssets, FetchSetAssets, SafeMemberName
from render.beatmap import ParseBeatmap
from render.lookup import BeatmapInfo, Md5

OSU_TEXT = """osu file format v14

[General]
AudioFilename: song.mp3

[Metadata]
BeatmapSetID:777

[Events]
0,0,"bg.jpg",0,0

[HitObjects]
256,192,1000,1,0
"""
OSU_BYTES = OSU_TEXT.encode()
OSU_HASH = Md5(OSU_BYTES)
BEATMAP = ParseBeatmap(OSU_TEXT)
AUDIO_BYTES = b"ID3" + b"\0" * 2000


def ImageBytes(size=(1920, 1080), image_format="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 40, 90)).save(buffer, image_format)
    return buffer.getvalue()


def MakeOsz(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return str(path)


def GoodMembers():
    return {"map.osu": OSU_BYTES, "song.mp3": AUDIO_BYTES, "BG.JPG": ImageBytes()}


def test_extracts_song_and_background(tmp_path):
    osz = MakeOsz(tmp_path / "set.osz", GoodMembers())
    staging = tmp_path / "out"
    staging.mkdir()
    background, audio, matched = ExtractSetAssets(osz, BEATMAP, OSU_HASH, str(staging))
    assert matched
    assert os.path.basename(audio) == "audio.mp3" and open(audio, "rb").read() == AUDIO_BYTES
    with Image.open(background) as image:
        assert image.size == (1280, 720)


def test_wrong_version_gives_nothing(tmp_path):
    members = GoodMembers()
    members["map.osu"] = OSU_BYTES + b"\n// edited"
    staging = tmp_path / "out"
    staging.mkdir()
    assert ExtractSetAssets(MakeOsz(tmp_path / "set.osz", members), BEATMAP, OSU_HASH, str(staging)) == (None, None, False)


@pytest.mark.parametrize("name", ["../song.mp3", "sub/song.mp3", "sub\\song.mp3", "C:song.mp3"])
def test_members_outside_the_root_are_ignored(tmp_path, name):
    members = {"map.osu": OSU_BYTES, name: AUDIO_BYTES}
    staging = tmp_path / "out"
    staging.mkdir()
    background, audio, matched = ExtractSetAssets(MakeOsz(tmp_path / "set.osz", members), BEATMAP, OSU_HASH, str(staging))
    assert matched and audio is None and background is None
    assert sorted(os.listdir(staging)) == []
    assert not os.path.exists(tmp_path / "song.mp3")


@pytest.mark.parametrize("name", ["../x", "a/b", "a\\b", "..", "", "c:x"])
def test_unsafe_member_names(name):
    assert not SafeMemberName(name)


def test_missing_members_give_nothing_but_match(tmp_path):
    staging = tmp_path / "out"
    staging.mkdir()
    osz = MakeOsz(tmp_path / "set.osz", {"map.osu": OSU_BYTES})
    assert ExtractSetAssets(osz, BEATMAP, OSU_HASH, str(staging)) == (None, None, True)


def test_oversize_members_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "MAX_AUDIO_BYTES", 100)
    monkeypatch.setattr(assets, "MAX_IMAGE_BYTES", 100)
    staging = tmp_path / "out"
    staging.mkdir()
    background, audio, matched = ExtractSetAssets(MakeOsz(tmp_path / "set.osz", GoodMembers()), BEATMAP, OSU_HASH, str(staging))
    assert matched and audio is None and background is None


def test_image_with_too_many_pixels_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "MAX_IMAGE_PIXELS", 1000)
    staging = tmp_path / "out"
    staging.mkdir()
    background, audio, _ = ExtractSetAssets(MakeOsz(tmp_path / "set.osz", GoodMembers()), BEATMAP, OSU_HASH, str(staging))
    assert background is None and audio is not None


def test_non_image_background_is_rejected(tmp_path):
    members = GoodMembers()
    members["BG.JPG"] = b"<html>not an image</html>"
    staging = tmp_path / "out"
    staging.mkdir()
    background, _, _ = ExtractSetAssets(MakeOsz(tmp_path / "set.osz", members), BEATMAP, OSU_HASH, str(staging))
    assert background is None


def test_disallowed_audio_extension_is_skipped(tmp_path):
    beatmap = ParseBeatmap(OSU_TEXT.replace("song.mp3", "song.exe"))
    members = {"map.osu": OSU_TEXT.replace("song.mp3", "song.exe").encode(), "song.exe": AUDIO_BYTES}
    staging = tmp_path / "out"
    staging.mkdir()
    osz = MakeOsz(tmp_path / "set.osz", members)
    _, audio, matched = ExtractSetAssets(osz, beatmap, Md5(members["map.osu"]), str(staging))
    assert matched and audio is None


class FakeResponse:
    def __init__(self, url, body, status=200):
        self.url = url
        self.body = body
        self.status_code = status
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(url)
        route = self.routes.get(url)
        if route is None:
            raise assets.requests.ConnectionError("mirror unreachable")
        return FakeResponse(url, *route) if isinstance(route, tuple) else FakeResponse(url, route)


SETTINGS = AssetSettings(mirrors=["https://mirror.example/d/{set_id}"])
INFO = BeatmapInfo(1, 777, "https://assets.ppy.sh/beatmaps/777/covers/cover@2x.jpg")


def OszBytes(tmp_path, members):
    return open(MakeOsz(tmp_path / "served.osz", members), "rb").read()


def test_fetches_from_mirror_and_caches(tmp_path):
    session = FakeSession({"https://mirror.example/d/777": OszBytes(tmp_path, GoodMembers())})
    result = FetchSetAssets(BEATMAP, OSU_HASH, INFO, str(tmp_path / "cache"), SETTINGS, session)
    assert result.audio_path and result.background_path and result.notes == []
    again = FetchSetAssets(BEATMAP, OSU_HASH, INFO, str(tmp_path / "cache"), SETTINGS, FakeSession({}))
    assert again.audio_path == result.audio_path
    assert os.listdir(tmp_path / "cache" / "assets") == [OSU_HASH]


def test_set_id_falls_back_to_the_osu_file(tmp_path):
    session = FakeSession({"https://mirror.example/d/777": OszBytes(tmp_path, GoodMembers())})
    result = FetchSetAssets(BEATMAP, OSU_HASH, None, str(tmp_path / "cache"), SETTINGS, session)
    assert result.audio_path is not None


def test_mirror_failure_falls_back_to_cover(tmp_path):
    session = FakeSession({INFO.cover_url: ImageBytes((1800, 500))})
    result = FetchSetAssets(BEATMAP, OSU_HASH, INFO, str(tmp_path / "cache"), SETTINGS, session)
    assert result.audio_path is None and result.background_path is not None
    assert result.notes == ["No music: the beatmap set couldn't be downloaded."]
    assert not os.path.exists(tmp_path / "cache" / "assets" / OSU_HASH)
    result.Cleanup()
    assert not os.path.exists(result.background_path)


def test_everything_failing_never_raises(tmp_path):
    result = FetchSetAssets(BEATMAP, OSU_HASH, INFO, str(tmp_path / "cache"), SETTINGS, FakeSession({}))
    assert (result.audio_path, result.background_path) == (None, None)
    assert "No music or map background" in result.notes[0]


def test_unexpected_errors_never_raise(tmp_path):
    class ExplodingSession:
        def get(self, url, **kwargs):
            raise ZeroDivisionError("boom")
    result = FetchSetAssets(BEATMAP, OSU_HASH, INFO, str(tmp_path / "cache"), SETTINGS, ExplodingSession())
    assert result.audio_path is None and result.notes


def test_wrong_version_on_mirror_is_reported(tmp_path):
    members = GoodMembers()
    members["map.osu"] = b"osu file format v14\n// older version"
    session = FakeSession({"https://mirror.example/d/777": OszBytes(tmp_path, members)})
    result = FetchSetAssets(BEATMAP, OSU_HASH, None, str(tmp_path / "cache"), SETTINGS, session)
    assert result.audio_path is None
    assert "different version" in result.notes[0]


def test_oversize_download_is_abandoned(tmp_path):
    settings = AssetSettings(mirrors=["https://mirror.example/d/{set_id}"], max_set_download_bytes=1000)
    session = FakeSession({"https://mirror.example/d/777": OszBytes(tmp_path, GoodMembers())})
    result = FetchSetAssets(BEATMAP, OSU_HASH, None, str(tmp_path / "cache"), settings, session)
    assert result.audio_path is None


def test_non_https_mirrors_are_skipped(tmp_path):
    settings = AssetSettings(mirrors=["http://mirror.example/d/{set_id}"])
    session = FakeSession({"http://mirror.example/d/777": OszBytes(tmp_path, GoodMembers())})
    FetchSetAssets(BEATMAP, OSU_HASH, None, str(tmp_path / "cache"), settings, session)
    assert session.requests == []


def test_cache_evicts_oldest_entries(tmp_path):
    cache = str(tmp_path / "cache")
    served = OszBytes(tmp_path, GoodMembers())
    settings = AssetSettings(mirrors=["https://mirror.example/d/{set_id}"], cache_max_bytes=1)
    first = FetchSetAssets(BEATMAP, OSU_HASH, None, cache, settings, FakeSession({"https://mirror.example/d/777": served}))
    other_text = OSU_TEXT + "\n"
    other_members = dict(GoodMembers(), **{"map.osu": other_text.encode()})
    other_served = OszBytes(tmp_path, other_members)
    FetchSetAssets(ParseBeatmap(other_text), Md5(other_text.encode()), None, cache, settings,
                   FakeSession({"https://mirror.example/d/777": other_served}))
    assert not os.path.exists(first.audio_path)
    assert os.listdir(os.path.join(cache, "assets")) == [Md5(other_text.encode())]
