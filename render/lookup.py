import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import requests

from render.errors import BeatmapError

log = logging.getLogger(__name__)

TOKEN_URL = "https://osu.ppy.sh/oauth/token"
LOOKUP_URL = "https://osu.ppy.sh/api/v2/beatmaps/lookup"
OSU_FILE_URL = "https://osu.ppy.sh/osu/{beatmap_id}"
COVER_HOST = "assets.ppy.sh"
REQUEST_TIMEOUT = 20
MAX_OSU_FILE_BYTES = 16 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
HASH_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def Md5(data):
    return hashlib.md5(data).hexdigest()


def SafeCoverUrl(url):
    """Only trust cover images served over HTTPS from osu!'s asset host."""
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.hostname != COVER_HOST or parts.port or parts.username or parts.password:
        return ""
    return url


def PositiveInt(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


@dataclass
class BeatmapInfo:
    beatmap_id: int
    beatmapset_id: int
    cover_url: str = ""

    @staticmethod
    def FromApi(payload):
        beatmapset = payload.get("beatmapset") or {}
        covers = beatmapset.get("covers") or {}
        return BeatmapInfo(
            beatmap_id=PositiveInt(payload.get("id")),
            beatmapset_id=PositiveInt(payload.get("beatmapset_id") or beatmapset.get("id")),
            cover_url=SafeCoverUrl(covers.get("cover@2x")) or SafeCoverUrl(covers.get("cover")),
        )

    @staticmethod
    def FromJson(payload):
        if not isinstance(payload, dict):
            return None
        info = BeatmapInfo(PositiveInt(payload.get("beatmap_id")), PositiveInt(payload.get("beatmapset_id")),
                           SafeCoverUrl(payload.get("cover_url")))
        return info if info.beatmap_id else None


class OsuApi:
    def __init__(self, client_id, client_secret, session=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self.token = None
        self.token_expiry = 0

    def Token(self):
        if self.token and time.time() < self.token_expiry - 60:
            return self.token
        try:
            response = self.session.post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
                "scope": "public",
            }, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as error:
            raise BeatmapError("Couldn't reach the osu! API. Try again in a bit.") from error
        if response.status_code != 200:
            log.error("osu! token request failed: %s %s", response.status_code, response.text[:200])
            raise BeatmapError("The bot couldn't log in to the osu! API. Tell the bot owner.")
        payload = response.json()
        self.token = payload["access_token"]
        self.token_expiry = time.time() + payload.get("expires_in", 3600)
        return self.token

    def LookupBeatmap(self, beatmap_hash):
        try:
            response = self.session.get(LOOKUP_URL, params={"checksum": beatmap_hash},
                                        headers={"Authorization": f"Bearer {self.Token()}"}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as error:
            raise BeatmapError("Couldn't reach the osu! API. Try again in a bit.") from error
        if response.status_code == 404:
            raise BeatmapError("Couldn't find this replay's beatmap on osu!. It may be deleted, unsubmitted, or the replay is for an old version of the map.")
        if response.status_code != 200:
            log.error("osu! lookup failed: %s %s", response.status_code, response.text[:200])
            raise BeatmapError("The osu! API returned an error while looking up the beatmap. Try again in a bit.")
        try:
            info = BeatmapInfo.FromApi(response.json())
        except (ValueError, AttributeError) as error:
            raise BeatmapError("The osu! API returned something unexpected for this beatmap.") from error
        if not info.beatmap_id:
            raise BeatmapError("The osu! API returned something unexpected for this beatmap.")
        return info

    def DownloadOsuFile(self, beatmap_id):
        try:
            with self.session.get(OSU_FILE_URL.format(beatmap_id=beatmap_id), timeout=REQUEST_TIMEOUT, stream=True) as response:
                if response.status_code != 200:
                    raise BeatmapError("The beatmap file isn't available from osu! (it may have been deleted).")
                data = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    data += chunk
                    if len(data) > MAX_OSU_FILE_BYTES:
                        raise BeatmapError("The beatmap file is too large to render.")
        except requests.RequestException as error:
            raise BeatmapError("Couldn't download the beatmap from osu!. Try again in a bit.") from error
        if not data:
            raise BeatmapError("The beatmap file isn't available from osu! (it may have been deleted).")
        return bytes(data)


def CachedBeatmapPath(cache_dir, beatmap_hash):
    return os.path.join(cache_dir, "beatmaps", f"{beatmap_hash}.osu")


def CachedInfoPath(cache_dir, beatmap_hash):
    return os.path.join(cache_dir, "beatmaps", f"{beatmap_hash}.json")


def WriteAtomically(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.tmp"
    with open(temp_path, "wb") as file:
        file.write(data)
    os.replace(temp_path, path)


def ReadCachedInfo(cache_dir, beatmap_hash):
    path = CachedInfoPath(cache_dir, beatmap_hash)
    try:
        if os.path.getsize(path) > MAX_SIDECAR_BYTES:
            return None
        with open(path, "rb") as file:
            return BeatmapInfo.FromJson(json.loads(file.read()))
    except (OSError, ValueError):
        return None


def WriteCachedInfo(cache_dir, beatmap_hash, info):
    WriteAtomically(CachedInfoPath(cache_dir, beatmap_hash), json.dumps(asdict(info)).encode())


def FetchBeatmap(beatmap_hash, cache_dir, api):
    """Return the .osu file contents for a beatmap hash and its BeatmapInfo (None if unknown), using the disk cache when possible."""
    if not HASH_PATTERN.match(beatmap_hash):
        raise BeatmapError("The replay has an invalid beatmap hash.")
    path = CachedBeatmapPath(cache_dir, beatmap_hash)
    if os.path.exists(path):
        with open(path, "rb") as file:
            data = file.read()
        if Md5(data) == beatmap_hash:
            info = ReadCachedInfo(cache_dir, beatmap_hash)
            if info is None and api is not None:
                try:
                    info = api.LookupBeatmap(beatmap_hash)
                    WriteCachedInfo(cache_dir, beatmap_hash, info)
                except BeatmapError as error:
                    log.warning("Couldn't refresh beatmap info for %s: %s", beatmap_hash, error)
            return data, info
        log.warning("Cached beatmap %s failed its hash check, downloading again", beatmap_hash)

    if api is None:
        raise BeatmapError("The bot has no osu! API credentials set up, so it can't fetch beatmaps.")
    info = api.LookupBeatmap(beatmap_hash)
    data = api.DownloadOsuFile(info.beatmap_id)
    if Md5(data) != beatmap_hash:
        raise BeatmapError("This replay was played on a different version of the beatmap than the one on osu! now, so it can't be rendered accurately.")

    WriteAtomically(path, data)
    WriteCachedInfo(cache_dir, beatmap_hash, info)
    return data, info
