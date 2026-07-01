"""
Ephemeral play-history — tracks played since the app started, newest first.

Persisted only in memory; intentionally discarded on restart so the list
stays relevant to the current streaming session.
"""

from __future__ import annotations

from typing import Optional

from player.queue_manager import Track


class PlayHistory:
    """Circular in-memory list of recently played tracks."""

    MAX = 100

    def __init__(self) -> None:
        # Index 0 = oldest, -1 = most-recently started
        self._tracks: list[Track] = []

    # ── Write ──────────────────────────────────────────────────────────────────

    def record(self, track: Track) -> None:
        """Called each time a track starts (including restarts / seeks)."""
        # Avoid consecutive duplicates (seek-to-start fires play_track again)
        if self._tracks and self._tracks[-1].id == track.id:
            return
        self._tracks.append(track)
        if len(self._tracks) > self.MAX:
            self._tracks.pop(0)

    # ── Read ───────────────────────────────────────────────────────────────────

    def previous(self) -> Optional[Track]:
        """The track played before the current one, or None."""
        if len(self._tracks) >= 2:
            return self._tracks[-2]
        return None

    def all_recent(self) -> list[Track]:
        """All tracks, most recent first."""
        return list(reversed(self._tracks))

    def clear(self) -> None:
        self._tracks.clear()
