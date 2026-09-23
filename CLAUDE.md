# IguanaXterm — pytincture + wapyt rewrite

Browser-based SSH/Telnet terminal manager with SFTP and FTP(S) file browsing. A rewrite of
`~/Development/workspace/iguanaxterm` (FastAPI + 1350 lines of vanilla JS) onto
**pytincture** (Python in the browser via Pyodide) and **wapyt** (the
DHTMLX-free widgetset at `../wa_pytincture_widgetset`).

There was an earlier rewrite attempt at `../iguanaxterm_pyt` using `dhxpyt`. It
never ran — see *Why the dhxpyt attempt failed* below, because several of its
mistakes are easy to repeat.

## Related repos

Siblings under `~/Development/Pytinc/`, each with its own `CLAUDE.md`:

- `pytincture/` — the framework (local `1.0.0rc8`).
- `wa_pytincture_widgetset/` — **wapyt**, the widgetset. This app added four
  widgets to it: `Terminal`, `Form`, `DataTable`, `Tree`.
- `iguanaxterm_pyt/` — the abandoned dhxpyt attempt. Reference only.

The original app is at `~/Development/workspace/iguanaxterm`
(github.com/El-Iguana/iguanaxterm) and is still the feature reference.

## Layout

```
service.py              # ASGI entrypoint: config, hooks, routers, static mount
appcode/                # pytincture modules_path
  iguanaxterm.py        #   browser UI (Pyodide). APP_ENTRYPOINT lives here.
  services/
    auth.py             #   authenticator + BFF policy hook
    db.py               #   SQLite, Fernet, bcrypt, session secret
    ssh.py              #   key loading + host-key pinning
    ftp.py              #   FTP/FTPS as a paramiko-shaped client (see below)
    telnet.py           #   IAC parser (pure, unit-tested)
    paths.py            #   path/format helpers + SESSION_TYPES — BOTH sides
    session_service.py  #   BFF: connection profiles
    user_service.py     #   BFF: accounts
    sftp_service.py     #   BFF: directory ops + the connection pool
    terminal_ws.py      #   WebSocket relay (SSH + Telnet)
    transfer.py         #   plain routes for binary upload/download
  vendor/xterm/         #   vendored xterm.js — served at /xterm
  wapyt-99.99.99-*.whl  #   dev wheel the BROWSER installs (not the venv copy)
tests/                  # CPython unit tests for the pure modules
```

## Running

```bash
uv sync
uv run python service.py            # http://127.0.0.1:8765/iguanaxterm
uv run --with pytest python -m pytest tests/ -q
```

**After any edit to `../wa_pytincture_widgetset/wapyt/assets/*`:**

```bash
cd ../wa_pytincture_widgetset && ./scripts/dev_wheel.sh ../iguanaxterm_wapyt/appcode
```

then restart the service. The browser micropip-installs the **wheel in
`appcode/`**, not the editable checkout in `.venv`. The venv copy exists only so
pytincture's widgetset discovery can read `__widgetset__` from the installed
distribution metadata — without it the bootstrap page ships `widgetlib: ""` and
no widgets load.

## Things that will bite

### `APP_ENTRYPOINT` is mandatory

pytincture resolves the browser entrypoint by AST and its MainWindow detection
is hardcoded to `dhxpyt.layout.MainWindow` (`pytincture/backend/pages.py`,
`_main_window_base_names`). It never matches a wapyt base. Without
`APP_ENTRYPOINT = "IguanaXterm"` in `appcode/iguanaxterm.py`, startup is a 422.

### Build UI in `load_ui()`, never `__init__`

wapyt's `LoadUICaller` metaclass calls `load_ui()` after construction. Defining
both renders everything twice.

### BFF calls use the generated `*_async` name

`SessionService().list()` in the browser is a **blocking XHR** (deprecated).
`await SessionService().list_async()` is the awaitable one. Every non-streaming
export in this app is a sync `def` for exactly this reason — pytincture
dispatches sync exports to a worker thread, so blocking sqlite and paramiko are
correct there, and the browser API stays uniformly `_async`.

