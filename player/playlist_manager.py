"""
Playlist manager — persists named playlists of tracks across sessions.

Playlists live in  <data_dir>/playlists.json  (separate from the main config
so they can grow large without slowing down config load).

Each playlist stores just enough information to reconstruct a playable
queue entry: the stream_url (full YouTube / SoundCloud page URL) is the
canonical key — the engine resolves it via yt-dlp at playback time, the
same way search results work.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from collections.abc import Callable

from data_dir import DATA_DIR
from player.queue_manager import Track, TrackSource

PLAYLISTS_PATH = os.path.join(DATA_DIR, "playlists.json")


@dataclass
class PlaylistTrack:
    """Minimal persistent track record inside a playlist."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    artist: str = ""
    stream_url: str = ""          # YouTube / SoundCloud page URL
    thumbnail_url: str = ""
    duration_seconds: int = 0
    source: str = "YOUTUBE"       # TrackSource.name

    @classmethod
    def from_track(cls, t: Track) -> PlaylistTrack:
        return cls(
            title=t.title,
            artist=t.artist,
            stream_url=t.stream_url,
            thumbnail_url=t.thumbnail_url,
            duration_seconds=t.duration_seconds,
            source=t.source.name,
        )

    def to_track(self) -> Track:
        import re as _re
        try:
            source = TrackSource[self.source]
        except KeyError:
            source = TrackSource.YOUTUBE

        thumb = self.thumbnail_url
        if not thumb and source == TrackSource.YOUTUBE:
            # Reconstruct from stream_url — handles playlists saved before the
            # import-time thumbnail fix landed (those have thumbnail_url="").
            m = _re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', self.stream_url)
            if m:
                thumb = f"https://i.ytimg.com/vi/{m.group(1)}/maxresdefault.jpg"

        return Track(
            title=self.title,
            artist=self.artist,
            url=self.stream_url,
            stream_url=self.stream_url,
            thumbnail_url=thumb,
            duration_seconds=self.duration_seconds,
            source=source,
        )


@dataclass
class Playlist:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "New Playlist"
    tracks: list[PlaylistTrack] = field(default_factory=list)
    # A playlist holds either local files or streamed tracks, never both.
    #
    # Mixing them means every consumer has to cope with two very different
    # failure modes in one list — a missing file next to a geo-blocked video,
    # a path next to a URL — and the import UI would have to offer both kinds
    # of "add" on the same playlist.  Keeping the two apart makes each one
    # simple to reason about.
    is_local: bool = False

    def accepts(self, source: TrackSource) -> bool:
        """True if a track from *source* belongs in this playlist."""
        return (source == TrackSource.LOCAL) == self.is_local

    def track_count(self) -> int:
        return len(self.tracks)

    def total_duration(self) -> int:
        """Total duration in seconds."""
        return sum(t.duration_seconds for t in self.tracks)


