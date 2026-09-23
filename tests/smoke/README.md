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
