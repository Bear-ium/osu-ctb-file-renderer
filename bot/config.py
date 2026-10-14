import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from render.assets import DEFAULT_MIRRORS, AssetSettings


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    discord_token: str
    osu_client_id: str
    osu_client_secret: str
    cache_dir: str
    max_upload_bytes: int
    max_concurrent_renders: int
    render_timeout_seconds: int
    user_cooldown_seconds: int
    max_queue_length: int
    dev_guild_id: int | None
    beatmap_mirrors: list = field(default_factory=lambda: list(DEFAULT_MIRRORS))
    cache_max_bytes: int = 500 * 1024 * 1024
    max_set_download_bytes: int = 80 * 1024 * 1024
    background_dim_play: float = 0.7
    background_dim_rest: float = 0.4
    skin_dir: str = ""

    def AssetSettings(self):
        return AssetSettings(mirrors=list(self.beatmap_mirrors), max_set_download_bytes=self.max_set_download_bytes,
                             cache_max_bytes=self.cache_max_bytes)


def Number(name, default, kind=int):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return kind(value)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number, got {value!r}") from error


def Fraction(name, default):
    value = Number(name, default, float)
    if not 0 <= value <= 1:
        raise ConfigError(f"{name} must be between 0 and 1, got {value}")
    return value


def Mirrors():
    value = os.environ.get("BEATMAP_MIRRORS", "").strip()
    if not value:
        return list(DEFAULT_MIRRORS)
    mirrors = [item.strip() for item in value.split(",") if item.strip()]
    for mirror in mirrors:
        if not mirror.startswith("https://") or "{set_id}" not in mirror:
            raise ConfigError(f"Each BEATMAP_MIRRORS entry must be an https:// URL containing {{set_id}}, got {mirror!r}")
    return mirrors


def LoadConfig(require_discord=True):
    load_dotenv()
    discord_token = os.environ.get("DISCORD_TOKEN", "").strip()
    if require_discord and not discord_token:
        raise ConfigError("DISCORD_TOKEN is not set. Put it in .env locally or in Railway Variables.")
    guild = os.environ.get("DEV_GUILD_ID", "").strip()
    return Config(
        discord_token=discord_token,
        osu_client_id=os.environ.get("OSU_CLIENT_ID", "").strip(),
        osu_client_secret=os.environ.get("OSU_CLIENT_SECRET", "").strip(),
        cache_dir=os.environ.get("CACHE_DIR", "").strip() or os.path.join(os.getcwd(), "cache"),
        max_upload_bytes=int(Number("MAX_UPLOAD_MB", 10, float) * 1024 * 1024),
        max_concurrent_renders=max(1, Number("MAX_CONCURRENT_RENDERS", 1)),
        render_timeout_seconds=Number("RENDER_TIMEOUT_SECONDS", 600),
        user_cooldown_seconds=Number("USER_COOLDOWN_SECONDS", 60),
        max_queue_length=Number("MAX_QUEUE_LENGTH", 10),
        dev_guild_id=int(guild) if guild.isdigit() else None,
        beatmap_mirrors=Mirrors(),
        cache_max_bytes=int(Number("CACHE_MAX_MB", 500, float) * 1024 * 1024),
        max_set_download_bytes=int(Number("MAX_SET_DOWNLOAD_MB", 80, float) * 1024 * 1024),
        background_dim_play=Fraction("BACKGROUND_DIM_PLAY", 0.7),
        background_dim_rest=Fraction("BACKGROUND_DIM_REST", 0.4),
        skin_dir=os.environ.get("SKIN_DIR", "").strip(),
    )
