"""
Track resolver — converts a URL or search query into a populated Track object.

Uses yt-dlp --dump-single-json to fetch title, artist, duration, and thumbnail
BEFORE the track is enqueued.  This means the now-playing card always has
correct metadata from the first moment a track starts, even for chat requests.

Never call from the Qt main thread — yt-dlp can take several seconds.
"""

from __future__ import annotations

import unicodedata

from player.net_guard import is_fetchable, is_internal_host
from player.queue_manager import RequestOrigin, Track, TrackSource

# ── Source detection ───────────────────────────────────────────────────────────

_URL_PREFIXES = ("http://", "https://", "www.")
_SC_DOMAINS   = ("soundcloud.com",)
_YT_DOMAINS   = ("youtube.com", "youtu.be", "music.youtube.com")

# Unicode categories that are always invisible or non-printing: control
# (Cc), format (Cf — zero-width space/joiners, tag characters), private-use
# and surrogate (Co, Cs), and combining marks (Mn, Mc, Me).  Twitch chat
# users/bots commonly append one of these — most often a combining grapheme
# joiner or zero-width space — to dodge Twitch's duplicate-message filter.
# str.strip() only removes whitespace, so it rides straight through into the
# yt-dlp search string and makes an otherwise-valid request return zero
# results.  Chat song requests are exactly the case where a stray character
# has no legitimate meaning worth preserving, so we strip the whole class.
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Mn", "Mc", "Me"}


def _sanitize_query(query: str) -> str:
    cleaned = "".join(
        ch for ch in query if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
    )
    return cleaned.strip()


def _is_url(query: str) -> bool:
    return query.startswith(_URL_PREFIXES)


# ── Local files ────────────────────────────────────────────────────────────────
# Only the streamer, acting through the app's own UI, may play files from disk.
# A path arriving from Twitch chat or a channel-point redemption would let a
# viewer enumerate and play arbitrary files off the streamer's machine, live on
# stream — so origin is checked before the filesystem is ever touched.
_LOCAL_ALLOWED_ORIGINS = frozenset({RequestOrigin.MANUAL})

# Origins whose request text comes from an untrusted viewer.  These get the
# allowlist treatment in net_guard; MANUAL (the streamer's own UI) gets the
# looser internal-address denylist.
_VIEWER_ORIGINS = frozenset({
    RequestOrigin.CHAT,
    RequestOrigin.CHANNEL_POINTS,
    RequestOrigin.SUGGESTION,
})


def _looks_like_local_path(query: str) -> bool:
    """True if *query* looks like a filesystem path rather than a URL or search.

    Deliberately generous: anything that might be a path is routed to the local
    branch, where the origin gate then decides whether it is allowed.  Being
    liberal here and strict at the gate is safer than the reverse, which would
    let an unusual path shape slip through to a code path with no gate at all.
    """
    if not query or _is_url(query):
        return False
    q = query.strip().strip('"')
    if q.startswith("file://"):
        return True
    if q.startswith(("\\\\", "/", "~")):        # UNC, POSIX absolute, home
        return True
    # Windows drive letter, e.g. C:\ or D:/
    if len(q) >= 3 and q[1] == ":" and q[2] in ("\\", "/") and q[0].isalpha():
        return True
    # Any backslash, or a ".." traversal segment.  Both are effectively absent
    # from real search queries but present in every relative path, so this
    # routes things like "..\..\Windows\System32\config\SAM" to the origin
    # gate instead of letting them fall through and be searched on YouTube.
    #
    # A bare "/" deliberately does NOT qualify: plenty of legitimate searches
    # contain one ("AC/DC back in black").
    if "\\" in q or q.startswith("..") or "/../" in q or "\\..\\" in q:
        return True
    # A bare filename with a known audio extension, e.g. "song.mp3"
    from player.local_media import is_supported_file
    return is_supported_file(q)


def _normalise_local_path(query: str) -> str:
    """Strip quoting and any file:// scheme, returning a plain path."""
    import urllib.parse
    import urllib.request

    q = query.strip().strip('"')
    if q.startswith("file://"):
        try:
            return urllib.request.url2pathname(urllib.parse.urlparse(q).path)
        except Exception:
            return q
    return q


