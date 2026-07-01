<#
.SYNOPSIS
    Generates a hash-pinned requirements-build.txt from requirements.txt.

.DESCRIPTION
    The release workflow installs dependencies using:
      pip install --require-hashes --no-deps -r requirements-build.txt

    --require-hashes means pip will REFUSE to install any package unless its
    hash is listed and matches exactly.  This defeats dependency-confusion /
    supply-chain poisoning even if an attacker manages to upload a malicious
    package with the same name and version.

    This script:
      1. Resolves all transitive dependencies of requirements.txt.
      2. Downloads the wheels for the current platform.
      3. Computes sha256 hashes.
      4. Writes requirements-build.txt with hash entries.

    Run this script whenever you update requirements.txt, then commit the
    resulting requirements-build.txt.

.PARAMETER PythonVersion
    Python version string to use (default: 3.11).

.PARAMETER OutputFile
    Path to write the lockfile (default: requirements-build.txt).

.EXAMPLE
    .\scripts\generate-lockfile.ps1
    .\scripts\generate-lockfile.ps1 -PythonVersion 3.12

.NOTES
    Requires: pip >= 23.0, pip-tools (installed into a temp venv automatically).
    Run from the repository root.
#>

[CmdletBinding()]
param(
    [string]$PythonVersion = "3.11",
    [string]$OutputFile    = "requirements-build.txt"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Locate Python ──────────────────────────────────────────────────────────────
$python = "python"
try {
    $ver = & $python --version 2>&1
    Write-Host "Using: $ver"
} catch {
    Write-Error "Python not found. Install Python $PythonVersion and add it to PATH."
    exit 1
}

# ── Create an isolated temp venv for pip-tools ────────────────────────────────
$tmpVenv = Join-Path $env:TEMP "lockfile_gen_venv_$(Get-Random)"
Write-Host "Creating temp venv at $tmpVenv ..."
& $python -m venv $tmpVenv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$pipCmd = Join-Path $tmpVenv "Scripts\pip.exe"
if (-not (Test-Path $pipCmd)) {
    # macOS / Linux path
    $pipCmd = Join-Path $tmpVenv "bin/pip"
}

# Upgrade pip inside the venv
& $pipCmd install --quiet --upgrade pip

# Install pip-tools (provides pip-compile)
Write-Host "Installing pip-tools ..."
& $pipCmd install --quiet pip-tools

$pipCompile = Join-Path $tmpVenv "Scripts\pip-compile.exe"
if (-not (Test-Path $pipCompile)) {
    $pipCompile = Join-Path $tmpVenv "bin/pip-compile"
}

# ── Compile lockfile ───────────────────────────────────────────────────────────
Write-Host "Compiling $OutputFile from requirements.txt ..."
Write-Host "(This downloads package metadata — may take a minute.)"

& $pipCompile `
    requirements.txt `
    --generate-hashes `
    --resolver=backtracking `
    --no-header `
    --output-file $OutputFile `
    --allow-unsafe `
    --quiet

if ($LASTEXITCODE -ne 0) {
    Write-Error "pip-compile failed. See output above."
    Remove-Item -Recurse -Force $tmpVenv -ErrorAction SilentlyContinue
    exit $LASTEXITCODE
}

# ── Sanity checks ──────────────────────────────────────────────────────────────
$content = Get-Content $OutputFile -Raw
$hashCount = ([regex]::Matches($content, '--hash=sha256:')).Count
Write-Host ""
Write-Host "✓ $OutputFile generated."
Write-Host "  Packages with hashes: $hashCount"

if ($hashCount -eq 0) {
    Write-Warning "No hashes were generated! Check that pip-tools supports --generate-hashes for your packages."
}

# ── Cleanup ────────────────────────────────────────────────────────────────────
Remove-Item -Recurse -Force $tmpVenv -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Review $OutputFile (check for unexpected packages)"
Write-Host "  2. git add $OutputFile && git commit -m 'deps: update hash-pinned lockfile'"
Write-Host "  3. The release workflow will use this file automatically."
