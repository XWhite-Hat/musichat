"""
Track resolver — converts a URL or search query into a populated Track object.

Uses yt-dlp --dump-single-json to fetch title, artist, duration, and thumbnail
BEFORE the track is enqueued.  This means the now-playing card always has
correct metadata from the first moment a track starts, even for chat requests.

Never call from the Qt main thread — yt-dlp can take several seconds.
"""

from __future__ import annotations

from typing import Optional

from player.queue_manager import RequestOrigin, Track, TrackSource

# ── Source detection ───────────────────────────────────────────────────────────

_URL_PREFIXES = ("http://", "https://", "www.")
_SC_DOMAINS   = ("soundcloud.com",)
_YT_DOMAINS   = ("youtube.com", "youtu.be", "music.youtube.com")


def _is_url(query: str) -> bool:
    return query.startswith(_URL_PREFIXES)


def _source_from_url(url: str) -> TrackSource:
    for d in _SC_DOMAINS:
        if d in url:
            return TrackSource.SOUNDCLOUD
    return TrackSource.YOUTUBE


# ── Public API ─────────────────────────────────────────────────────────────────

def resolve(
    query: str,
    requested_by: str = "",
    origin: RequestOrigin = RequestOrigin.CHAT,
) -> Optional[Track]:
    """
    Resolve a URL or plain-text search query to a Track with full metadata.

    For URLs: fetches metadata for that specific video/track.
    For plain text: performs a YouTube search and takes the first result.

    Returns None on failure (network error, yt-dlp not found, no results).
    """
    if _is_url(query):
        target = query.strip()
        source = _source_from_url(target)
    else:
        # YouTube search — wrap in ytsearch prefix
        target = f"ytsearch1:{query.strip()}"
        source = TrackSource.YOUTUBE

    from player.ytdlp_util import dump_info
    data = dump_info(target)
    if data is None:
        print(f"[resolver] yt-dlp returned no output for: {query!r}")
        return None

    # Search results come back as a playlist with one entry.
    if data.get("_type") == "playlist":
        entries = data.get("entries") or []
        if not entries:
            print(f"[resolver] no search results for: {query!r}")
            return None
        data = entries[0]
        # Resolve the actual source from the result's URL
        webpage = data.get("webpage_url", "")
        source = _source_from_url(webpage) if webpage else source

    return _data_to_track(data, query, source, requested_by, origin)


# ── Internal ───────────────────────────────────────────────────────────────────

def _data_to_track(
    data: dict,
    original_query: str,
    source: TrackSource,
    requested_by: str,
    origin: RequestOrigin,
) -> Track:
    """Convert a yt-dlp info dict to a Track with metadata pre-populated."""
    # Canonical page URL — the engine re-resolves the direct stream URL at
    # playback time, so we store the stable page URL here, not the CDN URL.
    stream_url = (
        data.get("webpage_url")
        or data.get("original_url")
        or data.get("url")
        or original_query
    )

    title = (data.get("title") or "").strip() or "Unknown title"

    # Artist precedence: explicit artist field → uploader → channel name.
    # Strip YouTube auto-channel suffix so "Xaon - Topic" stores as "Xaon".
    import re as _re
    _TOPIC_RE = _re.compile(r"\s*[-–]\s*Topic\s*$", _re.IGNORECASE)
    artist = _TOPIC_RE.sub("", (
        (data.get("artist") or "").strip()
        or (data.get("uploader") or "").strip()
        or (data.get("channel") or "").strip()
    )).strip()

    thumbnail = data.get("thumbnail") or ""
    duration  = int(data.get("duration") or 0)

    return Track(
        title=title,
        artist=artist,
        url=original_query,
        stream_url=stream_url,
        thumbnail_url=thumbnail,
        duration_seconds=duration,
        source=source,
        origin=origin,
        requested_by=requested_by,
    )
