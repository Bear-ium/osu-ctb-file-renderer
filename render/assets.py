"""Fetches a map's song and background from beatmap mirrors. Everything downloaded is treated as untrusted."""
import ipaddress
import json
import logging
import os
import shutil
import socket
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from PIL import Image, ImageOps

from render.lookup import HASH_PATTERN, Md5, SafeCoverUrl

log = logging.getLogger(__name__)

USER_AGENT = "osu-ctb-file-renderer/1.0 (+https://github.com/Bear-ium/osu-ctb-file-renderer)"
DEFAULT_MIRRORS = [
    "https://osu.direct/api/d/{set_id}?noVideo=1",
    "https://api.nerinyan.moe/d/{set_id}?nv=1",
]
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
OVERALL_TIMEOUT = 90
CHUNK_SIZE = 64 * 1024

MAX_ZIP_MEMBERS = 5000
MAX_OSU_MEMBER_BYTES = 16 * 1024 * 1024
MAX_OSU_CHECKS = 100
MAX_OSU_TOTAL_READ = 64 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_AUDIO_BYTES = 40 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
AUDIO_EXTENSIONS = {"mp3", "ogg", "wav"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png"}
IMAGE_FORMATS = {"JPEG", "PNG"}
BACKGROUND_SIZE = (1280, 720)
BACKGROUND_NAME = "background.png"
META_NAME = "meta.json"
STAGING_PREFIX = ".staging-"
STALE_STAGING_SECONDS = 3600


class AssetError(Exception):
    pass


@dataclass
class AssetSettings:
    mirrors: list = field(default_factory=lambda: list(DEFAULT_MIRRORS))
    max_set_download_bytes: int = 80 * 1024 * 1024
    cache_max_bytes: int = 500 * 1024 * 1024
    overall_timeout: float = OVERALL_TIMEOUT
    user_agent: str = USER_AGENT


@dataclass
class SetAssets:
    background_path: str | None = None
    audio_path: str | None = None
    notes: list = field(default_factory=list)
    cleanup_dir: str | None = None

    def Cleanup(self):
        if self.cleanup_dir:
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)


def Extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def SafeMemberName(name):
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name and ":" not in name


def FindMember(infos, wanted):
    wanted = wanted.lower()
    for info in infos:
        if not info.is_dir() and SafeMemberName(info.filename) and info.filename.lower() == wanted:
            return info
    return None


def CopyMember(archive, info, destination, limit):
    """Copy one archive member to destination, refusing anything larger than limit."""
    if info.file_size > limit:
        raise AssetError(f"{info.filename} is too large ({info.file_size} bytes)")
    written = 0
    with archive.open(info) as source, open(destination, "wb") as target:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise AssetError(f"{info.filename} is larger than it claims")
            target.write(chunk)


def ReadMember(archive, info, limit):
    if info.file_size > limit:
        raise AssetError(f"{info.filename} is too large")
    with archive.open(info) as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise AssetError(f"{info.filename} is larger than it claims")
    return data


def ArchiveHasVersion(archive, infos, beatmap_hash, osu_size=None):
    """Look for the replay's exact .osu. When its size is known, only members of that exact size are read."""
    checked = 0
    budget = MAX_OSU_TOTAL_READ
    for info in infos:
        if info.is_dir() or not info.filename.lower().endswith(".osu") or not SafeMemberName(info.filename):
            continue
        if info.file_size > MAX_OSU_MEMBER_BYTES or (osu_size is not None and info.file_size != osu_size):
            continue
        checked += 1
        budget -= info.file_size
        if checked > MAX_OSU_CHECKS or budget < 0:
            break
        if Md5(ReadMember(archive, info, MAX_OSU_MEMBER_BYTES)) == beatmap_hash:
            return True
    return False