Streaming exports (`@bff_stream`, e.g. `SFTPService.walk`) are the exception:
they generate an async generator under their own name — `async for item in
SFTPService().walk(...)`.

### Register hooks by dotted path, not by setter

`create_app()` loads its **own isolated backend module**, so
`set_bff_policy_hook()` / `set_user_authenticator()` called against the shared
`pytincture.backend.app` never reach it, and startup dies with
`@bff_policy exports require BFF_POLICY_HOOK_PATH`. Pass them through
`PytinctureConfig(environment={...})`:

```python
environment={
    "AUTH_USER_AUTHENTICATOR": "services.auth.authenticate",
    "BFF_POLICY_HOOK_PATH": "services.auth.policy_hook",
}
```

Setting them with `os.environ` before `create_app` also fails — the
configuration context replaces the environment with the typed config's own.

### Custom session claims must be declared

`_build_auth_session_user()` copies only a fixed set of claims into the session
cookie, plus whatever `AUTH_SESSION_CLAIM_KEYS` names. Anything else the
authenticator returns is **silently dropped**. Without

```python
"AUTH_SESSION_CLAIM_KEYS": "user_id,is_admin,username",
```

every BFF call fails with `403 BFF policy denied the operation` and nothing
anywhere says why — the hook simply sees no `user_id`. `roles` is a first-class
field and needs no declaration.

### BFF modules cannot use relative imports

pytincture imports a BFF module **by file path**, so it has no parent package
and `from .db import get_db` raises
`ImportError: attempted relative import with no known parent package` — at call
time, surfacing as a 500 with a correlation id. Every intra-package import in
`appcode/services/` is absolute (`from services.db import ...`), including the
ones inside functions.

### wapyt must be installed non-editable

An editable install exposes only a `.pth` and dist-info: `distribution.files`
never lists `wapyt/__init__.py`, and `PathFinder.find_spec()` returns
`origin=None` for the editable finder. Widgetset discovery then finds nothing,
the bootstrap page ships `widgetlib: ""`, and the app loads with no widgets and
no error. `pyproject.toml` pins `editable = false` for this reason.

### The login field is hardcoded to type="email"

pytincture emits `<input type="email" ... required>`, and a browser refuses to
submit a plain username into it — curl does not, which makes this easy to miss
in scripted testing. `services/login_page.py` rewrites the field and, unlike
the dhxpyt attempt's identical byte replacement, verifies its own assumptions at
startup and logs an error if pytincture's markup has moved.

### pytincture will not serve authenticated plain HTTP

Production auth mode requires an **https** `canonical_origin`, exact
non-wildcard `allowed_hosts`, a ≥32-character `session_secret`, and secure
cookies. The only exception is `enable_dev_email_login=True`, which additionally
demands that `allowed_hosts` and `canonical_origin` be **literal loopback IPs** —
`"localhost"` fails, because the check parses the host as an IP address.

`service.py` picks the mode from `GANXTERM_CANONICAL_ORIGIN`. Enabling
`enable_dev_email_login` is safe here: pytincture's password-less loopback
branch is unreachable once `AUTH_USER_AUTHENTICATOR` is set, because that path
returns or raises before ever reaching it. Passwords are still required in dev.

### No CDN, ever

pytincture's CSP is `script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:`,
`style-src 'self' 'unsafe-inline'`. jsDelivr is blocked outright — which is why
xterm is vendored into `appcode/vendor/xterm` and mounted at `/xterm` rather
than loaded from a CDN the way the original app does. Separately, `innerHTML`
never executes `<script>` tags regardless of CSP, so `attach_html` cannot be
used to bootstrap a library.

### Terminal bytes never cross into Python

Keystrokes go xterm → WebSocket, output goes WebSocket → xterm, entirely inside
`wapyt/assets/terminal.js`. Pyodide is single-threaded on the main thread; an
FFI hop per keypress and per output frame is what makes a browser terminal feel
laggy. The Python `Terminal` wrapper drives lifecycle only.

