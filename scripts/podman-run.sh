#!/usr/bin/env bash
# Rebuild and restart the local *development* IguanaXterm container from the
# working tree and the sibling wapyt checkout. Linux + Podman only.
# To install IguanaXterm, use compose instead: see INSTALL.md.
#
#   scripts/podman-run.sh            rebuild and restart
#   scripts/podman-run.sh --logs     ... and follow the logs
#
# --userns=keep-id makes the container's user (uid 10001, gid 999) *be* you on
# the host, so what it saves into the downloads folder is yours to open and
# delete. :U re-owns the data volume to match, since files written under the
# old mapping would otherwise read as someone else's ("attempt to write a
# readonly database").
#
# Uses plain podman rather than compose: `podman compose` delegates to the
# Docker Compose CLI plugin and needs the podman socket running, which is one
# more thing to go wrong for a local test instance.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME=iguanaxterm
IMAGE=localhost/iguanaxterm
PORT="${PORT:-8765}"
VOLUME=ganxterm_data
# Folder downloads the browser cannot write itself are saved here, one
# subdirectory per user. A dedicated folder, not ~/Downloads itself: the :z
# below relabels it for SELinux.
DOWNLOADS="${GANXTERM_DOWNLOAD_HOST_DIR:-$HOME/Downloads/IguanaXterm}"

cd "$ROOT"

[ -f .env ] || { echo "no .env — copy .env.example and set GANXTERM_ADMIN_PASS" >&2; exit 1; }
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"

# Build against the sibling wapyt checkout, not the commit the Containerfile
# pins, so unmerged widgetset work shows up here. A filtered copy, because the
# checkout carries a test virtualenv of a couple of hundred MB.
WAPYT="$(cd ../wa_pytincture_widgetset && pwd)"
CONTEXT="$(mktemp -d)"
trap 'rm -rf "$CONTEXT"' EXIT
tar -C "$WAPYT" --exclude=.git --exclude=.venv --exclude=__pycache__ \
    --exclude=build --exclude=dist --exclude='*.whl' -cf - . | tar -C "$CONTEXT" -xf -

echo "==> building $IMAGE:$VERSION (wapyt from $WAPYT)"
podman build --build-context "wapyt-src=$CONTEXT" \
  -t "$IMAGE:$VERSION" -t "$IMAGE:latest" -f Containerfile .

mkdir -p "$DOWNLOADS"

echo "==> restarting $NAME"
podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d \
  --name "$NAME" \
  --hostname "$NAME" \
  --restart unless-stopped \
  --label app=IguanaXterm \
  --userns=keep-id:uid=10001,gid=999 \
  -p "127.0.0.1:$PORT:8765" \
  --env-file .env \
  -e GANXTERM_DATA_DIR=/data \
  -e "GANXTERM_CANONICAL_ORIGIN=http://127.0.0.1:$PORT" \
  -e GANXTERM_DOWNLOAD_DIR=/downloads \
  -e "GANXTERM_DOWNLOAD_HOST_DIR=${DOWNLOADS/#$HOME/\~}" \
  -v "$VOLUME:/data:U" \
  -v "$DOWNLOADS:/downloads:z" \
  "$IMAGE:latest" >/dev/null

printf '==> waiting for startup'
for _ in $(seq 1 60); do
  if podman logs "$NAME" 2>&1 | grep -q "Application startup complete"; then
    echo; echo "    IguanaXterm $VERSION on http://127.0.0.1:$PORT/iguanaxterm"
    echo "    (use 127.0.0.1, not localhost — pytincture requires a literal loopback address)"
    echo "    server-side folder downloads: $DOWNLOADS"
    [ "${1:-}" = "--logs" ] && podman logs -f "$NAME"
    exit 0
  fi
  printf '.'; sleep 1
done

echo; echo "did not start; last lines:" >&2
podman logs "$NAME" 2>&1 | tail -20 >&2
exit 1
