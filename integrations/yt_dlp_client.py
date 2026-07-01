"""
yt-dlp based search and URL resolution for YouTube and SoundCloud.
"""

from __future__ import annotations

import re as _re
from typing import Optional

from player.queue_manager import Track, TrackSource
from player.ytdlp_util import _YTDLP_LOCK

# YouTube auto-generates "Artist - Topic" channels for official music uploads.
# The suffix is always ASCII hyphen (or occasionally en-dash) followed by " Topic".
_TOPIC_SUFFIX = _re.compile(r'\s*[-–]\s*Topic\s*$', _re.IGNORECASE)


def _is_topic_entry(e: dict) -> bool:
    """True when the entry comes from a YouTube auto-generated Topic channel."""
    ch = (e.get("channel") or e.get("uploader") or "").strip()
    return bool(_TOPIC_SUFFIX.search(ch))


def _has_playable_url(e: dict) -> bool:
    """True when the entry points to a single resolvable video (not a bundle/channel page)."""
    url    = e.get("webpage_url") or e.get("url") or ""
    vid_id = e.get("id") or ""
    # A valid video URL contains a watch path, or we have an 11-char video ID
    has_watch_url = "watch?v=" in url or "youtu.be/" in url or "soundcloud.com" in url
    has_video_id  = len(vid_id) == 11 and vid_id.replace("-", "").replace("_", "").isalnum()
    return has_watch_url or has_video_id

_YDL_OPTS_SEARCH = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "skip_download": True,
}

_YDL_OPTS_RESOLVE = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
}


def _source_from_url(url: str) -> TrackSource:
    if "soundcloud.com" in url:
        return TrackSource.SOUNDCLOUD
    return TrackSource.YOUTUBE


def search_youtube(query: str, max_results: int = 10) -> list[Track]:
    with _YTDLP_LOCK:
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(_YDL_OPTS_SEARCH) as ydl:
                info = ydl.extract_info(
                    f"ytsearch{max_results}:{query}", download=False
                )
            entries = [e for e in (info.get("entries") or []) if e and _has_playable_url(e)]
            # Stable-sort: Topic-channel entries first (official music recordings),
            # then YouTube's own relevance order within each group.
            entries.sort(key=lambda e: 0 if _is_topic_entry(e) else 1)
            return [_entry_to_track(e) for e in entries]
        except Exception as e:
            print(f"[yt-dlp] youtube search error: {e}")
            return []


def search_soundcloud(query: str, max_results: int = 10) -> list[Track]:
    with _YTDLP_LOCK:
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(_YDL_OPTS_SEARCH) as ydl:
                info = ydl.extract_info(
                    f"scsearch{max_results}:{query}", download=False
                )
            return [_entry_to_track(e) for e in (info.get("entries") or []) if e]
        except Exception as e:
            print(f"[yt-dlp] soundcloud search error: {e}")
            return []


def _is_internal_host(netloc: str) -> bool:
    import ipaddress as _ip
    import socket as _sock
    host = netloc.split(":")[0]
    try:
        addr = _ip.ip_address(_sock.gethostbyname(host))
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except Exception:
        return False


def resolve_url(url: str) -> Optional[Track]:
    """Resolve any YouTube or SoundCloud URL into a playable Track."""
    from urllib.parse import urlparse as _urlparse
    try:
        _p = _urlparse(url)
        if _p.scheme not in ("http", "https") or not _p.netloc:
            return None
        if _is_internal_host(_p.netloc):
            return None
    except Exception:
        return None
    with _YTDLP_LOCK:
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(_YDL_OPTS_RESOLVE) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                return _entry_to_track(info)
        except Exception as e:
            print(f"[yt-dlp] resolve error: {e}")
        return None


