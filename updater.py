"""
updater.py — background GitHub Releases update check.

Checks once on startup (after a short delay) and again on demand.
Never blocks the UI — runs in a daemon thread.
Fail-open: network errors are silently swallowed; the app starts fine.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from typing import Callable, Optional

from version import APP_VERSION

# ── Config ─────────────────────────────────────────────────────────────────────
# Update before publishing to GitHub.
GITHUB_REPO   = "xwhite-hat/musichat"
RELEASES_URL  = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
_STARTUP_DELAY = 15  # seconds after boot before the first check

# ── State (module-level, thread-safe reads for simple bool/str) ────────────────
_lock            = threading.Lock()
_latest_version: Optional[str] = None
_update_available: bool         = False


def get_status() -> dict:
    """Return a snapshot of current update state — safe to call from any thread."""
    with _lock:
        return {
            "current":   APP_VERSION,
            "latest":    _latest_version,
            "available": _update_available,
            "releases_page": RELEASES_PAGE,
        }


def check_now() -> dict:
    """Run a synchronous update check and return the new status dict."""
    global _latest_version, _update_available
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={
                "User-Agent":  f"musichat/{APP_VERSION}",
                "Accept":      "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "").lstrip("v").strip()
        available = _is_newer(tag, APP_VERSION) if tag else False
        with _lock:
            _latest_version   = tag or None
            _update_available = available
    except Exception:
        pass
    return get_status()


def start_background_check(on_update_found: Optional[Callable[[str], None]] = None) -> None:
    """
    Kick off a one-shot daemon thread that waits _STARTUP_DELAY seconds then
    checks GitHub.  Calls on_update_found(latest_tag) on the calling thread's
    context if a newer version is found (the callback must be thread-safe).
    """
    def _run() -> None:
        import time
        time.sleep(_STARTUP_DELAY)
        status = check_now()
        if status["available"] and status["latest"] and on_update_found:
            try:
                on_update_found(status["latest"])
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True, name="update-check").start()


# ── Semver helpers ──────────────────────────────────────────────────────────────

def _parse_ver(v: str) -> tuple:
    """Parse a version string to a comparable tuple, ignoring pre-release suffixes."""
    parts = []
    for seg in v.split(".")[:3]:
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _is_newer(remote: str, local: str) -> bool:
    if not remote or local == "dev":
        return False
    return _parse_ver(remote) > _parse_ver(local)
