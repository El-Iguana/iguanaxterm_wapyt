"""
Reconnect all: dial every restored pane, one at a time.

Needs the SSH target on :2222, ftp_target.py on :2121 and the app on :8799
(see README.md); creates `alpine-box` and `ftps-box` if they are missing.

What it pins:
  - after a restore the toolbar offers "Reconnect all (N)" and nothing dials
  - reconnecting one pane by hand takes it off the count
  - the button dials the rest *sequentially*: each terminal socket is opened
    only after the previous one reported connected -- measured from the
    socket's own "connected" message, not inferred
  - the files-only (FTP) pane comes up too, and the button then goes away
"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace  # noqa: E402


APP = "http://127.0.0.1:8799/iguanaxterm"
PROFILES = {"alpine-box": ("ssh", "2222"), "ftps-box": ("ftp", "2121")}
results = []

# Record when each terminal socket is constructed and when its relay says the
# shell is up, so the pacing can be checked from timestamps.
WATCH_SOCKETS = """
  window.__dials = [];
  const Real = window.WebSocket;
  window.WebSocket = function (url, ...rest) {
    const ws = new Real(url, ...rest);
    if (String(url).includes('/ws/terminal/')) {
      const rec = {opened: performance.now(), connected: null};
      window.__dials.push(rec);
      ws.addEventListener('message', (e) => {
        try {
          if (rec.connected === null && JSON.parse(e.data).type === 'connected')
            rec.connected = performance.now();
        } catch (_) {}
      });
    }
    return ws;
  };
  window.WebSocket.prototype = Real.prototype;
  Object.assign(window.WebSocket, Real);
"""


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def expand_all(page):
    for branch in page.locator(".wapyt-tree-row[data-branch='true']").all():
        if "▸" in branch.inner_text():  # ▸ = collapsed
            branch.click()
            page.wait_for_timeout(150)


def leaf(page, name):
    expand_all(page)
    return page.locator(f".wapyt-tree-row[data-node-id^='sess_']:has-text('{name}')").first


def button_state(page):
    return page.evaluate("""() => {
      const b = document.querySelector('.ix-reconnect-all');
      return {hidden: b.hidden, disabled: b.disabled, text: b.innerText.trim()};
    }""")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.add_init_script(WATCH_SOCKETS)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in page.url:
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass123")
        page.click('input[type="submit"]')
    page.wait_for_selector(".ix-toolbar", timeout=180000)
    reset_workspace(page)

    for name, (kind, port) in PROFILES.items():
        if leaf(page, name).count():
            continue
        page.click('.ix-toolbar-btn[data-action="new"]')
        form = page.locator(".wapyt-modal-body:visible .wapyt-form-body")
        form.wait_for(timeout=30000)
        form.locator('[name="session_type"]').select_option(kind)
        form.locator('[name="name"]').fill(name)
        form.locator('[name="host"]').fill("127.0.0.1")
        form.locator('[name="port"]').fill(port)
        form.locator('[name="username"]').fill("testuser")
        form.locator('[name="password"]').fill("testpass")
        page.locator(".wapyt-modal-body:visible .wapyt-form-button-primary").click()
        page.wait_for_timeout(1500)

    check(button_state(page)["hidden"], "no Reconnect all on a fresh, empty workspace")

    # Three SSH panes and one FTP pane. Three on one host is the MaxStartups case.
    for _ in range(3):
        leaf(page, "alpine-box").dblclick()
        page.wait_for_timeout(2500)
    leaf(page, "ftps-box").dblclick()
    page.wait_for_selector(".ix-pane td[data-column-id='name']", timeout=60000)
    check(button_state(page)["hidden"], "hidden while every pane is live")
    # Tiled, so every restored pane (and its Reconnect button) is on screen;
    # in tabbed mode only the active tab's is.
    page.click('.ix-mode-btn[data-mode="tiled"]')
    page.wait_for_selector("#ix-grid-host .grid-stack-item", timeout=30000)
    page.wait_for_timeout(1500)  # the debounced layout save

    # ── restore ──────────────────────────────────────────────────────────────
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(".ix-reconnect-btn", timeout=180000)
    page.wait_for_timeout(500)
    state = button_state(page)
    check(not state["hidden"] and state["text"] == "Reconnect all (4)",
          "restore offers Reconnect all with the count", str(state))
    check(page.evaluate("window.__dials.length") == 0, "restore dials nothing")

    first = page.eval_on_selector(".ix-reconnect-btn", "e => e.dataset.reconnect")
    page.click(f'.ix-reconnect-btn[data-reconnect="{first}"]')
    page.wait_for_function("window.__dials.length === 1 && window.__dials[0].connected !== null",
                           timeout=30000)
    check(button_state(page)["text"] == "Reconnect all (3)",
          "a hand reconnect comes off the count", button_state(page)["text"])

    # ── reconnect the rest ───────────────────────────────────────────────────
    page.click(".ix-reconnect-all")
    page.wait_for_timeout(200)
    during = button_state(page)
    check(during["disabled"] and during["text"].startswith("Reconnecting"),
          "button shows progress and cannot be pressed twice", str(during))

    page.wait_for_function(
        "() => document.querySelector('.ix-reconnect-all').hidden", timeout=90000
    )
    dials = page.evaluate("window.__dials")[1:]  # the hand-dialled one is first
    check(len(dials) == 2 and all(d["connected"] for d in dials),
          "both remaining terminals dialled and connected", f"{len(dials)} dial(s)")
    ordered = all(
        later["opened"] >= earlier["connected"] for earlier, later in zip(dials, dials[1:])
    )
    check(ordered, "each dial waited for the previous one to connect",
          "; ".join(f"open {d['opened']:.0f} -> up {d['connected']:.0f}" for d in dials))

    ftp_rows = page.evaluate("""() => [...document.querySelectorAll('.ix-pane')]
        .filter(p => !p.querySelector('.xterm'))
        .map(p => p.querySelectorAll("td[data-column-id='name']").length)""")
    check(ftp_rows and ftp_rows[0] > 0, "the FTP pane came up with its listing", str(ftp_rows))
    check(not page.locator(".ix-reconnect-btn").count(), "no Reconnect placeholders left")

    page.screenshot(path="/tmp/claude-1000/reconnect_all.png")
    reset_workspace(page)
    check(not errors, "no console errors", "; ".join(errors[:3]))
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
