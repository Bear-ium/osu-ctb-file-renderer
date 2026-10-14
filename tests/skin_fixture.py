"""Builds a small synthetic osu! skin for tests, since official skin images can't be bundled."""
import os

from PIL import Image, ImageDraw


def Shape(size, draw_shape, fill=(255, 255, 255, 255)):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_shape(ImageDraw.Draw(image), size, fill)
    return image


def MakeTestSkin(directory):
    """Fruits at @2x (128 display units), droplet and bananas at @1x, catcher animation frame 0 at @2x."""
    os.makedirs(directory, exist_ok=True)
    shapes = {
        "pear": lambda d, s, f: d.ellipse([s * 0.2, s * 0.1, s * 0.8, s * 0.9], fill=f),
        "grapes": lambda d, s, f: [d.ellipse([x, y, x + s * 0.35, y + s * 0.35], fill=f) for x in (s * 0.15, s * 0.5) for y in (s * 0.15, s * 0.5)],
        "apple": lambda d, s, f: d.rectangle([s * 0.15, s * 0.15, s * 0.85, s * 0.85], fill=f),
        "orange": lambda d, s, f: d.polygon([(s / 2, s * 0.1), (s * 0.9, s * 0.9), (s * 0.1, s * 0.9)], fill=f),
    }
    for name, draw_shape in shapes.items():
        Shape(256, draw_shape).save(os.path.join(directory, f"fruit-{name}@2x.png"))
        Shape(256, lambda d, s, f: d.ellipse([s * 0.4, s * 0.25, s * 0.55, s * 0.4], fill=f), (40, 40, 40, 255)).save(
            os.path.join(directory, f"fruit-{name}-overlay@2x.png"))
    Shape(64, lambda d, s, f: d.ellipse([8, 8, s - 8, s - 8], fill=f)).save(os.path.join(directory, "Fruit-Drop.png"))
    Shape(128, lambda d, s, f: d.ellipse([10, 30, s - 10, s - 30], fill=f)).save(os.path.join(directory, "fruit-bananas.png"))

    catcher = Image.new("RGBA", (612, 640), (0, 0, 0, 0))
    draw = ImageDraw.Draw(catcher)
    draw.rectangle([40, 32, 571, 60], fill=(230, 230, 240, 255))
    draw.rectangle([200, 60, 411, 600], fill=(90, 120, 200, 255))
    catcher.save(os.path.join(directory, "fruit-catcher-idle-0@2x.png"))
    return directory
