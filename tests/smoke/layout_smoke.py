"""Phase 4: the layout survives a reload, and restoring it dials nothing."""
import re
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace  # noqa: E402


APP = "http://127.0.0.1:8799/iguanaxterm"
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


# Count outbound terminal sockets, so "restores without dialling" is measured.
COUNTER = """
window.__sockets = [];
const OrigWS = window.WebSocket;
window.WebSocket = function (url, protos) {
  window.__sockets.push(String(url));
  return protos === undefined ? new OrigWS(url) : new OrigWS(url, protos);
};
window.WebSocket.prototype = OrigWS.prototype;
Object.assign(window.WebSocket, OrigWS);
"""


def login(pg):
    pg.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in pg.url:
        pg.fill('input[name="email"]', "admin")
        pg.fill('input[name="password"]', "testpass123")
        pg.click('input[type="submit"]')
    pg.wait_for_selector(".ix-toolbar", timeout=180000)
    pg.wait_for_timeout(600)





with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1600, "height": 1000})
    ctx.add_init_script(COUNTER)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

    login(pg)
    reset_workspace(pg)
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
    br = pg.locator(".wapyt-tree-row[data-branch='true']")
    if br.count():
        br.first.click(); pg.wait_for_timeout(300)
    leaf = pg.locator(".wapyt-tree-row").last

    # Two panes, tiled, one of them parked in its Files tab in a subdirectory.
    # Pane ids are minted per page load and the reset above already used some,
    # so discover them rather than assuming pane_1/pane_2.
    def open_pane():
        before = pg.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
        leaf.dblclick()
        pg.wait_for_function(
            "n => document.querySelectorAll('.ix-pane').length > n", arg=len(before)
        )
        after = pg.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
        fresh = [x for x in after if x not in before][0]
        pg.wait_for_selector(f"#pane-term-{fresh} .xterm-rows", timeout=60000)
        pg.wait_for_timeout(2200)
        return fresh

    first_pane = open_pane()
    second_pane = open_pane()
    pg.click('.ix-mode-btn[data-mode="tiled"]')
    pg.wait_for_selector("#ix-grid-host .grid-stack-item", timeout=30000)
    pg.wait_for_timeout(1200)

    # Address tiles by pane, never by DOM index: GridStack reorders the DOM by
    # position, so items[0] is not necessarily the first pane opened.
    pg.evaluate("""(ids) => {
      const grid = document.querySelector('.grid-stack').gridstack;
      const byPane = p => document.querySelector(`.grid-stack-item[data-pane="${p}"]`);
      grid.update(byPane(ids[0]), {x: 0, y: 0, w: 8, h: 9});
      grid.update(byPane(ids[1]), {x: 8, y: 0, w: 4, h: 5});
    }""", [first_pane, second_pane])
    pg.wait_for_timeout(900)

    pg.click(f'.ix-pane-tab[data-pane="{second_pane}"][data-pane-tab="files"]')
    pg.wait_for_selector(f"#pane-files-{second_pane} tr[data-row-id]", timeout=40000)
    pg.dblclick(f'#pane-files-{second_pane} tr[data-row-id] >> text="logs"')
    pg.wait_for_timeout(1500)

    saved = pg.evaluate("""() => {
      const g = document.querySelector('.grid-stack').gridstack.save(false);
      return JSON.stringify(g);
    }""")
    print("grid before reload:", saved)
    pg.wait_for_timeout(1500)   # let the debounced save land

    # ── reload ──────────────────────────────────────────────────────────────
    login(pg)
    pg.wait_for_timeout(3000)

    after = pg.evaluate("""() => ({
      mode: document.querySelector('.ix-mode-btn[aria-selected="true"]').dataset.mode,
      items: [...document.querySelectorAll('#ix-grid-host .grid-stack-item')].map(i => ({
         pane: i.dataset.pane,
         x: +i.getAttribute('gs-x'), y: +i.getAttribute('gs-y'),
         w: +i.getAttribute('gs-w'), h: +i.getAttribute('gs-h'),
      })),
      reconnects: document.querySelectorAll('.ix-reconnect-btn').length,
      terminals: document.querySelectorAll('.xterm').length,
      sockets: window.__sockets.filter(u => u.includes('/ws/terminal/')).length,
    })""")
    check(after["mode"] == "tiled", "tiled mode restored", after["mode"])
    check(len(after["items"]) == 2, "both tiles restored", str(after["items"]))
    check(after["reconnects"] == 2, "each restored tile offers Reconnect",
          f"{after['reconnects']} buttons")
    check(after["terminals"] == 0 and after["sockets"] == 0,
          "nothing dialled on restore",
          f"{after['sockets']} terminal sockets opened")

    # Panes restore in saved order, which is creation order: pane_1 carries the
    # first pane's box, pane_2 the second's.
    boxes = {i["pane"]: (i["x"], i["y"], i["w"], i["h"]) for i in after["items"]}
    check(boxes.get("pane_1") == (0, 0, 8, 9) and boxes.get("pane_2") == (8, 0, 4, 5),
          "each tile came back with its own geometry", str(boxes))

    # ── reconnect the one that was on its terminal ──────────────────────────
    # Panes restore in saved order, so pane_1 is the one left on Terminal and
    # pane_2 the one parked in Files. Targeting them by name rather than by
    # position: clicking "the first button" picked the Files pane, whose
    # terminal panel is correctly hidden.
    pg.click('.ix-reconnect-btn[data-reconnect="pane_1"]')
    pg.wait_for_selector("#pane-term-pane_1 .xterm-rows", timeout=60000)
    pg.wait_for_timeout(3000)
    live = pg.evaluate("""() => ({
      terminals: document.querySelectorAll('.xterm').length,
      sockets: window.__sockets.filter(u => u.includes('/ws/terminal/')).length,
      reconnects: document.querySelectorAll('.ix-reconnect-btn').length,
    })""")
    check(live["sockets"] == 1 and live["terminals"] == 1,
          "Reconnect dials exactly one connection", str(live))
    check(live["reconnects"] == 1, "the other tile is still parked")

    pane = pg.evaluate("""() => {
      const el = document.querySelector('#pane-term-pane_1 .xterm-rows');
      return el ? el.innerText : '';
    }""")
    check("Welcome" in pane or "$" in pane, "reconnected terminal has a live shell")

    # The pane parked in Files comes back to Files, in the same directory.
    pg.click('.ix-reconnect-btn[data-reconnect="pane_2"]')
    pg.wait_for_timeout(5000)
    files = pg.evaluate("""() => {
      const crumbs = [...document.querySelectorAll('.ix-crumbs')].map(
        c => [...c.children].map(k => k.textContent.trim()).join('/'));
      const filesTabs = [...document.querySelectorAll('.ix-pane-tab[data-pane-tab="files"]')]
        .filter(t => t.getAttribute('aria-selected') === 'true').length;
      return { crumbs, filesTabs };
    }""")
    check(files["filesTabs"] >= 1,
          "the pane parked in Files reopened on Files", str(files["filesTabs"]))
    check(any("logs" in c for c in files["crumbs"]),
          "and in the directory it was left in", str(files["crumbs"]))

    pg.screenshot(path="/tmp/ix_phase4.png")
    print()
    print("console/page errors:", errs or "none")
    print("RESULT:", "ALL PASS" if all(results) and not errs else "FAILURES")
    b.close()
