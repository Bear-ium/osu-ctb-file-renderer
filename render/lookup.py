import hashlib
import logging
import os
import re
import time

import requests

from render.errors import BeatmapError

log = logging.getLogger(__name__)

TOKEN_URL = "https://osu.ppy.sh/oauth/token"
LOOKUP_URL = "https://osu.ppy.sh/api/v2/beatmaps/lookup"
OSU_FILE_URL = "https://osu.ppy.sh/osu/{beatmap_id}"
REQUEST_TIMEOUT = 20
HASH_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def Md5(data):
    return hashlib.md5(data).hexdigest()


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

    def LookupBeatmapId(self, beatmap_hash):
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
        return response.json()["id"]

    def DownloadOsuFile(self, beatmap_id):
        try:
            response = self.session.get(OSU_FILE_URL.format(beatmap_id=beatmap_id), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as error:
            raise BeatmapError("Couldn't download the beatmap from osu!. Try again in a bit.") from error
        if response.status_code != 200 or not response.content:
            raise BeatmapError("The beatmap file isn't available from osu! (it may have been deleted).")
        return response.content


def CachedBeatmapPath(cache_dir, beatmap_hash):
    return os.path.join(cache_dir, "beatmaps", f"{beatmap_hash}.osu")


def FetchBeatmap(beatmap_hash, cache_dir, api):
    """Return the .osu file contents for a beatmap hash, using the disk cache when possible."""
    if not HASH_PATTERN.match(beatmap_hash):
        raise BeatmapError("The replay has an invalid beatmap hash.")
    path = CachedBeatmapPath(cache_dir, beatmap_hash)
    if os.path.exists(path):
        with open(path, "rb") as file:
            data = file.read()
        if Md5(data) == beatmap_hash:
            return data
        log.warning("Cached beatmap %s failed its hash check, downloading again", beatmap_hash)

    if api is None:
        raise BeatmapError("The bot has no osu! API credentials set up, so it can't fetch beatmaps.")
    beatmap_id = api.LookupBeatmapId(beatmap_hash)
    data = api.DownloadOsuFile(beatmap_id)
    if Md5(data) != beatmap_hash:
        raise BeatmapError("This replay was played on a different version of the beatmap than the one on osu! now, so it can't be rendered accurately.")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.tmp"
    with open(temp_path, "wb") as file:
        file.write(data)
    os.replace(temp_path, path)
    return data
