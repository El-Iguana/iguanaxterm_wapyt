"""
Phase 0 spike: can a live, PTY-connected xterm be moved to a different DOM
parent without losing its buffer, its socket, or its ability to type?

If it survives, the tabbed/tiled toggle and GridStack drag are both a plain
reparent. If it does not, panes must be absolutely positioned over placeholder
grid items -- materially more code. Everything downstream turns on this.
"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace  # noqa: E402


APP = "http://127.0.0.1:8799/iguanaxterm"
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


# Capture every xterm instance by intercepting the global the UMD bundle sets.
INTERCEPT = """
Object.defineProperty(window, 'Terminal', {
  configurable: true,
  get() { return window.__realTerm; },
  set(v) {
    const Wrapped = function (...args) {
      const inst = new v(...args);
      (window.__xterms = window.__xterms || []).push(inst);
      return inst;
    };
    Wrapped.prototype = v.prototype;
    Object.setPrototypeOf(Wrapped, v);
    window.__realTerm = Wrapped;
  }
});
"""

BUFFER_STATE = """() => {
  const t = window.__xterms && window.__xterms[0];
  if (!t) return null;
  const b = t.buffer.active;
  const lines = [];
  for (let i = 0; i < b.length; i++) lines.push(b.getLine(i).translateToString(true));
  const text = lines.join("\\n");
  return {
    bufferLines: b.length,
    cols: t.cols, rows: t.rows,
    hasMarker: text.includes("SPIKE_MARKER_ALPHA"),
    seqTail: text.includes("297") && text.includes("300"),
    renderedRows: document.querySelectorAll('.xterm-rows > div').length,
    renderedText: (document.querySelector('.xterm-rows')?.innerText || "").trim().length,
    wsOpen: window.__spikeWsOpen === undefined ? null : window.__spikeWsOpen,
    parent: document.querySelector('.wapyt-terminal')?.parentElement?.id
            || document.querySelector('.wapyt-terminal')?.parentElement?.className,
  };
}"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.add_init_script(INTERCEPT)
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

    # Seed the session through the real dialog unless it is already there.
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

    # Expand the folder branch, then open the leaf.
    branch = pg.locator(".wapyt-tree-row[data-branch='true']")
    if branch.count():
        branch.first.click()
        pg.wait_for_timeout(300)
    leaf = pg.locator(".wapyt-tree-row").last
    check("alpine-box" in leaf.inner_text(), "session leaf present", repr(leaf.inner_text().strip()))
    leaf.dblclick()

    pg.wait_for_selector(".xterm-rows", timeout=60000)
    pg.wait_for_timeout(3500)
    check(pg.evaluate("() => !!(window.__xterms && window.__xterms[0])"),
          "captured the live xterm instance")

    # Fill the scrollback so buffer loss would be unmistakable.
    pg.click(".xterm-screen")
    pg.keyboard.type("echo SPIKE_MARKER_ALPHA && seq 1 300\n")
    pg.wait_for_timeout(2500)

    before = pg.evaluate(BUFFER_STATE)
    check(before and before["hasMarker"] and before["seqTail"],
          "baseline: marker and 300 lines of scrollback present",
          f"buffer={before['bufferLines']} lines, {before['cols']}x{before['rows']}")

    # ── The move ────────────────────────────────────────────────────────────
    # Move the pane subtree, not the terminal's own host element, into a fresh
    # parent of a realistic size -- exactly what a host switch would do.
    moved = pg.evaluate("""() => {
      const term = document.querySelector('.wapyt-terminal');
      // The PANE ROOT, which is what the design says to move. Moving whatever
      // happens to be the terminal's parent picks up .ix-pane-body, whose
      // children are absolutely positioned: outside its flex context it has
      // zero height, the terminal gets 900x0, and fit() correctly skips it --
      // which reads as "the reparent broke the terminal" when it did not.
      const pane = term.closest('.ix-pane') || term.parentElement;
      const dest = document.createElement('div');
      dest.id = 'spike-dest';
      // Clear of the toolbar: it grew when the layout-mode switch was added,
      // and a box at top:60 ends up underneath it, so clicks into the moved
      // terminal hit a toolbar button instead.
      dest.style.cssText =
        'position:fixed;left:40px;top:140px;width:900px;height:560px;' +
        'z-index:9500;background:#000';
      document.body.appendChild(dest);
      window.__spikeOrigin = pane.parentElement;
      dest.appendChild(pane);                        // <-- the reparent
      return { destChildren: dest.children.length, stillConnected: term.isConnected };
    }""")
    check(moved["stillConnected"], "terminal element still in the document after the move")
    pg.wait_for_timeout(1200)

    after = pg.evaluate(BUFFER_STATE)
    check(after["hasMarker"] and after["seqTail"],
          "buffer SURVIVED the reparent",
          f"buffer={after['bufferLines']} lines (was {before['bufferLines']})")
    check(after["renderedText"] > 0,
          "renderer still painting after the reparent",
          f"{after['renderedRows']} rows rendered, {after['renderedText']} chars")

    # Socket and stdin: type into it in its new home.
    # Focus the helper textarea directly rather than clicking: the moved box
    # overlaps app chrome, and hit-testing which element is on top is not what
    # this spike is measuring.
    pg.evaluate("""() => {
      // xterm's input sink is a textarea inside .xterm-helpers; its class name
      // has changed across versions, so find it by tag under the moved box.
      const ta = document.querySelector('#spike-dest textarea');
      if (!ta) throw new Error('no xterm textarea under #spike-dest');
      ta.focus();
    }""")
    pg.keyboard.type("echo SPIKE_AFTER_MOVE_OK\n")
    pg.wait_for_timeout(2500)
    live = pg.evaluate("""() => {
      const t = window.__xterms[0]; const b = t.buffer.active;
      const out = [];
      for (let i = 0; i < b.length; i++) out.push(b.getLine(i).translateToString(true));
      const text = out.join("\\n");
      return { echoed: (text.match(/SPIKE_AFTER_MOVE_OK/g) || []).length,
               cols: t.cols, rows: t.rows };
    }""")
    check(live["echoed"] >= 2,
          "socket and stdin still live after the reparent",
          f"command echoed and executed ({live['echoed']} occurrences), now {live['cols']}x{live['rows']}")
    check(live["cols"] != before["cols"] or live["rows"] != before["rows"],
          "re-fitted to the new parent's size",
          f"{before['cols']}x{before['rows']} -> {live['cols']}x{live['rows']}")

    # And back again, which is what toggling the mode twice does.
    pg.evaluate("""() => {
      const pane = document.querySelector('#spike-dest').firstElementChild;
      window.__spikeOrigin.appendChild(pane);
      document.querySelector('#spike-dest').remove();
    }""")
    pg.wait_for_timeout(1500)
    back = pg.evaluate(BUFFER_STATE)
    check(back["hasMarker"] and back["seqTail"] and back["renderedText"] > 0,
          "survived the move back",
          f"buffer={back['bufferLines']} lines, {back['renderedText']} chars rendered")

    pg.screenshot(path=f"{__file__.rsplit('/', 1)[0]}/spike_after.png")
    print()
    print("console/page errors:", errs or "none")
    print()
    print("VERDICT:", "REPARENT IS SAFE" if all(results) else "REPARENT IS NOT SAFE")
    b.close()
