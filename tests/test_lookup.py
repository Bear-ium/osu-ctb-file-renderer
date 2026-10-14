import json

import pytest

from render.errors import BeatmapError
from render.lookup import (BeatmapInfo, CachedBeatmapPath, CachedInfoPath, FetchBeatmap, Md5, SafeCoverUrl,
                           WriteAtomically)

OSU_DATA = b"osu file format v14\n\n[HitObjects]\n256,192,1000,1,0\n"
API_PAYLOAD = {
    "id": 123,
    "beatmapset_id": 456,
    "beatmapset": {"id": 456, "covers": {"cover": "https://assets.ppy.sh/beatmaps/456/covers/cover.jpg",
                                         "cover@2x": "https://assets.ppy.sh/beatmaps/456/covers/cover@2x.jpg"}},
}


class FakeApi:
    def __init__(self, data, payload=None):
        self.data = data
        self.payload = payload or API_PAYLOAD
        self.downloads = 0
        self.lookups = 0

    def LookupBeatmap(self, beatmap_hash):
        self.lookups += 1
        return BeatmapInfo.FromApi(self.payload)

    def DownloadOsuFile(self, beatmap_id):
        self.downloads += 1
        return self.data


def test_downloads_then_uses_cache(tmp_path):
    api = FakeApi(OSU_DATA)
    beatmap_hash = Md5(OSU_DATA)
    data, info = FetchBeatmap(beatmap_hash, str(tmp_path), api)
    assert data == OSU_DATA
    assert info == BeatmapInfo(123, 456, "https://assets.ppy.sh/beatmaps/456/covers/cover@2x.jpg")
    assert FetchBeatmap(beatmap_hash, str(tmp_path), api) == (OSU_DATA, info)
    assert (api.downloads, api.lookups) == (1, 1)


def test_info_sidecar_is_written_next_to_osu_file(tmp_path):
    beatmap_hash = Md5(OSU_DATA)
    FetchBeatmap(beatmap_hash, str(tmp_path), FakeApi(OSU_DATA))
    with open(CachedInfoPath(str(tmp_path), beatmap_hash)) as file:
        assert json.load(file)["beatmapset_id"] == 456


def test_missing_sidecar_is_looked_up_again(tmp_path):
    beatmap_hash = Md5(OSU_DATA)
    WriteAtomically(CachedBeatmapPath(str(tmp_path), beatmap_hash), OSU_DATA)
    api = FakeApi(OSU_DATA)
    data, info = FetchBeatmap(beatmap_hash, str(tmp_path), api)
    assert info.beatmapset_id == 456
    assert (api.downloads, api.lookups) == (0, 1)


def test_cached_map_without_credentials_has_no_info(tmp_path):
    beatmap_hash = Md5(OSU_DATA)
    WriteAtomically(CachedBeatmapPath(str(tmp_path), beatmap_hash), OSU_DATA)
    assert FetchBeatmap(beatmap_hash, str(tmp_path), None) == (OSU_DATA, None)


def test_corrupt_sidecar_is_ignored(tmp_path):
    beatmap_hash = Md5(OSU_DATA)
    WriteAtomically(CachedBeatmapPath(str(tmp_path), beatmap_hash), OSU_DATA)
    WriteAtomically(CachedInfoPath(str(tmp_path), beatmap_hash), b"{not json")
    assert FetchBeatmap(beatmap_hash, str(tmp_path), None) == (OSU_DATA, None)


def test_cover_falls_back_to_plain_cover():
    payload = {"id": 1, "beatmapset_id": 2, "beatmapset": {"covers": {"cover": "https://assets.ppy.sh/c.jpg"}}}
    assert BeatmapInfo.FromApi(payload).cover_url == "https://assets.ppy.sh/c.jpg"


@pytest.mark.parametrize("url", [
    "http://assets.ppy.sh/c.jpg",
    "https://evil.example/c.jpg",
    "https://assets.ppy.sh.evil.example/c.jpg",
    "https://assets.ppy.sh@evil.example/c.jpg",
    "https://user@assets.ppy.sh/c.jpg",
    "https://assets.ppy.sh:8443/c.jpg",
    "file:///etc/passwd",
    None,
])
def test_only_osu_asset_covers_are_trusted(url):
    assert SafeCoverUrl(url) == ""


def test_rejects_wrong_version_of_map(tmp_path):
    with pytest.raises(BeatmapError, match="different version"):
        FetchBeatmap("0" * 32, str(tmp_path), FakeApi(OSU_DATA))


def test_redownloads_corrupt_cache(tmp_path):
    beatmap_hash = Md5(OSU_DATA)
    WriteAtomically(CachedBeatmapPath(str(tmp_path), beatmap_hash), b"garbage")
    api = FakeApi(OSU_DATA)
    assert FetchBeatmap(beatmap_hash, str(tmp_path), api)[0] == OSU_DATA
    assert api.downloads == 1


def test_needs_api_when_not_cached(tmp_path):
    with pytest.raises(BeatmapError, match="credentials"):
        FetchBeatmap("0" * 32, str(tmp_path), None)


def test_rejects_malformed_hash(tmp_path):
    with pytest.raises(BeatmapError, match="invalid beatmap hash"):
        FetchBeatmap("../../etc/passwd", str(tmp_path), FakeApi(OSU_DATA))