Same reasoning for file transfers: they use `services/transfer.py`, not the BFF,
because a JSON BFF means base64 — a third larger and resident in Pyodide's heap.

### JS `null` is `JsNull`, not `None`

`document.getElementById(...)` and `Element.closest(...)` return JS `null`,
which crosses the FFI as `JsNull`. **`JsNull is None` is `False`**, so an
`if x is None: return` guard never fires and the next attribute access raises.
`JsNull` is falsy, so test truthiness (`if not element:`) for anything that
comes back from the DOM. This bit the toolbar's delegated click handler: every
click *outside* the toolbar raised an AttributeError.

### A hidden tab has no size

`TabWidget` keeps inactive panels mounted (which terminals need), but a hidden
panel measures 0×0 and fitting against that produces a 1×1 terminal that never
recovers. `Terminal.fit()` skips while hidden and a `ResizeObserver` re-fits on
the way back; `_on_tab_change` also calls `fit()` explicitly.

## Connection types (2026-09-23)

`SESSION_TYPES` in `services/paths.py` is the one table of what a type can do —
`terminal`, `files`, default `port` — read by the editor, the pane, the
session validator, the terminal relay and the pool's dialler.

| type | terminal | files | dialled with |
|---|---|---|---|
| `ssh` | yes | SFTP | paramiko |
| `telnet` | yes | no | asyncio |
| `sftp` | no | SFTP | paramiko |
| `ftp` | no | FTP/FTPS | `services/ftp.py` |

A files-only pane has no Terminal button and opens on Files. It keeps its
terminal *panel*, because the Reconnect placeholder lives there; `_connect_pane`
then clears it and selects Files instead of mounting a Terminal. The relay
refuses a files-only profile server-side too.

### FTP pretends to be paramiko

`FTPFiles` answers the exact slice of `SFTPClient` the app calls, plus the
pool's `get_transport().is_active()` and `open_sftp()`, so `SFTPService`,
`SFTPPool` and `transfer.py` are protocol-blind. `sftp_service._dial` is the
only branch. Things that shaped it:

- **The NAS requires TLS.** `192.168.1.219:21` (SmbFTPD) answers a plain
  login with `504 TLS/SSL protection required`. So `ftp` always tries
  `AUTH TLS` and falls back to plain only for a server that refuses it.
- **The certificate is pinned in `host_key`** as `tls-sha256 <hex>` — NAS
  certs are self-signed, so there is no CA to trust. The pin is checked before
  the password goes out. A pinned server that stops offering TLS is a
  *downgrade* and raises `HostKeyChanged`, same as a changed cert. "Forget
  host key" resets it, like SSH.
- **Data connections reuse the TLS session** (`_FTPS.ntransfercmd`). ftplib
  does not, and vsftpd/FileZilla refuse a data channel without it.
- **One control connection, one thing at a time.** `_busy` is held by every
  command and by an open transfer handle until `close()`. An abandoned handle
  releases it from `__del__`; waiters time out at 120s rather than hang.
- **Idle servers hang up with 421.** `_call` reconnects once and retries.
  Connection loss is matched as `EOFError`/`ConnectionError`/`TimeoutError`,
  never bare `OSError` — `FileNotFoundError` is an OSError too, and it is an
  answer, not a dropped line.
- **`listdir_attr` CWDs in first.** LIST of a *file* succeeds on many servers
  and lists the file, which would send `_remove_recursive` hunting for its
  children. CWD makes "not a directory" an IOError everywhere.
- **Tested live** in `tests/test_ftp.py` against in-process pyftpdlib, plain
  and TLS-required (dev deps `pyftpdlib`, `pyopenssl`), and in the browser by
  `tests/smoke/ftp_smoke.py`.

## GridStack tiling (built)

