"""
Windows-safe folder downloads, driven through the real UI.

The browser runs on Linux here, so the page is told it is on Windows
(`navigator.platform` and `userAgentData`), which is all the app consults.
The in-memory folder picker from transfer_smoke.py records every local path
written, so the test asserts on the names the browser would actually create.

Needs the SSH target on :2222 and the app on :8799 (see README.md).

What it pins:
  - names Windows cannot store are cleaned: `a:b.txt`, `CON.log`, `trail.`
  - names that collide once cleaned, or differ only in case, are numbered
    rather than overwriting -- files *and* directories (`Sub` vs `sub`)
  - every byte still arrives, under the name it was mapped to
  - the summary toast says how many names changed
  - the single-file Save dialog is offered the cleaned name
  - on Linux the same download keeps every name exactly as on the server
"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace, session_leaf  # noqa: E402
from transfer_smoke import PICKER_STUB, login, row_for  # noqa: E402

results = []

AS_WINDOWS = """
Object.defineProperty(Navigator.prototype, 'platform', {get: () => 'Win32'});
Object.defineProperty(Navigator.prototype, 'userAgentData',
                      {get: () => ({platform: 'Windows', mobile: false, brands: []})});
"""

# One byte of distinct content per file, so a file that landed under the wrong
# name -- or overwrote another -- shows up as missing or wrong content.
MAKE_TREE = (
    "rm -rf ~/winnames && mkdir -p ~/winnames/Sub ~/winnames/sub && cd ~/winnames"
    " && printf A > 'a:b.txt' && printf B > 'a?b.txt'"
    " && printf C > Report.txt && printf D > report.txt"
    " && printf E > CON.log && printf F > 'trail.'"
    " && printf G > Sub/x.txt && printf H > sub/x.txt && cd && echo TREE_READY\n"
)


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def download_folder(page, folder):
    """Select one directory row and download it into the stub folder."""
    page.evaluate("() => { window.__saved = {}; }")
    row_for(page, folder).click()
    page.click('.ix-sftp-btn[data-sftp="download"]')
    page.wait_for_function(
        "() => Object.keys(window.__saved).length >= 8", timeout=60000
    )
    page.wait_for_timeout(800)
    return page.evaluate("""() => Object.fromEntries(Object.entries(window.__saved)
        .map(([k, v]) => [k, new TextDecoder().decode(v)]))""")


def open_files(page):
    session_leaf(page).click()
    page.click('.ix-toolbar-btn[data-action="sftp"]')
    page.wait_for_selector(".wapyt-datatable-table tbody tr", timeout=30000)
    page.click('.ix-sftp-btn[data-sftp="refresh"]')
    page.wait_for_function(
        """() => [...document.querySelectorAll('.wapyt-datatable-table tbody tr')]
                 .some(r => r.innerText.includes('winnames'))""",
        timeout=30000,
    )


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

    # ── Windows ──────────────────────────────────────────────────────────────
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.add_init_script(PICKER_STUB)
    page.add_init_script(AS_WINDOWS)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    login(page)
    reset_workspace(page)
    check(page.evaluate("navigator.userAgentData.platform") == "Windows",
          "page believes it is on Windows")

    # Make the awkward names on the server, through a real terminal.
    session_leaf(page).dblclick()
    page.wait_for_selector(".ix-pane .xterm-rows", timeout=60000)
    pane = page.eval_on_selector(".ix-pane", "e => e.dataset.pane")
    page.wait_for_timeout(2500)
    page.click(f"#pane-term-{pane} .xterm-screen")
    page.keyboard.type(MAKE_TREE)
    page.wait_for_function(
        "(p) => ((document.querySelector(`#pane-term-${p} .xterm-rows`) || {}).innerText || '')"
        ".split('TREE_READY').length > 2", arg=pane, timeout=30000,
    )

    page.click(f'.ix-pane-tab[data-pane="{pane}"][data-pane-tab="files"]')
    page.wait_for_selector(f"#pane-files-{pane} td[data-column-id='name']", timeout=30000)
    page.click(f'#pane-files-{pane} .ix-sftp-btn[data-sftp="refresh"]')
    page.wait_for_timeout(1500)

    saved = download_folder(page, "winnames")
    names = sorted(saved)
    print("   written:", names)
    base = "/dest/winnames/"

    check(len(saved) == 8, "all eight files written", f"{len(saved)} file(s)")
    check(not any(c in k[len("/dest/"):] for k in saved for c in ':?*<>|"\\'),
          "no Windows-forbidden character in any local name")
    check(sorted(saved[k] for k in saved) == list("ABCDEFGH"),
          "every file's bytes arrived exactly once -- nothing overwritten")
    check({saved.get(base + "a_b.txt"), saved.get(base + "a_b (2).txt")} == {"A", "B"},
          "a:b.txt and a?b.txt both kept, as a_b.txt and a_b (2).txt")
    check({saved.get(base + "Report.txt"), saved.get(base + "report (2).txt")} == {"C", "D"}
          or {saved.get(base + "report.txt"), saved.get(base + "Report (2).txt")} == {"C", "D"},
          "Report.txt and report.txt both kept, one numbered")
    check(saved.get(base + "CON_.log") == "E", "CON.log becomes CON_.log")
    check(saved.get(base + "trail") == "F", "a trailing dot is dropped")
    dirs = sorted({k[len(base):].split("/")[0] for k in saved if k[len(base):].count("/")})
    check(len(dirs) == 2 and len({d.casefold() for d in dirs}) == 2,
          "Sub and sub stay two directories", str(dirs))

    toast = page.inner_text("#ix-toast")
    check("Downloaded 8 of 8" in toast and "Renamed 6" in toast,
          "one summary toast, with the rename count", repr(toast))

    # The single-file path: the Save dialog is offered a name Windows accepts.
    row_for(page, "winnames").dblclick()
    page.wait_for_function(
        """() => [...document.querySelectorAll('.wapyt-datatable-table tbody tr')]
                 .some(r => r.innerText.includes('a:b.txt'))""", timeout=30000)
    row_for(page, "a:b.txt").click()
    page.click('.ix-sftp-btn[data-sftp="download"]')
    page.wait_for_timeout(2000)
    check(page.evaluate("window.__lastSuggested") == "a_b.txt",
          "Save dialog is offered a_b.txt", repr(page.evaluate("window.__lastSuggested")))
    check(not errors, "no console errors (Windows)", "; ".join(errors[:3]))
    reset_workspace(page)
    page.close()

    # ── Linux: the same download is left alone ───────────────────────────────
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.add_init_script(PICKER_STUB)
    login(page)
    reset_workspace(page)
    open_files(page)
    saved = download_folder(page, "winnames")
    expected = {base + n for n in ("a:b.txt", "a?b.txt", "Report.txt", "report.txt",
                                   "CON.log", "trail.", "Sub/x.txt", "sub/x.txt")}
    check(set(saved) == expected, "on Linux every name is kept exactly",
          str(sorted(set(saved) ^ expected)))
    check("Renamed" not in page.inner_text("#ix-toast"), "and nothing claims a rename")
    reset_workspace(page)
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
