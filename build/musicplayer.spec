# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for MusicHat.

Run from the repo root:
  pyinstaller build/musicplayer.spec --noconfirm --clean --distpath dist

The MUSICHAT_VERSION env var is injected by the release workflow.
If unset (local build), falls back to 'dev'.

DATA FILES
──────────
• server/static/     — spectrogram browser source and mod panel HTML
• resources/         — fonts bundled with the app
• customtkinter/     — theme data for the first-run setup wizard

PySide6 is intentionally NOT bundled.
It is downloaded into the user's data folder on first run (LGPL compliance:
the user can replace the shared library with any compatible version).
The bootstrap (launcher.py → bootstrap_check.py) adds the user's
data_dir/pyside6 directory to sys.path before importing main.py.
"""

import os
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
HERE    = Path(SPECPATH).parent    # repo root — spec lives in build/, parent is repo root
VERSION = os.environ.get("MUSICHAT_VERSION", "dev")

# Bake the version into a tiny data file that version.py reads at runtime.
_ver_file = HERE / "_bundled_version.txt"
_ver_file.write_text(VERSION, encoding="utf-8")

# ── customtkinter — collect everything (data, binaries, hidden submodules) ─────
try:
    from PyInstaller.utils.hooks import collect_all
    ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all("customtkinter")
    _dk_datas, _dk_bins, _dk_hidden = collect_all("darkdetect")
    ctk_datas      += _dk_datas
    ctk_binaries   += _dk_bins
    ctk_hiddenimports += _dk_hidden
except Exception:
    ctk_datas, ctk_binaries, ctk_hiddenimports = [], [], []

# ── Analysis ───────────────────────────────────────────────────────────────────
a = Analysis(
    [str(HERE / "launcher.py")],   # bootstrap entry point (not main.py directly)
    pathex=[str(HERE)],
    binaries=[*ctk_binaries],
    datas=[
        # (source, destination_in_bundle)
        (str(HERE / "server" / "static"), "server/static"),
        (str(HERE / "resources"), "resources"),
        (str(HERE / "assets"), "assets"),
        (str(_ver_file), "."),   # version.py reads this at runtime
        *ctk_datas,
    ],
    hiddenimports=[
        # FastAPI / Starlette / Pydantic internals
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi",
        "fastapi.middleware",
        "fastapi.middleware.cors",
        "starlette.routing",
        "starlette.applications",
        "starlette.middleware",
        "starlette.middleware.sessions",
        "starlette.responses",
        "starlette.staticfiles",
        "pydantic",
        "pydantic.networks",
        "pydantic.functional_validators",
        # anyio is FastAPI's async backend
        "anyio",
        "anyio._backends._asyncio",
        # sounddevice ships PortAudio as a DLL
        "sounddevice",
        # twitchio websocket backend
        "websockets",
        "websockets.legacy",
        "websockets.legacy.client",
        # aiohttp — used by twitchio and channel-points EventSub
        "aiohttp",
        "aiohttp.client",
        "aiohttp.connector",
        "aiohttp.client_ws",
        # Pillow — album art overlay
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        # yt-dlp extractors
        "yt_dlp",
        "yt_dlp.extractor",
        "yt_dlp.extractor.youtube",
        "yt_dlp.extractor.soundcloud",
        # av (PyAV) codec detection
        "av",
        "av.audio",
        "av.video",
        # jwt
        "jwt",
        "jwt.algorithms",
        # customtkinter — first-run setup wizard (MIT licence)
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        *ctk_hiddenimports,
    ],
    excludes=[
        # PySide6 is loaded from the user's data folder at runtime, not bundled.
        # This is the LGPL compliance mechanism — the user can swap the library.
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtMultimedia",
        "shiboken6",
        # Test frameworks
        "pytest",
        "matplotlib",
        "IPython",
        "notebook",
    ],
    noarchive=False,
    optimize=1,
)


# ── PYZ (Python archive) ───────────────────────────────────────────────────────
pyz = PYZ(a.pure)


# ── EXE (main executable) ──────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="musichat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        # UPX corrupts Qt plugins on some platforms — exclude them.
        "qwindows.dll",
        "qcocoa.dylib",
        "libqxcb.so",
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version_info=None,
    icon=str(HERE / "assets" / "icon.ico"),
    onefile=True,
)


# ── macOS app bundle (optional) ────────────────────────────────────────────────
# app = BUNDLE(
#     exe,
#     name="MusicHat.app",
#     icon=str(HERE / "resources" / "icon.icns"),
#     bundle_identifier="dev.xwhitehat.musichat",
#     info_plist={
#         "CFBundleShortVersionString": VERSION,
#         "LSUIElement": False,
#         "NSMicrophoneUsageDescription":
#             "MusicHat uses your audio device to capture output for the spectrogram.",
#     },
# )
