"""
Queue manager — owns the ordered list of tracks and exposes signals
that the UI and Twitch bot both consume.

Track lifecycle:
  pending → playing → history

YouTube suggestion expansion runs here when the queue falls below
`suggestion_threshold` and suggestion mode is enabled.
"""

from __future__ import annotations

import re as _re
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional


# Splits a title on its first "Artist - Song" separator.
_TITLE_SEP = _re.compile(r"\s*[-–—]\s+")

# Splits a name string ("Artist1, Artist2 feat. Artist3") into individual names.
_ARTIST_SEP = _re.compile(
    r"\s*,\s*"          # comma
    r"|\s+&\s+"          # ampersand
    r"|\s+x\s+"          # x  (collab notation)
    r"|\s+feat\.?\s+"    # feat. / feat
    r"|\s+ft\.?\s+"      # ft.  / ft
    r"|\s+and\s+",       # and
    _re.IGNORECASE,
)

# Ornamental / CJK / guillemet bracket pairs used in YouTube genre tags like
# ❰Chillstep❱, 【Lofi】, 《Ambient》 etc.  Translate them to plain ASCII [ ]
# before the noise regexes run so a single pattern covers everything.
_FANCY_OPEN  = '❰⟨【〔〖〘〚《「〈«❮❴｢'
_FANCY_CLOSE = '❱⟩】〕〗〙〛》」〉»❯❵｣'
_FANCY_BRACKETS = str.maketrans(_FANCY_OPEN + _FANCY_CLOSE,
                                '[' * len(_FANCY_OPEN) + ']' * len(_FANCY_CLOSE))

# YouTube title noise in square/round brackets: [Official Music Video], (HD), etc.
# Uses [^\[\]\(\)]* so it does NOT match if the content itself contains brackets —
# see _COPYRIGHT_NOISE below for a complementary pass that handles those.
_BRACKET_NOISE = _re.compile(r"\s*[\[\(][^\[\]\(\)]*[\]\)]\s*")

# Copyright / licence annotations — common on royalty-free music uploads.
# Examples: "(Copyright Free)", "[No Copyright Music]", "(Copyright © 2024 NCS)"
# Uses [^)]* / [^\]]* so nested brackets of the *other* type are fine, e.g.
# "(Copyright Free [Download])" is matched by the paren variant.
_COPYRIGHT_NOISE = _re.compile(
    r"\s*\([^)]*\bcopyright\b[^)]*\)\s*"
    r"|\s*\[[^\]]*\bcopyright\b[^\]]*\]\s*",
    _re.IGNORECASE,
)

# Leading track-number prefix: "01. ", "3) ", "02 - ", "4 – "
_TRACK_NUM_PREFIX = _re.compile(r"^\d{1,3}(?:[\.\)]|(?:\s*[-–—]))\s+")

# YouTube auto-generated "Artist - Topic" channel names
_TOPIC_SUFFIX = _re.compile(r"\s*[-–]\s*Topic\s*$", _re.IGNORECASE)


def _clean_raw_title(title: str) -> str:
    """Strip common YouTube/album title noise before artist-credit parsing."""
    # Normalise ornamental brackets (❰❱, 【】, 《》 …) to plain ASCII so the
    # noise regexes below don't need separate patterns for each Unicode pair.
    t = title.translate(_FANCY_BRACKETS)
    # Copyright annotations first (handles nested brackets the general pass misses)
    t = _COPYRIGHT_NOISE.sub(" ", t).strip()
    # Remove remaining bracket/paren annotation groups
    t = _BRACKET_NOISE.sub(" ", t).strip()
    # Remove leading track number ("01. ", "3) ", "02 - ")
    t = _TRACK_NUM_PREFIX.sub("", t).strip()
    # Collapse any double spaces left by the removals
    t = _re.sub(r"  +", " ", t)
    return t or title  # never return empty


