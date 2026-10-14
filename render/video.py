import bisect
import logging
import os
import subprocess

from PIL import Image, ImageDraw, ImageFont

from render.catch import BANANA, DROPLET, FRUIT, PLAYFIELD_HEIGHT, PLAYFIELD_WIDTH, TINY_DROPLET, CATCHER_BASE_SIZE
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
POP_MS = 160
MISS_MARK_MS = 400
TRAIL_OFFSETS_MS = (90, 60, 30)

BACKGROUND = (14, 14, 20)
PLAYFIELD_COLOUR = (24, 24, 34)
BORDER_COLOUR = (60, 60, 80)
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


def Mix(colour, other, alpha):
    return tuple(int(a * alpha + b * (1 - alpha)) for a, b in zip(colour, other))


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


def WrapText(text, font, max_width):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


class FrameRenderer:
    def __init__(self, beatmap, catch_beatmap, replay, track, judgements):
        self.beatmap = beatmap
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

        self.title_font = LoadFont(24, bold=True)
        self.body_font = LoadFont(18)
        self.label_font = LoadFont(15)
        self.big_font = LoadFont(44, bold=True)
        self.medium_font = LoadFont(30, bold=True)
        self.miss_font = LoadFont(26, bold=True)

        self.BuildSprites()
        self.background = self.BuildBackground()

    @property
    def duration_seconds(self):
        return self.frame_count / FPS

    def BuildSprites(self):
        self.sprites = {}
        colours = self.beatmap.combo_colours
        for index, colour in enumerate(colours):
            for hyper in (False, True):
                glow = HYPER_COLOUR if hyper else None
                self.sprites[(FRUIT, index, hyper)] = CircleSprite(self.fruit_diameter, colour, glow=glow)
                self.sprites[(DROPLET, index, hyper)] = CircleSprite(self.fruit_diameter * 0.55, colour, glow=glow)
            self.sprites[(TINY_DROPLET, index, False)] = CircleSprite(self.fruit_diameter * 0.3, colour)
        self.sprites[(BANANA, 0, False)] = CircleSprite(self.fruit_diameter * 0.8, BANANA_COLOUR)

        catcher_width = CATCHER_BASE_SIZE * self.catch.object_scale * 2 * SCALE
        self.catcher_sprite = CatcherSprite(catcher_width, CATCHER_COLOUR)
        self.hyper_catcher_sprite = CatcherSprite(catcher_width, HYPER_COLOUR)
        self.trail_sprites = {
            hyper: [WithAlpha(sprite, alpha) for alpha in (0.15, 0.25, 0.35)]
            for hyper, sprite in ((False, self.catcher_sprite), (True, self.hyper_catcher_sprite))
        }

    def BuildBackground(self):
        image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(image)
        left, right = ScreenX(0), ScreenX(PLAYFIELD_WIDTH)
        draw.rectangle([left, 0, right, HEIGHT], fill=PLAYFIELD_COLOUR)
        draw.line([left, 0, left, HEIGHT], fill=BORDER_COLOUR, width=2)
        draw.line([right, 0, right, HEIGHT], fill=BORDER_COLOUR, width=2)

        panel_width = left - 40
        y = 30
        for line in WrapText(self.beatmap.title, self.title_font, panel_width)[:3]:
            draw.text((20, y), line, font=self.title_font, fill=TEXT_COLOUR)
            y += 30
        for line in WrapText(self.beatmap.artist, self.body_font, panel_width)[:2]:
            draw.text((20, y), line, font=self.body_font, fill=MUTED_TEXT)
            y += 24
        y += 6
        for line in WrapText(f"[{self.beatmap.version}]", self.body_font, panel_width)[:2]:
            draw.text((20, y), line, font=self.body_font, fill=TEXT_COLOUR)
            y += 24
        if self.beatmap.creator:
            draw.text((20, y), f"mapped by {self.beatmap.creator}", font=self.label_font, fill=MUTED_TEXT)
            y += 22

        y += 30
        draw.text((20, y), "PLAYER", font=self.label_font, fill=MUTED_TEXT)
        draw.text((20, y + 20), self.replay.username, font=self.medium_font, fill=TEXT_COLOUR)
        mods = self.replay.mods.Acronyms()
        draw.text((20, y + 60), "+" + " ".join(mods) if mods else "No mods", font=self.body_font, fill=MUTED_TEXT)

        right_x = right + 20
        draw.text((right_x, 30), "ACCURACY", font=self.label_font, fill=MUTED_TEXT)
        draw.text((right_x, 130), "COMBO", font=self.label_font, fill=MUTED_TEXT)
        draw.text((right_x, 230), "MISSES", font=self.label_font, fill=MUTED_TEXT)
        return image

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
            hyper = obj.hyper_target is not None and obj.kind != TINY_DROPLET
            colour_index = 0 if obj.kind == BANANA else obj.colour_index
            sprite = self.sprites[(obj.kind, colour_index, hyper and obj.kind != BANANA)]
            frame.paste(sprite, (int(ScreenX(obj.x) - sprite.width / 2), int(y - sprite.height / 2)), sprite)

    def DrawPop(self, draw, obj, age):
        progress = age / POP_MS
        radius = self.fruit_diameter * (0.3 + 0.4 * progress)
        colour = BANANA_COLOUR if obj.kind == BANANA else self.beatmap.combo_colours[obj.colour_index]
        faded = Mix(colour, PLAYFIELD_COLOUR, 1 - progress)
        x = ScreenX(obj.x)
        draw.ellipse([x - radius, PLATE_Y - radius * 0.5, x + radius, PLATE_Y + radius * 0.5], outline=faded, width=3)

    def DrawCatcher(self, frame, time):
        hyper = self.IsHyperdashing(time)
        sprite = self.hyper_catcher_sprite if hyper else self.catcher_sprite
        top = int(PLATE_Y)
        if hyper or self.track.DashingAt(time):
            for trail, offset in zip(self.trail_sprites[hyper], TRAIL_OFFSETS_MS):
                past_x = ScreenX(self.track.XAt(time - offset * self.replay.mods.rate))
                frame.paste(trail, (int(past_x - trail.width / 2), top), trail)
        x = ScreenX(self.track.XAt(time))
        frame.paste(sprite, (int(x - sprite.width / 2), top), sprite)

    def DrawHud(self, draw, time):
        combo, accuracy = self.judgements.StateAt(time)
        misses = bisect.bisect_right(self.miss_times, time)
        right_x = ScreenX(PLAYFIELD_WIDTH) + 20
        draw.text((right_x, 50), f"{accuracy * 100:.2f}%", font=self.big_font, fill=TEXT_COLOUR)
        draw.text((right_x, 150), f"{combo}x", font=self.big_font, fill=TEXT_COLOUR)
        draw.text((right_x, 250), str(misses), font=self.big_font, fill=MISS_COLOUR if misses else TEXT_COLOUR)

        progress = min(max((time - self.start_time) / (self.end_time - self.start_time), 0), 1)
        draw.rectangle([0, HEIGHT - 4, WIDTH * progress, HEIGHT], fill=PROGRESS_COLOUR)

    def RenderFrame(self, index):
        time = self.start_time + index * self.ms_per_frame
        frame = self.background.copy()
        draw = ImageDraw.Draw(frame)
        self.DrawObjects(frame, draw, time)
        self.DrawCatcher(frame, time)
        self.DrawHud(draw, time)
        return frame


