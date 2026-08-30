"""
logging_setup.py — capture diagnostics to a rotating file in the data dir.

Why this exists
───────────────
Two independent blackouts meant a released build produced no diagnostics at all:

  1. PyInstaller's windowed bootloader (console=False) sets sys.stdout to None,
     which makes every print() a silent no-op.  The app has ~156 diagnostic
     print() calls; in a release build none of them went anywhere.

  2. When stdout *does* exist but is a cp1252 console or a redirected pipe,
     printing any non-ASCII character (the codebase uses →, …, ⚠, ✗) raises
     UnicodeEncodeError.  That is not theoretical — it killed the app on
     shutdown during testing.

Rather than rewrite 156 call sites, install() replaces sys.stdout/sys.stderr
with a tee that forwards to the original stream (when there is one) *and*
appends to a rotating log file opened as UTF-8 with errors="replace".  Existing
print() calls keep working unchanged, non-ASCII can never raise, and the output
survives in a file the user can send us.

The standard logging module is configured against the same file, so new code
can use logging directly instead of print().
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import TextIO

LOG_FILENAME = "musichat.log"
_MAX_BYTES   = 2 * 1024 * 1024   # 2 MB per file
_BACKUPS     = 3                 # musichat.log + .1 .2 .3  ≈ 8 MB ceiling

_installed = False
_log_path: str | None = None


class _Tee:
    """
    Minimal file-like object that writes to a log file and, when present, the
    original stream.

    Deliberately forgiving: this sits underneath every print() in the app, so a
    failure here would break unrelated code that has nothing to do with logging.
    Every write is wrapped — losing a diagnostic line is always preferable to
    raising out of a print().
    """

    def __init__(self, original: TextIO | None, sink: TextIO) -> None:
        self._original = original
        self._sink     = sink

    def write(self, text: str) -> int:
        if self._original is not None:
            try:
                self._original.write(text)
            except Exception:
                # Most likely UnicodeEncodeError on a cp1252 console.  Retry
                # with unrepresentable characters replaced so the line still
                # reaches the console instead of vanishing.
                try:
                    enc = getattr(self._original, "encoding", None) or "ascii"
                    self._original.write(
                        text.encode(enc, errors="replace").decode(enc, errors="replace")
                    )
                except Exception:
                    pass
        try:
            self._sink.write(text)
            # Flush every line rather than relying on buffering.  A crash or a
            # force-kill is precisely when the log matters, and that is also
            # exactly when buffered content is lost — an empty log file after a
            # hard exit is worse than useless.  Volume here is a few hundred
            # lines per session, so the cost is irrelevant.
            self._sink.flush()
        except Exception:
            pass
        return len(text)

    def flush(self) -> None:
        for stream in (self._original, self._sink):
            if stream is not None:
                try:
                    stream.flush()
                except Exception:
                    pass

    def isatty(self) -> bool:
        try:
            return bool(self._original is not None and self._original.isatty())
        except Exception:
            return False

    # Some libraries probe for these before writing.
    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def fileno(self):
        if self._original is None:
            raise OSError("no underlying stream")
        return self._original.fileno()


def log_path() -> str | None:
    """Absolute path of the active log file, or None before install()."""
    return _log_path


def install() -> str | None:
    """
    Route diagnostics to <data_dir>/logs/musichat.log.  Safe to call twice.

    Returns the log path, or None if the log file could not be opened (in which
    case the app still runs — it just keeps whatever output behaviour it had).
    """
    global _installed, _log_path
    if _installed:
        return _log_path

    try:
        from data_dir import DATA_DIR
        log_dir = os.path.join(DATA_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, LOG_FILENAME)

        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUPS,
            encoding="utf-8",
            errors="replace",       # the fix for the cp1252 crash class
            delay=False,
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)

        # Tee print() output into the same file.  handler.stream is the open
        # UTF-8 file object, so writes through it inherit errors="replace".
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout = _Tee(sys.stdout, handler.stream)   # type: ignore[assignment]
        sys.stderr = _Tee(sys.stderr, handler.stream)   # type: ignore[assignment]

        _installed = True
        _log_path  = path

        import datetime
        try:
            from version import APP_VERSION
        except Exception:
            APP_VERSION = "unknown"
        # Record the stream situation we inherited.  Whether stdout exists at
        # all, and what encoding it claims, decides where diagnostics can go —
        # and it is the first thing worth knowing when a log looks wrong.
        def _describe(stream: object) -> str:
            if stream is None:
                return "None (windowed build — print() alone would be a no-op)"
            return (f"{type(stream).__name__} "
                    f"encoding={getattr(stream, 'encoding', '?')}")

        print(
            f"\n=== MusicHat {APP_VERSION} started "
            f"{datetime.datetime.now().isoformat(timespec='seconds')} ==="
        )
        print(f"[logging] file:   {path}")
        print(f"[logging] stdout: {_describe(original_stdout)}")
        print(f"[logging] stderr: {_describe(original_stderr)}")
        return path
    except Exception as exc:
        # Never let logging setup prevent the app from starting.
        try:
            print(f"[logging] could not install file logging: {exc!r}")
        except Exception:
            pass
        return None
