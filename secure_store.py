"""
Thin wrapper around the OS credential store (Windows Credential Manager,
macOS Keychain, etc.) via the `keyring` library.

Tokens that were previously stored in plain text in config.json are instead
stored here under a per-service, per-key name.  config.json keeps a sentinel
value ("_keyring") to indicate that the real value lives in the credential
store.

Graceful degradation: if keyring is unavailable (headless server, CI, missing
backend), get/set fall back to plain-text in the config dict transparently.
The caller never needs to know which path was taken.
"""

from __future__ import annotations

_SERVICE = "MusicHat"
_SENTINEL = "_keyring"

try:
    import keyring as _kr
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


def _kr_get(key: str) -> str:
    try:
        val = _kr.get_password(_SERVICE, key)
        return val or ""
    except Exception:
        return ""


def _kr_set(key: str, value: str) -> bool:
    try:
        if value:
            _kr.set_password(_SERVICE, key, value)
        else:
            try:
                _kr.delete_password(_SERVICE, key)
            except Exception:
                pass
        return True
    except Exception:
        return False


def resolve(stored_value: str, key: str) -> str:
    """
    Return the actual secret for `key`.

    If `stored_value` is the sentinel ("_keyring"), retrieve the real value
    from the OS credential store.  Otherwise return `stored_value` as-is
    (backwards-compat with configs written before this module existed).
    """
    if not _AVAILABLE:
        return stored_value
    if stored_value == _SENTINEL:
        return _kr_get(key)
    return stored_value


def store(key: str, value: str) -> str:
    """
    Persist `value` in the OS credential store under `key`.

    Returns the value to write into config.json: the sentinel if keyring is
    available and the write succeeded, otherwise the plain value (fallback).
    """
    if not _AVAILABLE:
        return value
    if _kr_set(key, value):
        return _SENTINEL
    return value


# ── Named keys — keeps the key strings from scattering across the codebase ────

STREAMER_TOKEN         = "streamer_token"
STREAMER_REFRESH       = "streamer_refresh_token"
BOT_TOKEN              = "bot_token"
BOT_REFRESH            = "bot_refresh_token"
JWT_SECRET             = "jwt_secret"
DPOP_PRIVATE_KEY       = "dpop_private_key"


def get(key: str) -> str:
    """Read a secret directly from the credential store (no sentinel needed)."""
    if not _AVAILABLE:
        return ""
    return _kr_get(key)


def put(key: str, value: str) -> None:
    """Write a secret directly to the credential store (no sentinel needed)."""
    if _AVAILABLE:
        _kr_set(key, value)
