# IguanaXterm — pytincture + wapyt rewrite

Browser-based SSH/Telnet terminal manager with SFTP. A rewrite of
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
    telnet.py           #   IAC parser (pure, unit-tested)
    paths.py            #   path/format helpers — imported by BOTH sides
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