def PrepareBackground(source_path, destination):
    """Validate an image without trusting its contents, then store it cropped to the video size."""
    with Image.open(source_path) as image:
        if image.format not in IMAGE_FORMATS:
            raise AssetError(f"background is {image.format}, not JPEG or PNG")
        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
            raise AssetError(f"background is {width}x{height}, too large to decode safely")
        image.draft("RGB", BACKGROUND_SIZE)
        fitted = ImageOps.fit(image.convert("RGB"), BACKGROUND_SIZE, Image.LANCZOS)
    fitted.save(destination, "PNG")


def ExtractSetAssets(osz_path, beatmap, beatmap_hash, staging_dir, osu_size=None):
    """Pull the song and background out of a downloaded .osz. Returns (background, audio, matched_version)."""
    with zipfile.ZipFile(osz_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise AssetError("archive has too many files")
        if not ArchiveHasVersion(archive, infos, beatmap_hash, osu_size):
            return None, None, False

        audio_path = None
        audio_extension = Extension(beatmap.audio_filename)
        member = FindMember(infos, beatmap.audio_filename) if beatmap.audio_filename else None
        if member is not None and audio_extension in AUDIO_EXTENSIONS:
            try:
                audio_path = os.path.join(staging_dir, f"audio.{audio_extension}")
                CopyMember(archive, member, audio_path, MAX_AUDIO_BYTES)
            except AssetError as error:
                log.warning("Skipping audio: %s", error)
                audio_path = None

        background_path = None
        member = FindMember(infos, beatmap.background_filename) if beatmap.background_filename else None
        if member is not None and Extension(beatmap.background_filename) in IMAGE_EXTENSIONS:
            raw_path = os.path.join(staging_dir, "background.raw")
            try:
                CopyMember(archive, member, raw_path, MAX_IMAGE_BYTES)
                background_path = os.path.join(staging_dir, BACKGROUND_NAME)
                PrepareBackground(raw_path, background_path)
            except (AssetError, OSError, Image.DecompressionBombError) as error:
                log.warning("Skipping background: %s", error)
                background_path = None
            finally:
                if os.path.exists(raw_path):
                    os.remove(raw_path)
    return background_path, audio_path, True


def ResolveHost(hostname):
    return {info[4][0] for info in socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)}


