"""
version.py — single source of truth for the running app version.

Frozen binary: reads _bundled_version.txt written by the spec at build time.
Dev / source:  falls back to MUSICHAT_VERSION env var, then "dev".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _read() -> str:
    if getattr(sys, "frozen", False):
        try:
            return (Path(sys._MEIPASS) / "_bundled_version.txt").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            pass
    return os.environ.get("MUSICHAT_VERSION", "dev")


APP_VERSION: str = _read()
