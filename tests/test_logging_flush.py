"""
Log durability.

A log that only reaches disk on a clean shutdown is worthless: the crashes
worth diagnosing are exactly the ones that never get to flush.  The first
release-mode build with logging enabled produced a 0-byte log file after a
force-kill, which is what these tests pin down.
"""
from __future__ import annotations

import logging
import logging.handlers
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import logging_setup


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICHAT_DATA_DIR", str(tmp_path))
    import data_dir
    monkeypatch.setattr(data_dir, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(logging_setup, "_installed", False)
    monkeypatch.setattr(logging_setup, "_log_path", None)
    root = logging.getLogger()
    saved, root.handlers = root.handlers[:], []
    out, err = sys.stdout, sys.stderr
    yield tmp_path
    for h in root.handlers:
        try:
            h.close()
        except Exception:
            pass
    root.handlers = saved
    sys.stdout, sys.stderr = out, err


def test_print_reaches_disk_without_an_explicit_flush(fresh):
    path = Path(logging_setup.install())
    print("this must be on disk immediately")
    # Deliberately no flush() call — that is the point.
    assert "this must be on disk immediately" in path.read_text(encoding="utf-8")


def test_log_survives_a_hard_kill(tmp_path):
    """
    The real scenario: a process terminated without cleanup.

    Runs a child that writes to the log and then dies via os._exit, which skips
    atexit handlers, buffer flushing and interpreter shutdown entirely.
    """
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        os.environ["MUSICHAT_DATA_DIR"] = {str(tmp_path)!r}
        import logging_setup
        logging_setup.install()
        print("written just before a hard kill")
        os._exit(1)   # no flush, no atexit, no cleanup
    """)
    script_path = tmp_path / "child.py"
    script_path.write_text(script, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 1

    log = tmp_path / "logs" / "musichat.log"
    assert log.exists(), "no log file was created at all"
    contents = log.read_text(encoding="utf-8")
    assert contents.strip(), "log file exists but is empty — buffered content was lost"
    assert "written just before a hard kill" in contents


def test_unicode_is_stored_as_real_codepoints_not_replacements(fresh):
    """
    Verify at byte level, not by printing.

    Checking this by printing the log to a cp1252 terminal shows replacement
    characters and looks like corruption — the same encoding trap this module
    exists to solve, just moved into the diagnostic.
    """
    path = Path(logging_setup.install())
    print("em dash — and arrow → and check ✓")
    text = path.read_bytes().decode("utf-8")
    assert "\ufffd" not in text, "characters were replaced on the way to disk"
    assert "\u2014" in text and "\u2192" in text and "\u2713" in text
