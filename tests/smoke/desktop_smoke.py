"""
Smoke test for the Electron window (``desktop/``), headless under xvfb.

    uv run --with playwright python tests/smoke/desktop_smoke.py

Needs ``desktop/node_modules`` (``desktop/install.sh``, or ``npm ci`` there)
and ``xvfb-run``. Playwright attaches to Electron over CDP.

1. **window mode**: with ``IX_DESKTOP_URL`` set to a running IguanaXterm (say
   the container, ``http://127.0.0.1:8765/iguanaxterm``), the window opens
   that server and starts nothing. Skipped when the variable is unset.
2. **local fallback**: pointed at a port where nothing answers, the window
   starts ``service.py`` itself in a scratch data folder, the first account
   signs in with ``admin`` / ``change_me``, and closing the window stops that
   server.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ELECTRON = ROOT / "desktop" / "node_modules" / "electron" / "dist" / "electron"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def launch(scratch: Path, env_extra: dict[str, str]) -> tuple[subprocess.Popen, int]:
    cdp = free_port()
    scratch.mkdir(parents=True, exist_ok=True)
    log = open(scratch / "electron.log", "wb")  # noqa: SIM115 - lives as long as Electron
    env = dict(os.environ)
    # WAYLAND_DISPLAY would put the window on the real desktop (and Electron
    # stalled there starting the fallback): stay inside xvfb.
    for name in ("IGUANAXTERM_URL", "GANXTERM_DATA_DIR", "GANXTERM_DOWNLOAD_DIR", "WAYLAND_DISPLAY"):
        env.pop(name, None)
    env.update(env_extra)
    proc = subprocess.Popen(
        ["xvfb-run", "-a", str(ELECTRON), str(ROOT / "desktop"), "--ozone-platform=x11",
         f"--user-data-dir={scratch / 'electron'}", f"--remote-debugging-port={cdp}"],
        env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not answers(f"http://127.0.0.1:{cdp}/json/version"):
        time.sleep(0.3)
    return proc, cdp


def app_page(pw, cdp: int, timeout_s: float = 180):
    """
    Attach once the IguanaXterm page exists: over CDP, Playwright does not
    pick up a page Electron creates after it attached (the local fallback
    opens its window only when its server answers).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/list", timeout=2).read())
        except OSError:
            targets = []
        if any("/iguanaxterm" in t.get("url", "") for t in targets if t.get("type") == "page"):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("no IguanaXterm page appeared")
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp}")
    for context in browser.contexts:
        for page in context.pages:
            if "/iguanaxterm" in page.url:
                return browser, page
    raise RuntimeError("the IguanaXterm page is not visible over CDP")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)


def window_mode(pw, scratch: Path) -> None:
    url = os.environ.get("IX_DESKTOP_URL", "").strip()
    if not url:
        print("SKIP window mode (set IX_DESKTOP_URL to a running IguanaXterm)")
        return
    proc, cdp = launch(scratch / "window", {"IGUANAXTERM_URL": url})
    try:
        browser, page = app_page(pw, cdp, 60)
        origin = url.split("/iguanaxterm")[0]
        check("window mode opens the running server", page.url.startswith(origin), page.url)
        page.wait_for_selector('input[name="password"], .wapyt-toolbar', timeout=60000)
        check("window mode shows sign-in or the app", True)
        ua = page.evaluate("navigator.userAgent")
        check("page sees Electron in the user agent", "Electron/" in ua)
        browser.close()
    finally:
        stop(proc)


def local_mode(pw, scratch: Path) -> None:
    nothing = f"http://127.0.0.1:{free_port()}/iguanaxterm"
    data = scratch / "local-data"
    proc, cdp = launch(scratch / "local", {
        "IGUANAXTERM_URL": nothing,
        "GANXTERM_DATA_DIR": str(data),
        "GANXTERM_DOWNLOAD_DIR": str(scratch / "downloads"),
    })
    try:
        try:
            browser, page = app_page(pw, cdp)
        except RuntimeError:
            print((scratch / "local" / "electron.log").read_text(errors="replace")[-3000:])
            raise
        check("fallback starts a local server", page.url.startswith("http://127.0.0.1:")
              and not page.url.startswith(nothing.rsplit("/", 1)[0]), page.url)
        local_origin = page.url.split("/iguanaxterm")[0]
        page.wait_for_selector('input[name="password"]', timeout=60000)
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "change_me")
        page.click('input[type="submit"]')
        try:
            page.wait_for_selector(".wapyt-toolbar", timeout=90000)
        except Exception:
            shot = scratch.parent / "desktop-signin-failed.png"
            page.screenshot(path=str(shot))
            print("screenshot:", shot)
            print("URL:", page.url, "\nBODY:", page.inner_text("body")[:800])
            raise
        check("first account signs in with admin / change_me", True)
        check("data lands in GANXTERM_DATA_DIR", (data / "iguanaxterm.db").exists())
        check("home screen shows", page.locator("#ix-home").count() == 1)
        page.evaluate("window.close()")
        try:
            proc.wait(timeout=30)
            exited = True
        except subprocess.TimeoutExpired:
            exited = False
        check("closing the window quits Electron", exited)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and answers(f"{local_origin}/healthz"):
            time.sleep(0.5)
        check("quitting stops the local server", not answers(f"{local_origin}/healthz"))
    finally:
        stop(proc)


def main() -> int:
    if not ELECTRON.exists():
        print(f"Electron missing: run desktop/install.sh (or npm ci in desktop/)", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="ix-desktop-") as tmp, sync_playwright() as pw:
        scratch = Path(tmp)
        window_mode(pw, scratch)
        local_mode(pw, scratch)
    failed = [name for name, ok, _ in results if not ok]
    print("ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
