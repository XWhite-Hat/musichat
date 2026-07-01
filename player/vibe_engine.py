"""
Vibe engine — auto-queues suggestions to keep the music going.

Rules
-----
- One auto-slot: at most one auto-suggestion is queued ahead at any time.
  A user song request immediately overwrites it.
- Threshold: auto-fill only kicks in after `suggestion_threshold` non-auto
  tracks have played since the last fill (so a fresh user queue isn't
  immediately polluted with suggestions).
- Replay penalty (vibe-match ON):
    • Hard-exclude the last 4 played URLs — they can never be re-suggested.
    • Beyond that: 10 % weight penalty per track played since last appearance,
      applied during weighted-random selection (max 90 % reduction at 9+
      tracks ago, then floored to 10 %).
- Lynchpin: the seed track used when fetching YT suggestions.
    • Non-playlist + vibe ON → locked to the track playing when vibe was
      enabled.  Toggling off then back on on a new song updates the lynchpin.
    • Playlist + vibe was ON when the playlist started → pick a random
      playlist track as seed each time a suggestion is needed.
    • Playlist + vibe toggled ON mid-playlist → current track at toggle time
      is the lynchpin (locked, same as non-playlist behaviour).
- Vibe OFF + playlist active → queue random playlist tracks (one slot,
  true-random, repeats allowed, replay penalty still applied).
- Vibe OFF + no playlist → nothing auto-fills; playback runs dry naturally.
"""

from __future__ import annotations

import random
import threading
from typing import TYPE_CHECKING, Optional

from player.queue_manager import QueueManager, Track

if TYPE_CHECKING:
    from player.playlist_manager import PlaylistTrack
    from config import YouTubeConfig


def _norm_artist(artist: str) -> str:
    """Lowercase + stripped artist name for penalty comparisons."""
    return (artist or "").lower().strip()


