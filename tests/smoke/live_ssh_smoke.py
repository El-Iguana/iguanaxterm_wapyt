"""
End-to-end smoke test against a live SSH container.

Drives the real UI: opens a terminal from the session tree, types into it,
reads the rendered xterm buffer back, then exercises the SFTP browser.
"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace  # noqa: E402


BASE = "http://127.0.0.1:8799"
APP = f"{BASE}/iguanaxterm"

errors: list[str] = []
results: list[tuple[bool, str]] = []


def check(condition, label, detail=""):
    results.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'}: {label}" + (f"  {detail}" if detail else ""))


def terminal_text(page):
    """The visible xterm buffer, as text."""
    return page.evaluate("""() => {
        const rows = document.querySelectorAll('.xterm-rows > div');
        return Array.from(rows).map(r => r.textContent).join('\\n');
    }""")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(APP, wait_until="domcontentloaded", timeout=30000)
        if "/login" in page.url:
            page.fill('input[name="email"]', "admin")
            page.fill('input[name="password"]', "testpass123")
            page.click('input[type="submit"]')
            page.wait_for_load_state("domcontentloaded")

        page.wait_for_selector(".ix-toolbar", timeout=180000)
        reset_workspace(page)
        page.wait_for_selector(".wapyt-tree-row", timeout=30000)

        # ── Terminal ─────────────────────────────────────────────────────────
        page.click(".wapyt-tree-row[data-branch='true']")
        page.wait_for_timeout(300)
        leaf = page.locator(".wapyt-tree-row").nth(1)
        check(leaf.inner_text().strip().endswith("alpine-box"), "session leaf present",
              repr(leaf.inner_text().strip()))

        leaf.dblclick()
        check(page.wait_for_selector(".wapyt-terminal", timeout=20000) is not None,
              "terminal tab opens")

        # xterm loads from /xterm and the socket connects; the shell prompt is
        # the first thing the remote sends.
        page.wait_for_selector(".xterm-rows", timeout=30000)
        check(True, "xterm canvas mounted")

        got_prompt = False
        for _ in range(40):
            if "~" in terminal_text(page) or "$" in terminal_text(page):
                got_prompt = True
                break
            page.wait_for_timeout(500)
        check(got_prompt, "remote shell prompt received")

        page.click(".wapyt-terminal-screen")
        page.keyboard.type("echo SMOKE_$((6*7))")
        page.keyboard.press("Enter")

        saw_output = False
        for _ in range(30):
            if "SMOKE_42" in terminal_text(page):
                saw_output = True
                break
            page.wait_for_timeout(500)
        check(saw_output, "typed command executed remotely (SMOKE_42)")

        # The PTY should have been sized to the pane, not left at the 80x24
        # the relay opens with.
        pane = page.evaluate(
            "() => { const e=document.querySelector('.wapyt-terminal');"
            "  return e ? Math.round(e.getBoundingClientRect().width) : 0; }")
        check(pane > 900, "terminal pane fills the workspace", f"{pane}px")

        # alpine ships no tput; stty is a busybox built-in.
        page.keyboard.type("stty size")
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
        import re
        m = re.findall(r"^\s*(\d+)\s+(\d+)\s*$", terminal_text(page), re.M)
        rows_cols = m[-1] if m else None
        check(rows_cols and int(rows_cols[1]) > 100,
              "remote PTY matches the pane width",
              f"stty size -> {rows_cols}")

        # Ctrl+F opens the widget's own search, not the browser's.
        page.keyboard.press("Control+f")
        page.wait_for_timeout(400)
        check(page.locator(".wapyt-terminal-search").is_visible(), "Ctrl+F opens terminal search")
        page.keyboard.press("Escape")

        page.screenshot(path="/tmp/ix_smoke_term.png")

        # ── SFTP ─────────────────────────────────────────────────────────────
        page.click('.ix-toolbar-btn[data-action="sftp"]')
        check(page.wait_for_selector(".wapyt-datatable-table tbody tr", timeout=30000) is not None,
              "SFTP tab lists the remote home directory")

        names = page.locator(".wapyt-datatable-table tbody tr td:nth-child(3)").all_inner_texts()
        check("readme.txt" in names, "listing contains readme.txt", str(names))
        check(names.index("docs") < names.index("readme.txt"),
              "directories sort above files")

        crumbs = page.locator(".ix-crumb").all_inner_texts()
        check("testuser" in " ".join(crumbs), "breadcrumbs show the home path", str(crumbs))

        # Navigate into a directory by double-click.
        rows = page.locator(".wapyt-datatable-table tbody tr")
        for i in range(rows.count()):
            if rows.nth(i).inner_text().find("logs") >= 0:
                rows.nth(i).dblclick()
                break
        page.wait_for_timeout(2500)
        names = page.locator(".wapyt-datatable-table tbody tr td:nth-child(3)").all_inner_texts()
        check("app.log" in names, "navigating into a directory works", str(names))

        # Filter, then clear.
        page.fill(".wapyt-datatable-filter", "zzz")
        page.wait_for_timeout(400)
        check(page.locator(".wapyt-datatable-status").is_visible(), "filter shows empty state")
        page.fill(".wapyt-datatable-filter", "")
        page.wait_for_timeout(400)

        # Right-click menu on a file.
        page.locator(".wapyt-datatable-table tbody tr").first.click(button="right")
        page.wait_for_timeout(400)
        check(page.locator(".wapyt-datatable-menu-item").count() >= 3,
              "SFTP context menu opens",
              f"{page.locator('.wapyt-datatable-menu-item').count()} actions")
        page.keyboard.press("Escape")

        page.screenshot(path="/tmp/ix_smoke_sftp.png")

        # ── Closing a tab must end the session ───────────────────────────────
        tabs_before = page.locator(".wapyt-tab").count()
        page.locator(".wapyt-tab-close").first.click()
        page.wait_for_timeout(1000)
        check(page.locator(".wapyt-tab").count() == tabs_before - 1, "closing a tab removes it")

        browser.close()

    print(f"\nconsole/page errors: {len(errors)}")
    for e in errors[:10]:
        print("  ", e[:250])
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
