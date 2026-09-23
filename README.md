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

## Quick start

```bash
uv sync
cp .env.example .env      # edit GANXTERM_ADMIN_PASS first
uv run python service.py
```

Open <http://127.0.0.1:8765/iguanaxterm> and log in with the admin account.

> Use `127.0.0.1`, not `localhost`. pytincture's development mode requires a
> literal loopback address.

### Containers

```bash
# wapyt is not on PyPI, so build its wheel into the build context first
mkdir -p vendor-wheels
(cd ../wa_pytincture_widgetset && uv build --wheel -o ../iguanaxterm_wapyt/vendor-wheels)

podman compose build && podman compose up -d
podman compose logs -f
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GANXTERM_ADMIN_USER` | `admin` | Initial admin username (first run only) |
| `GANXTERM_ADMIN_PASS` | `changeme` | Initial admin password (first run only) |
| `GANXTERM_DATA_DIR` | project dir | SQLite database, `secret.key`, `session.key` |
| `GANXTERM_SECRET_KEY` | *(generated)* | Fernet key for credential encryption |
| `GANXTERM_SESSION_SECRET` | *(generated)* | Cookie-signing secret |
| `GANXTERM_CANONICAL_ORIGIN` | `http://127.0.0.1:$PORT` | Public origin |
| `GANXTERM_ALLOWED_HOSTS` | loopback | Comma-separated exact `Host` values |
| `PORT` | `8765` | Listen port |

## Deploying behind TLS

**pytincture refuses to run authenticated over plain HTTP.** Local development
on a loopback address is the only exception. For anything else:

```bash
GANXTERM_CANONICAL_ORIGIN=https://terminal.example.com
GANXTERM_ALLOWED_HOSTS=terminal.example.com
```

The app then requires secure cookies and trusts proxy headers, so it must sit
behind a TLS-terminating reverse proxy (nginx, Caddy, Traefik). It does not
terminate TLS itself.

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
