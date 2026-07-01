# build.ps1 - Build musichat.exe and smoke-test it.
#
# Usage (from repo root):
#   .\scripts\build.ps1
#   $env:MUSICHAT_VERSION="1.0.0-beta1"; .\scripts\build.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT   = Split-Path $PSScriptRoot -Parent
$VENV   = Join-Path $ROOT ".venv\Scripts"
$SPEC   = Join-Path $ROOT "build\musicplayer.spec"
$DIST   = Join-Path $ROOT "dist"
$EXE    = Join-Path $DIST "musichat.exe"
$PYINST = Join-Path $VENV "pyinstaller.exe"

function Log($msg) { Write-Host "[$([datetime]::Now.ToString('HH:mm:ss'))] $msg" }

# --- 1. Clear previous binary -------------------------------------------------
if (Test-Path $EXE) {
    Log "Removing previous binary..."
    $retries = 0
    while ((Test-Path $EXE) -and $retries -lt 6) {
        try { Remove-Item -Force $EXE -ErrorAction Stop } catch {}
        if (Test-Path $EXE) { Start-Sleep -Seconds 2 }
        $retries++
    }
    if (Test-Path $EXE) {
        Log "ERROR: Cannot remove $EXE - close any app holding the file and retry."
        exit 1
    }
    Log "Previous binary removed."
}

# --- 2. Install LGPL av wheel ------------------------------------------------
# vendor/av-lgpl-win64-cp314.whl is a PyAV build against FFmpeg 8.1 LGPL
# (no x264/x265). It replaces the PyPI av wheel which bundles GPL DLLs.
$VENDOR_AV = Join-Path $ROOT "vendor\av-17.1.0-cp314-cp314-win_amd64.whl"
if (Test-Path $VENDOR_AV) {
    Log "Installing LGPL av wheel..."
    & (Join-Path $VENV "pip.exe") install --quiet --force-reinstall $VENDOR_AV --no-deps
    if ($LASTEXITCODE -ne 0) {
        Log "ERROR: Failed to install vendor av wheel."
        exit 1
    }
    Log "LGPL av wheel installed."
}

# --- 3. Build -----------------------------------------------------------------
Log "Starting PyInstaller build..."
& $PYINST $SPEC --noconfirm --clean --distpath $DIST
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: PyInstaller failed (exit $LASTEXITCODE)."
    exit $LASTEXITCODE
}
if (-not (Test-Path $EXE)) {
    Log "ERROR: Build reported success but $EXE was not created."
    exit 1
}
$size = [math]::Round((Get-Item $EXE).Length / 1MB, 1)
Log "Build complete: $size MB -> $EXE"

# --- 4. Smoke test ------------------------------------------------------------
# Strategy: launch the exe, wait 5 s for extraction, then check if any musichat
# process with substantial RAM (>50 MB) exists. That's the real app process.
# The onefile launcher shim is tiny and exits; we don't track it at all.
Log "Launching binary for smoke test..."
$exeName = [System.IO.Path]::GetFileNameWithoutExtension($EXE)
Start-Process -FilePath $EXE -WindowStyle Normal

Log "Waiting 6 s for extraction and startup..."
Start-Sleep -Seconds 6

$alive = Get-Process $exeName -ErrorAction SilentlyContinue |
         Where-Object { $_.WorkingSet -gt 50MB }

if ($alive) {
    Log "Smoke test PASSED - process alive ($([math]::Round(($alive | Measure-Object WorkingSet -Maximum).Maximum/1MB))MB RAM)."
    Get-Process $exeName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Log "dist\musichat.exe is ready to distribute."
    exit 0
}

# No fat process found - it crashed. Build debug binary to get the traceback.
Log "Smoke test FAILED - no live process after 6 s. Building debug binary..."
$SPEC_DEBUG = Join-Path $ROOT "build\musicplayer-debug.spec"
$EXE_DEBUG  = Join-Path $DIST "musichat-debug.exe"

$spec = Get-Content $SPEC -Raw
($spec -replace 'console=False','console=True') -replace 'name="musichat"','name="musichat-debug"' |
    Out-File $SPEC_DEBUG -Encoding utf8

if (Test-Path $EXE_DEBUG) { Remove-Item -Force $EXE_DEBUG -ErrorAction SilentlyContinue }

Log "Building debug binary (console=True)..."
& $PYINST $SPEC_DEBUG --noconfirm --distpath $DIST 2>$null
Remove-Item $SPEC_DEBUG -ErrorAction SilentlyContinue

if (Test-Path $EXE_DEBUG) {
    Log "Running debug binary - waiting up to 15 s for crash output..."
    $dbg = Start-Process -FilePath $EXE_DEBUG -PassThru `
           -RedirectStandardOutput "$env:TEMP\mh_out.txt" `
           -RedirectStandardError  "$env:TEMP\mh_err.txt" `
           -WindowStyle Hidden
    $dbg.WaitForExit(15000)
    if (-not $dbg.HasExited) {
        $dbg.Kill()
        Log "Debug binary still alive after 15 s - crash appears to be fixed. Check dist\musichat-debug.exe manually."
    } else {
        $out = (Get-Content "$env:TEMP\mh_out.txt" -ErrorAction SilentlyContinue) -join "`n"
        $err = (Get-Content "$env:TEMP\mh_err.txt" -ErrorAction SilentlyContinue) -join "`n"
        if ($out) { Log "stdout:`n$out" }
        if ($err) { Log "stderr:`n$err" }
        if (-not $out -and -not $err) { Log "(no output captured)" }
    }
} else {
    Log "Debug build also failed - check PyInstaller output above."
}
exit 1
