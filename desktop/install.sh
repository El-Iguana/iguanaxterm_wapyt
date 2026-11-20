#!/usr/bin/env bash
# Install the IguanaXterm desktop app for this user: Electron (npm), the icon,
# and a menu entry pointing at this checkout. Run again after moving the repo.
#
# The window opens the IguanaXterm already running (the container on
# 127.0.0.1:8765 by default); with none running it starts service.py from
# this checkout, which needs the .venv (uv sync).
set -euo pipefail

DESKTOP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DESKTOP")"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

command -v npm >/dev/null || { echo "npm not found (brew install node)" >&2; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || echo "Note: no .venv, so the local fallback will use 'uv run' (or run 'uv sync' in $ROOT)" >&2

(cd "$DESKTOP" && npm ci --no-audit --no-fund)
# Newer npm skips dependency install scripts unless allowed; Electron's
# downloads the binary, so run it if the binary is missing.
[ -x "$DESKTOP/node_modules/electron/dist/electron" ] || node "$DESKTOP/node_modules/electron/install.js"

mkdir -p "$APPS" "$ICONS"
install -m 644 "$DESKTOP/icon.png" "$ICONS/iguanaxterm.png"
cat > "$APPS/iguanaxterm.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=IguanaXterm
GenericName=Terminal Manager
Comment=SSH, Telnet, RDP and file transfers in one window
Exec="$DESKTOP/node_modules/electron/dist/electron" "$DESKTOP"
Icon=iguanaxterm
Terminal=false
Categories=System;TerminalEmulator;Network;RemoteAccess;
Keywords=ssh;sftp;ftp;telnet;rdp;terminal;
StartupWMClass=iguanaxterm
Actions=local;

[Desktop Action local]
Name=Start a local server
Exec="$DESKTOP/node_modules/electron/dist/electron" "$DESKTOP" --local
ENTRY
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: IguanaXterm is in the application menu ($APPS/iguanaxterm.desktop)"
