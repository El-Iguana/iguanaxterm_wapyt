"""Per-pane maximize: one tile fills the workspace, and the grid is untouched."""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace, new_pane, session_leaf  # noqa: E402

APP = "http://127.0.0.1:8799/iguanaxterm"
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def boxes(pg):
    return pg.evaluate("""() => Object.fromEntries(
      [...document.querySelectorAll('#ix-grid-host .grid-stack-item')].map(i => {
        const r = i.getBoundingClientRect();
        return [i.dataset.pane, {w: Math.round(r.width), h: Math.round(r.height),
                                 x: Math.round(r.x), y: Math.round(r.y)}];
      }))""")


def nodes(pg):
    """The grid MODEL, which maximize must not disturb."""
    return pg.evaluate("""() => Object.fromEntries(
      [...document.querySelectorAll('#ix-grid-host .grid-stack-item')].map(i => {
        const n = i.gridstackNode;
        return [i.dataset.pane, {x: n.x, y: n.y, w: n.w, h: n.h}];
      }))""")


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": 1600, "height": 1000}).new_page()
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
    pg.wait_for_timeout(400)

    if pg.locator(".wapyt-tree-row").count() == 0:
        pg.click('.ix-toolbar-btn[data-action="new"]')
        pg.wait_for_selector(".wapyt-modal-body .wapyt-form-body", timeout=30000)
        for name, val in (("name", "alpine-box"), ("host", "127.0.0.1"),
                          ("port", "2222"), ("username", "testuser"),
                          ("password", "testpass")):
            pg.fill(f'[name="{name}"]', val)
        pg.click(".wapyt-form-button-primary")
        pg.wait_for_selector(".wapyt-tree-row", timeout=30000)
    pg.wait_for_timeout(400)
    leaf = session_leaf(pg)

    first = new_pane(pg, leaf); pg.wait_for_timeout(2000)
    second = new_pane(pg, leaf); pg.wait_for_timeout(2000)

    pg.click('.ix-mode-btn[data-mode="tiled"]')
    pg.wait_for_selector("#ix-grid-host .grid-stack-item", timeout=30000)
    pg.wait_for_timeout(1500)

    # Mark the shell only once tiled: in tabbed mode the first pane is a
    # background tab and its terminal is correctly not visible.
    pg.click(f"#pane-term-{first} .xterm-screen")
    pg.keyboard.type("echo MAX_MARKER\n")
    pg.wait_for_timeout(1200)

    check(pg.locator(".grid-stack .ix-pane-max").count() == 2,
          "every tile has a maximize button")
    before_boxes, before_nodes = boxes(pg), nodes(pg)
    host = pg.evaluate("""() => {
      const r = document.getElementById('ix-grid-host').getBoundingClientRect();
      return {w: Math.round(r.width), h: Math.round(r.height)};
    }""")

    # ── maximize ────────────────────────────────────────────────────────────
    pg.click(f'[data-pane-max="{first}"]')
    pg.wait_for_timeout(1200)
    after = boxes(pg)
    grown = after[first]
    check(grown["w"] > before_boxes[first]["w"] * 1.5
          and abs(grown["w"] - host["w"]) < 20 and abs(grown["h"] - host["h"]) < 20,
          "maximized tile fills the workspace",
          f"{before_boxes[first]['w']}x{before_boxes[first]['h']} -> {grown['w']}x{grown['h']} "
          f"(host {host['w']}x{host['h']})")

    covered = pg.evaluate("""(ids) => {
      const max = document.querySelector(`.grid-stack-item[data-pane="${ids[0]}"]`);
      const other = document.querySelector(`.grid-stack-item[data-pane="${ids[1]}"]`);
      const mr = max.getBoundingClientRect();
      const mid = {x: mr.x + mr.width / 2, y: mr.y + mr.height / 2};
      const top = document.elementFromPoint(mid.x, mid.y);
      return { maxOnTop: max.contains(top),
               otherStillThere: !!other,
               maxZ: getComputedStyle(max).zIndex };
    }""", [first, second])
    check(covered["maxOnTop"], "maximized tile is on top", f"z-index {covered['maxZ']}")
    check(covered["otherStillThere"], "the other tile still exists behind it")

    check(nodes(pg) == before_nodes,
          "the grid MODEL is untouched -- maximize is presentational",
          f"{nodes(pg)}")

    ptty = pg.evaluate("""(pane) => {
      const el = document.querySelector(`#pane-term-${pane} .xterm-rows`);
      return el ? el.innerText : '';
    }""", first)
    check("MAX_MARKER" in ptty, "scrollback intact while maximized")

    pg.click(f"#pane-term-{first} .xterm-screen")
    pg.keyboard.type("stty size\n")
    pg.wait_for_timeout(1500)
    import re
    text = pg.evaluate("""(pane) => document.querySelector(
      `#pane-term-${pane} .xterm-rows`).innerText""", first)
    sizes = re.findall(r"^(\d+)\s+(\d+)\s*$", text, re.M)
    check(bool(sizes) and int(sizes[-1][1]) > 120,
          "remote PTY grew with the tile",
          f"stty size -> {sizes[-1] if sizes else 'not found'}")

    hidden = pg.evaluate("""(pane) => {
      const item = document.querySelector(`.grid-stack-item[data-pane="${pane}"]`);
      const grip = item.querySelector('.ix-pane-grip');
      const handle = item.querySelector('.ui-resizable-handle');
      return { grip: getComputedStyle(grip).display,
               handle: handle ? getComputedStyle(handle).display : 'none' };
    }""", first)
    check(hidden["grip"] == "none" and hidden["handle"] == "none",
          "drag grip and resize handle hidden while maximized", str(hidden))

    # ── Escape restores, but only from outside a terminal ───────────────────
    # With the shell focused, Escape belongs to the remote -- it is how you
    # leave insert mode in vim -- so the app deliberately leaves it alone.
    pg.click(f"#pane-term-{first} .xterm-screen")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(800)
    still = pg.evaluate("""() => [...document.querySelectorAll(
      '#ix-grid-host .grid-stack-item')].filter(i => i.dataset.maximized).length""")
    check(still == 1, "Escape inside the terminal is left to the remote")

    # From outside the terminal, it restores. Focus is moved directly rather
    # than clicked: the pane's own Terminal tab would hand focus straight back
    # to the shell, and hit-testing around a full-workspace overlay is not what
    # this is measuring.
    pg.evaluate("() => document.querySelector('.wapyt-tree-filter').focus()")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(1200)
    restored = boxes(pg)
    check(abs(restored[first]["w"] - before_boxes[first]["w"]) < 6
          and abs(restored[second]["w"] - before_boxes[second]["w"]) < 6,
          "Escape restores both tiles to their old boxes",
          f"{restored}")
    check(nodes(pg) == before_nodes, "model still untouched after restoring")

    # ── the button toggles, and only one tile is ever maximized ─────────────
    # A maximized tile covers its neighbours, so the other tile's button is
    # genuinely unreachable while it is up -- you restore first. That is the
    # path a person takes, so it is the path tested.
    pg.click(f'[data-pane-max="{first}"]'); pg.wait_for_timeout(800)
    maximized = pg.evaluate("""() => [...document.querySelectorAll(
      '#ix-grid-host .grid-stack-item')].filter(i => i.dataset.maximized)
        .map(i => i.dataset.pane)""")
    check(maximized == [first], "the button maximizes", str(maximized))

    pg.click(f'[data-pane-max="{first}"]'); pg.wait_for_timeout(800)
    pg.click(f'[data-pane-max="{second}"]'); pg.wait_for_timeout(900)
    maximized = pg.evaluate("""() => [...document.querySelectorAll(
      '#ix-grid-host .grid-stack-item')].filter(i => i.dataset.maximized)
        .map(i => i.dataset.pane)""")
    check(maximized == [second],
          "restoring then maximizing the other leaves exactly one up",
          str(maximized))

    pg.click(f'[data-pane-max="{second}"]'); pg.wait_for_timeout(700)

    # ── switching to tabbed clears it ───────────────────────────────────────
    pg.click(f'[data-pane-max="{first}"]'); pg.wait_for_timeout(700)
    pg.click('.ix-mode-btn[data-mode="tabbed"]'); pg.wait_for_timeout(1000)
    pg.click('.ix-mode-btn[data-mode="tiled"]'); pg.wait_for_timeout(1500)
    back = pg.evaluate("""() => [...document.querySelectorAll(
      '#ix-grid-host .grid-stack-item')].filter(i => i.dataset.maximized).length""")
    check(back == 0, "a round trip through tabbed clears the maximize", f"{back}")
    check(nodes(pg) == before_nodes, "and the layout is still what it was")

    pg.screenshot(path="/tmp/ix_maximize.png")
    print()
    print("console/page errors:", errs or "none")
    print("RESULT:", "ALL PASS" if all(results) and not errs else "FAILURES")
    b.close()
