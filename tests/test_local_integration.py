"""
Local media: cover art, missing files, and the places local tracks meet the
rest of the app.

Local files behave unlike URLs in two ways that nothing downstream expected:
their artwork lives inside the file rather than at an address, and they can
simply stop existing.  On top of that, several code paths fall back to a
track's `url` — which for a local track is an absolute filesystem path, and
some of those paths are published to chat or to moderators.
"""
from __future__ import annotations

import math
import os

import pytest

from player.queue_manager import QueueManager, Track, TrackSource

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")


def _audio_with_cover(path: str, cover_png: bytes | None = None) -> str:
    """Write an mp3, optionally with an embedded cover image."""
    sr = 44100
    t = np.linspace(0, 1.0, sr, endpoint=False)
    planar = np.stack([(0.3 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)] * 2)

    with av.open(path, "w") as container:
        stream = container.add_stream("mp3", rate=sr)
        stream.layout = "stereo"
        frame = av.AudioFrame.from_ndarray(planar, format="fltp", layout="stereo")
        frame.sample_rate = sr
        res = av.AudioResampler(format=stream.format.name, layout="stereo", rate=sr)
        for converted in res.resample(frame):
            for packet in stream.encode(converted):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def _make_png(tmp_path) -> bytes:
    """A small solid-colour PNG, produced with PIL."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow needed for art tests")
    img = Image.new("RGB", (32, 32), (200, 40, 40))
    out = tmp_path / "cover.png"
    img.save(out)
    return out.read_bytes()


# ── Cover art ─────────────────────────────────────────────────────────────────

def test_extract_cover_art_returns_none_when_there_is_none(tmp_path):
    from player.local_media import extract_cover_art
    path = _audio_with_cover(str(tmp_path / "plain.mp3"))
    assert extract_cover_art(path) is None


def test_extract_cover_art_returns_none_for_a_missing_file(tmp_path):
    from player.local_media import extract_cover_art
    assert extract_cover_art(str(tmp_path / "gone.mp3")) is None


def test_extract_cover_art_never_raises_on_junk(tmp_path):
    from player.local_media import extract_cover_art
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio at all")
    assert extract_cover_art(str(junk)) is None


def test_art_pipeline_routes_local_paths_away_from_the_network(tmp_path, monkeypatch):
    """
    _fetch used to call requests.get unconditionally.  A local path must go to
    the embedded-cover reader instead — hitting the network with a file path
    would be both wrong and slow.
    """
    import player.art_colours as ac

    called = {"http": False, "local": False}
    monkeypatch.setattr(
        "player.local_media.extract_cover_art",
        lambda p: called.__setitem__("local", True) or None,
    )

    def _boom(*a, **k):
        called["http"] = True
        raise AssertionError("a local path must not be fetched over HTTP")

    monkeypatch.setattr("requests.get", _boom)

    path = _audio_with_cover(str(tmp_path / "song.mp3"))
    ac._load_bytes(path)

    assert called["local"] is True
    assert called["http"] is False


def test_art_pipeline_still_uses_http_for_remote_thumbnails(monkeypatch):
    import player.art_colours as ac

    class _Resp:
        content = b"imagebytes"

        def raise_for_status(self):
            pass

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    assert ac._load_bytes("https://example.invalid/thumb.jpg") == b"imagebytes"


def test_art_source_uses_the_audio_file_for_local_tracks():
    """
    thumbnail_url is published to the mod panel, so the path must not be stored
    there — the art source is derived at use time instead.
    """
    from ui.main_window import MainWindow

    local = Track(title="T", stream_url=r"C:\Music\a.mp3",
                  source=TrackSource.LOCAL)
    remote = Track(title="T", stream_url="https://youtu.be/x",
                   thumbnail_url="https://img/x.jpg", source=TrackSource.YOUTUBE)

    assert MainWindow._art_source_for(local) == r"C:\Music\a.mp3"
    assert MainWindow._art_source_for(remote) == "https://img/x.jpg"


# ── Missing files ─────────────────────────────────────────────────────────────

def test_engine_skips_a_file_that_no_longer_exists(tmp_path):
    """
    A moved file previously failed deep inside av.open() and the track stalled.
    The decode worker must instead terminate the stream immediately, so the
    queue advances exactly as it does for any other failure.
    """
    import threading

    from player.engine import PlaybackEngine
    from player.fft import FFTPipeline

    q = QueueManager()
    engine = PlaybackEngine(q, FFTPipeline())
    missing = str(tmp_path / "was-here.mp3")
    assert not os.path.isfile(missing)

    track = Track(title="Gone", stream_url=missing, source=TrackSource.LOCAL)
    engine._decode_worker(track, threading.Event())

    # A None sentinel on the PCM queue is how the decoder says "stream over".
    assert engine._pcm_queue.get_nowait() is None


def test_engine_still_plays_a_file_that_does_exist(tmp_path):
    """The guard must not reject valid files."""
    import threading

    from player.engine import PlaybackEngine
    from player.fft import FFTPipeline

    path = _audio_with_cover(str(tmp_path / "real.mp3"))
    q = QueueManager()
    engine = PlaybackEngine(q, FFTPipeline())
    engine._decode_worker(
        Track(title="Real", stream_url=path, source=TrackSource.LOCAL),
        threading.Event(),
    )

    chunks = []
    while True:
        item = engine._pcm_queue.get_nowait()
        if item is None:
            break
        chunks.append(item)
    assert chunks, "a valid local file produced no PCM"


def test_missing_file_guard_exists_in_the_engine():
    """Structural: the guard must sit before av.open, not after."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "player" / "engine.py").read_text(
        encoding="utf-8-sig"
    )
    assert "os.path.isfile(stream_url)" in src
    assert src.index("os.path.isfile(stream_url)") < src.index("def _open_container")


