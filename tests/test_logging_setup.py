"""
Diagnostics capture.

Two shipped blackouts are covered here:

  * The windowed PyInstaller bootloader sets sys.stdout to None, making every
    print() in the app a silent no-op, so released builds produced no output.
  * Printing non-ASCII to a cp1252 console raises UnicodeEncodeError.  The
    codebase uses arrows and check marks in log lines, and this crashed the app
    on shutdown during testing.
"""
from __future__ import annotations

import io
import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

import logging_setup


@pytest.fixture
def fresh_logging(tmp_path, monkeypatch):
    """Reset the install-once guard and root handlers between tests."""
    monkeypatch.setenv("MUSICHAT_DATA_DIR", str(tmp_path))
    import data_dir
    monkeypatch.setattr(data_dir, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(logging_setup, "_installed", False)
    monkeypatch.setattr(logging_setup, "_log_path", None)

    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    saved_out, saved_err = sys.stdout, sys.stderr
    yield tmp_path
    for h in root.handlers:
        try:
            h.close()
        except Exception:
            pass
    root.handlers = saved
    sys.stdout, sys.stderr = saved_out, saved_err


def _file_handler() -> logging.Handler:
    """
    The RotatingFileHandler installed by logging_setup.

    Not simply handlers[0]: pytest's own logging plugin installs a capture
    handler on the root logger too, and its position is not guaranteed.
    """
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            return h
    raise AssertionError("logging_setup did not install a RotatingFileHandler")


def _read_log(path=None):
    handler = _file_handler()
    handler.flush()
    target = Path(path) if path is not None else Path(logging_setup.log_path())
    return target.read_text(encoding="utf-8")


def test_creates_log_file(fresh_logging):
    path = logging_setup.install()
    assert path is not None
    assert (fresh_logging / "logs" / "musichat.log").exists()


def test_print_is_captured_when_stdout_is_none(fresh_logging, monkeypatch):
    """The windowed-build case: print() would otherwise go nowhere at all."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    path = logging_setup.install()
    print("hello from a windowed build")
    assert "hello from a windowed build" in _read_log(path)


def test_non_ascii_does_not_raise_on_cp1252(fresh_logging):
    """The exact crash: 'charmap' codec can't encode character '\\u2192'."""
    logging_setup.install()
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    tee = logging_setup._Tee(console, _file_handler().stream)
    tee.write("stream_gen 1 → 2  ⚠ ✗ …\n")   # must not raise
    tee.flush()


def test_non_ascii_survives_intact_in_the_log_file(fresh_logging):
    path = logging_setup.install()
    print("arrow → warning ⚠")
    text = _read_log(path)
    assert "→" in text and "⚠" in text


def test_console_still_receives_a_readable_substitute(fresh_logging):
    """Unencodable characters degrade rather than vanish or raise."""
    logging_setup.install()
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    tee = logging_setup._Tee(console, _file_handler().stream)
    tee.write("gen 1 → 2")
    tee.flush()
    written = console.buffer.getvalue().decode("cp1252", "replace")
    assert "gen 1" in written and "2" in written


def test_logging_module_writes_to_the_same_file(fresh_logging):
    path = logging_setup.install()
    logging.getLogger("test").warning("structured log line")
    assert "structured log line" in _read_log(path)


def test_install_is_idempotent(fresh_logging):
    first = logging_setup.install()
    def _file_handlers():
        return [h for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    before = len(_file_handlers())
    second = logging_setup.install()
    assert first == second
    assert len(_file_handlers()) == before   # no duplicate file handler


def test_tee_survives_a_broken_underlying_stream(fresh_logging):
    """A failing console must never break an unrelated print() call."""
    logging_setup.install()

    class Broken:
        encoding = "utf-8"

        def write(self, text):
            raise OSError("pipe closed")

        def flush(self):
            raise OSError("pipe closed")

    tee = logging_setup._Tee(Broken(), _file_handler().stream)
    tee.write("still reaches the log\n")
    tee.flush()
    assert "still reaches the log" in _read_log()