class PlaylistManager:
    """Load, mutate and persist playlists."""

    def __init__(self) -> None:
        self._playlists: list[Playlist] = []
        # Observers notified after every mutation
        self.on_changed: list[Callable[[], None]] = []
        self._load()

    # ── Read ───────────────────────────────────────────────────────────────────

    def playlists(self) -> list[Playlist]:
        return list(self._playlists)

    def get(self, playlist_id: str) -> Playlist | None:
        for pl in self._playlists:
            if pl.id == playlist_id:
                return pl
        return None

    # ── Mutations ──────────────────────────────────────────────────────────────

    def create(self, name: str = "New Playlist", is_local: bool = False) -> Playlist:
        pl = Playlist(name=name.strip() or "New Playlist", is_local=is_local)
        self._playlists.append(pl)
        self._save()
        self._notify()
        return pl

    def rename(self, playlist_id: str, new_name: str) -> bool:
        pl = self.get(playlist_id)
        if pl is None:
            return False
        pl.name = new_name.strip() or pl.name
        self._save()
        self._notify()
        return True

    def delete(self, playlist_id: str) -> bool:
        for i, pl in enumerate(self._playlists):
            if pl.id == playlist_id:
                self._playlists.pop(i)
                self._save()
                self._notify()
                return True
        return False

    def add_track(self, playlist_id: str, track: Track) -> bool:
        pl = self.get(playlist_id)
        if pl is None:
            return False
        if not pl.accepts(track.source):
            kind = "local-only" if pl.is_local else "streaming"
            print(
                f"[playlists] refused {track.source.name} track "
                f"'{track.title[:40]}' — '{pl.name}' is a {kind} playlist"
            )
            return False
        pl.tracks.append(PlaylistTrack.from_track(track))
        self._save()
        self._notify()
        return True

    def add_tracks(self, playlist_id: str, tracks: list[PlaylistTrack]) -> int:
        """Append several already-built PlaylistTracks; returns how many landed.

        Bulk import writes once rather than once per file — a folder of a few
        thousand songs would otherwise rewrite playlists.json a few thousand
        times.  Entries whose source does not match the playlist kind are
        dropped, so this cannot be used to sneak past the local/streaming split.
        """
        pl = self.get(playlist_id)
        if pl is None:
            return 0

        kept = []
        for t in tracks:
            try:
                source = TrackSource[t.source]
            except KeyError:
                continue
            if pl.accepts(source):
                kept.append(t)

        if not kept:
            return 0
        pl.tracks.extend(kept)
        self._save()
        self._notify()
        return len(kept)

    def remove_track(self, playlist_id: str, track_id: str) -> bool:
        pl = self.get(playlist_id)
        if pl is None:
            return False
        for i, t in enumerate(pl.tracks):
            if t.id == track_id:
                pl.tracks.pop(i)
                self._save()
                self._notify()
                return True
        return False

    def move_track(self, playlist_id: str, from_idx: int, to_idx: int) -> bool:
        pl = self.get(playlist_id)
        if pl is None or not (0 <= from_idx < len(pl.tracks)):
            return False
        track = pl.tracks.pop(from_idx)
        to_idx = max(0, min(to_idx, len(pl.tracks)))
        pl.tracks.insert(to_idx, track)
        self._save()
        self._notify()
        return True

    # ── Queue integration ──────────────────────────────────────────────────────

    def create_from_playlist_tracks(
        self, name: str, tracks: list[PlaylistTrack]
    ) -> Playlist:
        """Create a new playlist pre-populated with tracks in a single save."""
        pl = Playlist(name=name.strip() or "Imported Playlist")
        pl.tracks = list(tracks)
        self._playlists.append(pl)
        self._save()
        self._notify()
        return pl

    def enqueue_all(self, playlist_id: str, queue_manager, shuffle: bool = False) -> int:
        """Push all playlist tracks into queue_manager. Returns count added."""
        pl = self.get(playlist_id)
        if pl is None:
            return 0
        tracks = [pt.to_track() for pt in pl.tracks]
        if shuffle:
            import random
            random.shuffle(tracks)
        for t in tracks:
            queue_manager.enqueue(t)
        return len(tracks)

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        os.makedirs(os.path.dirname(PLAYLISTS_PATH), exist_ok=True)
        if not os.path.exists(PLAYLISTS_PATH):
            return
        try:
            with open(PLAYLISTS_PATH, encoding="utf-8") as fh:
                raw = json.load(fh)
            for pl_data in raw.get("playlists", []):
                tracks = [
                    PlaylistTrack(**t)
                    for t in pl_data.get("tracks", [])
                ]
                self._playlists.append(Playlist(
                    id=pl_data.get("id", str(uuid.uuid4())),
                    name=pl_data.get("name", "Unnamed"),
                    tracks=tracks,
                    # Absent in files written before local playlists existed,
                    # which is correct: everything back then was streamed.
                    is_local=bool(pl_data.get("is_local", False)),
                ))
        except Exception as exc:
            print(f"[playlists] load error: {exc}")

    def _save(self) -> None:
        os.makedirs(os.path.dirname(PLAYLISTS_PATH), exist_ok=True)
        data = {
            "playlists": [
                {
                    "id": pl.id,
                    "name": pl.name,
                    "is_local": pl.is_local,
                    "tracks": [asdict(t) for t in pl.tracks],
                }
                for pl in self._playlists
            ]
        }
        # Written to a temporary file and renamed over the target.  Opening
        # PLAYLISTS_PATH directly truncates it before the new content lands, so
        # a crash at that moment loses every playlist — which matters more now
        # that one may represent a whole imported library.  os.replace() is
        # atomic on Win32 and POSIX.
        tmp_path = PLAYLISTS_PATH + ".tmp"
        try:
            payload = json.dumps(data, indent=2, ensure_ascii=False)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, PLAYLISTS_PATH)
        except Exception as exc:
            print(f"[playlists] save error: {exc}")

    def _notify(self) -> None:
        for cb in self.on_changed:
            cb()