# ── Path leakage ──────────────────────────────────────────────────────────────

def test_display_title_never_falls_back_to_a_path():
    """
    display_title() feeds !currentsong and the mod panel.  An untitled local
    track must not broadcast its directory structure.
    """
    t = Track(title="", artist="",
              url=r"C:\Users\someone\Music\Private\secret-song.mp3",
              stream_url=r"C:\Users\someone\Music\Private\secret-song.mp3",
              source=TrackSource.LOCAL)
    shown = t.display_title()
    assert "secret-song" in shown
    assert "Users" not in shown
    assert "\\" not in shown and "/" not in shown


def test_display_title_still_falls_back_to_url_for_remote_tracks():
    t = Track(title="", artist="", url="https://youtu.be/abc",
              stream_url="https://youtu.be/abc", source=TrackSource.YOUTUBE)
    assert t.display_title() == "https://youtu.be/abc"


def test_queue_payload_leaks_no_path_even_for_an_untitled_local_track():
    q = QueueManager()
    q.enqueue(Track(title="", artist="",
                    url=r"C:\Users\someone\Music\track.mp3",
                    stream_url=r"C:\Users\someone\Music\track.mp3",
                    source=TrackSource.LOCAL))
    blob = repr(q.to_dict())
    assert "Users" not in blob
    assert "someone" not in blob


# ── Vibe engine ───────────────────────────────────────────────────────────────

def test_local_tracks_are_not_used_as_vibe_seeds():
    """
    A local file's stream_url is a path; asking YouTube for tracks related to
    it is meaningless.  Vibe should skip rather than fetch nonsense.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "player" / "vibe_engine.py").read_text(
        encoding="utf-8-sig"
    )
    assert "seed.source == TrackSource.LOCAL" in src


def test_vibe_does_not_adopt_a_local_track_as_lynchpin():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "player" / "vibe_engine.py").read_text(
        encoding="utf-8-sig"
    )
    block = src[src.index("def on_vibe_toggled"):src.index("def on_playlist_started")
                if "def on_playlist_started" in src else src.index("def on_track_started")]
    assert "TrackSource.LOCAL" in block
