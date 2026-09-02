"""
Local file playback: metadata probing, path routing, and the security gates.

The playback engine already handles local files — its yt-dlp re-resolution is
gated on a domain match, so a filesystem path falls through to PyAV untouched.
What these cover is everything around that: turning a path into a Track, and
making sure only the streamer can do it.

Test audio is generated at run time with PyAV rather than committed as
fixtures, so the suite stays small and exercises the same decoder the shipped
binary uses.
"""
from __future__ import annotations

import math
import os

import pytest

from player.local_media import (
    SUPPORTED_EXTENSIONS,
    is_supported_file,
    probe,
    scan_directory,
)
from player.queue_manager import RequestOrigin, TrackSource
from player.resolver import _looks_like_local_path, _normalise_local_path, resolve

av = pytest.importorskip("av", reason="PyAV is required to generate test audio")
np = pytest.importorskip("numpy")


def _write_audio(path: str, codec: str, meta: dict | None = None,
                 seconds: float = 1.0, rate: int = 44100) -> str:
    """Encode a short tone so tests run against real containers."""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    tone = (0.3 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)
    planar = np.stack([tone, tone])

    with av.open(path, "w") as container:
        stream = container.add_stream(codec, rate=rate)
        stream.layout = "stereo"
        if meta:
            container.metadata.update(meta)
        frame = av.AudioFrame.from_ndarray(planar, format="fltp", layout="stereo")
        frame.sample_rate = rate
        resampler = av.AudioResampler(format=stream.format.name,
                                      layout="stereo", rate=rate)
        for converted in resampler.resample(frame):
            for packet in stream.encode(converted):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


@pytest.fixture
def tagged_mp3(tmp_path):
    return _write_audio(
        str(tmp_path / "song.mp3"), "mp3",
        meta={"title": "Test Song", "artist": "Test Artist", "album": "Test Album"},
    )


@pytest.fixture
def untagged_wav(tmp_path):
    return _write_audio(str(tmp_path / "My Untagged Track.wav"), "pcm_s16le")


# ── Extension handling ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["a.mp3", "a.WAV", "a.FlAc", "a.m4a", "a.opus"])
def test_supported_extensions_are_case_insensitive(name):
    assert is_supported_file(name)


@pytest.mark.parametrize("name", ["a.txt", "a.exe", "a.mp4v", "noextension", "a.jpg"])
def test_unsupported_extensions_rejected(name):
    assert not is_supported_file(name)


def test_extension_set_matches_the_lgpl_decoder_inventory():
    """Formats the vendored LGPL FFmpeg build can actually decode."""
    for ext in (".mp3", ".flac", ".wav", ".opus", ".m4a", ".wma"):
        assert ext in SUPPORTED_EXTENSIONS


# ── Probing ───────────────────────────────────────────────────────────────────

def test_probe_reads_embedded_tags(tagged_mp3):
    data = probe(tagged_mp3)
    assert data is not None
    assert data["title"] == "Test Song"
    assert data["artist"] == "Test Artist"
    assert data["album"] == "Test Album"


def test_probe_reports_duration(tagged_mp3):
    assert probe(tagged_mp3)["duration"] >= 1


def test_probe_falls_back_to_filename_when_untagged(untagged_wav):
    """Untagged files are the common case — a filename beats 'Unknown title'."""
    assert probe(untagged_wav)["title"] == "My Untagged Track"


def test_probe_returns_absolute_path(tagged_mp3):
    data = probe(tagged_mp3)
    assert os.path.isabs(data["filepath"])


def test_probe_returns_none_for_a_missing_file(tmp_path):
    assert probe(str(tmp_path / "nope.mp3")) is None


def test_probe_returns_none_for_a_non_audio_file(tmp_path):
    junk = tmp_path / "notaudio.mp3"
    junk.write_bytes(b"this is definitely not an mp3")
    assert probe(str(junk)) is None


def test_probe_returns_none_rather_than_raising_on_empty_input():
    assert probe("") is None


def test_probe_handles_flac(tmp_path):
    path = _write_audio(str(tmp_path / "x.flac"), "flac")
    assert probe(path) is not None


# ── Directory scanning ────────────────────────────────────────────────────────

def test_scan_directory_finds_audio_and_ignores_other_files(tmp_path):
    _write_audio(str(tmp_path / "b.mp3"), "mp3")
    _write_audio(str(tmp_path / "a.wav"), "pcm_s16le")
    (tmp_path / "notes.txt").write_text("ignore me")

    found = scan_directory(str(tmp_path))
    assert len(found) == 2
    assert all(is_supported_file(p) for p in found)


def test_scan_directory_recurses(tmp_path):
    sub = tmp_path / "album"
    sub.mkdir()
    _write_audio(str(sub / "track.mp3"), "mp3")
    assert len(scan_directory(str(tmp_path), recursive=True)) == 1
    assert len(scan_directory(str(tmp_path), recursive=False)) == 0


def test_scan_directory_of_a_missing_folder_is_empty(tmp_path):
    assert scan_directory(str(tmp_path / "nope")) == []


