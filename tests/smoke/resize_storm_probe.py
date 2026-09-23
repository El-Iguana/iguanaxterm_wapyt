"""How many PTY resize messages does one drag actually send?"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace  # noqa: E402


APP = "http://127.0.0.1:8799/iguanaxterm"

COUNTER = """
window.__resizes = [];
const send = WebSocket.prototype.send;
WebSocket.prototype.send = function (data) {
  try {
    if (typeof data === 'string' && data.includes('"resize"')) {
      window.__resizes.push({t: performance.now(), d: JSON.parse(data)});
    }
  } catch (e) {}
  return send.call(this, data);
};
"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1600, "height": 1000})
    pg.add_init_script(COUNTER)
    pg.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in pg.url:
        pg.fill('input[name="email"]', "admin"); pg.fill('input[name="password"]', "testpass123")
        pg.click('input[type="submit"]')
    pg.wait_for_selector(".ix-toolbar", timeout=180000); pg.wait_for_timeout(500)
    reset_workspace(pg)
    br = pg.locator(".wapyt-tree-row[data-branch='true']")
    if br.count(): br.first.click(); pg.wait_for_timeout(300)
    pg.locator(".wapyt-tree-row").last.dblclick()
    pg.wait_for_selector("#pane-term-pane_1 .xterm-rows", timeout=60000)
    pg.wait_for_timeout(2500)
    pg.click('.ix-mode-btn[data-mode="tiled"]')
    pg.wait_for_selector("#ix-grid-host .grid-stack-item", timeout=30000)
    pg.wait_for_timeout(1500)

    pg.evaluate("window.__resizes = []")

    # The handle carries ui-resizable-autohide and is invisible until hover,
    # so it has no box to grab. Drop the class to make the probe deterministic.
    pg.evaluate("""() => {
      document.querySelectorAll('.grid-stack-item').forEach(
        i => i.classList.remove('ui-resizable-autohide'));
    }""")
    pg.hover("#ix-grid-host .grid-stack-item")
    pg.wait_for_timeout(300)
    handle = pg.locator("#ix-grid-host .grid-stack-item .ui-resizable-se").first
    box = handle.bounding_box()
    print("resize handle:", "found" if box else "NOT FOUND")
    if box:
        pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        pg.mouse.down()
        # 40 intermediate frames across ~600px, like a real slow drag.
        for i in range(40):
            pg.mouse.move(box["x"] + 5 + (i + 1) * 15, box["y"] + 5 + (i + 1) * 4)
            pg.wait_for_timeout(25)
        pg.mouse.up()
        pg.wait_for_timeout(1500)

    r = pg.evaluate("""() => {
      const rs = window.__resizes;
      const span = rs.length > 1 ? rs[rs.length - 1].t - rs[0].t : 0;
      return { count: rs.length, spanMs: Math.round(span),
               cols: rs.map(x => x.d.cols),
               unique: new Set(rs.map(x => x.d.cols + 'x' + x.d.rows)).size };
    }""")
    print(f"PTY resize messages during one drag: {r['count']} "
          f"({r['unique']} distinct sizes) over {r['spanMs']}ms")
    print("cols sequence:", r["cols"])
    b.close()
