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

## Maximize smoke

`maximize_smoke.py` covers zooming a tile: it fills the workspace, sits on top,
grows the remote PTY, hides its own grip and resize handle, and — the point —
leaves the grid **model** byte-identical, so restoring puts every neighbour
back where it was.

```bash
python3 tests/smoke/maximize_smoke.py
```

Two things it had to be taught. Escape only restores from outside a terminal
(inside one it goes to the remote, which is correct), so the test moves focus
with `.focus()` rather than a click. And a maximized tile covers its
neighbours, so the other tile's button is unreachable until you restore —
the test takes the path a person would.

## Clipboard smoke

`clipboard_smoke.py` grants the browser clipboard access and checks each
binding against the real system clipboard. Ctrl+V and Ctrl+Shift+V must
paste. Ctrl+C on a selection must copy without sending an interrupt, and
without a selection it must still interrupt a running `sleep`. Ctrl+Shift+C
must copy. It also drives the right-click menu (Copy is disabled with nothing
selected, Escape closes it). A second, unprivileged context checks that a
refused clipboard read is explained rather than silently ignored.

## VNC smoke

`vnc_smoke.py` runs against `Containerfile.vnctarget`: Xvfb with two
`x11vnc` servers and `sshd`. One VNC server is published on 5900 with the
password `vncpass`. The other listens only on the container's own localhost at
5901, so SSH is the only way in:

```bash
podman build -t iguanaxterm-vnctest -f tests/smoke/Containerfile.vnctarget tests/smoke
podman run -d --name ix-vnctest -p 127.0.0.1:5900:5900 -p 127.0.0.1:2223:22 iguanaxterm-vnctest
python3 tests/smoke/vnc_smoke.py
```

The remote screen is two known colours, and the checks read **real pixels** off
noVNC's canvas. Input is proven the same way: the test clicks into the remote
xterm and types `xsetroot -solid '#00aa00'`, then waits for the root window to
turn green. The same screen is read through the tunnel, the unpublished port
must fail directly, and a wrong password must show x11vnc's own reason. The
test also watches every request and WebSocket frame the page sends, and fails
if the VNC password appears in any of them.

The clipboard is checked both ways through `xclip` in the target (`podman
exec`). It sets the remote `CLIPBOARD` and expects it in the page's
clipboard. It presses Ctrl+V inside the desktop and clicks **Paste to
desktop**, then reads the remote `CLIPBOARD` back. The remote-side text is
unique per run, because x11vnc skips a selection identical to the last one it
forwarded. The shell line is cleared with Ctrl+C before each typed command:
the Ctrl+V check leaves a real ^V in the remote shell.

Two lessons from the target itself. Xvfb needs `-noreset`: an X server resets
when its last client leaves, and `xsetroot` exits as soon as it has set the
colour. And the start script clears `/tmp/.X1-lock`, because a restarted
container keeps `/tmp`, and a stale lock stops Xvfb and everything after it.
The test clears `vnc-ssh`'s pinned host key first: a rebuilt target has new
SSH keys, and the tunnel correctly refuses a changed one.

## Server save smoke

`server_save_smoke.py` removes the File System Access pickers from Chromium,
which is exactly what the app checks for, so it takes the path Firefox takes.
Playwright's Firefox is not installed here. It makes `~/savetest` on the SSH
target (three small files and a 20 MB one), selects it together with a loose
file, and downloads. The loose file must reach the browser, and the folder must
appear on disk under `GANXTERM_DOWNLOAD_DIR/admin/`, byte for byte. Then it
drives **Saved files**: open a folder, fetch a file back, refuse a `..` path,
and delete.

The app must be started with `GANXTERM_DOWNLOAD_DIR` set, and the script must
see the same value, because it reads the folder directly:

```bash
GANXTERM_DOWNLOAD_DIR=/tmp/ixdl GANXTERM_DATA_DIR=/tmp/ixsmoke ... uv run python service.py &
GANXTERM_DOWNLOAD_DIR=/tmp/ixdl python3 tests/smoke/server_save_smoke.py
```

## Windows names smoke

`windows_names_smoke.py` makes the page believe it is on Windows by overriding
`navigator.platform` and `userAgentData`, which is all the app consults. It
creates `~/winnames` on the SSH target through a real terminal: `a:b.txt`,
`a?b.txt`, `Report.txt`/`report.txt`, `CON.log`, `trail.`, and `Sub/`/`sub/`.
Then it downloads the folder into `transfer_smoke.py`'s in-memory picker. Each
file holds one distinct byte, so a file that landed under the wrong name or
overwrote another shows up as missing content, not just a wrong path. A second
page without the override downloads the same folder and must keep every name
exactly.