def TargetBitrateKbps(max_bytes, duration_seconds):
    kbps = max_bytes * SIZE_SAFETY * 8 / duration_seconds / 1000
    return int(min(max(kbps, MIN_BITRATE_KBPS), MAX_BITRATE_KBPS))


def EncoderArgs(bitrate_kbps):
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "animation", "-pix_fmt", "yuv420p",
        "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{int(bitrate_kbps * 1.5)}k", "-bufsize", f"{bitrate_kbps * 2}k",
        "-g", str(FPS * 2), "-movflags", "+faststart",
    ]


def EncodeFrames(renderer, output_path, bitrate_kbps):
    command = [
        FfmpegPath(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
        *EncoderArgs(bitrate_kbps), output_path,
    ]
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
    command = [FfmpegPath(), "-hide_banner", "-loglevel", "error", "-y", "-i", source_path, *EncoderArgs(bitrate_kbps), output_path]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg re-encode failed: {result.stderr.strip()[-500:]}")


def RenderVideo(renderer, output_path, max_bytes):
    bitrate = TargetBitrateKbps(max_bytes, renderer.duration_seconds)
    log.info("Rendering %d frames (%.1fs of video) at %d kbps", renderer.frame_count, renderer.duration_seconds, bitrate)
    EncodeFrames(renderer, output_path, bitrate)

    for _ in range(3):
        size = os.path.getsize(output_path)
        if size <= max_bytes:
            return
        if bitrate <= MIN_BITRATE_KBPS:
            break
        bitrate = max(int(bitrate * max_bytes / size * 0.85), MIN_BITRATE_KBPS)
        log.info("Output was %d bytes, re-encoding at %d kbps", size, bitrate)
        retry_path = output_path + ".retry.mp4"
        Transcode(output_path, retry_path, bitrate)
        os.replace(retry_path, output_path)
    if os.path.getsize(output_path) > max_bytes:
        raise RenderError("The video came out too large to upload, even at the lowest quality.")
