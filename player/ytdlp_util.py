"""
yt-dlp invocation via the Python API.

Replaces the former `subprocess [sys.executable, "-m", "yt_dlp", ...]` pattern,
which breaks in a frozen PyInstaller binary because sys.executable is the app
.exe rather than a Python interpreter.  The Python API works identically in
both frozen and non-frozen environments.
"""

from __future__ import annotations

import threading

# yt-dlp is not safe to call concurrently from multiple threads within the same
# process — it uses global extractor state and module-level caches that race.
# All three entry points below acquire this lock so playlist imports, track
# resolution, and metadata dumps are serialised.
_YTDLP_LOCK = threading.Lock()

# Every extraction runs while holding the lock above, so a slow call blocks
# playback too — resolve_direct_url() needs the same lock to start the next
# track.  These bounds keep any single call short.
_SOCKET_TIMEOUT = 15          # seconds per network operation
_FLAT_ENTRY_CAP = 60          # entries to enumerate from a playlist/channel

_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": _SOCKET_TIMEOUT,
}


def resolve_direct_url(page_url: str) -> str:
    """
    Return the direct audio stream URL for page_url, or '' on failure.

    Equivalent to:
        yt-dlp --format bestaudio/best --no-playlist --get-url URL
    """
    with _YTDLP_LOCK:
        try:
            import yt_dlp
            opts = {
                **_BASE_OPTS,
                "format":     "bestaudio/best",
                "noplaylist": True,
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


def resolve_playlist(url: str) -> dict | None:
    """
    Fetch playlist entries via flat extraction (fast — no per-video resolve).

    Returns the yt-dlp info dict (has 'title' and 'entries') when url points to
    a valid playlist, None otherwise.  Uses ignoreerrors so deleted/private
    entries are kept in the list with placeholder titles rather than aborting.
    """
    with _YTDLP_LOCK:
        try:
            import yt_dlp
            opts = {
                **_BASE_OPTS,
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


def dump_info(query: str, no_playlist: bool = True) -> dict | None:
    """
    Return the yt-dlp info dict for a URL or search query, or None on failure.

    Equivalent to:
        yt-dlp --dump-single-json [--no-playlist] QUERY
    """
    with _YTDLP_LOCK:
        try:
            import yt_dlp
            opts = {
                **_BASE_OPTS,
                "noplaylist": no_playlist,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(query, download=False)
        except Exception as exc:
            print(f"[ytdlp_util] dump_info failed: {exc}")
            return None


def flat_entries(url: str, limit: int = _FLAT_ENTRY_CAP) -> list[dict]:
    """
    List the items of a playlist or channel WITHOUT resolving each one.

    This exists because full extraction of a collection is catastrophically
    slow: a bare playlist URL takes over 90 seconds (measured), and every
    second of it is spent holding _YTDLP_LOCK — which also blocks
    resolve_direct_url(), so the currently-playing track cannot start the next
    one and the whole app appears to hang.  Flat extraction of the same
    playlist takes about 1.5 seconds.

    Entries come back as stubs: id/url/title/duration/view_count, no formats.
    Resolve the single chosen entry afterwards with dump_info().
    """
    with _YTDLP_LOCK:
        try:
            import yt_dlp
            opts = {
                **_BASE_OPTS,
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
                "playlistend":  limit,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                return []
            # ignoreerrors leaves None in place of unavailable items.
            return [e for e in (info.get("entries") or []) if e]
        except Exception as exc:
            print(f"[ytdlp_util] flat_entries failed for {url!r}: {exc}")
            return []
