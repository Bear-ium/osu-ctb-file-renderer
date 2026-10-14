import asyncio
import logging
import os
import shutil
import signal
import sys
import tempfile
import time

import discord
from discord import app_commands

from bot.config import ConfigError, LoadConfig
from render.errors import RenderError
from render.ffmpeg import CheckFfmpeg, FfmpegMissingError
from render.replay import MAX_REPLAY_BYTES, ParseReplay

log = logging.getLogger("bot")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER_SCRIPT = os.path.join(PROJECT_ROOT, "render.py")
USER_ERROR_PREFIX = "USER_ERROR: "
EXIT_USER_ERROR = 2


class RenderQueue:
    def __init__(self, config):
        self.config = config
        self.semaphore = asyncio.Semaphore(config.max_concurrent_renders)
        self.pending = 0
        self.running = 0
        self.active_users = set()
        self.last_request = {}

    def TryReserve(self, user_id):
        """Claim a queue slot for this user, or return why they can't have one.

        Runs without awaiting, so concurrent requests can't all pass the checks before any of them is counted.
        """
        if user_id in self.active_users:
            return "You already have a render in progress. Wait for it to finish first."
        elapsed = time.monotonic() - self.last_request.get(user_id, -1e9)
        if elapsed < self.config.user_cooldown_seconds:
            return f"Slow down a bit. You can render again in {int(self.config.user_cooldown_seconds - elapsed) + 1}s."
        if self.pending >= self.config.max_queue_length:
            return "The render queue is full right now. Try again in a few minutes."
        self.active_users.add(user_id)
        self.last_request[user_id] = time.monotonic()
        self.pending += 1
        return None


async def RunRender(config, replay_path, output_path, max_bytes):
    """Render in a child process so a stuck job can be killed. Returns None on success, or an error message."""
    command = [sys.executable, RENDER_SCRIPT, replay_path, output_path, "--max-mb", f"{max_bytes / (1024 * 1024):.3f}"]
    posix = os.name == "posix"
    process = await asyncio.create_subprocess_exec(
        *command, cwd=PROJECT_ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=posix,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=config.render_timeout_seconds)
    except asyncio.TimeoutError:
        if posix:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        await process.wait()
        log.warning("Render timed out after %ss: %s", config.render_timeout_seconds, replay_path)
        seconds = config.render_timeout_seconds
        limit = f"{seconds // 60} minutes" if seconds >= 120 else f"{seconds} seconds"
        return f"The render took longer than {limit} and was stopped. Try a shorter replay."

    output = stderr.decode(errors="replace").strip()
    for line in output.splitlines():
        if not line.startswith(USER_ERROR_PREFIX):
            log.info("render: %s", line)
    if process.returncode == 0 and os.path.exists(output_path):
        return None
    if process.returncode == EXIT_USER_ERROR:
        for line in reversed(output.splitlines()):
            if line.startswith(USER_ERROR_PREFIX):
                return line[len(USER_ERROR_PREFIX):]
    log.error("Render failed with exit code %s", process.returncode)
    return "Something went wrong while rendering. The bot owner can check the logs."


async def Reply(interaction, content, file=None):
    """Send the result, falling back to the channel if the interaction token has expired (15 minutes)."""
    try:
        if file is None:
            await interaction.edit_original_response(content=content)
        else:
            await interaction.edit_original_response(content=content, attachments=[file])
        return
    except (discord.NotFound, discord.HTTPException) as error:
        log.warning("Interaction reply failed (%s), falling back to channel", error)
    if interaction.channel is not None:
        if file is not None:
            file.reset()
        await interaction.channel.send(content=f"{interaction.user.mention} {content}", file=file,
                                       allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[interaction.user]))


def SafeName(name):
    """Replay usernames come from the uploaded file, so stop them carrying mentions or markdown."""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(name))[:64]


class RenderBot(discord.Client):
    def __init__(self, config):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.queue = RenderQueue(config)
        self.RegisterCommands()

    async def setup_hook(self):
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild %s", len(synced), self.config.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command(s)", len(synced))

    async def on_ready(self):
        log.info("Connected to Discord as %s (%s)", self.user, self.user.id if self.user else "?")

    def RegisterCommands(self):
        @self.tree.command(name="render", description="Render an osu!catch replay to a video")
        @app_commands.describe(osr_clip="The .osr replay file (osu!catch only)")
        async def render(interaction: discord.Interaction, osr_clip: discord.Attachment):
            await self.HandleRender(interaction, osr_clip)

    async def HandleRender(self, interaction, attachment):
        await interaction.response.defer(thinking=True)

        if not attachment.filename.lower().endswith(".osr"):
            await Reply(interaction, "That isn't an `.osr` file. Upload an osu!catch replay.")
            return
        if attachment.size > MAX_REPLAY_BYTES:
            await Reply(interaction, f"That replay is too large (limit is {MAX_REPLAY_BYTES // (1024 * 1024)} MB).")
            return
        user_id = interaction.user.id
        reason = self.queue.TryReserve(user_id)
        if reason:
            await Reply(interaction, reason)
            return

        work_dir = tempfile.mkdtemp(prefix="ctb-render-")
        holds_queue_slot = True
        try:
            replay_path = os.path.join(work_dir, "replay.osr")
            output_path = os.path.join(work_dir, "replay.mp4")
            await attachment.save(replay_path)
            try:
                replay = await asyncio.to_thread(ParseReplay, replay_path)
            except RenderError as error:
                await Reply(interaction, str(error))
                return

            player = SafeName(replay.username)
            max_bytes = self.config.max_upload_bytes
            if getattr(interaction, "filesize_limit", None):
                max_bytes = min(max_bytes, interaction.filesize_limit)

            if self.queue.running >= self.config.max_concurrent_renders:
                await Reply(interaction, f"Queued replay by **{player}** ({self.queue.pending} in the queue).")
            await self.queue.semaphore.acquire()
            self.queue.pending -= 1
            holds_queue_slot = False
            self.queue.running += 1
            try:
                await Reply(interaction, f"Rendering replay by **{player}**...")
                started = time.monotonic()
                error = await RunRender(self.config, replay_path, output_path, max_bytes)
                log.info("Render for user %s took %.1fs (%s)", user_id, time.monotonic() - started, "ok" if error is None else error)
            finally:
                self.queue.running -= 1
                self.queue.semaphore.release()

            if error is not None:
                await Reply(interaction, error)
                return
            await Reply(interaction, f"Replay by **{player}**", file=discord.File(output_path, filename="replay.mp4"))
        except Exception:
            log.exception("Unexpected error handling /render")
            try:
                await Reply(interaction, "Something went wrong while handling that replay.")
            except discord.HTTPException:
                pass
        finally:
            if holds_queue_slot:
                self.queue.pending -= 1
            self.queue.active_users.discard(user_id)
            shutil.rmtree(work_dir, ignore_errors=True)


def Main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = LoadConfig()
        log.info("ffmpeg found: %s", CheckFfmpeg())
    except (ConfigError, FfmpegMissingError) as error:
        log.critical("%s", error)
        sys.exit(1)
    if not (config.osu_client_id and config.osu_client_secret):
        log.warning("OSU_CLIENT_ID / OSU_CLIENT_SECRET are not set; only cached beatmaps can be rendered")
    os.makedirs(config.cache_dir, exist_ok=True)
    RenderBot(config).run(config.discord_token, log_handler=None)
