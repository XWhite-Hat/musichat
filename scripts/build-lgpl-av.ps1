# build-lgpl-av.ps1 - Build a GPL-free PyAV wheel and install it into vendor/.
#
# Run from repo root when upgrading PyAV or Python.
# Requires: VS 2022 (or later) with C++ workload, venv with Cython installed.
#
# What it does:
#   1. Downloads BtbN FFmpeg 8.1 LGPL shared build (no x264/x265)
#   2. Clones PyAV 17.1.0 source
#   3. Builds .pyd extensions with MSVC against the LGPL FFmpeg headers
#   4. Bundles FFmpeg DLLs via delvewheel (mangled names, no conflict)
#   5. Copies repaired wheel to vendor/av-lgpl-win64-cp314.whl

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT   = Split-Path $PSScriptRoot -Parent
$VENV   = Join-Path $ROOT ".venv\Scripts"
$PYTHON = Join-Path $VENV "python.exe"
$PIP    = Join-Path $VENV "pip.exe"
$VENDOR = Join-Path $ROOT "vendor"

$WORK   = Join-Path $env:TEMP "musichat-lgpl-av-build"
$FFMPEG_DIR = Join-Path $WORK "ffmpeg-lgpl"
$PYAV_DIR   = Join-Path $WORK "pyav-src"
$DIST_DIR   = Join-Path $WORK "pyav-dist"
$REPAIR_DIR = Join-Path $WORK "pyav-repaired"

$FFMPEG_URL  = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip"
$PYAV_TAG    = "v17.1.0"
$PYAV_REPO   = "https://github.com/PyAV-Org/PyAV.git"
$VCVARS      = "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"

function Log($msg) { Write-Host "[$([datetime]::Now.ToString('HH:mm:ss'))] $msg" }

# --- Prerequisite checks -------------------------------------------------------
if (-not (Test-Path $VCVARS)) {
    Log "ERROR: VS 2022 Community not found at $VCVARS"
    Log "Install 'Desktop development with C++' workload from Visual Studio Installer."
    exit 1
}

# --- Workspace setup -----------------------------------------------------------
New-Item -ItemType Directory -Force -Path $WORK, $VENDOR | Out-Null

# --- 1. Download FFmpeg 8.1 LGPL -----------------------------------------------
if (-not (Test-Path (Join-Path $FFMPEG_DIR "bin\avcodec-62.dll"))) {
    Log "Downloading FFmpeg 8.1 LGPL..."
    $zip = Join-Path $WORK "ffmpeg-lgpl.zip"
    Invoke-WebRequest -Uri $FFMPEG_URL -OutFile $zip -UseBasicParsing
    Log "Extracting..."
    Expand-Archive -Path $zip -DestinationPath $FFMPEG_DIR -Force
    Remove-Item $zip

    # Flatten: BtbN zips one level deep
    $inner = (Get-ChildItem $FFMPEG_DIR -Directory | Select-Object -First 1).FullName
    if ($inner -ne $FFMPEG_DIR) {
        Get-ChildItem $inner | Move-Item -Destination $FFMPEG_DIR
        Remove-Item $inner
    }

    # Verify no GPL DLLs
    $gpl = Get-ChildItem "$FFMPEG_DIR\bin" | Where-Object { $_.Name -match "x264|x265" }
    if ($gpl) {
        Log "ERROR: GPL DLLs found in FFmpeg build. Aborting."
        $gpl | ForEach-Object { Log "  $($_.Name)" }
        exit 1
    }
    Log "FFmpeg LGPL OK — avcodec: $((Get-ChildItem "$FFMPEG_DIR\bin\avcodec*.dll").Name)"
}

# --- 2. Install build deps -----------------------------------------------------
Log "Installing build dependencies..."
& $PIP install --quiet "cython>=3.1.0,<4" wheel delvewheel

# --- 3. Clone PyAV source ------------------------------------------------------
if (-not (Test-Path $PYAV_DIR)) {
    Log "Cloning PyAV $PYAV_TAG..."
    git clone --depth 1 --branch $PYAV_TAG $PYAV_REPO $PYAV_DIR 2>&1 | Out-Null
}

# --- 4. Build wheel with MSVC --------------------------------------------------
New-Item -ItemType Directory -Force -Path $DIST_DIR | Out-Null

$buildScript = Join-Path $WORK "build_pyav.cmd"
@"
call "$VCVARS" x64
cd /d "$PYAV_DIR"
"$PYTHON" setup.py bdist_wheel --ffmpeg-dir="$FFMPEG_DIR" --dist-dir "$DIST_DIR"
"@ | Set-Content $buildScript -Encoding ASCII

Log "Building PyAV with MSVC (this takes ~2 minutes)..."
cmd /c $buildScript
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: PyAV build failed."
    exit 1
}

$wheel = (Get-ChildItem "$DIST_DIR\*.whl" | Select-Object -First 1).FullName
if (-not $wheel) {
    Log "ERROR: No wheel produced in $DIST_DIR"
    exit 1
}
Log "Built: $(Split-Path $wheel -Leaf)"

# --- 5. Bundle DLLs with delvewheel -------------------------------------------
New-Item -ItemType Directory -Force -Path $REPAIR_DIR | Out-Null
Log "Running delvewheel..."
& $PYTHON -m delvewheel repair --add-path "$FFMPEG_DIR\bin" --wheel-dir $REPAIR_DIR $wheel
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: delvewheel failed."
    exit 1
}

$repaired = (Get-ChildItem "$REPAIR_DIR\*.whl" | Select-Object -First 1).FullName

# --- 6. Final GPL check --------------------------------------------------------
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($repaired)
$gplEntries = $zip.Entries | Where-Object { $_.FullName -match "x264|x265" }
$zip.Dispose()
if ($gplEntries) {
    Log "ERROR: GPL DLLs found in repaired wheel. Aborting."
    exit 1
}
Log "GPL check passed."

# --- 7. Install to vendor/ -----------------------------------------------------
$dest = Join-Path $VENDOR "av-17.1.0-cp314-cp314-win_amd64.whl"
Copy-Item $repaired $dest -Force
$sizeMB = [math]::Round((Get-Item $dest).Length/1MB,1)
Log "Wheel written to vendor\ ($sizeMB MB): $(Split-Path $dest -Leaf)"
Log ""
Log "Done. Run .\scripts\build.ps1 to use the new wheel."
