"""
bootstrap_check.py — pre-PySide6 bootstrap orchestration.

Must be imported (and run() called) before any PySide6 reference in the
process.  In development (not frozen) it sets the data dir to the default
location and returns immediately — no wizard, no download.

Bootstrap config lives at a fixed OS location that never moves:
  Windows:  %LOCALAPPDATA%\\musichat\\bootstrap.json
  Fallback: ~/musichat_bootstrap.json

bootstrap.json schema:
  {
    "data_dir":        "<absolute path chosen by user>",
    "pyside6_version": "<version string, e.g. 6.8.1>"
  }

After run() returns:
  - os.environ["MUSICHAT_DATA_DIR"] is set
  - sys.path[0] is data_dir/pyside6  (in frozen mode)
  - data_dir/pyside6/PySide6/ DLL directory is registered with Windows
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ── Bootstrap config location ──────────────────────────────────────────────────

def _bootstrap_config_path() -> Path:
    local_app = os.environ.get(
        "LOCALAPPDATA",
        str(Path.home() / "AppData" / "Local"),
    )
    return Path(local_app) / "musichat" / "bootstrap.json"


_DEFAULT_DATA_DIR = str(Path.home() / ".streamdeck_music")


# ── Public ─────────────────────────────────────────────────────────────────────

def run() -> None:
    """
    Orchestrate bootstrap:
      - Development (not frozen): set env var to default, return.
      - Frozen binary: read bootstrap.json, run wizard/recovery if needed,
        add PySide6 to sys.path and the Windows DLL search path.
    """
    if not getattr(sys, "frozen", False):
        # Running from source — PySide6 is in the venv, use legacy default dir
        os.environ.setdefault("MUSICHAT_DATA_DIR", _DEFAULT_DATA_DIR)
        return

    # Disposable-subprocess entry point for _pyside6_import_ok() below — must
    # be checked before anything else touches bootstrap.json.
    if len(sys.argv) >= 3 and sys.argv[1] == "--pyside6-smoke-test":
        _run_smoke_test_subprocess(sys.argv[2])
        return  # unreachable — _run_smoke_test_subprocess always exits

    cfg_path = _bootstrap_config_path()
    cfg      = _read_bootstrap_config(cfg_path)

    data_dir  = cfg.get("data_dir") if cfg else None
    valid_dir = data_dir and Path(data_dir).is_dir()

    if not valid_dir:
        # First run or data dir was deleted / moved
        data_dir = _run_setup_wizard(cfg_path)
        if not data_dir:
            # User cancelled — can't continue
            _show_abort_notice()
            sys.exit(0)
    else:
        # Data dir exists — verify the PySide6 install is complete.
        # Checking directory existence alone is not enough: a partial
        # download, a failed extraction, or a "keep PySide6" wipe that left
        # stale directories behind will all pass the dir check but crash at
        # import time.  The .pyside6_version sentinel is only written by
        # pyside_downloader after a fully successful extraction, so its
        # presence is a reliable "install completed" signal.
        pyside6_root  = Path(data_dir) / "pyside6"
        pyside6_dir   = pyside6_root / "PySide6"
        shiboken6_dir = pyside6_root / "shiboken6"
        ver_file      = pyside6_root / ".pyside6_version"
        install_ok    = (
            pyside6_dir.is_dir()
            and shiboken6_dir.is_dir()
            and ver_file.exists()
        )
        if not install_ok:
            dirs_exist = pyside6_dir.is_dir() and shiboken6_dir.is_dir()
            reason = "incomplete" if dirs_exist else "missing"
            data_dir = _run_recovery(data_dir, cfg_path, reason=reason)
            if not data_dir:
                _show_abort_notice()
                sys.exit(0)

    # The checks above only prove the expected files exist — never that they
    # actually load.  A stale install left by a different/older MusicHat
    # version, an interrupted "remove & exit" (deferred deletion didn't
    # finish before this launch), antivirus quarantine, or disk corruption
    # can all pass every structural check above and still fail here.  Catch
    # it now, before main.py or anything else starts up, so a broken install
    # gets the same clear recovery dialog every other case does — instead of
    # an opaque crash deep inside the app that nobody can diagnose.
    #
    # This check runs in a disposable subprocess (_pyside6_import_ok), not
    # here.  It must not run in this process: verifying it here would mean
    # calling _add_pyside6_to_path()'s ctypes.WinDLL() preload (see its
    # docstring) just to test importability, and those DLLs stay locked in
    # this process for the rest of its life once loaded.  If the test then
    # failed and recovery tried to wipe and re-extract that same directory
    # in this same process, the wipe would silently fail on the locked files
    # and the re-extraction would hang trying to overwrite them — this was
    # reproduced directly: a repair triggered right after a fresh install
    # hung forever re-extracting shiboken6.  A subprocess's locks vanish the
    # instant it exits, so the real process here never touches a locked file.
    #
    # Retried a few times before giving up: freshly-written, unsigned DLLs
    # can transiently fail to load right after being written (e.g. while an
    # antivirus on-access scan is still inspecting them) and succeed moments
    # later with no change on disk.
    ok = False
    for _attempt in range(1, 4):
        if _pyside6_import_ok(data_dir):
            ok = True
            break
        print(f"[bootstrap] PySide6 import check attempt {_attempt}/3 failed", flush=True)
        if _attempt < 3:
            time.sleep(1.5)

    if not ok:
        data_dir = _run_recovery(data_dir, cfg_path, reason="import_failed")
        if not data_dir or not _pyside6_import_ok(data_dir):
            _show_abort_notice()
            sys.exit(1)

    os.environ["MUSICHAT_DATA_DIR"] = data_dir
    _ensure_env_file(Path(data_dir))
    _add_pyside6_to_path(Path(data_dir))


# ── Internal ───────────────────────────────────────────────────────────────────

def _pyside6_import_ok(data_dir: str) -> bool:
    """
    Verify PySide6 actually imports from *data_dir*, out-of-process.

    Spawns this same frozen exe with a hidden "--pyside6-smoke-test" mode
    (handled at the top of run()) that does the import and exits 0/1.  See
    the comment in run() for why this must not happen in the real process.
    """
    try:
        result = subprocess.run(
            [sys.executable, "--pyside6-smoke-test", data_dir],
            timeout=30,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        return result.returncode == 0
    except Exception as exc:
        print(f"[bootstrap] smoke-test subprocess failed to run: {exc!r}", flush=True)
        return False


def _run_smoke_test_subprocess(data_dir: str) -> None:
    """Entry point when this exe is invoked as the disposable subprocess
    spawned by _pyside6_import_ok().  Exits 0 on success, 1 on failure —
    whatever DLLs get locked here are released the moment this exits."""
    _add_pyside6_to_path(Path(data_dir))
    try:
        import shiboken6            # noqa: F401
        from PySide6 import QtCore  # noqa: F401
    except Exception as exc:
        print(f"[bootstrap] smoke-test subprocess import failed: {exc!r}", flush=True)
        sys.exit(1)
    sys.exit(0)


def _read_bootstrap_config(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_bootstrap_config(data_dir: str, pyside6_version: str = "") -> None:
    """Write (or update) the bootstrap config.  Called by wizard + migration."""
    path = _bootstrap_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_bootstrap_config(path) or {}
    existing["data_dir"]        = data_dir
    existing["pyside6_version"] = pyside6_version or existing.get("pyside6_version", "")
    with path.open("w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2)


def read_bootstrap_config() -> dict:
    """Return the bootstrap config dict (empty dict if not found)."""
    return _read_bootstrap_config(_bootstrap_config_path()) or {}


def _run_setup_wizard(cfg_path: Path) -> Optional[str]:
    from bootstrap_ui import run_setup_wizard

    # Just the parent — the wizard's "create a MusicHat folder inside this
    # location" checkbox (checked by default) appends the "MusicHat" segment
    # itself.  Pre-including it here would double it up to .../MusicHat/MusicHat.
    default = str(Path.home() / "Documents")
    data_dir = run_setup_wizard(default)
    if not data_dir:
        return None

    # Read installed version from sentinel file if present
    ver_file = Path(data_dir) / "pyside6" / ".pyside6_version"
    version  = ver_file.read_text(encoding="utf-8").strip() if ver_file.exists() else ""

    write_bootstrap_config(data_dir, version)
    return data_dir


def _run_recovery(data_dir: str, cfg_path: Path, reason: str = "missing") -> Optional[str]:
    from bootstrap_ui import run_recovery_dialog

    result = run_recovery_dialog(data_dir, reason=reason)
    if not result:
        return None

    ver_file = Path(result) / "pyside6" / ".pyside6_version"
    version  = ver_file.read_text(encoding="utf-8").strip() if ver_file.exists() else ""
    write_bootstrap_config(result, version)
    return result


def _add_pyside6_to_path(data_dir: Path) -> None:
    """Prepend the PySide6 install dir to sys.path and register its DLLs."""
    pyside6_root  = data_dir / "pyside6"
    pyside6_pkg   = pyside6_root / "PySide6"
    shiboken6_pkg = pyside6_root / "shiboken6"

    sys.path.insert(0, str(pyside6_root))

    # On Windows, register DLL search directories so Windows can find Qt and
    # shiboken6 DLLs before the first import.  shiboken6 must be registered
    # first because PySide6/__init__.py depends on it at import time.
    if sys.platform == "win32":
        for dll_dir in (shiboken6_pkg, pyside6_pkg):
            if dll_dir.is_dir():
                try:
                    os.add_dll_directory(str(dll_dir))
                except (AttributeError, OSError):
                    pass

        # os.add_dll_directory() alone is not enough — confirmed by building
        # both the console and windowed (console=False) variants from
        # identical source: the console build imports PySide6 fine, the
        # windowed one fails with "DLL load failed while importing Shiboken:
        # The specified module could not be found" every time, even against
        # a brand-new install.  PyInstaller's windowed bootloader does not
        # reliably honour the safe-DLL-search-mode directories added via
        # os.add_dll_directory() the way the console bootloader does.
        #
        # Explicitly loading the native DLLs here sidesteps that entirely:
        # once a DLL of a given name is already resident in the process,
        # Windows reuses it to satisfy any later dependency lookup by that
        # name, regardless of what search path the bootloader set up.
        _preload_native_dlls(shiboken6_pkg)
        _preload_native_dlls(pyside6_pkg)

    _ensure_shiboken6_init(shiboken6_pkg)


def _preload_native_dlls(pkg_dir: Path) -> None:
    """Load every .dll directly inside *pkg_dir* into the process.

    Each file is loaded via its full path, so Windows resolves its own
    transitive dependencies against its containing directory regardless of
    the process-wide DLL search path — the same guarantee os.add_dll_directory
    is supposed to provide but doesn't in a windowed PyInstaller build.
    """
    if not pkg_dir.is_dir():
        return
    import ctypes
    for dll in sorted(pkg_dir.glob("*.dll")):
        try:
            ctypes.WinDLL(str(dll))
        except OSError as exc:
            print(f"[bootstrap] could not preload {dll.name}: {exc!r}", flush=True)


def _ensure_shiboken6_init(shiboken6_pkg: Path) -> None:
    """
    shiboken6 6.10+ ships two wheel variants: an abi3 wheel (contains
    __init__.py and the Python package files) and a cp-version-specific wheel
    (contains only Shiboken.pyd).  pip installs both; pyside_downloader picks
    only the best-ranked wheel and may pick the cp-specific one, leaving no
    __init__.py.  Without it, shiboken6 is a namespace package whose __file__
    is None.  shiboken6's internal signature_bootstrap then does
    Path(shiboken6.__file__).parent and crashes with a Fatal Python error.

    If __init__.py is absent, write the same stub that ships with the abi3
    wheel so shiboken6 is treated as a regular package.
    """
    init = shiboken6_pkg / "__init__.py"
    if init.exists():
        return
    if not (shiboken6_pkg / "Shiboken.pyd").exists():
        return
    try:
        init.write_text(
            "# Synthesised by MusicHat bootstrap — shiboken6 package init shim.\n"
            "# The cp-specific shiboken6 wheel omits this file; the abi3 wheel\n"
            "# includes it.  pip installs both; pyside_downloader picks one.\n"
            "# Pre-loading these modules mirrors what the abi3 __init__.py does\n"
            "# so that signature_bootstrap can find them in a frozen binary.\n"
            "import sys, os, zipfile, base64, marshal, io, contextlib, textwrap\n"
            "import traceback, types, struct, re, tempfile, keyword, functools, typing\n"
            "from shiboken6.Shiboken import *\n",
            encoding="utf-8",
        )
    except OSError:
        pass


_BYOI_ENV_TEMPLATE = """\
# MusicHat — BYOI (Bring Your Own Integration) Configuration
# ─────────────────────────────────────────────────────────────────────────────
# By default MusicHat uses a secure proxy (musicauth.xwhitehat.dev) to handle
# Twitch authentication — no client secret is ever bundled in the binary.
#
# To use your own registered Twitch application instead:
#   1. Go to https://dev.twitch.tv/console → "Register Your Application"
#   2. Set OAuth Redirect URI to:  http://localhost:7329/callback
#   3. Category: Application Integration
#   4. Copy the Client ID and Client Secret and uncomment both lines below
#   5. Save this file and restart MusicHat
#
# Auth Code flow is used in BYOI mode — a refresh token is issued so
# re-authentication is not required every time the access token expires.
# ─────────────────────────────────────────────────────────────────────────────

# TWITCH_CLIENT_ID=your_client_id_here
# TWITCH_CLIENT_SECRET=your_client_secret_here
"""


def _ensure_env_file(data_dir: Path) -> None:
    """Create a .env template in data_dir if one does not already exist."""
    env_path = data_dir / ".env"
    if env_path.exists():
        return
    try:
        env_path.write_text(_BYOI_ENV_TEMPLATE, encoding="utf-8")
    except OSError:
        pass


def _show_abort_notice() -> None:
    """
    Show a plain tkinter message (no customtkinter needed) telling the user
    the app cannot start without PySide6.
    """
    try:
        import tkinter as tk
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showerror(
            "MusicHat cannot start",
            "MusicHat requires the PySide6 library to run.\n\n"
            "Please restart MusicHat to set up the data folder, or install "
            "PySide6 manually and place it in your data folder.",
        )
        root.destroy()
    except Exception:
        print("[bootstrap] MusicHat cannot start: PySide6 not found.", flush=True)


