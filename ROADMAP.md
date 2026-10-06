# Roadmap

What is planned for IguanaXterm and not built yet. What is built, and the
traps found building it, are in CLAUDE.md.

## Native macOS install, browser only — planned (2026-10-01)

A `.dmg` with an `IguanaXterm.app` that runs IguanaXterm on a Mac **without
Docker and without HTTPS**, the macOS counterpart of the native Windows
install. Docker Desktop or Podman (INSTALL.md) stays the answer meanwhile.

The design and the work are shared with Monguana and written out in full
there: [Monguana ROADMAP, phase 40](https://github.com/El-Iguana/monguana_wapyt/blob/main/ROADMAP.md#phase-40-native-macos-install-browser-only--planned-2026-10-01).
In short:

- **Carries over:** the launcher (`tools/windows/launcher.py`, already
  cross-platform and tested on Linux), the bundled-Python approach, and
  plain HTTP on loopback. The launcher defaults to 127.0.0.3 (its own
  cookie jar, see CLAUDE.md), which macOS does not answer without
  `ifconfig lo0 alias 127.0.0.3`: the macOS build must add the alias or set
  `GANXTERM_HOST=127.0.0.1` (safe once the cookie namespace is switched on).
- **Apple Silicon first, unsigned.** Every compiled package in `uv.lock` (12)
  publishes macOS arm64 wheels. Intel is blocked the same way as in Monguana:
  `cryptography` 50.0.1 and `argon2-cffi-bindings` 26.1.0 publish no x86_64
  macOS wheels.
- **Runtime:** python-build-standalone, pinned by sha256, since python.org has
  no embeddable macOS Python. Dependencies from `uv.lock` with
  `uv pip --python-platform aarch64-apple-darwin --only-binary :all:`.
- **New pieces:** the `.app` bundle (`Info.plist` with `LSUIElement`, `.icns`
  icon, a small executable in `Contents/MacOS`), a menu-bar icon (pystray
  with PyObjC, on the main thread), data in
  `~/Library/Application Support/IguanaXterm`, a DMG built with `hdiutil`, and
  a `macos` CI workflow that builds, mounts, runs `--check`, starts, stops and
  attaches the DMG to `v*` releases.
- **Updates:** dragging the new app over the old one keeps the data, but
  nothing stops a running copy first. Either tell people to quit from the
  menu bar first, or ship a `.pkg` whose preinstall script runs `--stop`.
- **Signing is a later decision for the owner.** Unsigned, macOS 15 needs
  **System Settings → Privacy & Security → Open Anyway** after a first
  attempt. Signing and notarization need the Apple Developer Program
  (US$99/year), and the bundled Python may need entitlements for cffi.

**What differs from Monguana:**

- **Port 8765**, or the next free one, and the app path `/iguanaxterm`.
- **Folder downloads saved on the server** go to `~/Downloads/IguanaXterm`.
  The launcher already uses that path outside Windows. The menu-bar menu gets
  the same **Downloads** item as the Windows tray.
- **More served assets to check in CI:** xterm and noVNC must be served as
  `text/javascript`, as `windows.yml` checks today.
- **Remote desktops and SSH need no extra system pieces:** VNC runs through
  noVNC in the browser and SSH through paramiko, so the bundle has nothing to
  add for them.
- The browser side already handles Mac filenames: the download code treats a
  macOS browser's file system as case-insensitive.

**Testing.** No Mac on the development workstation: CI covers the build and
`--check`; the first hand test (install, menu-bar icon, sign-in, a terminal, a
file download to `~/Downloads/IguanaXterm`, an update over an older build)
needs a real Mac.
