#!/usr/bin/env bash
# Rebuild and restart the local IguanaXterm container from the working tree.
#
#   scripts/podman-run.sh            rebuild and restart
#   scripts/podman-run.sh --logs     ... and follow the logs
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

cd "$ROOT"

[ -f .env ] || { echo "no .env — copy .env.example and set GANXTERM_ADMIN_PASS" >&2; exit 1; }
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"

# wapyt is not on PyPI, so its wheel has to be in the build context.
echo "==> building the wapyt wheel"
mkdir -p vendor-wheels && rm -f vendor-wheels/wapyt-*.whl
( cd ../wa_pytincture_widgetset && uv build --wheel -o "$ROOT/vendor-wheels" >/dev/null )

echo "==> building $IMAGE:$VERSION"
podman build -t "$IMAGE:$VERSION" -t "$IMAGE:latest" -f Containerfile .

echo "==> restarting $NAME"
podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d \
  --name "$NAME" \
  --hostname "$NAME" \
  --restart unless-stopped \
  --label app=IguanaXterm \
  -p "127.0.0.1:$PORT:8765" \
  --env-file .env \
  -e GANXTERM_DATA_DIR=/data \
  -e "GANXTERM_CANONICAL_ORIGIN=http://127.0.0.1:$PORT" \
  -v "$VOLUME:/data" \
  "$IMAGE:latest" >/dev/null

printf '==> waiting for startup'
for _ in $(seq 1 60); do
  if podman logs "$NAME" 2>&1 | grep -q "Application startup complete"; then
    echo; echo "    IguanaXterm $VERSION on http://127.0.0.1:$PORT/iguanaxterm"
    echo "    (use 127.0.0.1, not localhost — pytincture requires a literal loopback address)"
    [ "${1:-}" = "--logs" ] && podman logs -f "$NAME"
    exit 0
  fi
  printf '.'; sleep 1
done

echo; echo "did not start; last lines:" >&2
podman logs "$NAME" 2>&1 | tail -20 >&2
exit 1