## Reconnect all smoke

`reconnect_all_smoke.py` opens three SSH panes on one host and one FTP pane,
tiles them, reloads, and checks that the toolbar offers **Reconnect all (4)**
while nothing dials. Reconnecting one pane by hand drops the count to 3. The
button then dials the rest, and the pacing is **measured**: the wrapped
`WebSocket` records when each terminal socket opened and when its relay sent
`connected`, and each dial must open after the previous one connected.

It tiles before reloading because in tabbed mode only the active tab's
Reconnect button is visible.

## Layout smoke

`layout_smoke.py` covers persistence: mode, tile geometry, the tab each pane
was on and the directory it was browsing all survive a reload, and restoring
opens **zero** sockets — counted by wrapping `window.WebSocket`, not inferred.
Clicking one Reconnect opens exactly one.

```bash
python3 tests/smoke/layout_smoke.py
```

## FTP smoke

`ftp_smoke.py` covers the files-only connection types. `ftp_target.py` is a
throwaway pyftpdlib server on `127.0.0.1:2121` that **requires** TLS on both
channels, because the NAS that prompted the feature does
(`504 TLS/SSL protection required`).

```bash
uv run --with pyftpdlib --with pyopenssl python tests/smoke/ftp_target.py /tmp/ixftp &
python3 tests/smoke/ftp_smoke.py
```

It creates its own `ftps-box` profile in `Lab` through the editor, checking
that FTP fills in port 21 and disables the key field. Then: the pane has no
Terminal button and opens on Files, no `/ws/terminal/` socket is ever
constructed (counted, as in the layout smoke), a download streams through
`/files`, and a restored FTP pane's Reconnect lands back in the directory it
was browsing.

## Large upload smoke

`large_upload_smoke.py` pushes 40 MB through the upload route to both the SSH
target and `ftp_target.py`, downloads it back and compares SHA-256 in the page.
It also sends a 5 MB file into `Amber/2026/` with a `relative_path`, which is a
folder upload, and aborts a 60 MB upload partway. The FTP target's log shows
the partial `STOR` followed by a `DELE`, so the cleanup is proven to have run,
not just a missing file. Finally it checks that the 2 MiB limit still applies
to a BFF call, and that an upload without the CSRF header gets 403.

Both targets must be up, as for the transfer and FTP smokes.

## harness.py

Shared `reset_workspace()`, `new_pane()` and `session_leaf()`.

Pick a session with `session_leaf(pg, "alpine-box")`, never "the last tree row".
The FTP, large-upload and Reconnect all smokes add `ftps-box` to the same
scratch database. The last row then belongs to whichever profile sorts last,
and clicking a folder that is already open collapses it. Both broke the older
scripts until they switched to the helper.

Every script calls the reset right
after login, because the layout persists and a run would otherwise inherit the
last one's panes — which surfaces as a timeout on `pane_1` and looks nothing
like the real cause. The reset also reloads the page once the layout is empty:
closing panes alone leaves the pane counter past `pane_1`.

Address tiles by `[data-pane="..."]`, never by DOM index — GridStack reorders
the DOM by position, so `items[0]` is not the first pane opened. That one cost
a round of confusion about swapped geometry that turned out to be the test's
fault, not the app's.

## Tiled smoke

`tiled_smoke.py` covers the tiled workspace: both panes get grid items, pane
roots move into them, grips and close buttons appear only while tiled, tiles
lay out side by side, and — the part that matters — every scrollback survives
the switch there and back, with the PTY re-negotiated to the tile width.

```bash
python3 tests/smoke/tiled_smoke.py
```

## Resize storm probe

`resize_storm_probe.py` counts PTY resize frames during one real drag of a
tile's resize handle. It is a measurement, not a pass/fail test — run it after
touching `fit_debounce_ms`, the grid options or `terminal.js`'s observer.

```
before the debounce: 41 messages, 41 distinct sizes, over 1327ms
after  (120ms):       1 message, at the final size
```

The handle carries `ui-resizable-autohide` and has no box until hover, so the
probe strips that class first — worth knowing before concluding the handle is
missing.

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
