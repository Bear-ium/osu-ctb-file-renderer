import bisect
import logging
import os
import subprocess

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from render.catch import BANANA, DROPLET, FRUIT, PLAYFIELD_HEIGHT, PLAYFIELD_WIDTH, TINY_DROPLET, CATCHER_BASE_SIZE
from render import skin as skinning
from render.errors import RenderError
from render.ffmpeg import FfmpegPath

log = logging.getLogger(__name__)

WIDTH = 1280
HEIGHT = 720
FPS = 60
SCALE = 1.5
PLAYFIELD_LEFT = (WIDTH - PLAYFIELD_WIDTH * SCALE) / 2
PLAYFIELD_TOP = 40
PLATE_Y = PLAYFIELD_TOP + PLAYFIELD_HEIGHT * SCALE
SUPERSAMPLE = 4
LEAD_IN_MS = 1000
LEAD_OUT_MS = 1500
MAX_VIDEO_SECONDS = 20 * 60
MIN_BITRATE_KBPS = 150
MAX_BITRATE_KBPS = 6000
SIZE_SAFETY = 0.9
AUDIO_KBPS = 128
AUDIO_OVERHEAD = 1.05
AUDIO_SAMPLE_RATE = 44100
AUDIO_FADE_SECONDS = 1.5
AUDIO_PROBE_SECONDS = 3
VIDEO_TUNE = "animation"
POP_MS = 160
MISS_MARK_MS = 400
TRAIL_OFFSETS_MS = (90, 60, 30)
DIM_STEPS = 8
DIM_FADE_MS = 500
TEXT_MARGIN = 16
SIDE_TEXT_WIDTH = PLAYFIELD_LEFT - TEXT_MARGIN * 2
TEXT_STROKE = 2
STROKE_COLOUR = (0, 0, 0)

BACKGROUND = (14, 14, 20)
TEXT_COLOUR = (235, 235, 240)
MUTED_TEXT = (150, 150, 170)
BANANA_COLOUR = (255, 220, 60)
HYPER_COLOUR = (255, 40, 40)
CATCHER_COLOUR = (240, 240, 250)
MISS_COLOUR = (255, 70, 70)
PROGRESS_COLOUR = (120, 170, 255)

FONT_CANDIDATES = {
    True: ["DejaVuSans-Bold.ttf", "segoeuib.ttf", "arialbd.ttf"],
    False: ["DejaVuSans.ttf", "segoeui.ttf", "arial.ttf"],
}


def LoadFont(size, bold=False):
    for name in FONT_CANDIDATES[bold]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def ScreenX(x):
    return PLAYFIELD_LEFT + x * SCALE


def Darken(colour, amount):
    return tuple(int(c * amount) for c in colour)


def CircleSprite(diameter, colour, outline=None, glow=None):
    pad = 6 if glow else 1
    size = int(diameter + pad * 2)
    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    centre = big / 2
    radius = diameter * SUPERSAMPLE / 2
    if glow:
        glow_radius = radius + pad * SUPERSAMPLE * 0.9
        draw.ellipse([centre - glow_radius, centre - glow_radius, centre + glow_radius, centre + glow_radius], fill=glow + (200,))
    draw.ellipse([centre - radius, centre - radius, centre + radius, centre + radius], fill=(outline or Darken(colour, 0.55)) + (255,))
    inner = radius * 0.84
    draw.ellipse([centre - inner, centre - inner, centre + inner, centre + inner], fill=colour + (255,))
    shine_layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    shine = radius * 0.32
    shine_x, shine_y = centre - radius * 0.3, centre - radius * 0.3
    ImageDraw.Draw(shine_layer).ellipse([shine_x - shine, shine_y - shine, shine_x + shine, shine_y + shine], fill=(255, 255, 255, 90))
    image = Image.alpha_composite(image, shine_layer)
    return image.resize((size, size), Image.LANCZOS)


