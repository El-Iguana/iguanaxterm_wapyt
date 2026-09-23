# Live smoke test

Drives the real UI against a real SSH server: opens a terminal from the session
tree, types a command, reads the rendered xterm buffer back, then exercises the
SFTP browser. This is the only test that proves the WebSocket relay, the PTY
resize path and the SFTP pool actually work — everything else is unit-level.

Not run by `pytest tests/`: it needs a container, a running service and a
browser.

## Running it

```bash
# 1. throwaway SSH target on 127.0.0.1:2222
podman build -t iguanaxterm-sshtest -f tests/smoke/Containerfile.sshtarget tests/smoke
podman run -d --name ix-sshtest -p 127.0.0.1:2222:22 iguanaxterm-sshtest

# 2. the app, against a scratch data dir
GANXTERM_DATA_DIR=/tmp/ixsmoke GANXTERM_ADMIN_PASS=testpass123 PORT=8799 \
  uv run python service.py &

# 3. log in once and save a profile pointing at the container, then:
python3 tests/smoke/live_ssh_smoke.py     # terminal + SFTP browsing
python3 tests/smoke/transfer_smoke.py    # download + upload + queue

# 4. tear down
podman rm -f ix-sshtest && rm -rf /tmp/ixsmoke
```

The session profile it expects is named `alpine-box` in folder `Lab`, pointing
at `127.0.0.1:2222` as `testuser`/`testpass`.

`transfer_smoke.py` stubs `showSaveFilePicker` / `showDirectoryPicker` with
in-memory handles, because Playwright cannot drive Chromium's native pickers.
Everything on our side of that boundary is still exercised — activation
ordering, streaming, progress, the queue, the routes — and the stub returns a
real `WritableStream`, since `response.body.pipeTo()` rejects anything else and
the genuine `FileSystemWritableFileStream` is one.

## Pane smoke

`pane_smoke.py` covers the pane model: a connection carries its own
Terminal/Files tab strip, the file browser is built only when first asked for,
connecting twice gives two independent shells, and closing one pane leaves its
neighbour working.

It seeds its own `alpine-box` session, so it needs only the SSH target and the
service:

```bash
python3 tests/smoke/pane_smoke.py
```

Note what it does **not** prove: the SFTP pool's refcounting. The pool re-dials
transparently, so a listing after a stray `close()` looks identical to one
after a correct `release()`. The refcount is pinned by the unit tests in
`tests/test_sftp_pool.py` instead, and the damage it prevents is an in-flight
transfer dying, not a later listing.

## Narrow pane smoke

`narrow_pane_smoke.py` drives the SFTP panel at grid-cell widths from 1200px
down to 320px: labels collapse to icons and back, every icon-only button keeps
a tooltip and still works, a nine-crumb path stays on one line with the current
directory in view across repeated resizes, and the capability note degrades to
its icon without overflowing.

```bash
python3 tests/smoke/narrow_pane_smoke.py
```

It resizes the pane element directly rather than the viewport, because that is
what a grid cell does and what the container queries actually respond to.

## Reparent spike

`reparent_spike.py` answers one architectural question for the planned
GridStack tiling: can a live, PTY-connected xterm be moved to a different DOM
parent without losing its buffer, its socket or its stdin?

It can. With 300 lines of scrollback the buffer came through untouched, the
command typed after the move executed, and the `ResizeObserver` re-fitted the
PTY 139x24 -> 104x24 and back unaided. So the tabbed/tiled toggle and grid drag
are both a plain `appendChild` of the pane root, not an absolute-position
overlay.

It seeds its own `alpine-box` session through the real dialog, so it needs only
the SSH target and the service:

```bash
python3 tests/smoke/reparent_spike.py
```

Keep it: it is the regression test for anything that moves a mounted terminal.

## Throttle probe

`throttle_probe.py` measures how well the SSH connect retry absorbs a host that
refuses connections, using `Containerfile.sshtarget-throttled` — the same image
with `MaxStartups 1:100:2`, which reproduces the "Error reading SSH protocol
banner" failure seen against a real host. It prints a table rather than
passing or failing; run it before changing the retry ceiling in
`services/ssh.py`. Its docstring carries the numbers measured so far.

## Notes

- Alpine ships no `tput`; use `stty size` to read the remote PTY geometry.
- The first run is slow — Pyodide boots and micropip-installs the widgetset —
  so the toolbar wait is 180s on purpose.
- `Containerfile.sshtarget` uses password auth and a weak credential
  deliberately. It binds to loopback and is torn down after.
