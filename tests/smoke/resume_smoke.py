"""
Interrupted downloads pick up where they stopped.

Real dropped connections, not simulated errors: flaky_proxy.py sits between
the browser and the app and resets download connections mid-body. The app
must be told the proxy is its public origin (see README.md):

    GANXTERM_DATA_DIR=/tmp/ixresume GANXTERM_ADMIN_PASS=testpass123 PORT=8797 \\
      GANXTERM_CANONICAL_ORIGIN=http://127.0.0.1:8798 uv run python service.py &
    python3 tests/smoke/flaky_proxy.py 8798 8797 /tmp/ixresume-cuts &
    python3 tests/smoke/resume_smoke.py

Needs the SSH target (ix-sshtest) for the remote file.

What it pins:
  A. two drops: the download resumes by itself and arrives byte-identical
  B. more drops than retries: the row pauses, and Resume finishes it
  C. paused, the file changes on the server, Resume: it starts over and
     delivers the new file, never a splice of old and new
  D. the route itself: 206 for a range, 416 past the end, 200 when If-Range
     no longer matches
"""
import hashlib
import os
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import reset_workspace, session_leaf  # noqa: E402
from transfer_smoke import PICKER_STUB, row_for  # noqa: E402

APP = os.environ.get("RESUME_APP", "http://127.0.0.1:8798/iguanaxterm")
CUTS = Path(os.environ.get("RESUME_CUTS", "/tmp/ixresume-cuts"))
REMOTE = "/home/testuser/resumeme.bin"
SIZE = 12_000_000
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def target(cmd):
    return subprocess.run(["podman", "exec", "ix-sshtest", "sh", "-c", cmd],
                          capture_output=True, text=True, check=True, timeout=60).stdout


def remote_sha():
    return target(f"sha256sum {REMOTE}").split()[0]


def cuts(n):
    CUTS.write_text(str(n))


def saved_sha(page, name):
    return page.evaluate(
        """async (name) => {
             const key = Object.keys(window.__saved).find(k => k.endsWith('/' + name));
             if (!key) return null;
             const d = await crypto.subtle.digest('SHA-256', window.__saved[key]);
             return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
           }""", name)


def queue_row(page):
    return page.evaluate("""() => { const r = [...document.querySelectorAll('.ix-queue-row')].pop();
        return r ? {state: r.dataset.state || '', status: r.querySelector('.ix-queue-status').innerText,
                    resume: !!r.querySelector('.ix-queue-resume')} : null; }""")


def download(page, timeout=90000):
    """Download resumeme.bin and wait for *its* queue row -- not the last
    row, which until this one appears is the previous download's."""
    page.evaluate("() => { window.__saved = {}; }")
    before = page.locator(".ix-queue-row").count()
    row_for(page, "resumeme.bin").click()
    page.click('.ix-sftp-btn[data-sftp="download"]')
    page.wait_for_function(
        """(n) => { const rows = document.querySelectorAll('.ix-queue-row');
                    return rows.length > n && rows[rows.length - 1].dataset.state; }""",
        arg=before, timeout=timeout)
    return queue_row(page)


