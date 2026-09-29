# IguanaXterm

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-2.0.0-green.svg)]()

A browser-based SSH/Telnet terminal manager with SFTP and FTP file browsing,
and remote desktops over VNC. Manage all your remote
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
`MaxStartups` limit and get a screen full of banner errors. To bring them all
back, **Reconnect all** appears in the toolbar with a count of the waiting
panes. It dials them one after another, each waiting for the previous one to
connect or fail, so it never makes the burst that restoring avoids.

### Connections

- **SSH & Telnet** — connect to any host directly in the browser
- **SFTP and FTP profiles** — files-only connections for hosts that have no
  shell to offer, like a NAS. FTP upgrades to TLS (`AUTH TLS`) whenever the
  server supports it, and pins the certificate on first use the same way an
  SSH host key is pinned
- **Session library** — saved connections organised into folders, with a filter
- **Terminal search** — Ctrl+F over the scrollback
- **Copy and paste** — as in Windows Terminal: Ctrl+C copies a selection
  (and interrupts without one), Ctrl+Shift+C always copies, Ctrl+V and
  Ctrl+Shift+V paste (Cmd on a Mac), and right-click offers Copy, Paste and
  Select all. Ctrl+V is no longer sent to the remote; in vim, Ctrl+Q starts
  a visual block instead
- **Auto-reconnect** — exponential backoff, up to 5 attempts
- **Transient-failure retry** — a reset banner or a refused connection is
  retried up to 3 times before you ever see an error
- **Host-key pinning** — trust on first use, and a refusal (not a silent accept)
  when a host key changes
- **Key auth** — RSA, Ed25519 and ECDSA, with passphrase support (DSA is gone;
  Paramiko 5 dropped it)

### Remote desktops

- **VNC in a pane** — a remote desktop tiles, maximizes and restores like any
  other connection, scaled to fit its tile, with a Ctrl+Alt+Del button
  (the browser cannot pass that combination through itself)
- **Direct, or through an SSH session** — a VNC profile either connects
  straight to `host:port` (a trusted LAN only: VNC is unencrypted), or rides one
  of your saved SSH sessions to a desktop that listens only on that machine's
  `localhost`. Tunnelling reuses the session's key and pinned host key
- **Clipboard both ways** — what you copy on the remote desktop lands in
  your clipboard, and Ctrl+V inside the desktop first sends your clipboard
  across and then pastes, so the remote app pastes what you copied here. A
  **Paste to desktop** header button does the same explicitly (Firefox needs
  it). Classic VNC clipboard text is Latin-1: x11vnc turns characters beyond
  it into `?`
- **The VNC password stays on the server** — the relay does the VNC login and
  hands the browser an already-authenticated desktop, so the password is never
  sent to the page
