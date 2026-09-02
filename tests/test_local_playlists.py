"""
Local-only playlists.

A playlist holds either local files or streamed tracks, never both.  Mixing
them means every consumer has to handle two unrelated failure modes in one
list — a missing file beside a geo-blocked video — and the import UI would
have to offer both kinds of "add" on the same playlist.

These cover the model side: the flag, its enforcement, and that it survives a
save/load round trip including files written before the flag existed.
"""
from __future__ import annotations

import json

import pytest

from player.playlist_manager import Playlist, PlaylistManager, PlaylistTrack
from player.queue_manager import Track, TrackSource


@pytest.fixture
def pm(tmp_path, monkeypatch):
    """A PlaylistManager writing to a throwaway playlists.json."""
    import player.playlist_manager as mod
    path = str(tmp_path / "playlists.json")
    monkeypatch.setattr(mod, "PLAYLISTS_PATH", path)
    return PlaylistManager()


def _local_track(title: str = "Local Song") -> Track:
    return Track(title=title, artist="Someone",
                 url=r"C:\Music\song.mp3", stream_url=r"C:\Music\song.mp3",
                 source=TrackSource.LOCAL)


def _remote_track(title: str = "Remote Song") -> Track:
    return Track(title=title, artist="Someone",
                 url="https://youtu.be/abc", stream_url="https://youtu.be/abc",
                 source=TrackSource.YOUTUBE)


# ── The flag ──────────────────────────────────────────────────────────────────

def test_playlists_default_to_streaming(pm):
    assert pm.create("Normal").is_local is False


def test_a_playlist_can_be_created_local_only(pm):
    assert pm.create("My Files", is_local=True).is_local is True


def test_accepts_matches_the_playlist_kind():
    local = Playlist(is_local=True)
    remote = Playlist(is_local=False)
    assert local.accepts(TrackSource.LOCAL)
    assert not local.accepts(TrackSource.YOUTUBE)
    assert remote.accepts(TrackSource.YOUTUBE)
    assert remote.accepts(TrackSource.SOUNDCLOUD)
    assert not remote.accepts(TrackSource.LOCAL)


# ── Enforcement ───────────────────────────────────────────────────────────────

def test_local_track_goes_into_a_local_playlist(pm):
    pl = pm.create("Files", is_local=True)
    assert pm.add_track(pl.id, _local_track()) is True
    assert pm.get(pl.id).track_count() == 1


def test_remote_track_is_refused_by_a_local_playlist(pm):
    pl = pm.create("Files", is_local=True)
    assert pm.add_track(pl.id, _remote_track()) is False
    assert pm.get(pl.id).track_count() == 0


def test_local_track_is_refused_by_a_streaming_playlist(pm):
    pl = pm.create("Streams")
    assert pm.add_track(pl.id, _local_track()) is False
    assert pm.get(pl.id).track_count() == 0


def test_soundcloud_counts_as_streaming(pm):
    pl = pm.create("Streams")
    track = _remote_track()
    track.source = TrackSource.SOUNDCLOUD
    assert pm.add_track(pl.id, track) is True


# ── Bulk import ───────────────────────────────────────────────────────────────

def _pl_track(source: str) -> PlaylistTrack:
    return PlaylistTrack(title="T", artist="A", stream_url="x",
                         duration_seconds=1, source=source)


def test_add_tracks_appends_matching_entries(pm):
    pl = pm.create("Files", is_local=True)
    added = pm.add_tracks(pl.id, [_pl_track("LOCAL") for _ in range(3)])
    assert added == 3
    assert pm.get(pl.id).track_count() == 3


def test_add_tracks_drops_entries_of_the_wrong_kind(pm):
    """Bulk import must not be a way around the split."""
    pl = pm.create("Files", is_local=True)
    added = pm.add_tracks(pl.id, [
        _pl_track("LOCAL"), _pl_track("YOUTUBE"), _pl_track("LOCAL"),
    ])
    assert added == 2
    assert pm.get(pl.id).track_count() == 2


def test_add_tracks_ignores_an_unknown_source(pm):
    pl = pm.create("Files", is_local=True)
    assert pm.add_tracks(pl.id, [_pl_track("NOT_A_SOURCE")]) == 0


def test_add_tracks_on_a_missing_playlist_is_harmless(pm):
    assert pm.add_tracks("no-such-id", [_pl_track("LOCAL")]) == 0


def test_add_tracks_writes_once_for_the_whole_batch(pm, monkeypatch):
    """A folder of a few thousand songs must not rewrite the file per track."""
    pl = pm.create("Files", is_local=True)
    saves = []
    monkeypatch.setattr(pm, "_save", lambda: saves.append(1))
    pm.add_tracks(pl.id, [_pl_track("LOCAL") for _ in range(50)])
    assert len(saves) == 1


# ── Persistence ───────────────────────────────────────────────────────────────

def test_is_local_survives_a_reload(pm, tmp_path, monkeypatch):
    pm.create("Files", is_local=True)
    pm.create("Streams", is_local=False)

    import player.playlist_manager as mod
    monkeypatch.setattr(mod, "PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
    reloaded = PlaylistManager()

    by_name = {p.name: p for p in reloaded.playlists()}
    assert by_name["Files"].is_local is True
    assert by_name["Streams"].is_local is False


def test_playlists_written_before_the_flag_existed_load_as_streaming(tmp_path, monkeypatch):
    """Everything predating local playlists was streamed, so absent means False."""
    path = tmp_path / "playlists.json"
    path.write_text(json.dumps({"playlists": [
        {"id": "old", "name": "Legacy", "tracks": []}     # no is_local key
    ]}), encoding="utf-8")

    import player.playlist_manager as mod
    monkeypatch.setattr(mod, "PLAYLISTS_PATH", str(path))
    pl = PlaylistManager().playlists()[0]
    assert pl.name == "Legacy"
    assert pl.is_local is False


def test_save_is_atomic_and_leaves_no_temp_file(pm, tmp_path):
    pm.create("Files", is_local=True)
    assert not (tmp_path / "playlists.json.tmp").exists()
    with open(tmp_path / "playlists.json", encoding="utf-8") as fh:
        json.load(fh)      # must be complete, parseable JSON


def test_existing_playlists_survive_a_failed_write(pm, tmp_path, monkeypatch):
    """Truncating in place would lose every playlist on a crash mid-save."""
    import os
    pm.create("Keep me", is_local=True)

    def boom(src, dst):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(os, "replace", boom)
    try:
        pm.create("Never lands")
    except OSError:
        pass
    monkeypatch.undo()

    import player.playlist_manager as mod
    monkeypatch.setattr(mod, "PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
    names = [p.name for p in PlaylistManager().playlists()]
    assert "Keep me" in names
