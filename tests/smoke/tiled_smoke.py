"""Phase 3: the tiled workspace, and switching between modes."""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace, session_leaf  # noqa: E402


APP = "http://127.0.0.1:8799/iguanaxterm"
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def term_text(pg, pane):
    return pg.evaluate("""(pane) => {
        const el = document.querySelector(`#pane-term-${pane} .xterm-rows`);
        return el ? el.innerText : "";
      }""", pane)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1600, "height": 1000})
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
    leaf = session_leaf(pg)

    check(pg.locator(".ix-mode-btn").count() == 2, "mode switch in the toolbar")

    # Two connections, each with a marker in its scrollback.
    for n in (1, 2):
        leaf.dblclick()
        pg.wait_for_selector(f"#pane-term-pane_{n} .xterm-rows", timeout=60000)
        pg.wait_for_timeout(2500)
        pg.click(f"#pane-term-pane_{n} .xterm-screen")
        pg.keyboard.type(f"echo GRID_MARKER_{n}\n")
        pg.wait_for_timeout(1200)
    check("GRID_MARKER_1" in term_text(pg, "pane_1")
          and "GRID_MARKER_2" in term_text(pg, "pane_2"),
          "two panes with distinct scrollback")

    # ── switch to tiled ─────────────────────────────────────────────────────
    pg.click('.ix-mode-btn[data-mode="tiled"]')
    pg.wait_for_selector("#ix-grid-host .grid-stack-item", timeout=30000)
    pg.wait_for_timeout(1800)

    tiled = pg.evaluate("""() => {
      const items = [...document.querySelectorAll('#ix-grid-host .grid-stack-item')];
      return {
        items: items.length,
        tabsHidden: document.getElementById('ix-tabs-host').hidden,
        panesInGrid: items.filter(i => i.querySelector('.ix-pane')).length,
        boxes: items.map(i => { const r = i.getBoundingClientRect();
                                return {w: Math.round(r.width), h: Math.round(r.height),
                                        x: Math.round(r.x), y: Math.round(r.y)}; }),
        gripsVisible: [...document.querySelectorAll('.ix-pane-grip')]
                        .every(g => g.getBoundingClientRect().width > 2),
      };
    }""")
    check(tiled["items"] == 2, "both panes got grid items", f"{tiled['items']}")
    check(tiled["tabsHidden"], "tab host hidden while tiled")
    check(tiled["panesInGrid"] == 2, "pane roots moved inside the grid items")
    check(tiled["gripsVisible"], "drag grips appear only in the grid")
    check(tiled["boxes"][0]["y"] == tiled["boxes"][1]["y"]
          and tiled["boxes"][0]["x"] != tiled["boxes"][1]["x"],
          "panes tile side by side",
          f"{tiled['boxes'][0]} | {tiled['boxes'][1]}")

    # The tab strip is hidden while tiled, so the pane header has to say
    # which connection this is.
    names = pg.evaluate("""() => [...document.querySelectorAll('#ix-grid-host .ix-pane-name')]
        .map(n => ({text: n.innerText.trim(), w: Math.round(n.getBoundingClientRect().width)}))""")
    check(len(names) == 2 and all(n["text"] == "alpine-box" and n["w"] > 20 for n in names),
          "each tile's header names its connection", str(names))

    # The whole point: buffers survived the move into the grid.
    check("GRID_MARKER_1" in term_text(pg, "pane_1")
          and "GRID_MARKER_2" in term_text(pg, "pane_2"),
          "both scrollbacks survived the switch to tiled")

    # And the PTYs re-fitted to their new, narrower boxes.
    pg.click("#pane-term-pane_1 .xterm-screen")
    pg.keyboard.type("stty size\n")
    pg.wait_for_timeout(1500)
    text = term_text(pg, "pane_1")
    check("GRID_MARKER_1" in text and "stty size" in text,
          "tiled pane still accepts input")
    import re
    sizes = re.findall(r"^(\d+)\s+(\d+)$", text, re.M)
    check(bool(sizes) and int(sizes[-1][1]) < 150,
          "remote PTY re-negotiated to the tile width",
          f"stty size -> {sizes[-1] if sizes else 'not found'}")

    pg.screenshot(path="/tmp/ix_phase3_tiled.png")

    # ── resize a tile ───────────────────────────────────────────────────────
    before = pg.evaluate("""() => {
      const i = document.querySelector('#ix-grid-host .grid-stack-item');
      return Math.round(i.getBoundingClientRect().width);
    }""")
    pg.evaluate("""() => {
      const grid = document.querySelector('.grid-stack').gridstack;
      const item = document.querySelector('#ix-grid-host .grid-stack-item');
      grid.update(item, {w: 9, h: 9});
    }""")
    pg.wait_for_timeout(1500)
    after = pg.evaluate("""() => {
      const i = document.querySelector('#ix-grid-host .grid-stack-item');
      return Math.round(i.getBoundingClientRect().width);
    }""")
    check(after > before, "resizing a tile widens it", f"{before}px -> {after}px")
    check("GRID_MARKER_1" in term_text(pg, "pane_1"), "buffer survived the resize")

    # ── a narrow tile keeps its name, and the name gives way first ──────────
    pg.evaluate("""() => {
      const grid = document.querySelector('.grid-stack').gridstack;
      const item = document.querySelector('#ix-grid-host .grid-stack-item[data-pane="pane_2"]');
      grid.update(item, {w: 3});
    }""")
    pg.wait_for_timeout(1200)
    narrow = pg.evaluate("""() => {
      const pane = document.querySelector('.ix-pane[data-pane="pane_2"]');
      const strip = pane.querySelector('.ix-pane-tabs');
      const close = pane.querySelector('.ix-pane-close').getBoundingClientRect();
      const box = strip.getBoundingClientRect();
      return {
        pane: Math.round(pane.getBoundingClientRect().width),
        name: Math.round(pane.querySelector('.ix-pane-name').getBoundingClientRect().width),
        overflow: strip.scrollWidth > strip.clientWidth + 1,
        closeInside: close.right <= box.right + 1 && close.width > 0,
      };
    }""")
    check(narrow["name"] > 0 and not narrow["overflow"] and narrow["closeInside"],
          "narrow tile: name still shown, strip does not overflow, close reachable",
          str(narrow))
    pg.screenshot(path="/tmp/claude-1000/ix_tiled_names.png")

    # ── back to tabbed ──────────────────────────────────────────────────────
    pg.click('.ix-mode-btn[data-mode="tabbed"]')
    pg.wait_for_timeout(1500)
    back = pg.evaluate("""() => ({
      gridHidden: document.getElementById('ix-grid-host').hidden,
      gridItems: document.querySelectorAll('#ix-grid-host .grid-stack-item').length,
      tabs: document.querySelectorAll('.wapyt-tab').length,
      panesInTabs: document.querySelectorAll('#ix-tabs-host .ix-pane').length,
    })""")
    check(back["gridHidden"] and back["gridItems"] == 0, "grid emptied and hidden")
    check(pg.evaluate("""() => [...document.querySelectorAll('.ix-pane-name')]
            .every(n => n.getBoundingClientRect().width === 0)"""),
          "the header name hides again in tabbed mode (the tab carries it)")
    check(back["tabs"] == 2 and back["panesInTabs"] == 2,
          "panes are back in their tabs", f"{back}")
    check("GRID_MARKER_1" in term_text(pg, "pane_1")
          and "GRID_MARKER_2" in term_text(pg, "pane_2"),
          "both scrollbacks survived the round trip")

    # ── closing from the pane's own button while tiled ──────────────────────
    pg.click('.ix-mode-btn[data-mode="tiled"]')
    pg.wait_for_timeout(1200)
    pg.click('.ix-pane-close[data-pane-close="pane_1"]')
    pg.wait_for_timeout(1200)
    closed = pg.evaluate("""() => ({
      items: document.querySelectorAll('#ix-grid-host .grid-stack-item').length,
      tabs: document.querySelectorAll('.wapyt-tab').length,
    })""")
    check(closed["items"] == 1 and closed["tabs"] == 1,
          "closing a tile removes both the item and its tab", f"{closed}")

    print()
    print("console/page errors:", errs or "none")
    print("RESULT:", "ALL PASS" if all(results) and not errs else "FAILURES")
    b.close()
