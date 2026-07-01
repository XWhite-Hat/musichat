"""
Pre-launch system dependency checks.

Called early in main() — after config is loaded, before any service is started.
Returns a list of Missing entries describing what's absent and which feature it
affects.  The caller decides whether to block or warn-and-continue.
"""

from __future__ import annotations

import importlib
import shutil
from dataclasses import dataclass

# Swap for the real URL once the setup guide is published.
SETUP_DOCS_URL = "https://github.com/xwhitehat/musichat/blob/main/docs/SETUP.md"


@dataclass
class Missing:
    label:   str   # short name shown in the list
    feature: str   # which app feature is affected


def _module(import_name: str, label: str, feature: str, out: list) -> None:
    try:
        importlib.import_module(import_name)
    except ImportError:
        out.append(Missing(label, feature))


def _binary(cmd: str, label: str, feature: str, out: list) -> None:
    if not shutil.which(cmd):
        out.append(Missing(label, feature))


def _audio_output(out: list) -> None:
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        if isinstance(devices, dict):
            devices = [devices]
        if not any(d.get("max_output_channels", 0) > 0 for d in devices):
            out.append(Missing(
                "audio output device",
                "music playback — no output device detected",
            ))
    except Exception as exc:
        out.append(Missing(
            "audio output device",
            f"music playback — could not query devices ({exc})",
        ))


def check(cfg=None) -> list[Missing]:
    """
    Run all checks and return a (possibly empty) list of Missing items.

    cfg — AppConfig instance; enables tunnel-binary checks based on tunnel_mode.
    """
    missing: list[Missing] = []

    _module("yt_dlp",      "yt-dlp",             "music search and playback",      missing)
    _module("av",          "PyAV",                "audio decoding",                 missing)
    _module("sounddevice", "sounddevice/PortAudio","audio playback",                missing)

    _audio_output(missing)

    if cfg is not None:
        mode = (getattr(getattr(cfg, "server", None), "tunnel_mode", None) or "none")
        if mode == "cloudflare":
            _binary("cloudflared", "cloudflared", "Cloudflare tunnel (remote mod panel access)", missing)
        elif mode == "ngrok":
            _binary("ngrok",       "ngrok",       "ngrok tunnel (remote mod panel access)",      missing)
        elif mode == "tailscale":
            _binary("tailscale",   "Tailscale",   "Tailscale tunnel (remote mod panel access)",  missing)

    return missing