def _merge_credited_artists(title: str, artist: str) -> tuple[str, str]:
    """
    Detect artist credits embedded in a track title and merge them with the
    track's artist metadata.

    Returns ``(display_artist, clean_title)``.

    Many tracks on YouTube / SoundCloud embed all collaborating artists in
    the title because the metadata only holds the uploading account::

        title  = "KoruSe, mzmff - Two Different Worlds"
        artist = "KoruSe"   # only the uploader

    would otherwise display as "KoruSe — KoruSe, mzmff - Two Different Worlds".
    This function detects the pattern and returns::

        ("KoruSe, mzmff", "Two Different Worlds")

    Algorithm
    ---------
    1.  Split the title on its **first** separator (`` - ``, `` – ``, `` — ``).
    2.  Parse individual names from the left (title credit) and from *artist*
        by splitting on ``,``, ``&``, ``x``, ``feat.``, ``ft.``, or ``and``.
    3.  **Overlap guard** — only proceed when at least one name appears in both
        lists (case-insensitive equality).  This prevents false splits on song
        titles that happen to contain a dash (e.g. "Back to Back - Freestyle"
        with artist "Drake" has no name overlap → returns unchanged).
    4.  Merge: left-side names take precedence (they may be the more complete
        list), then any extra names from metadata are appended.

    Limitations
    -----------
    The group-uploaded-by-member case ("BTS - Dynamite" uploaded by Jung Kook)
    cannot be resolved by string analysis alone — there's no textual link
    between "Jung Kook" and "BTS", so the function falls through gracefully
    and returns the input unchanged.
    """
    # Strip YouTube auto-generated channel suffix before any other processing.
    # Handles both newly resolved tracks and old playlist entries stored with
    # the raw channel name (e.g. "Xaon - Topic" → "Xaon").
    artist = _TOPIC_SUFFIX.sub("", artist).strip()

    if not title:
        return artist, title

    # Strip bracket noise and leading track numbers before any other analysis.
    title = _clean_raw_title(title)

    m = _TITLE_SEP.search(title)
    if not m:
        return artist, title

    left  = title[: m.start()].strip()
    right = title[m.end() :].strip()

    if not left or not right:
        return artist, title

    left_names   = [n.strip() for n in _ARTIST_SEP.split(left)   if n and n.strip()]
    right_names  = [n.strip() for n in _ARTIST_SEP.split(right)  if n and n.strip()]
    artist_names = [n.strip() for n in _ARTIST_SEP.split(artist) if n and n.strip()]

    if not artist_names:
        # No metadata artist — use a naive first-separator split so the card
        # shows two lines rather than one long title.  "Artist - Song" is the
        # dominant YouTube title convention so false positives are rare.
        if left and right:
            return left, right
        return artist, title

    left_flat   = {n.lower() for n in left_names}
    right_flat  = {n.lower() for n in right_names}
    artist_flat = {n.lower() for n in artist_names}

    def _merge(credit_names: list[str], credit_flat: set[str]) -> str:
        seen   = set(credit_flat)
        merged = list(credit_names)
        for name in artist_names:
            if name.lower() not in seen:
                merged.append(name)
                seen.add(name.lower())
        return ", ".join(merged)

    if left_flat & artist_flat:
        # "Artist — Song" format (most common on YouTube uploads)
        return _merge(left_names, left_flat), right

    if right_flat & artist_flat:
        # "Song — Artist" format (common in album rips and SoundCloud)
        return _merge(right_names, right_flat), left

    # No name overlap — title separator is part of the song name, not a credit.
    return artist, title


class TrackSource(Enum):
    YOUTUBE = auto()
    SOUNDCLOUD = auto()
    LOCAL = auto()


class RequestOrigin(Enum):
    MANUAL = auto()       # streamer added directly
    CHAT = auto()         # Twitch chat !songrequest
    CHANNEL_POINTS = auto()
    SUGGESTION = auto()   # auto-expanded by YouTube suggestion engine


