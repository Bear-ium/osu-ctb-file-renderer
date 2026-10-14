import os
from dataclasses import dataclass

from dotenv import load_dotenv


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


def Number(name, default, kind=int):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return kind(value)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number, got {value!r}") from error


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
    )
