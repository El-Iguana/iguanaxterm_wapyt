# IguanaXterm

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-2.0.0-green.svg)]()

A browser-based SSH/Telnet terminal manager with SFTP and FTP file browsing. Manage all your remote
connections from a single web UI — no client software required. Tile them side
by side, or keep them in tabs; either way the workspace is there when you come
back.

![IguanaXterm](appcode/static/el_iguana.png)

Version 2 is a rewrite onto [pytincture](https://github.com/pytincture/pytincture)
and the [wapyt](https://github.com/WAwesome-AI/wa_pytincture_widgetset)
widgetset: the UI is Python running in the browser under Pyodide instead of
1,350 lines of hand-written JavaScript, and the hand-rolled REST API and token
store are replaced by pytincture's backend-for-frontend layer.

## Features

### Workspace

Every connection is a **pane** carrying its own terminal *and* its own file
browser behind a two-entry tab strip — no more hunting for the file tab that
belongs to a given host. Connect to the same host twice and you get two panes
with two independent shells.

The workspace lays panes out two ways, switched from the toolbar:

- **Tabbed** — one pane at a time, the classic layout.
- **Tiled** — panes in a drag-and-resize grid ([GridStack](https://gridstackjs.com)),
  so several sessions stay visible at once. Drag a pane by its grip, resize
  from the corner; the remote PTY follows the tile.

Any tile can be **maximized** to fill the workspace and restored again — by its
button, or with Escape when the focus is not in a terminal (inside one, Escape
belongs to the remote; that is how you leave insert mode in vim). Maximizing is
purely a view: the grid keeps its positions, so restoring puts everything back
exactly as it was and the saved layout is never touched.

Switching modes moves panes, it does not rebuild them: scrollback, sockets and
whatever you were typing all survive the switch, in both directions.

**Your layout comes back.** Mode, tile positions and sizes, which tab each pane
was on and the directory it was browsing are all saved per user. Restored panes
deliberately do **not** reconnect — each one offers a Reconnect button, because
dialling every saved session at once on page load is how you trip a server's
`MaxStartups` limit and get a screen full of banner errors.

### Connections

- **SSH & Telnet** — connect to any host directly in the browser
- **SFTP and FTP profiles** — files-only connections for hosts that have no
  shell to offer, like a NAS. FTP upgrades to TLS (`AUTH TLS`) whenever the
  server supports it, and pins the certificate on first use the same way an
  SSH host key is pinned
- **Session library** — saved connections organised into folders, with a filter
- **Terminal search** — Ctrl+F over the scrollback
- **Auto-reconnect** — exponential backoff, up to 5 attempts
- **Transient-failure retry** — a reset banner or a refused connection is
  retried up to 3 times before you ever see an error
- **Host-key pinning** — trust on first use, and a refusal (not a silent accept)
  when a host key changes
- **Key auth** — RSA, Ed25519 and ECDSA, with passphrase support (DSA is gone;
  Paramiko 5 dropped it)

### Files

- **A file browser per pane** — browse, filter, rename, delete, make folders,
  over SFTP or FTP/FTPS alike
- **Download where you want it** — a folder picker and a filename prompt before
  the transfer, not a dump into `~/Downloads` (Chrome and Edge; elsewhere the
  panel says so up front)
- **Upload files or whole folders** — pick individual files, or a directory
  whose structure is recreated on the far side. Drag and drop works too.
- **A transfer queue** with per-file progress, and cancel
- **Narrow-pane aware** — in a small tile the toolbar collapses to icons and
  secondary columns give way, so the filename never gets squeezed out

### Accounts

- **Multi-user** — private session libraries, plus an admin panel
- **Encrypted at rest** — SSH passwords and private keys under Fernet (AES-128)

## Planned

- **Transfer resume** — HTTP range requests for interrupted downloads.

## Stack

| Layer | Tech |
|---|---|
| UI | Python in the browser (Pyodide) + wapyt widgets |
| Backend | pytincture (FastAPI) + Uvicorn |
| SSH/SFTP | Paramiko |
| FTP/FTPS | ftplib, behind a paramiko-shaped adapter |
| Telnet | asyncio + an RFC 854/1073 IAC parser |
| Auth | pytincture sessions + bcrypt |
| Terminal | xterm.js 5.5, vendored and served same-origin |
| Tiling | GridStack 14, vendored, loaded on demand |

## Install

### Prerequisites

Either a container engine (**Podman 4+** or **Docker 20.10+**, each with their
compose plugin) or, for running from source, **Python 3.13** and
[uv](https://docs.astral.sh/uv/).

The image builds `wapyt` from a local wheel, so clone both repos side by side:

```bash
git clone https://github.com/El-Iguana/iguanaxterm_wapyt.git
git clone https://github.com/WAwesome-AI/wa_pytincture_widgetset.git
cd iguanaxterm_wapyt
```

`pytincture` is pulled from git during the build and needs no local checkout.

### 1. Configure

```bash
cp .env.example .env
```

Set `GANXTERM_ADMIN_PASS` before the first run — it creates the admin account,
and only on the first run. Everything else has a working default.

### 2. Build the widgetset wheel

`wapyt` is not on PyPI, so its wheel goes into the build context:

```bash
mkdir -p vendor-wheels
(cd ../wa_pytincture_widgetset && uv build --wheel -o ../iguanaxterm_wapyt/vendor-wheels)
```

Repeat this whenever the widgetset changes.

### 3a. Podman

One command rebuilds the widgetset wheel, rebuilds the image and restarts the
container:

```bash
scripts/podman-run.sh            # add --logs to follow the output
```

It runs as `iguanaxterm` — container name, hostname and `app=IguanaXterm`
label — on <http://127.0.0.1:8765/iguanaxterm>, with `restart: unless-stopped`
so it survives a reboot.

Or with compose:

```bash
podman compose build
podman compose up -d
podman compose logs -f
```

`podman compose` delegates to the Docker Compose CLI plugin, which talks to
Podman over its socket. If it reports *"failed to connect to the docker API at
unix:///run/user/$UID/podman/podman.sock"*, start the socket:

```bash
systemctl --user enable --now podman.socket
```

The `podman-compose` Python tool is an alternative that needs no socket. Or
skip compose entirely — see *Without compose* below.

Rootless Podman cannot bind ports below 1024, so keep 8765 or put a proxy in
front.

### 3b. Docker

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

The `Containerfile` is an ordinary Dockerfile and `compose.yaml` names it
explicitly, so both engines read the same two files. If your user is not in the
`docker` group you will need `sudo`, or rootless Docker.

### Without compose

```bash
podman build -t iguanaxterm -f Containerfile .      # or: docker build ...

podman run -d --name iguanaxterm \
  -p 127.0.0.1:8765:8765 \
  -e GANXTERM_ADMIN_PASS='choose-something' \
  -e GANXTERM_CANONICAL_ORIGIN=http://127.0.0.1:8765 \
  -v ganxterm_data:/data \
  iguanaxterm
```

Open <http://127.0.0.1:8765/iguanaxterm>.

> **Use `127.0.0.1`, not `localhost`.** pytincture's development mode requires
> a literal loopback address and rejects the name.

### From source

```bash
uv sync
cp .env.example .env      # edit GANXTERM_ADMIN_PASS first
uv run python service.py
```

`service.py` reads `.env` itself, so it behaves the same in and out of a
container.

To develop against a local pytincture checkout instead of the pinned git tag:

```bash
uv add --editable ../pytincture
```

### Verifying it came up

```bash
podman logs iguanaxterm | tail            # expect "Application startup complete"
curl -I -H 'Host: 127.0.0.1:8765' http://127.0.0.1:8765/iguanaxterm   # 307 to /login
```

The first browser load takes 30–60 seconds while Pyodide boots and the
widgetset is installed. It is cached afterwards.

## Deploying beyond localhost

**pytincture refuses to serve authenticated plain HTTP anywhere but loopback.**
Reaching the app from another machine therefore needs TLS in front and two
variables set:

```bash
GANXTERM_CANONICAL_ORIGIN=https://terminal.example.com
GANXTERM_ALLOWED_HOSTS=terminal.example.com
```

`GANXTERM_ALLOWED_HOSTS` takes **hostnames, not host:port** — a port is
stripped if you include one. The canonical origin's hostname is added
automatically, so the two cannot disagree.

With an https canonical origin the app also requires secure cookies and trusts
proxy headers, so it must sit behind a TLS-terminating reverse proxy (nginx,
Caddy, Traefik). It does not terminate TLS itself, and the proxy must forward
`X-Forwarded-Proto`.

**Let large uploads through the proxy.** Uploads stream straight through to
the remote host, so there is no size limit in the app beyond
`GANXTERM_MAX_UPLOAD_BYTES`, but proxies have their own: nginx refuses any
body over **1 MB** by default, which fails every photo. Set
`client_max_body_size 0;` (or a real cap) and `proxy_request_buffering off;` on
this site, and raise `proxy_read_timeout` for slow links.

This is also what makes "choose where to save" work: the File System Access API
needs a secure context, so downloads can only offer a destination picker over
https or on loopback.

### Updating

```bash
git pull
(cd ../wa_pytincture_widgetset && git pull && \
   uv build --wheel -o ../iguanaxterm_wapyt/vendor-wheels)
podman compose build && podman compose up -d
```

The `ganxterm_data` volume carries the database and keys across rebuilds.
**Back up `secret.key`** — losing it makes every stored credential
unrecoverable.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GANXTERM_ADMIN_USER` | `admin` | Initial admin username (first run only) |
| `GANXTERM_ADMIN_PASS` | `changeme` | Initial admin password (first run only) |
| `GANXTERM_DATA_DIR` | project dir (`/data` in the image) | SQLite database, `secret.key`, `session.key` |
| `GANXTERM_SECRET_KEY` | *(generated)* | Fernet key for credential encryption |
| `GANXTERM_SESSION_SECRET` | *(generated)* | Cookie-signing secret |
| `GANXTERM_CANONICAL_ORIGIN` | `http://127.0.0.1:$PORT` | Public origin. An `https://` value switches on production mode |
| `GANXTERM_ALLOWED_HOSTS` | `127.0.0.1` | Comma-separated hostnames (no ports); the canonical origin's host is always added |
| `GANXTERM_MAX_UPLOAD_BYTES` | `68719476736` (64 GiB) | Largest single upload. Every other request stays capped at 2 MiB |
| `PORT` | `8765` | Listen port |

## Data persistence

One volume, `ganxterm_data`, mounted at `/data`:

- `iguanaxterm.db` — users, saved connection profiles and workspace layouts
- `secret.key` — Fernet key. **Back this up.** Losing it means every stored
  credential is unrecoverable.
- `session.key` — cookie-signing secret; losing it just logs everyone out.

## Security notes

- Change the default admin password on first login.
- Saved credentials are encrypted at rest and are **never sent to the browser** —
  the session editor shows whether a secret is stored, not the secret.
- Host keys are pinned per saved session on first connect. If a host key
  changes, the connection is refused; clear the pin from the session's
  right-click menu once you know why it changed.
- Login is rate-limited by pytincture (20 attempts per 60s, per IP and per
  account).

## Development

See `CLAUDE.md` for the framework-specific pitfalls — they are not obvious and
several of them fail silently.

```bash
# unit tests: plain CPython, no browser, no containers
uv run --with pytest python -m pytest tests/ -q

# after editing any wapyt asset, rebuild the dev wheel
(cd ../wa_pytincture_widgetset && ./scripts/dev_wheel.sh ../iguanaxterm_wapyt/appcode)

# a local container that identifies itself as IguanaXterm, on 127.0.0.1:8765
./scripts/podman-run.sh
```

### Smoke tests

`tests/smoke/` drives the real UI in a real browser against a throwaway SSH
container. It is the only thing that exercises the WebSocket relay, the PTY
resize path and the SFTP pool — everything else is unit-level. See
[tests/smoke/README.md](tests/smoke/README.md) for setup.

| Script | Covers |
|---|---|
| `live_ssh_smoke.py` | terminal, PTY sizing, search, SFTP browsing |
| `transfer_smoke.py` | download, folder download, upload, the queue |
| `large_upload_smoke.py` | 40 MB over SFTP and FTPS, folder uploads, cancel, the 2 MiB cap elsewhere |
| `upload_race_smoke.py` | the file-picker activation race |
| `pane_smoke.py` | panes, lazy file mounting, independent shells |
| `narrow_pane_smoke.py` | the SFTP panel from 1200px down to 320px |
| `tiled_smoke.py` | the grid, and switching layout modes |
| `layout_smoke.py` | persistence, and that a restore dials nothing |
| `ftp_smoke.py` | files-only FTP panes over TLS, against `ftp_target.py` |
| `maximize_smoke.py` | zooming a tile, and that the grid model is untouched |
| `reparent_spike.py` | that a live terminal survives being moved |
| `resize_storm_probe.py` | PTY resize traffic during a drag (a measurement) |

Two are worth knowing about even if you never run them. `reparent_spike.py`
is why the tiling works the way it does: moving a mounted xterm to a new DOM
parent keeps its buffer, socket and stdin, so switching modes and dragging a
tile are both a plain `appendChild`. `resize_storm_probe.py` is why
`fit_debounce_ms` exists — one 1.3s resize drag sent **41** PTY resizes before
it, and **1** after.

## License

MIT. See [LICENSE](LICENSE).

xterm.js and GridStack are vendored under `appcode/vendor/` and keep their own
MIT licences, reproduced there as `LICENSE.xterm` and `LICENSE.gridstack`.
Both are served same-origin rather than from a CDN, which pytincture's CSP
blocks.