# ── Path detection ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    r"C:\Music\song.mp3",
    "C:/Music/song.mp3",
    "/home/user/song.flac",
    "~/Music/song.wav",
    r"\\server\share\song.mp3",
    "file:///C:/Music/song.mp3",
    "song.mp3",
])
def test_local_paths_are_detected(value):
    assert _looks_like_local_path(value)


@pytest.mark.parametrize("value", [
    "https://www.youtube.com/watch?v=abc",
    "http://soundcloud.com/a/b",
    "www.youtube.com/watch?v=abc",
    "daft punk one more time",
    "",
])
def test_urls_and_searches_are_not_treated_as_paths(value):
    assert not _looks_like_local_path(value)


def test_file_uri_is_normalised_to_a_plain_path():
    assert _normalise_local_path("file:///C:/Music/song.mp3").endswith("song.mp3")


def test_surrounding_quotes_are_stripped():
    """Windows 'Copy as path' wraps the result in quotes."""
    assert _normalise_local_path('"C:\\Music\\song.mp3"') == r"C:\Music\song.mp3"


# ── Resolution ────────────────────────────────────────────────────────────────

def test_resolve_builds_a_local_track(tagged_mp3):
    track = resolve(tagged_mp3, requested_by="streamer",
                    origin=RequestOrigin.MANUAL)
    assert track is not None
    assert track.source == TrackSource.LOCAL
    assert track.title == "Test Song"
    assert track.artist == "Test Artist"
    assert track.stream_url == os.path.abspath(tagged_mp3)


def test_resolved_local_track_needs_no_ytdlp_resolution(tagged_mp3):
    """The engine only re-resolves URLs whose host it recognises."""
    track = resolve(tagged_mp3, origin=RequestOrigin.MANUAL)
    needs_resolve = ("youtube.com", "youtu.be", "soundcloud.com")
    assert not any(d in track.stream_url for d in needs_resolve)


def test_resolve_returns_none_for_a_missing_file(tmp_path):
    assert resolve(str(tmp_path / "gone.mp3"), origin=RequestOrigin.MANUAL) is None


# ── Security gate: chat must never reach the filesystem ───────────────────────

@pytest.mark.parametrize("origin", [
    RequestOrigin.CHAT,
    RequestOrigin.CHANNEL_POINTS,
    RequestOrigin.SUGGESTION,
])
def test_local_paths_are_refused_from_untrusted_origins(tagged_mp3, origin):
    """
    A viewer must not be able to play files off the streamer's machine.
    The file genuinely exists here — only the origin should stop it.
    """
    assert resolve(tagged_mp3, requested_by="viewer", origin=origin) is None


def test_only_manual_origin_may_play_local_files(tagged_mp3):
    assert resolve(tagged_mp3, origin=RequestOrigin.MANUAL) is not None


def test_traversal_style_paths_are_refused_from_chat(tmp_path):
    assert resolve(r"..\..\Windows\System32\config\SAM",
                   origin=RequestOrigin.CHAT) is None


def test_file_uri_is_also_gated(tagged_mp3):
    """The scheme must not be a way around the origin check."""
    uri = "file:///" + os.path.abspath(tagged_mp3).replace("\\", "/")
    assert resolve(uri, origin=RequestOrigin.CHAT) is None


# ── Security gate: the mod panel must not leak filesystem paths ───────────────

def test_local_paths_are_withheld_from_the_panel_payload(tagged_mp3):
    """
    _track_dict is pushed to every moderator over the public tunnel.  A local
    track's url is a filesystem path, and folder names alone say a lot about
    the machine.
    """
    from player.queue_manager import QueueManager

    track = resolve(tagged_mp3, origin=RequestOrigin.MANUAL)
    q = QueueManager()
    q.enqueue(track)
    payload = q.to_dict()

    entry = payload["queue"][0] if payload.get("queue") else None
    assert entry is not None
    assert entry["url"] == ""
    blob = repr(payload)
    assert os.path.dirname(os.path.abspath(tagged_mp3)) not in blob


def test_remote_tracks_keep_their_url():
    """The panel links back to the source page for YouTube/SoundCloud."""
    from player.queue_manager import QueueManager, Track

    q = QueueManager()
    q.enqueue(Track(title="Remote", url="https://youtu.be/abc",
                    stream_url="https://youtu.be/abc",
                    source=TrackSource.YOUTUBE))
    assert q.to_dict()["queue"][0]["url"] == "https://youtu.be/abc"


# ── Search queries must not be misread as paths ───────────────────────────────

@pytest.mark.parametrize("query", [
    "AC/DC back in black",          # a slash is legitimate in a search
    "daft punk one more time",
    "artist - song title",
    "song (feat. someone)",
])
def test_legitimate_searches_are_not_routed_to_the_local_branch(query):
    assert not _looks_like_local_path(query)


@pytest.mark.parametrize("query", [
    r"..\..\Windows\System32\config\SAM",
    "../../etc/passwd",
    r"Music\song.mp3",
])
def test_relative_and_traversal_paths_reach_the_gate(query):
    """
    These must not fall through to the YouTube search branch — not because
    searching is dangerous, but because a path shape that bypasses the local
    branch also bypasses the origin gate protecting it.
    """
    assert _looks_like_local_path(query)
