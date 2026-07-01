"""
Extract a 3-stop spectrogram gradient from a track's cover-art thumbnail.

Returns ``(color_start, color_mid, color_end)`` as ``"#rrggbb"`` hex strings:

  color_start  — low-intensity bars  → coolest colour in the art
                 (blues, purples, dark greens)
  color_mid    — mid-intensity bars  → mid-palette colour
  color_end    — high-intensity bars → warmest colour in the art
                 (reds, oranges, yellows)

The "warmth" ranking mirrors the classic thermal / spectrogram heat-map scale
(red = loud, blue = quiet).  Colours that are too dark, too grey, or near-white
are discarded so the gradient always reads clearly against a dark background.

Falls back to the classic matrix-green palette on any error.
"""
from __future__ import annotations

import io
import math


# Classic matrix green — used when the image is unavailable or has unexpected content.
_FALLBACK: tuple[str, str, str] = ("#003300", "#00cc33", "#00ff41")

# Clean greyscale ramp — used when cover art is black-and-white / monochrome.
_BW_GRADIENT: tuple[str, str, str] = ("#0d0d0d", "#7a7a7a", "#e0e0e0")


# ── Public API ─────────────────────────────────────────────────────────────────

def extract_gradient(image_url: str) -> tuple[str, str, str]:
    """Download *image_url* and return ``(low, mid, high)`` hex colour stops.

    The call is intentionally synchronous — run it on a worker thread.
    """
    try:
        img = _fetch(image_url)
        if img is None:
            return _FALLBACK

        colours = _dominant_rgb(img, n=16)
        if not colours:
            # No saturated colours — check whether the image is genuinely B&W
            # (greyscale art) vs. just a bad fetch or unexpected format.
            return _BW_GRADIENT if _is_achromatic(img) else _FALLBACK

        ranked = _sort_by_warmth(colours)

        # If the image is nearly monochromatic we may end up with very few
        # candidates after filtering.  Pad by interpolation so we always have 3.
        while len(ranked) < 3:
            mid = _lerp_hex(_to_hex(*ranked[0]), _to_hex(*ranked[-1]), 0.5)
            ranked.insert(len(ranked) // 2, _parse_hex(mid))

        n = len(ranked)
        warm   = ranked[0]          # warmest  → high-intensity colour_end
        middle = ranked[n // 2]     # middle   → colour_mid
        cool   = ranked[-1]         # coolest  → high-intensity colour_start

        # Darken the cool end so quiet bars are subtle (matching how presets like
        # "matrix" and "fire" keep their low-end nearly black).
        cool_dark = _darken(cool, factor=0.25)

        return (_to_hex(*cool_dark), _to_hex(*middle), _to_hex(*warm))

    except Exception as exc:
        print(f"[art_colours] extraction failed: {exc!r}")
        return _FALLBACK


# ── Internals ──────────────────────────────────────────────────────────────────

def _fetch(url: str):
    """Download *url* and return a small (64×64) PIL Image in RGB, or None."""
    try:
        import requests
        from PIL import Image

        resp = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img = img.resize((64, 64), Image.LANCZOS)
        return img
    except Exception:
        return None


def _dominant_rgb(img, n: int = 16) -> list[tuple[int, int, int]]:
    """Return up to *n* perceptually distinct, saturated colours from *img*."""
    from PIL import Image

    # Quantise to reduce noise; FASTOCTREE preserves edge colours well.
    quantized = img.quantize(colors=n, method=Image.Quantize.FASTOCTREE)
    palette   = quantized.getpalette()          # flat [R,G,B, R,G,B, …]

    # Count pixel frequency per palette index.
    freq: dict[int, int] = {}
    for px in quantized.getdata():
        freq[px] = freq.get(px, 0) + 1

    result: list[tuple[int, int, int]] = []
    for idx in sorted(freq, key=freq.__getitem__, reverse=True):
        r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
        h, s, v = _rgb_to_hsv(r, g, b)

        if s < 0.18:             # too grey / near-achromatic
            continue
        if v < 0.12:             # too dark to show on screen
            continue
        if v > 0.95 and s < 0.2: # near-white / washed out
            continue

        result.append((r, g, b))
        if len(result) >= n:
            break

    return result


def _sort_by_warmth(
    colours: list[tuple[int, int, int]],
) -> list[tuple[int, int, int]]:
    """Sort colours from warmest (red/orange/yellow) to coolest (blue/cyan)."""

    def warmth(rgb: tuple[int, int, int]) -> float:
        h, s, v = _rgb_to_hsv(*rgb)
        h_deg = h * 360.0
        # Circular distance from red (0° / 360°).
        # cos mapping: d=0 (red) → 1.0,  d=180 (cyan) → 0.0
        d = min(h_deg, 360.0 - h_deg)
        return (math.cos(math.radians(d)) + 1.0) / 2.0

    return sorted(colours, key=warmth, reverse=True)


def _is_achromatic(img) -> bool:
    """True when the image is predominantly greyscale (B&W cover art).

    Samples every 4th pixel of the already-small 64×64 image for speed,
    then checks whether average HSV saturation is below a low threshold.
    """
    pixels = list(img.getdata())
    sample = pixels[::4] if len(pixels) > 64 else pixels
    if not sample:
        return False
    avg_sat = sum(_rgb_to_hsv(r, g, b)[1] for r, g, b in sample) / len(sample)
    return avg_sat < 0.10


def _darken(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Multiply all channels by *factor* (0 = black, 1 = unchanged)."""
    r, g, b = rgb
    return (int(r * factor), int(g * factor), int(b * factor))


# ── Colour math helpers ─────────────────────────────────────────────────────────

def _rgb_to_hsv(r: int, g: int, b: int) -> tuple[float, float, float]:
    r_, g_, b_ = r / 255.0, g / 255.0, b / 255.0
    cmax  = max(r_, g_, b_)
    cmin  = min(r_, g_, b_)
    delta = cmax - cmin
    v     = cmax
    s     = 0.0 if cmax == 0.0 else delta / cmax
    if delta == 0.0:
        h = 0.0
    elif cmax == r_:
        h = ((g_ - b_) / delta) % 6.0 / 6.0
    elif cmax == g_:
        h = ((b_ - r_) / delta + 2.0) / 6.0
    else:
        h = ((r_ - g_) / delta + 4.0) / 6.0
    return h, s, v


def _lerp_hex(a: str, b: str, t: float) -> str:
    ar, ag, ab = _parse_hex(a)
    br, bg, bb = _parse_hex(b)
    return _to_hex(
        int(ar + (br - ar) * t),
        int(ag + (bg - ag) * t),
        int(ab + (bb - ab) * t),
    )


def _parse_hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"