def _source_from_url(url: str) -> TrackSource:
    for d in _SC_DOMAINS:
        if d in url:
            return TrackSource.SOUNDCLOUD
    return TrackSource.YOUTUBE


# ── URL kind ───────────────────────────────────────────────────────────────────
# A pasted link is not necessarily a single track.  Handing a playlist or
# channel URL straight to full extraction is what made the app appear to hang:
# yt-dlp resolves every item, which took over 90 seconds for one playlist
# (measured) while holding the global yt-dlp lock — blocking playback of the
# current track as well as every other request.  Classify first, enumerate
# cheaply, then resolve exactly one video.

# Channel forms: /channel/UC..., /c/Name, /user/Name, /@handle
_YT_CHANNEL_MARKERS = ("/channel/", "/c/", "/user/", "/@")
# Tabs that already point at a listing rather than the channel root.
_YT_CHANNEL_TABS = ("/videos", "/streams", "/shorts", "/featured", "/playlists")


def _url_kind(url: str) -> str:
    """Return "video", "playlist" or "channel" for *url*."""
    low = url.lower()

    if "soundcloud.com" in low:
        # /sets/ is SoundCloud's playlist form.  A bare profile URL has a single
        # path segment and no track slug.
        if "/sets/" in low:
            return "playlist"
        path = low.split("soundcloud.com/", 1)[-1].strip("/")
        if path and "/" not in path:
            return "channel"
        return "video"

    # A watch URL that also carries list= is still a video request — the user
    # clicked a track that happened to be inside a playlist.
    if "/watch?" in low or "youtu.be/" in low or "/shorts/" in low:
        return "video"
    if "/playlist" in low or "list=" in low:
        return "playlist"
    if any(m in low for m in _YT_CHANNEL_MARKERS):
        return "channel"
    return "video"


def _channel_listing_url(url: str) -> str:
    """Point a channel URL at its video listing.

    A channel root flat-extracts to its *tabs* (Videos, Shorts, Live), so the
    first entry is itself a playlist rather than a video — which is how a
    channel link previously produced a Track whose stream URL was a channel ID.
    Asking for /videos directly yields real videos.
    """
    trimmed = url.split("?", 1)[0].rstrip("/")
    if any(trimmed.lower().endswith(t) for t in _YT_CHANNEL_TABS):
        return trimmed
    return f"{trimmed}/videos"


def _is_playable_entry(entry: dict) -> bool:
    """True if a flat entry looks like a single playable video."""
    if not isinstance(entry, dict):
        return False
    if entry.get("_type") == "playlist":       # a nested tab/collection
        return False
    if not (entry.get("url") or entry.get("id")):
        return False
    # Upcoming premieres and live streams have no fixed content to play.
    return entry.get("live_status") not in ("is_upcoming", "is_live")


def _entry_target(entry: dict) -> str:
    """Best URL for resolving a flat entry."""
    url = entry.get("url") or ""
    if url.startswith(("http://", "https://")):
        return url
    vid = entry.get("id") or ""
    return f"https://www.youtube.com/watch?v={vid}" if vid else url


# ── Public API ─────────────────────────────────────────────────────────────────