def CatcherSprite(width, colour, alpha=255):
    height = max(int(width * 0.32), 12)
    big_w, big_h = int(width * SUPERSAMPLE), height * SUPERSAMPLE
    image = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    plate_h = int(big_h * 0.3)
    draw.rounded_rectangle([0, 0, big_w - 1, plate_h], radius=plate_h // 2, fill=colour + (alpha,))
    body_inset = big_w * 0.18
    draw.rounded_rectangle([body_inset, plate_h * 0.6, big_w - body_inset, big_h - 1], radius=big_h // 4, fill=Darken(colour, 0.7) + (alpha,))
    return image.resize((int(width), height), Image.LANCZOS)


def WithAlpha(sprite, factor):
    faded = sprite.copy()
    faded.putalpha(sprite.getchannel("A").point(lambda a: int(a * factor)))
    return faded


def Truncate(text, font, max_width):
    """Shorten text with an ellipsis so it fits on one line."""
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def OutlinedText(draw, position, text, font, fill, anchor="la"):
    draw.text(position, text, font=font, fill=fill, anchor=anchor, stroke_width=TEXT_STROKE, stroke_fill=STROKE_COLOUR)


def LoadBackgroundImage(path):
    if not path:
        return None
    try:
        with Image.open(path) as image:
            return ImageOps.fit(image.convert("RGB"), (WIDTH, HEIGHT), Image.LANCZOS)
    except OSError as error:
        log.warning("Couldn't load background %s: %s", path, error)
        return None


def RestIntervals(beatmap, catch_beatmap):
    """Map-time stretches where the background is shown lighter: before the first object, breaks, after the last."""
    first_visible = catch_beatmap.start_time - catch_beatmap.preempt
    return [(float("-inf"), first_visible), *beatmap.breaks, (catch_beatmap.end_time, float("inf"))]


class FrameRenderer:
    def __init__(self, beatmap, catch_beatmap, replay, track, judgements, background_path=None, dim_play=0.7, dim_rest=0.4,
                 skin=None):
        self.beatmap = beatmap
        self.skin = skin
        self.facing = 1
        self.catch = catch_beatmap
        self.replay = replay
        self.track = track
        self.judgements = judgements
        self.objects = catch_beatmap.objects
        self.object_times = [o.time for o in self.objects]
        self.hyper_starts = [start for start, _ in judgements.hyper_intervals]
        self.miss_times = [o.time for o in self.objects if o.kind in (FRUIT, DROPLET) and not o.caught]

        self.fruit_diameter = 128 * catch_beatmap.object_scale * 0.75 * SCALE
        self.fall_distance = PLATE_Y - PLAYFIELD_TOP
        self.fall_speed = self.fall_distance / catch_beatmap.preempt
        self.after_ms = (HEIGHT + self.fruit_diameter - PLATE_Y) / self.fall_speed

        self.start_time = catch_beatmap.start_time - catch_beatmap.preempt - LEAD_IN_MS
        self.end_time = catch_beatmap.end_time + LEAD_OUT_MS
        last_frame_time = replay.frames[-1].time
        if last_frame_time + LEAD_OUT_MS < self.end_time:
            self.end_time = max(last_frame_time + LEAD_OUT_MS, self.start_time + 1000)
        self.ms_per_frame = 1000 / FPS * replay.mods.rate
        self.frame_count = int((self.end_time - self.start_time) / self.ms_per_frame) + 1
        if self.frame_count / FPS > MAX_VIDEO_SECONDS:
            raise RenderError(f"That replay is too long to render (limit is {MAX_VIDEO_SECONDS // 60} minutes).")

        self.title_font = LoadFont(22, bold=True)
        self.body_font = LoadFont(16)
        self.small_font = LoadFont(14)
        self.player_font = LoadFont(20, bold=True)
        self.big_font = LoadFont(44, bold=True)
        self.combo_font = LoadFont(56, bold=True)
        self.medium_font = LoadFont(22, bold=True)
        self.miss_font = LoadFont(26, bold=True)

        self.rest_intervals = RestIntervals(beatmap, catch_beatmap)
        self.rest_starts = [start for start, _ in self.rest_intervals]
        self.BuildSprites()
        self.backgrounds = self.BuildBackground(LoadBackgroundImage(background_path), dim_play, dim_rest)

    @property
    def duration_seconds(self):
        return self.frame_count / FPS

    def BuildSprites(self):
        self.sprites = {}
        if self.skin is not None:
            self.BuildSkinSprites()
        else:
            colours = self.beatmap.combo_colours
            for index, colour in enumerate(colours):
                for hyper in (False, True):
                    glow = HYPER_COLOUR if hyper else None
                    self.sprites[(FRUIT, index, hyper, 0)] = CircleSprite(self.fruit_diameter, colour, glow=glow)
                    self.sprites[(DROPLET, index, hyper, 0)] = CircleSprite(self.fruit_diameter * 0.55, colour, glow=glow)
                self.sprites[(TINY_DROPLET, index, False, 0)] = CircleSprite(self.fruit_diameter * 0.3, colour)
            self.sprites[(BANANA, 0, False, 0)] = CircleSprite(self.fruit_diameter * 0.8, BANANA_COLOUR)

        catcher_texture = self.skin.Texture("fruit-catcher-idle", animated_first=True) if self.skin is not None else None
        if catcher_texture is not None:
            catch_width_px = self.catch.catch_width * SCALE
            normal, self.catcher_lift = skinning.CatcherSprite(catcher_texture, catch_width_px)
            hyper, _ = skinning.CatcherSprite(catcher_texture, catch_width_px, HYPER_COLOUR)
        else:
            catcher_width = CATCHER_BASE_SIZE * self.catch.object_scale * 2 * SCALE
            normal, hyper = CatcherSprite(catcher_width, CATCHER_COLOUR), CatcherSprite(catcher_width, HYPER_COLOUR)
            self.catcher_lift = 0
        self.catcher_sprites = {}
        self.trail_sprites = {}
        for is_hyper, sprite in ((False, normal), (True, hyper)):
            for facing, image in ((1, sprite), (-1, ImageOps.mirror(sprite))):
                self.catcher_sprites[(is_hyper, facing)] = image
                self.trail_sprites[(is_hyper, facing)] = [WithAlpha(image, alpha) for alpha in (0.15, 0.25, 0.35)]

    def BuildSkinSprites(self):
        """Skinned pieces, sized as osu! sizes them: texture size times the object scale, droplets 0.8, tiny droplets 0.4."""
        unit = self.catch.object_scale * SCALE
        drop, drop_overlay = self.skin.Texture("fruit-drop"), self.skin.Texture("fruit-drop-overlay")
        for index, colour in enumerate(self.beatmap.combo_colours):
            for hyper in (False, True):
                glow = HYPER_COLOUR if hyper else None
                for variant, name in enumerate(skinning.FRUIT_NAMES):
                    base, overlay = self.skin.Texture(f"fruit-{name}"), self.skin.Texture(f"fruit-{name}-overlay")
                    self.sprites[(FRUIT, index, hyper, variant)] = skinning.PieceSprite(base, overlay, colour, unit, glow)
                self.sprites[(DROPLET, index, hyper, 0)] = (
                    skinning.PieceSprite(drop, drop_overlay, colour, unit * 0.8, glow) if drop is not None
                    else CircleSprite(self.fruit_diameter * 0.55, colour, glow=glow))
            self.sprites[(TINY_DROPLET, index, False, 0)] = (
                skinning.PieceSprite(drop, drop_overlay, colour, unit * 0.4) if drop is not None
                else CircleSprite(self.fruit_diameter * 0.3, colour))
        bananas = self.skin.Texture("fruit-bananas")
        self.sprites[(BANANA, 0, False, 0)] = (
            skinning.PieceSprite(bananas, self.skin.Texture("fruit-bananas-overlay"), BANANA_COLOUR, unit * 0.8) if bananas is not None
            else CircleSprite(self.fruit_diameter * 0.8, BANANA_COLOUR))

    def BuildTextLayer(self):
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        lines = [
            (Truncate(self.beatmap.title, self.title_font, SIDE_TEXT_WIDTH), self.title_font, TEXT_COLOUR, 28),
            (Truncate(self.beatmap.artist, self.body_font, SIDE_TEXT_WIDTH), self.body_font, MUTED_TEXT, 22),
            (Truncate(f"[{self.beatmap.version}]", self.body_font, SIDE_TEXT_WIDTH), self.body_font, TEXT_COLOUR, 22),
        ]
        if self.beatmap.creator:
            lines.append((Truncate(f"mapped by {self.beatmap.creator}", self.small_font, SIDE_TEXT_WIDTH), self.small_font, MUTED_TEXT, 20))
        y = TEXT_MARGIN
        for text, font, colour, height in lines:
            OutlinedText(draw, (TEXT_MARGIN, y), text, font, colour)
            y += height

        mods = self.replay.mods.Acronyms()
        right = WIDTH - TEXT_MARGIN
        OutlinedText(draw, (right, HEIGHT - 52), Truncate(self.replay.username, self.player_font, SIDE_TEXT_WIDTH),
                     self.player_font, TEXT_COLOUR, anchor="rs")
        OutlinedText(draw, (right, HEIGHT - 28), "+" + " ".join(mods) if mods else "No mods", self.body_font, MUTED_TEXT, anchor="rs")
        return layer

    def BuildBackground(self, image, dim_play, dim_rest):
        """Pre-build the background at DIM_STEPS levels from rest to play, so frames only pick one."""
        base = image if image is not None else Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
        text = self.BuildTextLayer()
        variants = []
        for step in range(DIM_STEPS):
            dim = dim_rest + (dim_play - dim_rest) * step / (DIM_STEPS - 1)
            dimmed = ImageEnhance.Brightness(base).enhance(1 - dim) if image is not None else base.copy()
            variants.append(Image.alpha_composite(dimmed.convert("RGBA"), text).convert("RGB"))
        return variants

    def PlayAmount(self, time):
        """0 while resting, 1 during play, ramping over DIM_FADE_MS at each edge."""
        index = bisect.bisect_right(self.rest_starts, time) - 1
        distances = []
        for start, end in self.rest_intervals[max(index, 0):index + 2]:
            if start <= time <= end:
                return 0.0
            distances.append(start - time if time < start else time - end)
        nearest = min(distances) if distances else DIM_FADE_MS
        return min(nearest / DIM_FADE_MS, 1.0)

    def BackgroundAt(self, time):
        return self.backgrounds[round(self.PlayAmount(time) * (DIM_STEPS - 1))]

    def ObjectY(self, obj, time):
        return PLATE_Y - (obj.time - time) * self.fall_speed

    def IsHyperdashing(self, time):
        index = bisect.bisect_right(self.hyper_starts, time) - 1
        return index >= 0 and time <= self.judgements.hyper_intervals[index][1]

    def DrawObjects(self, frame, draw, time):
        first = bisect.bisect_left(self.object_times, time - max(self.after_ms, MISS_MARK_MS))
        last = bisect.bisect_right(self.object_times, time + self.catch.preempt)
        for obj in reversed(self.objects[first:last]):
            if obj.time <= time:
                if obj.caught:
                    age = time - obj.time
                    if age < POP_MS and obj.kind != TINY_DROPLET:
                        self.DrawPop(draw, obj, age)
                    continue
                if obj.kind in (FRUIT, DROPLET) and time - obj.time < MISS_MARK_MS:
                    draw.text((ScreenX(obj.x), PLATE_Y - 40), "x", font=self.miss_font, fill=MISS_COLOUR, anchor="mm")
            y = self.ObjectY(obj, time)
            if y > HEIGHT + self.fruit_diameter:
                continue
            hyper = obj.hyper_target is not None and obj.kind not in (TINY_DROPLET, BANANA)
            colour_index = 0 if obj.kind == BANANA else obj.colour_index
            variant = obj.index_in_beatmap % 4 if obj.kind == FRUIT and self.skin is not None else 0
            sprite = self.sprites[(obj.kind, colour_index, hyper, variant)]
            frame.paste(sprite, (int(ScreenX(obj.x) - sprite.width / 2), int(y - sprite.height / 2)), sprite)

    def DrawPop(self, draw, obj, age):
        progress = age / POP_MS
        radius = self.fruit_diameter * (0.3 + 0.4 * progress)
        colour = BANANA_COLOUR if obj.kind == BANANA else self.beatmap.combo_colours[obj.colour_index]
        x = ScreenX(obj.x)
        draw.ellipse([x - radius, PLATE_Y - radius * 0.5, x + radius, PLATE_Y + radius * 0.5], outline=colour,
                     width=max(1, round(3 * (1 - progress))))

    def DrawCatcher(self, frame, time):
        hyper = self.IsHyperdashing(time)
        movement = self.track.XAt(time) - self.track.XAt(time - self.ms_per_frame)
        if abs(movement) > 0.01:
            self.facing = 1 if movement > 0 else -1
        sprite = self.catcher_sprites[(hyper, self.facing)]
        top = int(PLATE_Y) - self.catcher_lift
        if hyper or self.track.DashingAt(time):
            for trail, offset in zip(self.trail_sprites[(hyper, self.facing)], TRAIL_OFFSETS_MS):
                past_x = ScreenX(self.track.XAt(time - offset * self.replay.mods.rate))
                frame.paste(trail, (int(past_x - trail.width / 2), top), trail)
        x = ScreenX(self.track.XAt(time))
        frame.paste(sprite, (int(x - sprite.width / 2), top), sprite)

    def DrawHud(self, draw, time):
        combo, accuracy = self.judgements.StateAt(time)
        misses = bisect.bisect_right(self.miss_times, time)
        right = WIDTH - TEXT_MARGIN
        OutlinedText(draw, (right, TEXT_MARGIN - 4), f"{accuracy * 100:.2f}%", self.big_font, TEXT_COLOUR, anchor="ra")
        OutlinedText(draw, (right, TEXT_MARGIN + 48), f"{misses} {'miss' if misses == 1 else 'misses'}", self.medium_font,
                     MISS_COLOUR if misses else MUTED_TEXT, anchor="ra")
        OutlinedText(draw, (TEXT_MARGIN, HEIGHT - TEXT_MARGIN - 4), f"{combo}x", self.combo_font, TEXT_COLOUR, anchor="ls")

        progress = min(max((time - self.start_time) / (self.end_time - self.start_time), 0), 1)
        draw.rectangle([0, HEIGHT - 4, WIDTH * progress, HEIGHT], fill=PROGRESS_COLOUR)

    def RenderFrame(self, index):
        time = self.start_time + index * self.ms_per_frame
        frame = self.BackgroundAt(time).copy()
        draw = ImageDraw.Draw(frame)
        self.DrawObjects(frame, draw, time)
        self.DrawCatcher(frame, time)
        self.DrawHud(draw, time)
        return frame


def TargetBitrateKbps(max_bytes, duration_seconds, audio_kbps=0):
    """Video bitrate that fits max_bytes once the audio stream and its container overhead are paid for."""
    total_kbps = max_bytes * SIZE_SAFETY * 8 / duration_seconds / 1000
    kbps = total_kbps - audio_kbps * AUDIO_OVERHEAD
    return int(min(max(kbps, MIN_BITRATE_KBPS), MAX_BITRATE_KBPS))


def AudioFilter(start_time_ms, rate, nightcore, duration_seconds):
    """ffmpeg filter that lines the song up with the video: map time t appears at video time (t - start) / rate."""
    filters = [f"aresample={AUDIO_SAMPLE_RATE}"]
    if start_time_ms >= 0:
        filters += [f"atrim=start={start_time_ms / 1000:.3f}", "asetpts=PTS-STARTPTS"]
    else:
        filters.append(f"adelay={round(abs(start_time_ms))}:all=1")
    if rate != 1:
        if nightcore:
            filters += [f"asetrate={AUDIO_SAMPLE_RATE}*{rate:g}", f"aresample={AUDIO_SAMPLE_RATE}"]
        else:
            filters.append(f"atempo={rate:g}")
    fade_start = max(duration_seconds - AUDIO_FADE_SECONDS, 0)
    filters += [f"afade=t=out:st={fade_start:.3f}:d={AUDIO_FADE_SECONDS:g}", "apad"]
    return ",".join(filters)


def EncoderArgs(bitrate_kbps):
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", VIDEO_TUNE, "-pix_fmt", "yuv420p",
        "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{int(bitrate_kbps * 1.5)}k", "-bufsize", f"{bitrate_kbps * 2}k",
        "-g", str(FPS * 2), "-movflags", "+faststart",
    ]


def AudioInputArgs(audio_path):
    """Only allow local file access, so a crafted audio file can't make ffmpeg open network or other protocols."""
    return ["-protocol_whitelist", "file", "-i", audio_path]


def AudioDecodes(audio_path):
    command = [FfmpegPath(), "-hide_banner", "-loglevel", "error", *AudioInputArgs(audio_path),
               "-t", str(AUDIO_PROBE_SECONDS), "-map", "0:a:0", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        log.warning("Audio file doesn't decode: %s", result.stderr.strip()[-300:])
    return result.returncode == 0


def EncodeFrames(renderer, output_path, bitrate_kbps, audio_path=None):
    command = [
        FfmpegPath(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
    ]
    if audio_path:
        mods = renderer.replay.mods
        audio_filter = AudioFilter(renderer.start_time, mods.rate, mods.nightcore, renderer.duration_seconds)
        command += [*AudioInputArgs(audio_path), "-map", "0:v", "-map", "1:a:0", "-filter:a", audio_filter,
                    "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", str(AUDIO_SAMPLE_RATE)]
    command += [*EncoderArgs(bitrate_kbps), "-t", f"{renderer.duration_seconds:.3f}", output_path]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for index in range(renderer.frame_count):
            process.stdin.write(renderer.RenderFrame(index).tobytes())
        process.stdin.close()
    except BrokenPipeError:
        pass
    finally:
        stderr = process.stderr.read().decode(errors="replace")
        process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with code {process.returncode}: {stderr.strip()[-500:]}")


def Transcode(source_path, output_path, bitrate_kbps):
    command = [FfmpegPath(), "-hide_banner", "-loglevel", "error", "-y", "-protocol_whitelist", "file", "-i", source_path,
               "-map", "0:v", "-map", "0:a?", *EncoderArgs(bitrate_kbps), "-c:a", "copy", output_path]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg re-encode failed: {result.stderr.strip()[-500:]}")


def RenderVideo(renderer, output_path, max_bytes, audio_path=None):
    """Encode the video, with the song if one is given. Returns notes for the person who asked."""
    notes = []
    if audio_path and not AudioDecodes(audio_path):
        notes.append("No music: the song file couldn't be read.")
        audio_path = None
    audio_kbps = AUDIO_KBPS if audio_path else 0
    bitrate = TargetBitrateKbps(max_bytes, renderer.duration_seconds, audio_kbps)
    log.info("Rendering %d frames (%.1fs of video) at %d kbps video%s", renderer.frame_count, renderer.duration_seconds,
             bitrate, f" + {audio_kbps} kbps audio" if audio_path else ", no audio")
    try:
        EncodeFrames(renderer, output_path, bitrate, audio_path)
    except RuntimeError as error:
        if not audio_path:
            raise
        log.warning("Encoding with audio failed, retrying without it: %s", error)
        notes.append("No music: the song couldn't be encoded.")
        bitrate = TargetBitrateKbps(max_bytes, renderer.duration_seconds)
        EncodeFrames(renderer, output_path, bitrate)

    for _ in range(3):
        size = os.path.getsize(output_path)
        if size <= max_bytes:
            return notes
        if bitrate <= MIN_BITRATE_KBPS:
            break
        bitrate = max(int(bitrate * max_bytes / size * 0.85), MIN_BITRATE_KBPS)
        log.info("Output was %d bytes, re-encoding at %d kbps", size, bitrate)
        retry_path = output_path + ".retry.mp4"
        Transcode(output_path, retry_path, bitrate)
        os.replace(retry_path, output_path)
    if os.path.getsize(output_path) > max_bytes:
        raise RenderError("The video came out too large to upload, even at the lowest quality.")
    return notes