@dataclass
class Track:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    artist: str = ""
    url: str = ""                    # original URL or search term
    stream_url: str = ""             # resolved playback URL / YouTube video ID
    thumbnail_url: str = ""
    duration_seconds: int = 0
    source: TrackSource = TrackSource.YOUTUBE
    origin: RequestOrigin = RequestOrigin.MANUAL
    requested_by: str = ""           # Twitch username or ""
    # Set True for vibe-engine auto-suggestions (one slot, overwritable by user requests).
    is_auto_suggestion: bool = False

    # Channel-points redemption metadata — only set for CHANNEL_POINTS origin.
    # Used to FULFILL the redemption when the song plays, or CANCEL (refund) on
    # !wrongsong / !whoops.  Both fields must be non-empty for updates to fire.
    redemption_id:        str = ""   # Twitch redemption UUID
    redemption_reward_id: str = ""   # Twitch reward UUID (required by the PATCH endpoint)

    def display_title(self) -> str:
        display_artist, clean_title = _merge_credited_artists(
            self.title or self.url, self.artist or ""
        )
        if display_artist:
            return f"{display_artist} — {clean_title}"
        return clean_title or self.url


class QueueManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[Track] = []
        self._history: list[Track] = []
        self._current: Optional[Track] = None

        # Callbacks — multiple listeners supported via list
        self.on_queue_changed: list[Callable[[], None]] = []
        self.on_track_started: Optional[Callable[[Track], None]] = None
        self.on_track_finished: Optional[Callable[[Track], None]] = None
        self.on_empty: Optional[Callable[[], None]] = None

        # Per-user request tracking — current queue count (for queue_limit cap)
        self._user_request_counts: dict[str, int] = {}
        # Per-stream session counter — total requests made this session (for max_per_stream cap)
        self._session_requests: dict[str, int] = {}

    # ── Read accessors ─────────────────────────────────────────────────────────

    @property
    def current(self) -> Optional[Track]:
        return self._current

    def snapshot(self) -> list[Track]:
        with self._lock:
            return list(self._queue)

    def history_snapshot(self) -> list[Track]:
        with self._lock:
            return list(reversed(self._history[-50:]))

    def length(self) -> int:
        with self._lock:
            return len(self._queue)

    def position_of(self, track_id: str) -> int:
        """1-based position, or -1 if not found."""
        with self._lock:
            for i, t in enumerate(self._queue):
                if t.id == track_id:
                    return i + 1
            return -1

    # ── Mutations ──────────────────────────────────────────────────────────────

    def enqueue(self, track: Track, position: Optional[int] = None) -> int:
        """Append or insert. Returns 1-based queue position."""
        with self._lock:
            if position is not None:
                idx = max(0, min(position - 1, len(self._queue)))
                self._queue.insert(idx, track)
            else:
                self._queue.append(track)
            pos = self._queue.index(track) + 1
        # Track per-stream totals for viewer request-cap enforcement
        if track.requested_by:
            self.record_session_request(track.requested_by)
        self._notify_changed()
        return pos

    def enqueue_request(self, track: Track) -> int:
        """Insert a user-requested track before any auto-suggestion entries.

        Auto-suggestions sit at the tail of the queue; human requests should
        always jump ahead of them so chat/channel-point requests feel instant
        rather than landing after a dozen vibe-engine suggestions.

        Returns the 1-based position among *non-auto* tracks (used for chat
        confirmation messages).
        """
        with self._lock:
            # Find the index of the first auto-suggestion
            insert_at = len(self._queue)  # default: append
            for i, t in enumerate(self._queue):
                if getattr(t, "is_auto_suggestion", False):
                    insert_at = i
                    break
            self._queue.insert(insert_at, track)
        if track.requested_by:
            self.record_session_request(track.requested_by)
        self._notify_changed()
        return self.human_position_of(track.id)

    def human_position_of(self, track_id: str) -> int:
        """1-based position counting only non-auto-suggestion entries, -1 if not found."""
        with self._lock:
            human_pos = 0
            for t in self._queue:
                if not getattr(t, "is_auto_suggestion", False):
                    human_pos += 1
                    if t.id == track_id:
                        return human_pos
        return -1

    def remove(self, track_id: str) -> bool:
        removed = False
        with self._lock:
            for i, t in enumerate(self._queue):
                if t.id == track_id:
                    self._queue.pop(i)
                    removed = True
                    break
        if removed:
            self._notify_changed()
        return removed

    def remove_last_by_user(self, username: str) -> Optional[Track]:
        """!wrongsong — remove the most recently queued track by this user."""
        removed: Optional[Track] = None
        # Comparison is case-insensitive — twitchio lowercases names but be safe.
        key = username.lower()
        with self._lock:
            for i in range(len(self._queue) - 1, -1, -1):
                if self._queue[i].requested_by.lower() == key:
                    removed = self._queue.pop(i)
                    break
        # _notify_changed() must be called OUTSIDE the lock — it calls length()
        # which re-acquires the lock, causing a deadlock on threading.Lock.
        if removed is not None:
            self._notify_changed()
        return removed

    def move(self, track_id: str, new_position: int) -> bool:
        moved = False
        with self._lock:
            for i, t in enumerate(self._queue):
                if t.id == track_id:
                    self._queue.pop(i)
                    idx = max(0, min(new_position - 1, len(self._queue)))
                    self._queue.insert(idx, t)
                    moved = True
                    break
        if moved:
            self._notify_changed()
        return moved

    def pop_next(self) -> Optional[Track]:
        """Called by the engine when it's ready for the next track."""
        with self._lock:
            if not self._queue:
                return None
            track = self._queue.pop(0)

        if self._current is not None:
            with self._lock:
                self._history.append(self._current)
            if self.on_track_finished:
                self.on_track_finished(self._current)

        self._current = track
        self._notify_changed()
        if self.on_track_started:
            self.on_track_started(track)
        return track

    def clear(self) -> None:
        """Remove all user-requested tracks but preserve any auto-suggestion slot.

        Vibe-engine suggestions are flagged with is_auto_suggestion=True.  Keeping
        them means the queue never goes fully empty just because a streamer hit
        "clear" — the next song is already lined up.
        """
        with self._lock:
            preserved = [t for t in self._queue if getattr(t, "is_auto_suggestion", False)]
            self._queue.clear()
            self._queue.extend(preserved)
        self._notify_changed()

    def skip(self) -> Optional[Track]:
        return self.pop_next()

    def set_current(self, track: Optional[Track]) -> None:
        """Update the current-track pointer without popping from the queue.

        Called by the engine when a track is started directly (not via pop_next),
        e.g. auto-start of the first song or a manual 'play now' action.  Fires
        _notify_changed only when the track actually changes so callers that go
        through pop_next (which already sets _current) don't get a double push.
        """
        if self._current is track:
            return
        if (self._current is not None and track is not None
                and self._current.id == track.id):
            return
        self._current = track
        self._notify_changed()

    # ── Per-user caps ──────────────────────────────────────────────────────────

    def user_request_count(self, username: str) -> int:
        return self._user_request_counts.get(username, 0)

    def increment_user_count(self, username: str) -> None:
        self._user_request_counts[username] = (
            self._user_request_counts.get(username, 0) + 1
        )

    def reset_user_counts(self) -> None:
        self._user_request_counts.clear()

    # ── Per-stream session caps ────────────────────────────────────────────────

    def get_session_request_count(self, username: str) -> int:
        """Total requests made by this user since the app started (per-stream proxy)."""
        return self._session_requests.get(username.lower(), 0)

    def record_session_request(self, username: str) -> None:
        """Increment the per-stream request counter.  Called from enqueue()."""
        key = username.lower()
        self._session_requests[key] = self._session_requests.get(key, 0) + 1

    def reset_session_counts(self) -> None:
        """Clear all per-stream counters (e.g. between streams)."""
        self._session_requests.clear()

    # ── Serialisation (for WebSocket push) ────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "current": self._track_dict(self._current),
            "queue": [self._track_dict(t) for t in self.snapshot()],
        }

    @staticmethod
    def _track_dict(t: Optional[Track]) -> Optional[dict]:
        if t is None:
            return None
        return {
            "id": t.id,
            "title": t.display_title(),
            "url": t.url,
            "thumbnail": t.thumbnail_url,
            "duration": t.duration_seconds,
            "source": t.source.name,
            "requested_by": t.requested_by,
        }

    # ── Internal ───────────────────────────────────────────────────────────────

    def _notify_changed(self) -> None:
        for cb in self.on_queue_changed:
            cb()
        if self.length() == 0 and self.on_empty:
            self.on_empty()