def get_stream_info(url: str) -> Optional[dict]:
    """
    Return {'url': str, 'headers': dict} for the best audio stream.
    Headers must be forwarded to PyAV/FFmpeg so CDN servers don't reject
    the request (YouTube signs URLs for a specific client User-Agent).
    """
    with _YTDLP_LOCK:
        try:
            import yt_dlp
            opts = {**_YDL_OPTS_RESOLVE, "format": "bestaudio/best"}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                if "entries" in info:
                    info = info["entries"][0]

                # Pick the best audio stream entry
                chosen = None
                direct = info.get("url")
                if direct:
                    chosen = info
                else:
                    formats = info.get("formats") or []
                    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("url")]
                    chosen = audio[-1] if audio else (formats[-1] if formats else None)

                if not chosen or not chosen.get("url"):
                    return None

                return {
                    "url": chosen["url"],
                    "headers": chosen.get("http_headers") or info.get("http_headers") or {},
                }
        except Exception as e:
            print(f"[yt-dlp] stream info error: {e}")
        return None


def get_stream_url(url: str) -> Optional[str]:
    """Convenience wrapper — returns URL only (no headers)."""
    info = get_stream_info(url)
    return info["url"] if info else None


def get_suggestions(seed: "Track", count: int = 10) -> list["Track"]:
    """
    Fetch vibe-matched suggestions for a seed track via YouTube's auto-radio.

    YouTube generates a continuous mix playlist for any video at
    ``watch?v=ID&list=RDID``.  yt-dlp extracts the playlist entries without
    needing a Data API key, giving us organic vibe-matched tracks.

    Falls back to a title+artist search query if the seed URL has no
    extractable video ID (e.g. SoundCloud seeds).
    """
    try:
        import yt_dlp

        # Prefer the YouTube page URL (contains ?v=<id>); the stream_url is
        # a CDN audio URL that won't have a recognisable video ID in it.
        vid_id: Optional[str] = None
        for _seed_url in (seed.url, seed.stream_url):
            if not _seed_url:
                continue
            m = _re.search(r'[?&]v=([a-zA-Z0-9_-]{11})(?:[^a-zA-Z0-9_-]|$)', _seed_url)
            if m:
                vid_id = m.group(1)
                break

        if vid_id:
            radio_url = f"https://www.youtube.com/watch?v={vid_id}&list=RD{vid_id}"
            opts = {
                **_YDL_OPTS_SEARCH,
                "playlistend": count + 2,   # +1 seed + 1 buffer
            }
            with _YTDLP_LOCK:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(radio_url, download=False)
            entries = info.get("entries") or []
            # Skip the first entry — it's the seed itself
            results = [_entry_to_track(e) for e in entries[1:] if e]
            if results:
                return results[:count]

        # Fallback: search by seed title + artist
        query = f"{seed.artist} {seed.title}".strip() or seed.title
        return search_youtube(query, max_results=count) if query else []

    except Exception as e:
        print(f"[yt-dlp] get_suggestions error: {e}")
        return []


def _entry_to_track(e: dict) -> Track:
    url    = e.get("webpage_url") or e.get("url") or ""
    vid_id = e.get("id") or ""

    # Normalise music.youtube.com → www.youtube.com so the engine's _NEEDS_RESOLVE
    # check matches reliably and the video ID is always in the standard watch URL.
    url = url.replace("music.youtube.com", "www.youtube.com")

    # Flat-extracted entries sometimes return a bare video ID rather than a full
    # watch URL.  Reconstruct the canonical URL so the engine's yt-dlp resolver
    # is triggered correctly at play time.
    if vid_id and "watch?v=" not in url and "youtu.be/" not in url and "soundcloud.com" not in url:
        url = f"https://www.youtube.com/watch?v={vid_id}"

    source = _source_from_url(url)

    # Store the webpage URL; actual audio stream is resolved at play time
    stream_url = url

    thumbnail = e.get("thumbnail") or ""
    if not thumbnail and e.get("thumbnails"):
        thumbnail = e["thumbnails"][-1].get("url", "")

    # YouTube Music populates a dedicated 'artist' field; regular YouTube only
    # has 'uploader' / 'channel'.  Check all three so both paths get an artist.
    raw_artist = (
        e.get("artist") or e.get("uploader") or e.get("channel") or ""
    )
    artist = _TOPIC_SUFFIX.sub("", raw_artist).strip()

    return Track(
        title=e.get("title") or e.get("fulltitle") or "",
        artist=artist,
        url=url,
        stream_url=stream_url,
        thumbnail_url=thumbnail,
        duration_seconds=int(e.get("duration") or 0),
        source=source,
    )
