"""
Copy and paste in the terminal, against the real clipboard.

Needs the SSH target on :2222 and the app on :8799 (see README.md). The
browser context is granted clipboard read/write so the test can seed and read
the system clipboard the way a person's desktop would.

What it pins:
  - Ctrl+V and Ctrl+Shift+V paste (Ctrl+V used to send ^V to the remote)
  - Ctrl+C with a selection copies it, says so, and does NOT interrupt
  - Ctrl+C with no selection still interrupts a running command
  - Ctrl+Shift+C copies
  - the right-click menu: Copy is disabled with nothing selected, Copy and
    Paste both work, and a browser that refuses clipboard access is explained
"""
import re

from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import new_pane, reset_workspace, session_leaf  # noqa: E402
from transfer_smoke import login  # noqa: E402

results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def rows(page, pane):
    return page.evaluate("(p) => document.querySelector(`#pane-term-${p} .xterm-rows`).innerText", pane)


def clipboard(page):
    return page.evaluate("() => navigator.clipboard.readText()")


def seed(page, text):
    page.evaluate("t => navigator.clipboard.writeText(t)", text)


def select_line(page, pane, marker):
    """Triple-click the last row containing ``marker``: xterm selects the line."""
    row = page.locator(f"#pane-term-{pane} .xterm-rows > div:has-text('{marker}')").last
    row.click(click_count=3)
    page.wait_for_timeout(250)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(viewport={"width": 1400, "height": 900},
                                  permissions=["clipboard-read", "clipboard-write"])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    login(page)
    reset_workspace(page)
    pane = new_pane(page, session_leaf(page))
    page.wait_for_timeout(2500)
    screen = f"#pane-term-{pane} .xterm-screen"
    page.click(screen)

    # ── paste ────────────────────────────────────────────────────────────────
    for keys, marker in (("Control+v", "PASTE_CTRL_V"), ("Control+Shift+v", "PASTE_CTRL_SHIFT_V")):
        seed(page, f"echo {marker}")
        page.keyboard.press(keys)
        page.keyboard.press("Enter")
        page.wait_for_timeout(900)
        check(rows(page, pane).count(marker) >= 2, f"{keys} pastes", f"seen {rows(page, pane).count(marker)}x")

    # ── copy with Ctrl+C ────────────────────────────────────────────────────
    page.keyboard.type("echo COPY_CTRL_C_ABC\n")
    page.wait_for_timeout(800)
    seed(page, "nothing yet")
    select_line(page, pane, "COPY_CTRL_C_ABC")
    before = rows(page, pane).count("^C")
    page.keyboard.press("Control+c")
    page.wait_for_timeout(500)
    copied = clipboard(page)
    check("COPY_CTRL_C_ABC" in copied, "Ctrl+C with a selection copies it", repr(copied[:60]))
    check(rows(page, pane).count("^C") == before, "and sends no interrupt to the remote")
    check(re.search(r"Copied \d+ characters", page.inner_text("#ix-toast") or ""),
          "and says so", repr(page.inner_text("#ix-toast")))

    # ── Ctrl+C without a selection is still the interrupt ───────────────────
    page.click(screen)
    page.keyboard.type("sleep 30; echo SLEEP_NOT_INTERRUPTED\n")
    page.wait_for_timeout(800)
    page.keyboard.press("Control+c")
    page.keyboard.type("echo AFTER_INTERRUPT\n")
    page.wait_for_timeout(1200)
    text = rows(page, pane)
    check("AFTER_INTERRUPT" in text.split("sleep 30")[-1] and "SLEEP_NOT_INTERRUPTED" not in
          text.split("sleep 30")[-1].replace("echo SLEEP_NOT_INTERRUPTED", ""),
          "Ctrl+C with nothing selected interrupts, as before")

    # ── Ctrl+Shift+C ────────────────────────────────────────────────────────
    page.keyboard.type("echo COPY_SHIFT_DEF\n")
    page.wait_for_timeout(800)
    select_line(page, pane, "COPY_SHIFT_DEF")
    page.keyboard.press("Control+Shift+c")
    page.wait_for_timeout(400)
    check("COPY_SHIFT_DEF" in clipboard(page), "Ctrl+Shift+C copies")

    # ── right-click menu ────────────────────────────────────────────────────
    page.click(screen)  # clears the selection
    page.click(screen, button="right")
    page.wait_for_selector(".wapyt-terminal-menu", timeout=5000)
    labels = page.eval_on_selector_all(".wapyt-terminal-menu-item",
                                       "els => els.map(e => [e.innerText.split('\\n')[0], e.disabled])")
    check(labels == [["Copy", True], ["Paste", False], ["Select all", False]],
          "menu: Copy is disabled with nothing selected", str(labels))
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    open_menus = page.locator(".wapyt-terminal-menu").count()
    check(open_menus == 0, "Escape closes the menu", f"{open_menus} still open")

    page.keyboard.type("echo MENU_COPY_GHI\n")
    page.wait_for_timeout(800)
    select_line(page, pane, "MENU_COPY_GHI")
    row = page.locator(f"#pane-term-{pane} .xterm-rows > div:has-text('MENU_COPY_GHI')").last
    row.click(button="right")
    page.locator(".wapyt-terminal-menu-item[data-action='copy']").dispatch_event("mousedown")
    page.wait_for_timeout(400)
    check("MENU_COPY_GHI" in clipboard(page), "menu Copy copies the selection")

    seed(page, "echo MENU_PASTE_JKL")
    page.click(screen, button="right")
    page.locator(".wapyt-terminal-menu-item[data-action='paste']").dispatch_event("mousedown")
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")
    page.wait_for_timeout(900)
    check(rows(page, pane).count("MENU_PASTE_JKL") >= 2, "menu Paste pastes")
    check(not errors, "no console errors", "; ".join(errors[:3]))
    reset_workspace(page)

    # ── a browser that will not hand over the clipboard ─────────────────────
    strict = browser.new_context(viewport={"width": 1400, "height": 900})
    page = strict.new_page()
    login(page)
    pane = new_pane(page, session_leaf(page))
    page.wait_for_timeout(2000)
    page.click(f"#pane-term-{pane} .xterm-screen", button="right")
    page.locator(".wapyt-terminal-menu-item[data-action='paste']").dispatch_event("mousedown")
    page.wait_for_timeout(800)
    toast = page.inner_text("#ix-toast")
    check("Use Ctrl+V instead" in toast, "a refused clipboard read is explained", repr(toast))
    reset_workspace(page)
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