- **Supported logins:** no password, and VNC password (what x11vnc, wayvnc,
  KDE's krfb and most NAS consoles use). TigerVNC's default VeNCrypt/TLS is not;
  set `SecurityTypes=VncAuth` on it and reach it through an SSH tunnel for
  encryption

A minimal server for a Linux desktop, reachable only through SSH:

```bash
x11vnc -display :0 -localhost -rfbport 5900 -usepw -forever -shared
```

Then add a **VNC** profile with Host `localhost`, Port `5900`, the password
from `x11vnc -storepasswd`, and **Connect** set to that machine's SSH session.

### Files

- **A file browser per pane** — browse, filter, rename, delete, make folders,
  over SFTP or FTP/FTPS alike
- **Download where you want it** — a folder picker and a filename prompt before
  the transfer, not a dump into `~/Downloads` (Chrome and Edge; elsewhere the
  panel says so up front)
- **Windows- and Mac-safe folder downloads** — a Linux server allows names
  Windows cannot store (`a:b.txt`, `CON.log`, `trail.`), and on a
  case-insensitive disk `report.txt` would silently overwrite `Report.txt`.
  Those are cleaned (`a_b.txt`, `CON_.log`) or numbered (`report (2).txt`)
  instead, and the summary says how many changed. Linux keeps every name as-is
- **Downloads never replace what is already there** — a folder or file that
  already exists in the chosen destination is kept, and the new one saved as
  `Amber (2)` / `readme (2).txt`, the way a browser names a second download.
  Nothing is merged into an existing folder
- **Folder downloads in any browser** — where the browser cannot pick a
  destination (Firefox, or any non-https origin), a folder is saved on the
  server instead, in `~/Downloads/IguanaXterm/<you>/` with the local container.
  **Saved files** in the toolbar browses it, fetches files back and deletes.
  The copy runs on the server, so closing the tab does not stop it
- **Upload files or whole folders** — pick individual files, or a directory
  whose structure is recreated on the far side. Drag and drop works too.
- **A transfer queue** with per-file progress, and cancel
- **Downloads survive a dropped connection** — the download asks the server
  for the rest (`Range`) and picks up where it stopped, up to four times on
  its own. After that the row pauses with a **Resume** button, keeping what it
  has; the partial data lives in the browser's temporary file, never under the
  real name. If the file changed on the server meanwhile, it starts over
  rather than splicing old and new (`ETag` + `If-Range`). Where the page
  cannot pick a destination, the browser's own download manager resumes
  through the same range support
- **Narrow-pane aware** — in a small tile the toolbar collapses to icons and
  secondary columns give way, so the filename never gets squeezed out

### Accounts

- **Multi-user** — private session libraries, plus an admin panel
- **Encrypted at rest** — SSH passwords and private keys under Fernet (AES-128)

## Stack

| Layer | Tech |
|---|---|
| UI | Python in the browser (Pyodide) + wapyt widgets |
| Backend | pytincture (FastAPI) + Uvicorn |
| SSH/SFTP | Paramiko |
| VNC | noVNC 1.7 (vendored), with the RFB login done server-side |
| FTP/FTPS | ftplib, behind a paramiko-shaped adapter |
| Telnet | asyncio + an RFC 854/1073 IAC parser |
| Auth | pytincture sessions + bcrypt |
| Terminal | xterm.js 5.5, vendored and served same-origin |
| Tiling | GridStack 14, vendored, loaded on demand |

## Installing

IguanaXterm runs as a container, with **Docker or Podman on Windows, macOS or
Linux**. **[INSTALL.md](INSTALL.md)** has the full instructions — engines,
reaching servers on the same computer, folder downloads, HTTPS for other
machines (including the proxy settings terminals need), backups,
troubleshooting. In short:

```sh
git clone https://github.com/El-Iguana/iguanaxterm_wapyt.git
cd iguanaxterm_wapyt
cp .env.example .env              # set GANXTERM_ADMIN_PASS
docker compose up -d --build      # or: podman compose up -d --build
```

Then open <http://127.0.0.1:8765/iguanaxterm> — `127.0.0.1`, not `localhost`
— and sign in as `admin`.

### Running from source (development)

Requires Python 3.13, [uv](https://docs.astral.sh/uv/) and the wapyt checkout
next to this one at `../wa_pytincture_widgetset`:

```sh
cp .env.example .env
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode   # the wheel the browser installs
uv run python service.py
```

To develop against a local pytincture checkout instead of the pinned git tag:
`uv add --editable ../pytincture`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GANXTERM_ADMIN_USER` | `admin` | Initial admin username (first run only) |
| `GANXTERM_ADMIN_PASS` | `change_me` | Initial admin password (first run only); asked to change it on every load until it does |
| `GANXTERM_DATA_DIR` | `./data` (`/data` in the image) | SQLite database, `secret.key`, `session.key` — leave unset in `.env` |
| `GANXTERM_SECRET_KEY` | *(generated)* | Fernet key for credential encryption |
| `GANXTERM_SESSION_SECRET` | *(generated)* | Cookie-signing secret |
| `GANXTERM_PORT` | `8765` | Port on the host, with compose |
| `GANXTERM_CANONICAL_ORIGIN` | `http://127.0.0.1:$PORT` | Public origin. An `https://` value switches on production mode |
| `GANXTERM_ALLOWED_HOSTS` | `127.0.0.1` | Comma-separated hostnames (no ports); the canonical origin's host is always added |
| `GANXTERM_MAX_UPLOAD_BYTES` | `68719476736` (64 GiB) | Largest single upload. Every other request stays capped at 2 MiB |
| `GANXTERM_DOWNLOAD_DIR` | `$GANXTERM_DATA_DIR/downloads` | Where folder downloads are saved on the server, one subfolder per user |
| `GANXTERM_DOWNLOAD_HOST_DIR` | `~/Downloads/IguanaXterm` | Host folder mounted at `/downloads` by the downloads options (INSTALL.md §6); also what messages show |
| `GANXTERM_BIND` | `0.0.0.0` | Listen address (`127.0.0.1` with host networking) |
| `PORT` | `8765` | Listen port of the service itself |

## Data persistence

One volume, `iguanaxterm-data` (compose), mounted at `/data`:

- `iguanaxterm.db` — users, saved connection profiles and workspace layouts
- `secret.key` — Fernet key. **Back this up.** Losing it means every stored
  credential is unrecoverable.
- `session.key` — cookie-signing secret; losing it just logs everyone out.
- `downloads/` — folders saved on the server, unless a host folder is mounted
  instead (INSTALL.md §6).

Backups: INSTALL.md §7.

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
| `clipboard_smoke.py` | terminal copy and paste against the real clipboard |
| `resume_smoke.py` | interrupted downloads: automatic resume, pause and Resume, a file that changed |
| `vnc_smoke.py` | remote desktops, direct and tunnelled, with real pixels and input |
| `server_save_smoke.py` | folder downloads saved on the server when there is no picker |
| `windows_names_smoke.py` | Windows-safe folder download names, and Linux left alone |
| `reconnect_all_smoke.py` | Reconnect all: the count, and that dials are sequential |
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