target(f"head -c {SIZE} /dev/urandom > {REMOTE} && chown testuser:testuser {REMOTE}")
cuts(0)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.add_init_script(PICKER_STUB)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append("console: " + m.text[:300]) if m.type == "error" else None)
    page.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in page.url:
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass123")
        page.click('input[type="submit"]')
    page.wait_for_selector(".ix-toolbar", timeout=180000)
    reset_workspace(page)

    if not session_leaf(page).count():
        page.click('.ix-toolbar-btn[data-action="new"]')
        form = page.locator(".wapyt-modal-body:visible .wapyt-form-body")
        form.wait_for(timeout=30000)
        form.locator('[name="name"]').fill("alpine-box")
        form.locator('[name="host"]').fill("127.0.0.1")
        form.locator('[name="port"]').fill("2222")
        form.locator('[name="username"]').fill("testuser")
        form.locator('[name="password"]').fill("testpass")
        page.locator(".wapyt-modal-body:visible .wapyt-form-button-primary").click()
        page.wait_for_timeout(1500)
    session_leaf(page).click()
    page.click('.ix-toolbar-btn[data-action="sftp"]')
    page.wait_for_selector(".wapyt-datatable-table tbody tr", timeout=30000)
    page.click('.ix-sftp-btn[data-sftp="refresh"]')
    page.wait_for_function("""() => [...document.querySelectorAll('.wapyt-datatable-table tbody tr')]
        .some(r => r.innerText.includes('resumeme.bin'))""", timeout=30000)
    sid = page.evaluate("""() => document.querySelector('.wapyt-tree-row[data-node-id^="sess_"]')
        .dataset.nodeId.slice(5)""")

    # ── D: the route ────────────────────────────────────────────────────────
    route = page.evaluate(
        """async ([sid, path, size]) => {
             const url = `/files/${sid}/download?path=${encodeURIComponent(path)}`;
             const whole = await fetch(url, {headers: {Range: 'bytes=0-0'}});
             const etag = whole.headers.get('etag');
             const part = await fetch(url, {headers: {Range: 'bytes=11999990-'}});
             const past = await fetch(url, {headers: {Range: `bytes=${size}-`}});
             const stale = await fetch(url, {headers: {Range: 'bytes=10-', 'If-Range': '"stale"'}});
             stale.body.cancel();
             return {first: whole.status, etag, accept: whole.headers.get('accept-ranges'),
                     part: part.status, partRange: part.headers.get('content-range'),
                     partLen: (await part.arrayBuffer()).byteLength,
                     past: past.status, stale: stale.status};
           }""", [sid, REMOTE, SIZE])
    check(route["first"] == 206 and route["accept"] == "bytes" and route["etag"],
          "downloads advertise ranges and a validator", str(route))
    check(route["part"] == 206 and route["partRange"] == f"bytes 11999990-11999999/{SIZE}"
          and route["partLen"] == 10, "a range gets 206 and exactly those bytes")
    check(route["past"] == 416, "a range past the end gets 416")
    check(route["stale"] == 200, "a stale If-Range gets the whole file (200), not a splice")

    # ── A: two drops, resumed automatically ─────────────────────────────────
    cuts(2)
    row = download(page)
    check(row["state"] == "done" and "resumed 2×" in row["status"],
          "A: two drops, resumed by itself", str(row))
    check(saved_sha(page, "resumeme.bin") == remote_sha(), "A: and the file is byte-identical")

    # ── B: more drops than retries: pause, then Resume ──────────────────────
    cuts(5)   # the first attempt and all four retries
    row = download(page, timeout=120000)
    check(row["state"] == "paused" and row["resume"], "B: out of retries, the row pauses", str(row))
    check(saved_sha(page, "resumeme.bin") is None, "B: nothing written under the real name yet")
    page.click(".ix-queue-row:last-child .ix-queue-resume")
    page.wait_for_function("() => [...document.querySelectorAll('.ix-queue-row')].pop().dataset.state === 'done'",
                           timeout=60000)
    check(saved_sha(page, "resumeme.bin") == remote_sha(), "B: Resume finishes it, byte-identical")

    # ── C: paused, the file changes, Resume starts over ─────────────────────
    cuts(5)
    row = download(page, timeout=120000)
    check(row["state"] == "paused", "C: paused again")
    # A different size alone changes the ETag, which is all If-Range compares.
    target(f"head -c 4000000 /dev/urandom > {REMOTE}")
    page.click(".ix-queue-row:last-child .ix-queue-resume")
    page.wait_for_function("() => [...document.querySelectorAll('.ix-queue-row')].pop().dataset.state === 'done'",
                           timeout=60000)
    check("restarted" in queue_row(page)["status"], "C: the change is noticed and it restarts",
          queue_row(page)["status"])
    check(saved_sha(page, "resumeme.bin") == remote_sha(), "C: the new file, whole, not a splice")

    # The 416 (D) and the resets (the proxy) are provoked on purpose; the
    # browser logs each. Anything else is not expected.
    expected = ("Range Not Satisfiable", "ERR_CONNECTION_RESET")
    unexpected = [e for e in errors if not any(x in e for x in expected)]
    check(not unexpected, "no errors beyond the ones provoked", "; ".join(unexpected[:3]))
    reset_workspace(page)
    browser.close()

cuts(0)
print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
