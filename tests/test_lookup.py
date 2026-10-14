import pytest

from render.errors import BeatmapError
from render.lookup import CachedBeatmapPath, FetchBeatmap, Md5

OSU_DATA = b"osu file format v14\n\n[HitObjects]\n256,192,1000,1,0\n"


class FakeApi:
    def __init__(self, data):
        self.data = data
        self.downloads = 0

    def LookupBeatmapId(self, beatmap_hash):
        return 123

    def DownloadOsuFile(self, beatmap_id):
        self.downloads += 1
        return self.data


def test_downloads_then_uses_cache(tmp_path):
    api = FakeApi(OSU_DATA)
    beatmap_hash = Md5(OSU_DATA)
    assert FetchBeatmap(beatmap_hash, str(tmp_path), api) == OSU_DATA
    assert FetchBeatmap(beatmap_hash, str(tmp_path), api) == OSU_DATA
    assert api.downloads == 1


def test_rejects_wrong_version_of_map(tmp_path):
    with pytest.raises(BeatmapError, match="different version"):
        FetchBeatmap("0" * 32, str(tmp_path), FakeApi(OSU_DATA))


def test_redownloads_corrupt_cache(tmp_path):
    beatmap_hash = Md5(OSU_DATA)
    path = CachedBeatmapPath(str(tmp_path), beatmap_hash)
    (tmp_path / "beatmaps").mkdir()
    with open(path, "wb") as file:
        file.write(b"garbage")
    api = FakeApi(OSU_DATA)
    assert FetchBeatmap(beatmap_hash, str(tmp_path), api) == OSU_DATA
    assert api.downloads == 1


def test_needs_api_when_not_cached(tmp_path):
    with pytest.raises(BeatmapError, match="credentials"):
        FetchBeatmap("0" * 32, str(tmp_path), None)


def test_rejects_malformed_hash(tmp_path):
    with pytest.raises(BeatmapError, match="invalid beatmap hash"):
        FetchBeatmap("../../etc/passwd", str(tmp_path), FakeApi(OSU_DATA))
