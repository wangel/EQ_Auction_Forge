<#
.SYNOPSIS
    Build, stage, and (optionally) publish a Windows release of EQ Auction Forge.

.DESCRIPTION
    Compiles EQ-Auction_Forge.py with Nuitka in --standalone mode (a normal
    program folder, NOT a self-extracting onefile exe). The onefile/dropper
    shape is what tripped antivirus false positives; standalone scans clean.

    Steps: read APP_VERSION from the source -> Nuitka build -> stage the dist
    folder + items.txt.gz + README.md -> zip. With -Publish it also creates the
    matching GitHub release via gh.

.PARAMETER Publish
    After staging+zipping, create the GitHub release (tag vX.Y.Z) with gh.
    Requires release_stage\NOTES_v<version>.md and a clean, pushed HEAD
    (gh tags the current commit).

.PARAMETER SkipBuild
    Reuse the existing Nuitka dist folder; only re-stage and re-zip.

.EXAMPLE
    .\build.ps1                # build + stage + zip (no publish)
    .\build.ps1 -Publish       # build + stage + zip + GitHub release
#>
[CmdletBinding()]
param(
    [switch]$Publish,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

# --- version: single source of truth is APP_VERSION in the .py ---
$src = Join-Path $root 'EQ-Auction_Forge.py'
$verLine = Select-String -Path $src -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $verLine) { throw "Could not find APP_VERSION in $src" }
$Version = $verLine.Matches[0].Groups[1].Value

$python    = Join-Path $root '.venv\Scripts\python.exe'
$distDir   = Join-Path $root 'nuitka_standalone\EQ-Auction_Forge.dist'
$stageName = "EQ_Auction_Forge_v${Version}_Windows"
$stageDir  = Join-Path $root "release_stage\$stageName"
$zipPath   = Join-Path $root "$stageName.zip"

Write-Host "=== EQ Auction Forge build: v$Version ===" -ForegroundColor Cyan
if (-not (Test-Path $python)) { throw "venv python not found: $python" }

# --- 1. compile (Nuitka standalone) ---
if (-not $SkipBuild) {
    if (Test-Path 'nuitka_standalone') { Remove-Item 'nuitka_standalone' -Recurse -Force }
    Write-Host "Compiling with Nuitka (first build is slow; reuses ccache after)..." -ForegroundColor DarkCyan
    & $python -m nuitka `
        --standalone `
        --enable-plugin=tk-inter `
        --windows-console-mode=disable `
        --assume-yes-for-downloads `
        --output-dir=nuitka_standalone `
        --output-filename=EQ_Auction_Forge.exe `
        EQ-Auction_Forge.py
    if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed (exit $LASTEXITCODE)" }
}
$exe = Join-Path $distDir 'EQ_Auction_Forge.exe'
if (-not (Test-Path $exe)) { throw "Build output missing: $exe" }

# --- 2. stage: dist folder + the item DB + README ---
if (Test-Path $stageDir) { Remove-Item $stageDir -Recurse -Force }
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null
Copy-Item (Join-Path $distDir '*') $stageDir -Recurse -Force
Copy-Item (Join-Path $root 'items.txt.gz') $stageDir -Force
Copy-Item (Join-Path $root 'README.md')    $stageDir -Force
Write-Host "Staged -> $stageDir" -ForegroundColor Green

# --- 3. zip (top-level folder preserved inside the archive) ---
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path $stageDir -DestinationPath $zipPath -CompressionLevel Optimal
$zipMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host "Packaged -> $zipPath ($zipMB MB)" -ForegroundColor Green

# --- 4. publish (optional) ---
if ($Publish) {
    $notes = Join-Path $root "release_stage\NOTES_v$Version.md"
    if (-not (Test-Path $notes)) { throw "Release notes not found: $notes" }
    $tag = "v$Version"
    Write-Host "Creating GitHub release $tag ..." -ForegroundColor Cyan
    & gh release create $tag $zipPath --title "EQ Auction Forge v$Version" --notes-file $notes
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed (exit $LASTEXITCODE)" }
    Write-Host "Published release $tag" -ForegroundColor Green
} else {
    Write-Host "NOT published. Re-run with -Publish to create the GitHub release v$Version." -ForegroundColor Yellow
}
