"""Phase 1: panes carry their own Terminal and Files tabs, and two panes on one
host do not fight over the pooled SFTP channel."""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace  # noqa: E402


APP = "http://127.0.0.1:8799/iguanaxterm"
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def term_text(page, pane):
    return page.evaluate(
        """(pane) => {
             const el = document.querySelector(`#pane-term-${pane} .xterm-rows`);
             return el ? el.innerText : "";
           }""", pane)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

    pg.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in pg.url:
        pg.fill('input[name="email"]', "admin")
        pg.fill('input[name="password"]', "testpass123")
        pg.click('input[type="submit"]')
    pg.wait_for_selector(".ix-toolbar", timeout=180000)
    reset_workspace(pg)
    pg.wait_for_timeout(500)

    if pg.locator(".wapyt-tree-row").count() == 0:
        pg.click('.ix-toolbar-btn[data-action="new"]')
        pg.wait_for_selector(".wapyt-modal-body .wapyt-form-body", timeout=30000)
        pg.fill('[name="name"]', "alpine-box")
        pg.fill('[name="host"]', "127.0.0.1")
        pg.fill('[name="port"]', "2222")
        pg.fill('[name="username"]', "testuser")
        pg.fill('[name="password"]', "testpass")
        pg.click(".wapyt-form-button-primary")
        pg.wait_for_selector(".wapyt-tree-row", timeout=30000)
    pg.wait_for_timeout(400)
    branch = pg.locator(".wapyt-tree-row[data-branch='true']")
    if branch.count():
        branch.first.click(); pg.wait_for_timeout(300)
    leaf = pg.locator(".wapyt-tree-row").last

    # ── pane 1 ───────────────────────────────────────────────────────────────
    leaf.dblclick()
    pg.wait_for_selector("#pane-term-pane_1 .xterm-rows", timeout=60000)
    pg.wait_for_timeout(3000)
    check(pg.locator('.ix-pane[data-pane="pane_1"] .ix-pane-tab').count() == 2,
          "pane has its own Terminal/Files tab strip")
    check(term_text(pg, "pane_1").strip() != "", "pane 1 terminal live")

    pg.click("#pane-term-pane_1 .xterm-screen")
    pg.keyboard.type("echo PANE_ONE_MARKER\n")
    pg.wait_for_timeout(1500)
    check("PANE_ONE_MARKER" in term_text(pg, "pane_1"), "pane 1 accepts input")

    # Files tab in the SAME pane, mounted lazily.
    check(pg.locator("#pane-files-pane_1 .wapyt-datatable").count() == 0,
          "files panel not built until asked for")
    pg.click('.ix-pane-tab[data-pane="pane_1"][data-pane-tab="files"]')
    pg.wait_for_selector("#pane-files-pane_1 tr[data-row-id]", timeout=40000)
    check(True, "files mounted inside the pane, not as a separate top-level tab")
    check(pg.locator(".wapyt-tab").count() == 1,
          "still one workspace tab",
          f"{pg.locator('.wapyt-tab').count()} tab(s)")
    check(pg.locator("#pane-files-pane_1 .ix-sftp-btn").count() >= 5,
          "SFTP toolbar present in the pane")

    # Terminal survived being hidden behind the Files tab.
    pg.click('.ix-pane-tab[data-pane="pane_1"][data-pane-tab="terminal"]')
    pg.wait_for_timeout(800)
    check("PANE_ONE_MARKER" in term_text(pg, "pane_1"),
          "terminal buffer survived the Files round trip")

    # ── pane 2: same host, second connection ─────────────────────────────────
    leaf.dblclick()
    pg.wait_for_selector("#pane-term-pane_2 .xterm-rows", timeout=60000)
    pg.wait_for_timeout(2500)
    check(pg.locator(".wapyt-tab").count() == 2,
          "connecting twice gives two panes")
    pg.click("#pane-term-pane_2 .xterm-screen")
    pg.keyboard.type("echo PANE_TWO_MARKER\n")
    pg.wait_for_timeout(1500)
    check("PANE_TWO_MARKER" in term_text(pg, "pane_2")
          and "PANE_ONE_MARKER" not in term_text(pg, "pane_2"),
          "the two panes are independent shells")

    pg.click('.ix-pane-tab[data-pane="pane_2"][data-pane-tab="files"]')
    pg.wait_for_selector("#pane-files-pane_2 tr[data-row-id]", timeout=40000)
    check(True, "pane 2 files mounted (second hold on the same channel)")

    # ── closing one pane leaves the other working ────────────────────────────
    # Note this does NOT prove the pool refcounting: the pool re-dials
    # transparently, so a later listing looks fine either way. The refcount
    # itself is pinned by tests/test_sftp_pool.py. What this checks is the
    # user-visible property -- closing a pane does not disturb its neighbour.
    pg.click('.wapyt-tab[data-tab-id="pane_1"] .wapyt-tab-close')
    pg.wait_for_timeout(1500)
    check(pg.locator(".wapyt-tab").count() == 1, "pane 1 closed")

    pg.click('.ix-pane-tab[data-pane="pane_2"][data-pane-tab="files"]')
    pg.click('#pane-files-pane_2 .ix-sftp-btn[data-sftp="refresh"]')
    pg.wait_for_timeout(2500)
    rows = pg.locator("#pane-files-pane_2 tr[data-row-id]").count()
    check(rows > 0,
          "pane 2's file browser still works after pane 1 closed",
          f"{rows} rows listed")
    check(term_text(pg, "pane_2").count("PANE_TWO_MARKER") >= 1,
          "pane 2's terminal still alive too")

    pg.screenshot(path="/tmp/ix_pane_smoke.png")
    print()
    print("console/page errors:", errs or "none")
    print()
    print("RESULT:", "ALL PASS" if all(results) and not errs else "FAILURES")
    b.close()
