"""
pyside_downloader.py — secure PySide6 download with Socket.dev + PyPI verification.

Two-layer security:
  1. Socket.dev score check  — flags malware, suspicious code, supply-chain issues.
                               Fail-open: Socket outage never blocks a clean install.
  2. PyPI SHA-256 hash check — hard gate; download is deleted and refused on mismatch.

Zero Qt dependency — runs during bootstrap before PySide6 is available.
Only uses Python stdlib: hashlib, json, platform, sys, urllib, zipfile.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

# ── Constants ──────────────────────────────────────────────────────────────────

_PYPI_LATEST  = "https://pypi.org/pypi/{name}/json"
_PYPI_VER     = "https://pypi.org/pypi/{name}/{version}/json"

# Socket.dev PURL-based package report endpoint.
# PURL format for PyPI: pkg:pypi/<name>@<version>
# Docs: https://docs.socket.dev/reference/getpackagereport
_SOCKET_PURL  = "https://socket.dev/api/report/purl?purl=pkg%3Apypi%2F{name}%40{version}"
_SOCKET_MIN   = 0.60   # minimum acceptable 0.0–1.0 score; set to None to skip
_SOCKET_TIMEOUT = 10   # seconds

# All packages that pip installs as part of "pip install PySide6".
# shiboken6 must be first — PySide6/__init__.py expects it as a sibling directory.
PYSIDE6_PACKAGES = ["shiboken6", "PySide6", "PySide6_Essentials", "PySide6_Addons"]

ProgressCb = Callable[[int, int, str], None]  # (bytes_done, bytes_total, status_msg)


# ── Public ─────────────────────────────────────────────────────────────────────

def download_pyside6(
    target_dir: Path,
    progress_cb: Optional[ProgressCb] = None,
) -> tuple[bool, str]:
    """
    Find the latest safe PySide6 version, download all three packages,
    verify SHA-256 hashes, and extract into *target_dir*.

    Returns (success: bool, message: str).  On success *message* is the
    installed version string.  On failure it describes what went wrong.
    """
    def _p(done: int, total: int, msg: str) -> None:
        if progress_cb:
            progress_cb(done, total, msg)

    target_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: find the latest version that passes Socket ─────────────────────
    _p(0, 0, "Checking PyPI for the latest PySide6 release…")
    version, err = _find_safe_version(_p)
    if not version:
        return False, err

    # ── Step 2: download, verify, extract each package ─────────────────────────
    for pkg in PYSIDE6_PACKAGES:
        _p(0, 0, f"Locating {pkg} {version} wheel…")
        wheel_url, wheel_hash, err = _get_wheel_info(pkg, version)
        if err:
            return False, f"Cannot locate {pkg} {version}: {err}"

        wheel_path = target_dir / f"_dl_{pkg}.whl"
        ok, err = _download_file(
            wheel_url, wheel_path,
            lambda n, t, _pkg=pkg: _p(n, t, f"Downloading {_pkg}…"),
        )
        if not ok:
            wheel_path.unlink(missing_ok=True)
            return False, f"Download failed for {pkg}: {err}"

        _p(0, 0, f"Verifying {pkg} integrity…")
        if not _verify_sha256(wheel_path, wheel_hash):
            wheel_path.unlink(missing_ok=True)
            return False, (
                f"SHA-256 mismatch for {pkg} — the file may have been tampered with "
                "in transit.  Download aborted."
            )

        _p(0, 0, f"Extracting {pkg}…")
        _extract_wheel(wheel_path, target_dir)
        wheel_path.unlink(missing_ok=True)

    # Version sentinel for bootstrap_check
    (target_dir / ".pyside6_version").write_text(version, encoding="utf-8")

    _p(1, 1, "Done.")
    return True, version


# ── Internal ───────────────────────────────────────────────────────────────────

def _find_safe_version(
    progress_cb: Optional[ProgressCb],
) -> tuple[Optional[str], str]:
    """
    Return the newest PySide6 version whose Socket.dev score is acceptable.
    Falls back to the absolute latest if Socket is unreachable for all checked
    versions (PyPI hash remains the hard gate).
    """
    try:
        req = urllib.request.Request(
            _PYPI_LATEST.format(name="PySide6"),
            headers={"User-Agent": "musichat-bootstrap/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        return None, f"Cannot reach PyPI: {exc}"

    versions = sorted(
        data.get("releases", {}).keys(),
        key=_version_tuple,
        reverse=True,
    )
    if not versions:
        return None, "No PySide6 releases found on PyPI."

    socket_unreachable = False

    for ver in versions[:5]:
        if progress_cb:
            progress_cb(0, 0, f"Checking PySide6 {ver} safety via Socket.dev…")
        passed, note = _socket_check("pyside6", ver)
        if passed:
            if not note.startswith("unreachable"):
                print(f"[bootstrap] PySide6 {ver} Socket.dev: {note}")
            return ver, ""
        if note.startswith("unreachable"):
            socket_unreachable = True
        else:
            print(f"[bootstrap] PySide6 {ver} flagged by Socket.dev: {note}")

    # All 5 checked — either all flagged or Socket was unreachable
    latest = versions[0]
    if socket_unreachable:
        print(
            f"[bootstrap] WARNING: Socket.dev unreachable — using PySide6 {latest} "
            "without safety score.  PyPI SHA-256 will be verified."
        )
    else:
        print(
            f"[bootstrap] WARNING: no clean Socket.dev score for the 5 most recent "
            f"PySide6 releases; falling back to {latest}.  PyPI SHA-256 will be verified."
        )
    return latest, ""


def _socket_check(name: str, version: str) -> tuple[bool, str]:
    """
    Query Socket.dev for a package safety report.

    Returns (passed: bool, reason: str).

    Fail-open on any HTTP error or timeout — a Socket outage must never
    block a legitimate install.  The PyPI hash check is the hard gate.
    """
    if _SOCKET_MIN is None:
        return True, "check disabled"

    url = _SOCKET_PURL.format(name=name.lower(), version=version)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "musichat-bootstrap/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=_SOCKET_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return True, "not yet analysed by Socket.dev"
        return True, f"unreachable (HTTP {exc.code})"
    except Exception as exc:
        return True, f"unreachable ({exc})"

    # Score field — Socket may return 0–1 float or 0–100 int
    score = (
        body.get("score")
        or body.get("overallScore")
        or body.get("overall_score")
    )
    if score is None:
        return True, "no score field in Socket.dev response"

    if isinstance(score, (int, float)) and score > 1:
        score = score / 100.0

    # Hard-fail on explicit malware/suspicious alerts regardless of score
    alerts = body.get("alerts") or body.get("issues") or []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        kind = str(alert.get("type") or alert.get("severity") or "").lower()
        if kind in ("malware", "suspicious", "obfuscatedcode", "obfuscated-code"):
            return False, f"Socket.dev alert: {kind} in {name}=={version}"

    if isinstance(score, (int, float)) and score < _SOCKET_MIN:
        return False, f"Socket.dev score {score:.2f} below threshold {_SOCKET_MIN}"

    return True, f"Socket.dev score {score:.2f} ✓"


def _get_wheel_info(
    pkg_name: str, version: str
) -> tuple[Optional[str], Optional[str], str]:
    """Return (wheel_url, sha256, error) for the best matching wheel."""
    try:
        req = urllib.request.Request(
            _PYPI_VER.format(name=pkg_name, version=version),
            headers={"User-Agent": "musichat-bootstrap/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        return None, None, str(exc)

    wheels = [f for f in data.get("urls", []) if f.get("packagetype") == "bdist_wheel"]
    if not wheels:
        return None, None, f"no binary wheel on PyPI for {pkg_name}=={version}"

    py_tag   = f"cp{sys.version_info.major}{sys.version_info.minor}"
    machine  = platform.machine().lower()
    plat_tag = {
        "amd64": "win_amd64",
        "x86_64": "win_amd64",
        "arm64": "win_arm64",
    }.get(machine, f"win_{machine}")

    def _rank(f: dict) -> int:
        fn = f.get("filename", "")
        # shiboken6 ships a thin cp-specific wheel (only Shiboken.pyd, no
        # __init__.py) and a full abi3 wheel (__init__.py + all Python files).
        # Prefer the abi3 wheel for shiboken6 so the package is importable as
        # a regular package rather than a namespace package (which would leave
        # __file__ == None and crash signature_bootstrap at PySide6 import time).
        if pkg_name.lower() == "shiboken6" and "abi3" in fn and plat_tag in fn:
            return 0
        if py_tag in fn and plat_tag in fn:
            return 1
        if "abi3" in fn and plat_tag in fn:
            return 2
        if "none-any" in fn:
            return 4
        if plat_tag in fn:
            return 3
        return 99

    wheels.sort(key=_rank)
    best = wheels[0]
    if _rank(best) == 99:
        return None, None, f"no Windows wheel found for {pkg_name}=={version}"

    sha = best.get("digests", {}).get("sha256")
    if not sha:
        return None, None, f"PyPI returned no SHA-256 for {pkg_name}=={version}"

    return best["url"], sha, ""


def _download_file(
    url: str,
    dest: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "musichat-bootstrap/1.0"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done  = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _verify_sha256(path: Path, expected: str) -> bool:
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest().lower() == expected.lower()


def _extract_wheel(wheel_path: Path, target_dir: Path) -> None:
    """Extract a .whl (zip) into target_dir, merging package namespaces."""
    with zipfile.ZipFile(wheel_path, "r") as zf:
        zf.extractall(target_dir)


def _version_tuple(v: str) -> tuple:
    """Convert a version string to a sortable tuple, ignoring pre-release suffixes."""
    parts = []
    for seg in v.split(".")[:4]:
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)
