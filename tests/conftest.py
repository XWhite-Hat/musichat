"""
Shared pytest fixtures.

Every test module must import application code only *after* MUSICHAT_DATA_DIR
points somewhere disposable — data_dir.DATA_DIR is read at import time, and
config.CONFIG_PATH is derived from it at import time too.  Setting the env var
here, before any test module body runs, keeps tests off the real user data
directory.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Must happen at import time, before any test module imports config/data_dir.
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="musichat_tests_")
os.environ["MUSICHAT_DATA_DIR"] = _TMP_DATA_DIR


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """An isolated data directory, with config.CONFIG_PATH repointed at it."""
    import config
    monkeypatch.setenv("MUSICHAT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    return tmp_path


@pytest.fixture
def track_factory():
    """Build throwaway Tracks without repeating the constructor everywhere."""
    from player.queue_manager import Track, TrackSource

    def _make(n: int = 0, requested_by: str = "", **kw):
        return Track(
            id=kw.pop("id", f"track-{n}"),
            title=kw.pop("title", f"Song {n}"),
            artist=kw.pop("artist", "Artist"),
            url=kw.pop("url", f"https://example.invalid/{n}"),
            source=kw.pop("source", TrackSource.YOUTUBE),
            requested_by=requested_by,
            **kw,
        )

    return _make