class VibeEngine:
    """Auto-queue manager.  Thread-safe; all public methods may be called from
    any thread.  Network I/O runs on a daemon worker thread to avoid blocking
    the Qt event loop."""

    def __init__(self, queue: QueueManager, cfg: "YouTubeConfig") -> None:
        self._queue = queue
        self._cfg = cfg
        self._lock = threading.Lock()

        # ── State ──────────────────────────────────────────────────────────────
        self._enabled: bool = False

        # Seed track for YT suggestions.  None means "pick from playlist".
        self._lynchpin: Optional[Track] = None

        # Playlist context
        self._playlist_tracks: list[PlaylistTrack] = []
        # True when vibe was already ON when the playlist was started —
        # in this mode we pick a random playlist track as seed each cycle.
        self._playlist_vibe_from_start: bool = False

        # Recently played stream_urls (oldest-first, capped at 50).
        # Used for the URL replay penalty and hard-exclude window.
        self._recent_urls: list[str] = []

        # Normalized artist names for the same plays (parallel list to _recent_urls).
        # Used to compute artist-recency penalties when rigidness < 1.0.
        self._artist_history: list[str] = []

        # ID of the currently queued auto-suggestion.
        self._auto_slot_id: Optional[str] = None

        # Non-auto tracks played since the last auto-fill.
        self._user_tracks_played: int = 0

        # Prevents more than one network fetch from being in flight at a time.
        # Without this, rapid toggles or a long playlist fire many _maybe_fill
        # threads that pile up waiting for _YTDLP_LOCK, starving the audio thread.
        self._fetch_lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    def on_vibe_toggled(self, enabled: bool, current_track: Optional[Track]) -> None:
        """Call when the vibe-match toggle changes state."""
        do_fill = False
        evict_id: Optional[str] = None
        with self._lock:
            self._enabled = enabled
            if enabled:
                if self._playlist_tracks and self._playlist_vibe_from_start:
                    # Was already in "random-from-playlist" mode.
                    # User toggled off/on mid-playlist → switch to current-track
                    # lynchpin (single-song mode, no threshold).
                    self._playlist_vibe_from_start = False
                self._lynchpin = current_track
                # Single-song lynchpin never uses the threshold, so no counter
                # manipulation needed — _maybe_fill will fill immediately.
                do_fill = True
            else:
                # Vibe turned off — evict any queued vibe suggestion so the
                # user isn't about to hear something they didn't ask for.
                # Save the ID and remove OUTSIDE the lock: _queue.remove() fires
                # _notify_changed which can call back into on_queue_cleared →
                # deadlock if we're still holding self._lock.
                if self._auto_slot_id:
                    evict_id = self._auto_slot_id
                    self._auto_slot_id = None
                # If a playlist is active, re-fill immediately with a random
                # playlist track.  Playlist-random fill also has no threshold,
                # so just schedule the fill directly.
                if self._playlist_tracks:
                    do_fill = True
        if evict_id:
            self._queue.remove(evict_id)
        if do_fill:
            threading.Thread(target=self._maybe_fill, daemon=True,
                             name="VibeEngine-fill").start()

    def on_playlist_started(
        self,
        tracks: list["PlaylistTrack"],
        vibe_enabled: bool,
    ) -> None:
        """Call when a playlist begins playback."""
        with self._lock:
            self._playlist_tracks = list(tracks)
            self._playlist_vibe_from_start = vibe_enabled
            if vibe_enabled:
                self._lynchpin = None   # will seed from random playlist track
            self._user_tracks_played = 0
            self._auto_slot_id = None

    def on_playlist_ended(self) -> None:
        """Call when the active playlist context is cleared."""
        with self._lock:
            self._playlist_tracks = []
            self._playlist_vibe_from_start = False

    def on_track_started(self, track: Track) -> None:
        """Call on every on_track_started event (auto or user)."""
        with self._lock:
            url = track.stream_url or track.url
            self._recent_urls.append(url)
            if len(self._recent_urls) > 50:
                self._recent_urls.pop(0)

            self._artist_history.append(_norm_artist(track.artist))
            if len(self._artist_history) > 50:
                self._artist_history.pop(0)

            if track.id == self._auto_slot_id:
                # Auto-slot track started playing — clear the reservation.
                # The threshold only applies to playlist-from-start mode; for
                # single-song lynchpin the next _maybe_fill will fill regardless.
                # For playlist-from-start, bump the counter so fill fires
                # immediately after an auto-suggestion rather than waiting for
                # N more user tracks.
                self._auto_slot_id = None
                if self._playlist_vibe_from_start:
                    self._user_tracks_played = self._cfg.suggestion_threshold
            else:
                # User / playlist track — count toward threshold (only relevant
                # in playlist-from-start mode; harmless to increment otherwise).
                self._user_tracks_played += 1

        threading.Thread(target=self._maybe_fill, daemon=True,
                         name="VibeEngine-fill").start()

    def on_user_request(self, track: Track) -> None:
        """Call when a user song request is enqueued.

        Evicts the current auto-slot (user's song takes the spot) and, when
        vibe is active, immediately schedules a new fill so the queue stays
        populated after the user's song finishes.
        """
        do_fill = False
        evict_id: Optional[str] = None
        with self._lock:
            if self._auto_slot_id:
                evict_id = self._auto_slot_id   # remove outside lock (see on_vibe_toggled)
                self._auto_slot_id = None
            if self._enabled and self._lynchpin is not None:
                # For playlist-from-start mode, bump the counter so the next
                # fill fires immediately after the user's song rather than
                # waiting for the full threshold to tick down.
                # Single-song lynchpin has no threshold so no manipulation needed.
                if self._playlist_vibe_from_start:
                    self._user_tracks_played = max(
                        self._user_tracks_played,
                        self._cfg.suggestion_threshold,
                    )
                do_fill = True
        if evict_id:
            self._queue.remove(evict_id)
        if do_fill:
            threading.Thread(
                target=self._maybe_fill, daemon=True, name="VibeEngine-fill"
            ).start()

    def on_queue_cleared(self) -> None:
        """Call when the queue is fully cleared (auto-suggestion was also removed).

        Immediately schedules a refill so the slot is repopulated without any
        manual intervention — the streamer cleared user requests, not vibe.
        """
        do_fill = False
        with self._lock:
            self._auto_slot_id = None
            if self._enabled or self._playlist_tracks:
                do_fill = True
        if do_fill:
            threading.Thread(
                target=self._maybe_fill, daemon=True, name="VibeEngine-fill"
            ).start()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _maybe_fill(self) -> None:
        """Decide whether to queue a suggestion.  Runs on a worker thread.

        All queue mutations happen OUTSIDE self._lock to avoid deadlocking with
        QueueManager callbacks that may re-enter vibe engine methods.
        """
        track_to_queue = None   # set in locked section, enqueued outside

        with self._lock:
            # Bail if there's already a live auto-slot in the queue.
            if self._auto_slot_id is not None:
                if self._queue.position_of(self._auto_slot_id) > 0:
                    return
                self._auto_slot_id = None   # slot was removed externally

            if not self._enabled:
                # Vibe OFF — only fill from an active playlist.
                if not self._playlist_tracks:
                    return
                track_to_queue = self._pick_playlist_track_locked()
                if track_to_queue is None:
                    return
                track_to_queue.is_auto_suggestion = True
                self._auto_slot_id = track_to_queue.id
                # Fall through to enqueue outside lock

            else:
                # Vibe ON.
                #
                # Threshold only applies to playlist-from-start mode — the
                # user chose to start a playlist *with* vibe already on, so we
                # let them hear a few tracks naturally before auto-fill begins.
                #
                # For a single-song lynchpin (standalone *or* mid-playlist
                # toggle), the user explicitly pointed vibe at a song, so fill
                # immediately and keep filling after every auto-suggestion.
                if self._playlist_vibe_from_start and self._playlist_tracks:
                    threshold = max(1, self._cfg.suggestion_threshold)
                    if self._user_tracks_played < threshold:
                        return
                    # Reset counter only once we know we'll fetch.
                    self._user_tracks_played = 0
                    seed = self._random_playlist_seed_locked()
                else:
                    # Single-song lynchpin: no threshold — always fill.
                    seed = self._lynchpin

                if seed is None:
                    # No lynchpin yet (toggled on before any track started).
                    # Don't reset the counter — retry on the next track start.
                    return

                recent_snapshot  = list(self._recent_urls)
                artist_snapshot  = list(self._artist_history)

        if not self._enabled:
            # Playlist-random path: enqueue outside lock
            if track_to_queue is not None:
                self._queue.enqueue(track_to_queue)
            return

        # ── Vibe ON: network fetch outside lock ─────────────────────────────
        # Only one fetch at a time — if another thread is already fetching,
        # skip rather than queuing up (the in-flight fetch will fill the slot).
        if not self._fetch_lock.acquire(blocking=False):
            return
        try:
            suggestion = self._fetch_suggestion(seed, recent_snapshot, artist_snapshot)
        finally:
            self._fetch_lock.release()

        if suggestion is None:
            return

        with self._lock:
            # Re-check: another thread may have raced us.
            if self._auto_slot_id is not None:
                if self._queue.position_of(self._auto_slot_id) > 0:
                    return
            suggestion.is_auto_suggestion = True
            self._auto_slot_id = suggestion.id

        # Enqueue outside the lock
        self._queue.enqueue(suggestion)

    def _pick_playlist_track_locked(self) -> Optional[Track]:
        """Random playlist pick with replay penalty.  Call with lock held."""
        if not self._playlist_tracks:
            return None
        hard_exclude = set(self._recent_urls[-4:]) if self._recent_urls else set()
        candidates = [
            pt for pt in self._playlist_tracks
            if (pt.stream_url or "") not in hard_exclude
        ]
        if not candidates:
            candidates = self._playlist_tracks  # all excluded — pick anyway
        pt = random.choice(candidates)
        return pt.to_track()

    def _random_playlist_seed_locked(self) -> Optional[Track]:
        """Random playlist track to use as a YT suggestion seed.  Lock held."""
        if not self._playlist_tracks:
            return None
        return random.choice(self._playlist_tracks).to_track()

    def _fetch_suggestion(
        self,
        seed: Track,
        recent_urls: list[str],
        artist_history: list[str],
    ) -> Optional[Track]:
        """Fetch YT suggestions and pick one using URL + artist penalties."""
        try:
            import integrations.yt_dlp_client as ytdlp
            fetch_count = max(1, self._cfg.suggestion_count) * 3  # over-fetch for filtering
            candidates = ytdlp.get_suggestions(seed, fetch_count)
        except Exception as exc:
            print(f"[vibe] suggestion fetch failed: {exc!r}")
            return None

        if not candidates:
            return None

        rigidness    = float(getattr(self._cfg, "vibe_rigidness",    0.7))
        artist_guard = bool(getattr(self._cfg,  "vibe_artist_guard", True))

        hard_exclude = set(recent_urls[-4:]) if recent_urls else set()

        # Dynamic artist guard: measure how much each artist dominates this batch.
        batch_artist_share: dict[str, float] = {}
        if artist_guard and candidates:
            from collections import Counter
            counts = Counter(_norm_artist(t.artist) for t in candidates)
            total_c = len(candidates)
            batch_artist_share = {a: n / total_c for a, n in counts.items() if a}

        rev_urls = list(reversed(recent_urls))
        weighted: list[tuple[Track, float]] = []
        for track in candidates:
            url = track.stream_url or track.url
            if url in hard_exclude:
                continue

            # ── URL replay penalty (existing) — 10 % per track ago ────────────
            try:
                idx = rev_urls.index(url)
                tracks_ago = idx + 1
                weight = max(0.1, 1.0 - tracks_ago * 0.10)
            except ValueError:
                weight = 1.0

            artist = _norm_artist(track.artist)

            # ── Artist recency penalty (rigidness-scaled) ─────────────────────
            # At rigidness=1.0: zero extra penalty — trust the YT signal.
            # At rigidness=0.0: 35 % penalty per play in the last 20, capping
            # at 5 % weight so the track can still escape if there's nothing else.
            if artist and rigidness < 1.0:
                recent_plays = sum(1 for a in artist_history[-20:] if a == artist)
                if recent_plays:
                    penalty = recent_plays * 0.35 * (1.0 - rigidness)
                    weight *= max(0.05, 1.0 - penalty)

            # ── Dynamic batch artist guard ────────────────────────────────────
            # Independent of rigidness — fires whenever one artist makes up >30 %
            # of what YouTube returned, signalling a "locked into one band" radio.
            # Weight is reduced proportionally to how dominant they are.
            if artist_guard and artist:
                share = batch_artist_share.get(artist, 0.0)
                if share > 0.30:
                    guard_factor = (share - 0.30) / 0.70   # 0→0, 0.7→1 (70 % dominance)
                    weight *= max(0.05, 1.0 - guard_factor * 0.90)

            weighted.append((track, weight))

        if not weighted:
            return None

        # Weighted random selection
        total = sum(w for _, w in weighted)
        r = random.uniform(0.0, total)
        cumulative = 0.0
        for track, w in weighted:
            cumulative += w
            if r <= cumulative:
                return track
        return weighted[-1][0]
