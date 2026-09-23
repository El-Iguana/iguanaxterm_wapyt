"""
Folder downloads saved on the server, for a browser without folder pickers.

Firefox has no File System Access pickers; this removes them from Chromium,
which is the condition the app actually checks, so the fallback runs.

Needs the SSH target on :2222 and the app on :8799 started with
GANXTERM_DOWNLOAD_DIR set -- this script reads that directory directly to see
what was written (see README.md):

    GANXTERM_DOWNLOAD_DIR=/tmp/ixdl ... uv run python service.py

What it pins:
  - selecting a folder and pressing Download saves it on the server, with a
    queue row that finishes "done" and a toast naming where it went
  - the saved tree matches the remote, byte for byte, including a 20 MB file
  - Saved files lists it, opens folders, fetches a file to the browser, and
    deletes from the server
  - plain files in the same selection still go to the browser's downloads
"""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import reset_workspace, session_leaf  # noqa: E402
from transfer_smoke import login, row_for  # noqa: E402

DOWNLOADS = Path(os.environ.get("GANXTERM_DOWNLOAD_DIR", "/tmp/ixdl")) / "admin"
BIG = 20_000_000
results = []

NO_PICKERS = """
delete window.showDirectoryPicker; delete window.showSaveFilePicker;
delete window.showOpenFilePicker;
window.showDirectoryPicker = undefined; window.showSaveFilePicker = undefined;
window.showOpenFilePicker = undefined;
"""

MAKE_TREE = (
    "rm -rf ~/savetest && mkdir -p ~/savetest/inner/deeper && cd ~/savetest"
    " && printf 'top file' > top.txt && printf 'inner file' > inner/in.txt"
    " && printf 'deep file' > inner/deeper/deep.txt"
    f" && head -c {BIG} /dev/zero | tr '\\0' 'z' > inner/big.bin"
    " && printf 'loose' > ~/loose.txt && cd && echo TREE_READY\n"
)


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(viewport={"width": 1500, "height": 950}, accept_downloads=True)
    context.add_init_script(NO_PICKERS)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    login(page)
    reset_workspace(page)
    check(page.evaluate("typeof window.showDirectoryPicker") == "undefined",
          "the browser has no folder picker (as Firefox)")

    session_leaf(page).dblclick()
    page.wait_for_selector(".ix-pane .xterm-rows", timeout=60000)
    pane = page.eval_on_selector(".ix-pane", "e => e.dataset.pane")
    page.wait_for_timeout(2500)
    page.click(f"#pane-term-{pane} .xterm-screen")
    page.keyboard.type(MAKE_TREE)
    page.wait_for_function(
        "(p) => ((document.querySelector(`#pane-term-${p} .xterm-rows`) || {}).innerText || '')"
        ".split('TREE_READY').length > 2", arg=pane, timeout=60000,
    )
    page.click(f'.ix-pane-tab[data-pane="{pane}"][data-pane-tab="files"]')
    page.wait_for_selector(f"#pane-files-{pane} td[data-column-id='name']", timeout=30000)
    page.click(f'#pane-files-{pane} .ix-sftp-btn[data-sftp="refresh"]')
    page.wait_for_timeout(1500)

    # A folder and a loose file in one selection.
    for existing in DOWNLOADS.glob("savetest*"):
        import shutil
        shutil.rmtree(existing)
    row_for(page, "savetest").click()
    row_for(page, "loose.txt").click(modifiers=["Control"])
    with page.expect_download(timeout=30000) as loose:
        page.click(f'#pane-files-{pane} .ix-sftp-btn[data-sftp="download"]')
    check(Path(loose.value.path()).read_text() == "loose",
          "the loose file still goes to the browser's downloads")

    page.wait_for_selector(".ix-queue-row[data-state]", timeout=60000)
    queue = page.evaluate("""() => [...document.querySelectorAll('.ix-queue-row')].map(r => ({
        name: r.querySelector('.ix-queue-name').innerText,
        state: r.dataset.state || '',
        status: r.querySelector('.ix-queue-status').innerText}))""")
    check(queue and queue[-1]["name"] == "savetest → server" and queue[-1]["state"] == "done",
          "queue row for the folder finished", str(queue[-1] if queue else queue))
    toast = page.inner_text("#ix-toast")
    check("Saved 4 file(s) on the server" in toast and "savetest" in toast,
          "toast says where it went", repr(toast))

    saved = DOWNLOADS / "savetest"
    check((saved / "top.txt").read_text() == "top file"
          and (saved / "inner" / "in.txt").read_text() == "inner file"
          and (saved / "inner" / "deeper" / "deep.txt").read_text() == "deep file",
          "the tree is on the server's disk, contents intact")
    big = saved / "inner" / "big.bin"
    check(big.stat().st_size == BIG and big.read_bytes().count(b"z") == BIG,
          "the 20 MB file is complete and uncorrupted")
    check(not list(saved.rglob("*.part")), "no partial files left behind")

    # ── Saved files ──────────────────────────────────────────────────────────
    page.click('.ix-toolbar-btn[data-action="saved"]')
    page.wait_for_selector("#ix-saved-table td[data-column-id='name']", timeout=30000)
    names = page.eval_on_selector_all("#ix-saved-table td[data-column-id='name']",
                                      "els => els.map(e => e.innerText.trim())")
    check("savetest" in names, "Saved files lists the folder", str(names))

    page.locator("#ix-saved-table tr:has(td[data-column-id='name']:text-is('savetest'))").dblclick()
    page.wait_for_function("() => document.getElementById('ix-saved-path').innerText === '/savetest'",
                           timeout=15000)
    page.locator("#ix-saved-table tr:has(td[data-column-id='name']:text-is('top.txt'))").click()
    with page.expect_download(timeout=30000) as fetched:
        page.click('[data-saved="download"]')
    check(Path(fetched.value.path()).read_text() == "top file",
          "a saved file downloads back to the browser")

    unauth = context.request.get("http://127.0.0.1:8799/downloads/file?path=/savetest/../../../etc/passwd")
    check(unauth.status == 404, "a path outside the folder is refused", str(unauth.status))

    page.click('[data-saved="up"]')
    page.wait_for_function("() => document.getElementById('ix-saved-path').innerText === '/'",
                           timeout=15000)
    page.locator("#ix-saved-table tr:has(td[data-column-id='name']:text-is('savetest'))").click()
    page.once("dialog", lambda d: d.accept())
    page.click('[data-saved="delete"]')
    page.wait_for_timeout(1500)
    check(not saved.exists(), "Delete removes it from the server")

    check(not errors, "no console errors", "; ".join(errors[:3]))
    page.keyboard.press("Escape")
    reset_workspace(page)
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