Tiled workspace where each cell is one connection carrying its own Terminal and
Files tabs, [GridStack](https://gridstackjs.com) doing the tiling. Designed and
built 2026-09-23; phases below.

### Decisions

- **Tabbed and tiled are both modes**, switched from the toolbar, not one
  replacing the other.
- **A cell is one connection instance**, not one saved session: connect to the
  same host twice and get two cells.
- **GridStack is vendored in this app**, at `appcode/vendor/gridstack/` served
  from its own mount, exactly as xterm is at `service.py:197`. It stays out of
  wapyt, whose manifest loads every asset on every page for every app.
- **A saved layout restores cells but does not dial.** Each restored cell shows
  a Reconnect button. Auto-dialling N sessions on page load walks straight into
  the `MaxStartups` banner resets that `ssh.py`'s retry exists to survive.

### Per-pane maximize (2026-09-23)

A tile zooms to fill the workspace via `_toggle_maximize`, and it is
**presentational on purpose**: `[data-maximized]` overlays the item with
`position:absolute; inset:0` and the grid model is not touched, so neighbours
keep their positions and the saved layout is unaffected. Resizing the item to
full width instead would reflow its neighbours *and persist that reflow*.

- GridStack v14 sets `width`/`height` inline as `calc()` over its CSS
  variables, so the overlay rules need `!important`.
- `.grid-stack` is `position:relative` and stretched by `min-height:100%`, so
  `inset:0` resolves to the whole workspace. A grid taller than its host can be
  scrolled, so maximizing also scrolls the host to the top.
- The drag grip and resize handle are hidden while maximized: they would move
  the item in the model behind an overlay that is pretending to fill the
  screen.
- **Escape is ignored when the focus is inside a terminal.** Escape belongs to
  the remote there — stealing it breaks vim. The guard checks
  `event.target.closest(".wapyt-terminal")`.
- A maximized tile covers its neighbours, so another tile's maximize button is
  genuinely unreachable until you restore. That is fine, but it means a test
  cannot click straight from one to the other.

### Phase 4 is built (2026-09-23)

The workspace layout persists per user: `layouts` table (one row, opaque JSON),
`services/layout_service.py`, saves debounced 500ms through
`_schedule_layout_save`. Mode, tile geometry, which tab each pane was on and
the directory it was browsing all come back.

**Restoring never dials.** Each restored pane renders a Reconnect placeholder
and `_connect_pane` mounts the Terminal on click. Measured in
`tests/smoke/layout_smoke.py` by counting WebSocket constructions: 0 terminal
sockets on restore, exactly 1 after one Reconnect.

Dead panes are filtered on **read**, not on write — a session deleted while the
layout sat untouched still has to be dropped, and `get()` intersects the saved
panes with the sessions you own.

Traps:

- **A default geometry stacks every new tile.** `{x:0, y:0, w:6, h:7}` looks
  harmless, but naming a position tells GridStack exactly where to put it, so
  the second tile lands *under* the first instead of beside it. A new pane
  carries size only and gets `gs-auto-position`; only a restored pane names
  `gs-x`/`gs-y`.
- **Persistence broke every existing smoke test.** They assumed an empty
  workspace and named `pane_1`. `tests/smoke/harness.py` now resets first — and
  closing panes is not enough on its own, because the pane counter has already
  advanced past the restored ones, so it reloads once the layout is empty.

### Phase 3 is built (2026-09-23)

GridStack **14.0.0** is vendored at `appcode/vendor/gridstack/` and served from
`/gridstack` (`service.py`), mirroring xterm. It is loaded **on demand** the
first time the workspace is tiled, so nobody pays 92KB for a mode they never
use. The `sourceMappingURL` comment is stripped: the `.map` is not vendored.

The workspace holds two hosts, `#ix-tabs-host` and `#ix-grid-host`. The
TabWidget always keeps a tab per pane and is the source of truth for which
panes exist; the grid mirrors it while tiled. Switching modes moves pane roots
and nothing else.

Traps found building it:

- **`addWidget(HTMLElement)` was removed in GridStack v11.** It warns and
  quietly builds its own element instead, which left the pane in a detached
  node — the tile *rendered*, so it looked fine, but typing into it did
  nothing. Build the item, move the pane root in, append it to `.grid-stack`,
  then `makeWidget(el)`.
- **The drag handle must be a dedicated grip.** The pane's tab strip holds the
  Terminal/Files buttons, and making the strip the handle eats their clicks.
  `draggable: {handle: ".ix-pane-grip"}`.
- **A tiled pane needs its own close button**, since the tab strip is hidden.
  The TabWidget emits `close` from its close button, *not* from `removeTab`, so
  closing from the pane must call the teardown directly or the terminal and its
  socket leak.
- **The resize handle is `.ui-resizable-se` and carries
  `ui-resizable-autohide`** — no bounding box until hover, which reads as "the
  handle does not exist" in a test.
- **A resize drag floods the PTY.** Measured: one 1.3s drag sent **41** resize
  messages, all 41 distinct, so `fit()`'s cols/rows dedupe never engaged.
  `fit_debounce_ms=120` (new in wapyt) brings it to **1**.

### Phase 1 is built (2026-09-23)

The Pane abstraction and the pool refcounting are in, hosted by the existing
TabWidget. GridStack is not started. What changed:

- `_connect` opens a **pane**, not a terminal tab. `_open_pane` builds the
  pane chrome, mounts the Terminal into `#pane-term-<id>` and leaves
  `#pane-files-<id>` empty.
- The Files panel is built on first click (`_mount_files`), so a pane that is
  only ever a terminal never dials SFTP.
- `_open_sftp` no longer makes a top-level tab. It reuses an open pane for that
  session, or starts one, and selects its Files tab.
- Both panels are absolutely positioned siblings, toggled with `hidden`. The
  terminal's host element is created once and never replaced.
- `_terminals` is gone; `_panes` replaces it. `_sftp_tabs` is now keyed by pane
  id, which is why every `_sftp_*` method still reads unchanged.
- `_SFTP_CSS` moved out of the per-pane markup into the one-time head
  injection — it used to be re-injected with every tab.

### The abstraction

Two workspace modes stay affordable only if the content is mode-agnostic:

```
Pane (one connection)          <- every behaviour lives here
  .ix-pane root · mini tab strip [Terminal][Files]
  Terminal widget (mounted once)
  SFTP panel + DataTable (built lazily on first Files click)

TabbedHost / TiledHost         <- thin: add · remove · focus
```

A host only decides where a pane root lives. Put terminal or SFTP logic in
either one and there will be two copies of it within a month. Note this also
retires `_open_sftp` as a top-level-tab maker in *both* modes — files become a
tab inside the connection's own pane.

### Traps, in the order they bite

- **Reparenting a live xterm is safe — measured, not assumed.** An earlier note
  here claimed a detached and re-attached xterm loses its buffer. It does not.
  Spiked 2026-09-23 against a real PTY (`tests/smoke/reparent_spike.py`): with
  313 lines of scrollback, moving the pane subtree to a new parent kept every
  line, kept the socket open, kept stdin working, and the `ResizeObserver`
  re-fitted 139x24 -> 104x24 and back on its own. Zero console errors. So the
  mode toggle and GridStack drag are both a plain `appendChild`.

  Still move the **pane root**, never the terminal's own host element: the
  widget's observer is bound to that element and the spike only exercised
  moving an ancestor.
- **The SFTP pool is refcounted now — and the browser cannot see it.** The pool
  is keyed `(user_id, session_id)`, and `disconnect` used to close the channel
  outright, so with a pane per connection the first pane closed pulled the
  channel out from under the rest. `retain`/`release` fixed that.

  What matters for testing: a browser test **cannot** catch a regression here.
  `acquire()` re-dials transparently, so a listing after a wrong `close()` is
  indistinguishable from one after a correct `release()`. The real damage is an
  in-flight transfer dying. `tests/test_sftp_pool.py` pins it at the unit level;
  a mutation of `release()` back to the old semantics fails those tests, which
  is how we know they bite.
- **Narrow panes are handled (Phase 2, 2026-09-23)** with container queries on
  `.ix-pane`, not media queries: a pane is narrow because its cell is narrow,
  which has nothing to do with the window. Measured, not guessed — the toolbar's
  natural width is **469px**, so it starts clipping just under that:

  | tier | what gives way |
  |---|---|
  | ≤700px | the capability note's sentence (icon keeps it as a tooltip); `permissions` column |
  | ≤560px | toolbar labels → icons; `modified` column |
  | ≤420px | crumb width, queue name and status widths |

  The worst of it was not the toolbar. Below ~500px the fixed columns squeezed
  **Name to 0px** — the filename vanished behind a horizontal scrollbar and a
  directory could not be double-clicked at all. Fixed by dropping secondary
  columns, which needed `data-column-id` on `td` in wapyt (headers had it,
  cells did not).

  Two things that are not CSS: every toolbar button needs a `title`, since
  icon-only is unusable without one; and the breadcrumb strip needs a
  `ResizeObserver` re-pinning `scrollLeft` to its tail. Scrolling to the end on
  navigation alone is not enough — a resize keeps the old offset and leaves the
  middle of the path showing, which a grid drag would do continuously.
- **Resize storms are handled** by `fit_debounce_ms` (wapyt), set to 120 here.
  See the measurement above. An explicit `fit()` is never debounced, so a pane
  becoming visible still fits immediately.
- **Grid items need `min-height: 0`**, the same discipline as the layout cell
  fix above. Without it a terminal pushes its item wider instead of scrolling —
  the 81px-pane / 20-column bug again.
- **Layout persistence is built** — see Phase 4 above. Geometry is read from
  `item.gridstackNode`, not the `gs-*` attributes, which lag behind a drag.

## Live smoke test

`tests/smoke/` drives the real UI against a throwaway SSH container — the only
test that exercises the WebSocket relay, the PTY resize path and the SFTP pool.
See its README. Run it after touching `terminal_ws.py`, `terminal.js`,
`sftp_service.py` or anything in the layout chain.

It caught the wapyt layout bug that made the terminal pane 81px wide (the PTY
was being negotiated to 20 columns, which reads as a terminal fault rather than
a layout one).

## Why the dhxpyt attempt failed

Worth knowing, because most of these are framework-shaped rather than typos:

1. Login pointed at `AUTH_PASSWORD_HASHES` with no authenticator installed, so
   **nobody could log in** — the seeded SQLite users were never consulted.
2. The policy hook bcrypt-verified `user["password"]` on every BFF call, but
   pytincture strips `password` from session claims, so every call 401'd.
3. Every UI call did `await service.method()` on a sync export — a `TypeError`
   on a non-awaitable, swallowed by a bare `ensure_future`.
4. `grid.data.parse(...)` — dhxpyt's `Grid` has no `data` attribute.
5. `channel.settimeout(0)` put the SSH channel in non-blocking mode, so the
   reader exited on the first empty poll and every session died instantly.
6. `SSHClient.connect(keepalive=30)` — no such parameter.
7. xterm loaded from jsDelivr, which the CSP blocks, via a `<script>` tag inside
   `attach_html`, which `innerHTML` would not execute anyway.

## Conventions carried from the original

- `GANXTERM_*` environment prefix, `/data` volume, SQLite + Fernet at rest.
- `.gitignore` excludes `.env`, `data/`, `*.db`, `secret.key`, `session.key`
  and the rebuilt dev wheel. This file is tracked, unlike in the v1 repo —
  the framework pitfalls below are the expensive part of the project.
- **MIT**, unlike the AGPL-3.0 v1 app at `El-Iguana/iguanaxterm`.
- Session profiles belong to one user; there is **no unscoped read** of the
  `sessions` table anywhere.