def resolve(
    query: str,
    requested_by: str = "",
    origin: RequestOrigin = RequestOrigin.CHAT,
) -> Track | None:
    """
    Resolve a URL or plain-text search query to a Track with full metadata.

    For URLs: fetches metadata for that specific video/track.
    For plain text: performs a YouTube search and takes the first result.

    Returns None on failure (network error, yt-dlp not found, no results).
    """
    query = _sanitize_query(query)
    if not query:
        return None

    if _looks_like_local_path(query):
        return _resolve_local(query, requested_by, origin)

    if not _is_url(query):
        # Plain text — YouTube search, first result.
        data = _dump_single(f"ytsearch1:{query}")
        if data is None:
            print(f"[resolver] no search results for: {query!r}")
            return None
        return _data_to_track(data, query, TrackSource.YOUTUBE, requested_by, origin)

    # Gate the URL before yt-dlp sees it.  yt-dlp fetches the page to sniff for
    # media, and the resolved stream is later opened by FFmpeg — so an
    # unchecked URL here turns a chat message into two requests from the
    # streamer's machine to a host of the viewer's choosing.
    viewer_supplied = origin in _VIEWER_ORIGINS
    allowed, why = is_fetchable(query, viewer_supplied)
    if not allowed:
        print(f"[resolver] refused URL from origin={origin.name}: {why}")
        return None

    source = _source_from_url(query)
    kind   = _url_kind(query)

    if kind == "video":
        data = _dump_single(query)
        if data is None:
            print(f"[resolver] could not resolve link: {query!r}")
            return None
        return _data_to_track(data, query, source, requested_by, origin)

    # Playlist or channel: enumerate cheaply, choose ONE entry, resolve just it.
    target = _channel_listing_url(query) if kind == "channel" else query
    from player.ytdlp_util import flat_entries
    entries = [e for e in flat_entries(target) if _is_playable_entry(e)]
    if not entries:
        print(f"[resolver] {kind} had no playable entries: {query!r}")
        return None

    if kind == "channel":
        # "Give me something from this channel" — the most-viewed item is the
        # best proxy for what a viewer actually meant.  view_count comes back
        # on flat entries for free, so this costs nothing extra.
        entries.sort(key=lambda e: e.get("view_count") or 0, reverse=True)

    chosen = entries[0]
    data = _dump_single(_entry_target(chosen))
    if data is None:
        print(f"[resolver] could not resolve chosen entry from {kind}: {query!r}")
        return None
    return _data_to_track(data, query, source, requested_by, origin)


def _resolve_local(
    query: str,
    requested_by: str,
    origin: RequestOrigin,
) -> Track | None:
    """Build a Track from a file on disk, if this origin is permitted to."""
    if origin not in _LOCAL_ALLOWED_ORIGINS:
        # Logged rather than silent: a chat request for a local path is either a
        # probe worth seeing, or a viewer confused about what the bot accepts.
        print(
            f"[resolver] refused local path from origin={origin.name} "
            f"requested_by={requested_by!r} — local files are streamer-only"
        )
        return None

    from player.local_media import probe

    path = _normalise_local_path(query)
    data = probe(path)
    if data is None:
        print(f"[resolver] not a readable audio file: {path!r}")
        return None

    return Track(
        title=data["title"],
        artist=data["artist"],
        url=data["filepath"],
        stream_url=data["filepath"],
        thumbnail_url="",          # embedded art is handled separately
        duration_seconds=data["duration"],
        source=TrackSource.LOCAL,
        origin=origin,
        requested_by=requested_by,
    )


def _dump_single(target: str) -> dict | None:
    """Resolve one video (or a one-result search) to a full info dict.

    Returns None unless the result is a single playable item — a collection
    that slipped through classification is rejected here rather than being
    turned into a Track whose stream URL points at a playlist or channel.
    """
    from player.ytdlp_util import dump_info
    data = dump_info(target)
    if data is None:
        return None

    # A search wraps its results in a one-entry playlist.
    if data.get("_type") == "playlist":
        entries = [e for e in (data.get("entries") or []) if e]
        if not entries:
            return None
        data = entries[0]

    # Still a collection?  Refuse rather than build an unplayable Track.
    if not isinstance(data, dict) or data.get("_type") == "playlist":
        print(f"[resolver] expected a single video, got a collection: {target!r}")
        return None
    return data


# ── Internal ───────────────────────────────────────────────────────────────────

def _data_to_track(
    data: dict,
    original_query: str,
    source: TrackSource,
    requested_by: str,
    origin: RequestOrigin,
) -> Track | None:
    """Convert a yt-dlp info dict to a Track, or None if it must not be played."""
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

    # Second gate, on the URL that will actually be opened.  The input was
    # checked before yt-dlp ran, but yt-dlp and FFmpeg both follow redirects,
    # so the resolved target can differ from what was requested.  Rejecting an
    # internal stream_url here closes that gap without having to police every
    # hop of the chain.
    if stream_url.startswith(("http://", "https://")) and is_internal_host(stream_url):
        print("[resolver] resolved stream points at internal address space — refusing")
        return None

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
