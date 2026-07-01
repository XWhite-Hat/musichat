"""Generate assets/icon.ico from scratch using Pillow."""

from __future__ import annotations
import pathlib
from PIL import Image, ImageDraw, ImageFont

SIZES = [256, 128, 64, 48, 32, 16]
LABEL = "<|MH|>"
OUT = pathlib.Path(__file__).parent.parent / "assets" / "icon.ico"
OUT.parent.mkdir(parents=True, exist_ok=True)

FONT_CANDIDATES = [
    "consolab.ttf",   # Consolas Bold (Windows built-in)
    "consola.ttf",    # Consolas Regular
    "cour.ttf",       # Courier New
    "lucon.ttf",      # Lucida Console
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_frame(px: int) -> Image.Image:
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    radius = max(4, px // 8)
    draw.rounded_rectangle([0, 0, px - 1, px - 1], radius=radius, fill=(0, 0, 0, 255))

    font_size = int(px * 0.28)
    font = _font(font_size)

    bbox = draw.textbbox((0, 0), LABEL, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (px - w) // 2 - bbox[0]
    y = (px - h) // 2 - bbox[1]

    draw.text((x, y), LABEL, font=font, fill=(255, 255, 255, 255))
    return img


frames = [make_frame(s) for s in SIZES]
frames[0].save(
    OUT,
    format="ICO",
    append_images=frames[1:],
    sizes=[(s, s) for s in SIZES],
)
print(f"Written: {OUT}  ({OUT.stat().st_size // 1024} KB)")
