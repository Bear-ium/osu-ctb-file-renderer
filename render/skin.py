"""Loads catch images from an osu! skin folder, following lazer's legacy catch skin rules."""
import logging
import os

from PIL import Image, ImageChops

log = logging.getLogger(__name__)

FRUIT_NAMES = ["pear", "grapes", "apple", "orange"]
MAX_TEXTURE_SIZE = 160
MAX_SKIN_FILE_BYTES = 8 * 1024 * 1024
MAX_SKIN_PIXELS = 4096 * 4096
CATCHER_PLATE_ROW = 16
VISIBLE_ALPHA = 32


class SkinTexture:
    """An image plus its display scale: @2x files count as half their pixel size, like osu! does."""

    def __init__(self, image, density):
        self.image = image
        self.density = density

    @property
    def display_size(self):
        return self.image.width / self.density, self.image.height / self.density


class Skin:
    def __init__(self, directory):
        self.directory = directory
        self.files = {}
        try:
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    self.files[name.lower()] = path
        except OSError as error:
            log.warning("Couldn't read skin folder %s: %s", directory, error)
        self.cache = {}

    def LoadFile(self, filename, density):
        path = self.files.get(filename)
        if path is None:
            return None
        try:
            if os.path.getsize(path) > MAX_SKIN_FILE_BYTES:
                raise ValueError("file too large")
            with Image.open(path) as image:
                if image.width * image.height > MAX_SKIN_PIXELS:
                    raise ValueError("image too large")
                return SkinTexture(image.convert("RGBA"), density)
        except (OSError, ValueError) as error:
            log.warning("Skipping skin image %s: %s", filename, error)
            return None

    def Texture(self, name, animated_first=False):
        """Look up name (or frame 0 of its animation), preferring @2x, as osu!'s skin lookup does."""
        key = (name, animated_first)
        if key not in self.cache:
            candidates = [f"{name}-0", name] if animated_first else [name, f"{name}-0"]
            texture = None
            for candidate in candidates:
                texture = self.LoadFile(f"{candidate}@2x.png", 2) or self.LoadFile(f"{candidate}.png", 1)
                if texture is not None:
                    break
            self.cache[key] = texture
        return self.cache[key]

    def HasCatchFruit(self):
        return all(self.Texture(f"fruit-{name}") is not None for name in FRUIT_NAMES)


def LoadSkin(directory):
    if not directory:
        return None
    if not os.path.isdir(directory):
        log.warning("SKIN_DIR %s is not a folder, using the built-in look", directory)
        return None
    skin = Skin(directory)
    if not skin.HasCatchFruit():
        log.warning("Skin %s has no catch fruit images, using the built-in look", directory)
        return None
    return skin


def Tint(image, colour):
    """Multiply an RGBA image's colour by colour, keeping its alpha (osu!'s sprite colour)."""
    rgb = ImageChops.multiply(image.convert("RGB"), Image.new("RGB", image.size, colour))
    rgb.putalpha(image.getchannel("A"))
    return rgb


def Scaled(texture, display_scale, max_display=None):
    """Resize a texture to screen pixels; display_scale is screen pixels per osu! display unit."""
    width, height = texture.display_size
    if max_display:
        shrink = min(1.0, max_display / max(width, height))
        width, height = width * shrink, height * shrink
    size = (max(1, round(width * display_scale)), max(1, round(height * display_scale)))
    return texture.image.resize(size, Image.LANCZOS)


def FadeAlpha(image, factor):
    faded = image.copy()
    faded.putalpha(image.getchannel("A").point(lambda a: int(a * factor)))
    return faded


def CentreOn(layers):
    """Stack images centred on each other, first at the bottom."""
    width = max(layer.width for layer in layers)
    height = max(layer.height for layer in layers)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for layer in layers:
        canvas.alpha_composite(layer, ((width - layer.width) // 2, (height - layer.height) // 2))
    return canvas


def PieceSprite(base, overlay, colour, display_scale, hyper_colour=None):
    """A fruit, droplet or banana: tinted base, untinted overlay, and a red glow behind it when hyperdashing."""
    layers = []
    if hyper_colour is not None:
        layers.append(FadeAlpha(Tint(Scaled(base, display_scale * 1.2, MAX_TEXTURE_SIZE * 1.2), hyper_colour), 0.7))
    layers.append(Tint(Scaled(base, display_scale, MAX_TEXTURE_SIZE), colour))
    if overlay is not None:
        layers.append(Scaled(overlay, display_scale, MAX_TEXTURE_SIZE))
    return CentreOn(layers)


def VisibleBox(image):
    """Bounding box of clearly visible pixels, ignoring faint shadows and resampling fringes."""
    return image.getchannel("A").point(lambda a: 255 if a > VISIBLE_ALPHA else 0).getbbox()


def CatcherSprite(texture, catch_width_px, colour=None):
    """Scale the catcher so its visible width equals the judging width. Returns (image, rows above the plate)."""
    bbox = VisibleBox(texture.image)
    visible_width = (bbox[2] - bbox[0]) if bbox else texture.image.width
    scale = catch_width_px / visible_width
    size = (max(1, round(texture.image.width * scale)), max(1, round(texture.image.height * scale)))
    image = texture.image.resize(size, Image.LANCZOS)
    if colour is not None:
        image = Tint(image, colour)
    return image, round(CATCHER_PLATE_ROW * texture.density * scale)
