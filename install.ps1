# Install photo-importer as a global `photo-importer` command, runnable from
# any directory, via pipx (an editable install -- pulls from this repo
# checkout, so future edits here take effect without reinstalling).
#
# Windows. For macOS/Linux, use install.sh instead.
#
# Usage (from an elevated or normal PowerShell prompt, in the repo directory):
#   .\install.ps1
# If script execution is disabled, run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigDir = Join-Path $env:APPDATA "photo-importer"
$ConfigFile = Join-Path $ConfigDir "config.yaml"

if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Write-Host "pipx not found -- installing..."
    python -m pip install --user pipx
    python -m pipx ensurepath
    Write-Host "pipx installed. You may need to restart your terminal for PATH changes to take effect."
}

Write-Host "Installing photo-importer (editable, from $RepoDir)..."
pipx install --editable --force $RepoDir

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
if (Test-Path $ConfigFile) {
    Write-Host "Existing config found at $ConfigFile, leaving it alone."
} elseif (Test-Path (Join-Path $RepoDir "config.yaml")) {
    Copy-Item (Join-Path $RepoDir "config.yaml") $ConfigFile
    Write-Host "Copied $RepoDir\config.yaml -> $ConfigFile"
} else {
    Copy-Item (Join-Path $RepoDir "config.example.yaml") $ConfigFile
    Write-Host "No config.yaml found -- created $ConfigFile from the example template."
    Write-Host "Edit it to set your local_root and NAS settings before running."
}

Write-Host ""
Write-Host "Done. Try: photo-importer --help (from any directory)"
Write-Host ""
Write-Host "Note: NAS sync needs rsync on PATH, which Windows doesn't ship." -ForegroundColor Yellow
Write-Host "Install it via WSL, or a native port such as cwrsync." -ForegroundColor Yellow
if (-not (Get-Command photo-importer -ErrorAction SilentlyContinue)) {
    Write-Host "Note: pipx's bin directory isn't on your PATH in this shell yet. Open a new terminal." -ForegroundColor Yellow
}
