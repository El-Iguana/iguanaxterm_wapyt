# IguanaXterm

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-2.0.0-green.svg)]()

A browser-based SSH/Telnet terminal manager with SFTP. Manage all your remote
connections from a single web UI — no client software required.

![IguanaXterm](appcode/static/el_iguana.png)

Version 2 is a rewrite onto [pytincture](https://github.com/pytincture/pytincture)
and the [wapyt](../wa_pytincture_widgetset) widgetset: the UI is Python running
in the browser under Pyodide instead of 1,350 lines of hand-written JavaScript,
and the hand-rolled REST API and token store are replaced by pytincture's
backend-for-frontend layer.

## Features

- **SSH & Telnet** — connect to any host directly in the browser
- **SFTP** — browse, filter, upload (drag and drop), download, rename, delete
- **Multi-tab** — several terminals and file browsers at once, each kept alive
  in the background
- **Session library** — saved connections organised into folders, with a filter
- **Terminal search** — Ctrl+F over the scrollback
- **Auto-reconnect** — exponential backoff, up to 5 attempts
- **Host-key pinning** — trust on first use, and a refusal (not a silent accept)
  when a host key changes
- **Any key type** — RSA, Ed25519, ECDSA, with passphrase support
- **Multi-user** — private session libraries, plus an admin panel
- **Encrypted at rest** — SSH passwords and private keys under Fernet (AES-128)

## Planned

- **Tiled panes via [GridStack](https://gridstackjs.com/#demo)** — drag and
  resize terminals and file browsers into a grid instead of stacking them in
  tabs, so several sessions stay visible at once. Layouts save per user.
  Wanted as a wapyt widget so any pytincture app can use it; GridStack is MIT
  and would be vendored and served same-origin like xterm, since pytincture's
  CSP blocks CDNs.
- **Transfer resume** — HTTP range requests for interrupted downloads.

## Stack

| Layer | Tech |
|---|---|
| UI | Python in the browser (Pyodide) + wapyt widgets |
| Backend | pytincture (FastAPI) + Uvicorn |
| SSH/SFTP | Paramiko |
| Telnet | asyncio + an RFC 854/1073 IAC parser |
| Auth | pytincture sessions + bcrypt |
| Terminal | xterm.js 5.5, vendored and served same-origin |

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
| `PORT` | `8765` | Listen port |

## Data persistence

One volume, `ganxterm_data`, mounted at `/data`:

- `iguanaxterm.db` — users and saved connection profiles
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
uv run --with pytest python -m pytest tests/ -q

# after editing any wapyt asset
(cd ../wa_pytincture_widgetset && ./scripts/dev_wheel.sh ../iguanaxterm_wapyt/appcode)
```

## License

MIT. See [LICENSE](LICENSE).

xterm.js is vendored under `appcode/vendor/xterm/` and keeps its own MIT
licence, reproduced there as `LICENSE.xterm`.