def CheckPublicHttpsUrl(url):
    """Refuse anything but HTTPS on the default port to a host that only resolves to public addresses.

    Returns the address to connect to, so the connection can't be re-resolved somewhere else afterwards.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443):
        raise AssetError(f"refusing to fetch {url!r}")
    try:
        addresses = ResolveHost(parts.hostname)
    except OSError as error:
        raise AssetError(f"couldn't resolve {parts.hostname}: {error}") from error
    if not addresses:
        raise AssetError(f"{parts.hostname} has no addresses")
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise AssetError(f"{parts.hostname} resolves to a non-public address")
    return sorted(addresses)[0]


class PinnedAdapter(HTTPAdapter):
    """Connects to an address chosen in advance while still checking TLS (SNI and certificate) against the real hostname."""

    def __init__(self, hostname):
        self.hostname = hostname
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["server_hostname"] = self.hostname
        pool_kwargs["assert_hostname"] = self.hostname
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


class PinnedHttp:
    """Makes each request to the exact address that passed CheckPublicHttpsUrl, closing the DNS rebinding gap."""

    def Get(self, url, address, headers, timeout):
        parts = urlsplit(url)
        host = f"[{address}]" if ":" in address else address
        pinned_url = urlunsplit(("https", host, parts.path or "/", parts.query, ""))
        session = requests.Session()
        session.trust_env = False
        session.mount("https://", PinnedAdapter(parts.hostname))
        return session.get(pinned_url, headers={**headers, "Host": parts.hostname}, stream=True, timeout=timeout,
                           allow_redirects=False)


def OpenUrl(session, url, user_agent):
    """GET with redirects followed by hand, so every hop is checked before it is requested."""
    headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    for _ in range(MAX_REDIRECTS + 1):
        address = CheckPublicHttpsUrl(url)
        response = session.Get(url, address, headers, (CONNECT_TIMEOUT, READ_TIMEOUT))
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise AssetError("redirect without a location")
            url = urljoin(url, location)
            continue
        return response
    raise AssetError("too many redirects")


def Download(session, url, destination, limit, deadline, user_agent):
    with OpenUrl(session, url, user_agent) as response:
        if response.status_code != 200:
            raise AssetError(f"HTTP {response.status_code}")
        if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
            raise AssetError("server sent a compressed response")
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > limit:
            raise AssetError(f"file is {declared} bytes, over the limit")
        written = 0
        with open(destination, "wb") as target:
            for chunk in response.iter_content(CHUNK_SIZE):
                written += len(chunk)
                if written > limit:
                    raise AssetError("file is over the size limit")
                if time.monotonic() > deadline:
                    raise AssetError("download took too long")
                target.write(chunk)


def MirrorUrls(settings, set_id):
    for template in settings.mirrors:
        if template.startswith("https://") and "{set_id}" in template:
            yield template.replace("{set_id}", str(int(set_id)))


def TryMirrors(session, beatmap, beatmap_hash, set_id, settings, staging_dir, deadline, osu_size=None):
    """Returns (background, audio, outcome) where outcome is "ok", "wrong_version" or "unavailable"."""
    work_dir = tempfile.mkdtemp(prefix="ctb-osz-")
    try:
        osz_path = os.path.join(work_dir, "set.osz")
        for url in MirrorUrls(settings, set_id):
            if time.monotonic() > deadline:
                break
            started = time.monotonic()
            try:
                Download(session, url, osz_path, settings.max_set_download_bytes, deadline, settings.user_agent)
                background, audio, matched = ExtractSetAssets(osz_path, beatmap, beatmap_hash, staging_dir, osu_size)
            except Exception as error:
                log.warning("Mirror %s failed after %.1fs: %s", url, time.monotonic() - started, error)
                continue
            finally:
                if os.path.exists(osz_path):
                    os.remove(osz_path)
            log.info("Fetched set %s from %s in %.1fs", set_id, url, time.monotonic() - started)
            return (background, audio, "ok") if matched else (None, None, "wrong_version")
        return None, None, "unavailable"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def TryCover(session, cover_url, settings, staging_dir, deadline):
    cover_url = SafeCoverUrl(cover_url)
    if not cover_url or time.monotonic() > deadline:
        return None
    raw_path = os.path.join(staging_dir, "cover.raw")
    try:
        Download(session, cover_url, raw_path, MAX_IMAGE_BYTES, deadline, settings.user_agent)
        background_path = os.path.join(staging_dir, BACKGROUND_NAME)
        PrepareBackground(raw_path, background_path)
        return background_path
    except (AssetError, requests.RequestException, OSError, Image.DecompressionBombError) as error:
        log.warning("Cover image failed: %s", error)
        return None
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)


def AssetsRoot(cache_dir):
    return os.path.join(cache_dir, "assets")


def ReadCachedAssets(entry_dir):
    meta_path = os.path.join(entry_dir, META_NAME)
    try:
        with open(meta_path, "rb") as file:
            meta = json.loads(file.read(64 * 1024))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    background = meta.get("background")
    audio = meta.get("audio")
    if background not in (None, BACKGROUND_NAME):
        return None
    if audio is not None and audio not in {f"audio.{extension}" for extension in AUDIO_EXTENSIONS}:
        return None
    notes = [note for note in meta.get("notes", []) if isinstance(note, str)][:4]
    paths = [os.path.join(entry_dir, name) if name else None for name in (background, audio)]
    if any(path is not None and not os.path.isfile(path) for path in paths):
        return None
    os.utime(meta_path)
    return SetAssets(paths[0], paths[1], notes)


def DirectorySize(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def EvictOldEntries(root, max_bytes, keep):
    entries = []
    now = time.time()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if name.startswith(STAGING_PREFIX):
            if now - os.path.getmtime(path) > STALE_STAGING_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
            continue
        meta_path = os.path.join(path, META_NAME)
        last_used = os.path.getmtime(meta_path) if os.path.exists(meta_path) else 0
        entries.append((last_used, path, DirectorySize(path)))
    total = sum(size for _, _, size in entries)
    for _, path, size in sorted(entries):
        if total <= max_bytes:
            break
        if os.path.abspath(path) == os.path.abspath(keep):
            continue
        shutil.rmtree(path, ignore_errors=True)
        total -= size
        log.info("Evicted cached assets %s", os.path.basename(path))


def CommitToCache(staging_dir, entry_dir, background, audio, notes):
    meta = {
        "background": os.path.basename(background) if background else None,
        "audio": os.path.basename(audio) if audio else None,
        "notes": notes,
        "created": time.time(),
    }
    with open(os.path.join(staging_dir, META_NAME), "w", encoding="utf-8") as file:
        json.dump(meta, file)
    try:
        os.rename(staging_dir, entry_dir)
    except OSError:
        shutil.rmtree(staging_dir, ignore_errors=True)
    return ReadCachedAssets(entry_dir)


def NotesFor(background, audio, outcome):
    if background and audio:
        return []
    if outcome == "wrong_version":
        reason = "the mirrors only have a different version of this map"
    elif outcome == "no_set":
        reason = "the beatmap set isn't known"
    else:
        reason = "the beatmap set couldn't be downloaded"
    if not audio and not background:
        return [f"No music or map background: {reason}."]
    if not audio:
        return [f"No music: {reason}."]
    return [f"No map background: {reason}."]


def FetchSetAssets(beatmap, replay_hash, info, cache_dir, settings, session=None, osu_size=None):
    """Return the song and background for a replay's map. Never raises: failures become notes.

    osu_size is the byte length of the replay's verified .osu, used to skip reading other difficulties.
    """
    try:
        return FetchSetAssetsUnsafe(beatmap, replay_hash, info, cache_dir, settings, session or PinnedHttp(), osu_size)
    except Exception:
        log.exception("Fetching the song and background failed")
        return SetAssets(notes=["No music or map background: something went wrong fetching them."])


def FetchSetAssetsUnsafe(beatmap, replay_hash, info, cache_dir, settings, session, osu_size):
    if not HASH_PATTERN.match(replay_hash):
        return SetAssets(notes=NotesFor(None, None, "no_set"))
    root = AssetsRoot(cache_dir)
    entry_dir = os.path.join(root, replay_hash)
    cached = ReadCachedAssets(entry_dir)
    if cached is not None:
        log.info("Using cached song and background for %s", replay_hash)
        return cached

    os.makedirs(root, exist_ok=True)
    if os.path.isdir(entry_dir):
        shutil.rmtree(entry_dir, ignore_errors=True)
    staging_dir = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=root)
    deadline = time.monotonic() + settings.overall_timeout
    started = time.monotonic()
    try:
        set_id = (info.beatmapset_id if info else 0) or beatmap.beatmapset_id
        if set_id:
            background, audio, outcome = TryMirrors(session, beatmap, replay_hash, set_id, settings, staging_dir, deadline, osu_size)
        else:
            background, audio, outcome = None, None, "no_set"
        if background is None and info is not None:
            background = TryCover(session, info.cover_url, settings, staging_dir, deadline)
        notes = NotesFor(background, audio, outcome)
        log.info("Song and background lookup took %.1fs (outcome %s)", time.monotonic() - started, outcome)

        if outcome in ("ok", "wrong_version"):
            result = CommitToCache(staging_dir, entry_dir, background, audio, notes)
            EvictOldEntries(root, settings.cache_max_bytes, entry_dir)
            if result is not None:
                return result

        uncached_dir = tempfile.mkdtemp(prefix="ctb-assets-")
        moved = [shutil.move(path, os.path.join(uncached_dir, os.path.basename(path))) if path and os.path.exists(path) else None
                 for path in (background, audio)]
        return SetAssets(moved[0], moved[1], notes, cleanup_dir=uncached_dir)
    finally:
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
