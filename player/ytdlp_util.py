"""
yt-dlp invocation via the Python API.

Replaces the former `subprocess [sys.executable, "-m", "yt_dlp", ...]` pattern,
which breaks in a frozen PyInstaller binary because sys.executable is the app
.exe rather than a Python interpreter.  The Python API works identically in
both frozen and non-frozen environments.
"""

from __future__ import annotations

import threading
from typing import Optional

# yt-dlp is not safe to call concurrently from multiple threads within the same
# process — it uses global extractor state and module-level caches that race.
# All three entry points below acquire this lock so playlist imports, track
# resolution, and metadata dumps are serialised.
_YTDLP_LOCK = threading.Lock()


def resolve_direct_url(page_url: str) -> str:
    """
    Return the direct audio stream URL for page_url, or '' on failure.

    Equivalent to:
        yt-dlp --format bestaudio/best --no-playlist --get-url URL
    """
    with _YTDLP_LOCK:
        try:
            import yt_dlp  # noqa: PLC0415
            opts = {
                "format":     "bestaudio/best",
                "noplaylist": True,
                "quiet":      True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(page_url, download=False)
            if not info:
                return ""
            if "entries" in info:          # playlist accidentally matched
                info = info["entries"][0]
            url = info.get("url", "")
            if not url:
                fmts = info.get("requested_formats") or info.get("formats") or []
                url = fmts[0].get("url", "") if fmts else ""
            return url
        except Exception as exc:
            print(f"[ytdlp_util] resolve_direct_url failed: {exc}")
            return ""


def resolve_playlist(url: str) -> Optional[dict]:
    """
    Fetch playlist entries via flat extraction (fast — no per-video resolve).

    Returns the yt-dlp info dict (has 'title' and 'entries') when url points to
    a valid playlist, None otherwise.  Uses ignoreerrors so deleted/private
    entries are kept in the list with placeholder titles rather than aborting.
    """
    with _YTDLP_LOCK:
        try:
            import yt_dlp  # noqa: PLC0415
            opts = {
                "quiet":         True,
                "no_warnings":   True,
                "ignoreerrors":  True,
                "extract_flat":  True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info or info.get("_type") not in ("playlist", "multi_video"):
                return None
            return info
        except Exception as exc:
            print(f"[ytdlp_util] resolve_playlist failed: {exc}")
            return None


def dump_info(query: str, no_playlist: bool = True) -> Optional[dict]:
    """
    Return the yt-dlp info dict for a URL or search query, or None on failure.

    Equivalent to:
        yt-dlp --dump-single-json [--no-playlist] QUERY
    """
    with _YTDLP_LOCK:
        try:
            import yt_dlp  # noqa: PLC0415
            opts = {
                "quiet":      True,
                "no_warnings": True,
                "noplaylist": no_playlist,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(query, download=False)
        except Exception as exc:
            print(f"[ytdlp_util] dump_info failed: {exc}")
            return None
