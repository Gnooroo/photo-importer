#!/usr/bin/env bash
# Install photo-importer as a global `photo-importer` command, runnable from
# any directory, via pipx (an editable install -- pulls from this repo
# checkout, so future edits here take effect without reinstalling).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/photo-importer"
CONFIG_FILE="$CONFIG_DIR/config.yaml"

if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx not found -- installing via Homebrew..."
  brew install pipx
  pipx ensurepath
fi

echo "Installing photo-importer (editable, from $REPO_DIR)..."
pipx install --editable --force "$REPO_DIR"

mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
  echo "Existing config found at $CONFIG_FILE, leaving it alone."
elif [ -f "$REPO_DIR/config.yaml" ]; then
  cp "$REPO_DIR/config.yaml" "$CONFIG_FILE"
  echo "Copied $REPO_DIR/config.yaml -> $CONFIG_FILE"
else
  cp "$REPO_DIR/config.example.yaml" "$CONFIG_FILE"
  echo "No config.yaml found -- created $CONFIG_FILE from the example template."
  echo "Edit it to set your local_root and NAS settings before running."
fi

echo ""
echo "Done. Try: photo-importer --help (from any directory)"
if ! command -v photo-importer >/dev/null 2>&1; then
  echo "Note: pipx's bin directory isn't on your PATH in this shell yet."
  echo "Run 'pipx ensurepath', then open a new terminal."
fi
