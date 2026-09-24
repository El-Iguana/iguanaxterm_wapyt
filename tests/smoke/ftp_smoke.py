"""
Files-only connection types: an FTP profile opens a pane that is a file browser
and nothing else.

Needs ftp_target.py on 127.0.0.1:2121 (TLS required, like the NAS that
prompted this) and the app on :8799 — see README.md.

What it pins:
  - the editor offers FTP and fills in port 21, and disables the key field
  - connecting opens a pane with no Terminal button, straight onto Files
  - no terminal WebSocket is ever constructed for it
  - the listing arrives over FTPS and a download streams through /files
  - a restored FTP pane shows Reconnect, and reconnecting lands back in the
    same directory
"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace  # noqa: E402


APP = "http://127.0.0.1:8799/iguanaxterm"
NAME = "ftps-box"
results = []

COUNT_SOCKETS = """
  window.__terminalSockets = 0;
  const Real = window.WebSocket;
  window.WebSocket = function (url, ...rest) {
    if (String(url).includes('/ws/terminal/')) window.__terminalSockets += 1;
    return new Real(url, ...rest);
  };
  window.WebSocket.prototype = Real.prototype;
  Object.assign(window.WebSocket, Real);
"""


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def login(page):
    page.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in page.url:
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass123")
        page.click('input[type="submit"]')
    page.wait_for_selector(".ix-toolbar", timeout=180000)


def rows(page, pane):
    return page.eval_on_selector_all(
        f"#pane-files-{pane} td[data-column-id='name']", "els => els.map(e => e.innerText.trim())"
    )


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.add_init_script(COUNT_SOCKETS)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    login(page)
    reset_workspace(page)

    def expand_lab():
        lab = page.locator(".wapyt-tree-row[data-branch='true']:has-text('Lab')")
        if lab.count() and not page.locator(f".wapyt-tree-row:has-text('{NAME}')").is_visible():
            lab.click()
            page.wait_for_timeout(300)

    # ── the editor ───────────────────────────────────────────────────────────
    expand_lab()
    if page.locator(f".wapyt-tree-row:has-text('{NAME}')").count() == 0:
        page.click('.ix-toolbar-btn[data-action="new"]')
        page.wait_for_selector(".wapyt-modal-body .wapyt-form-body", timeout=30000)
        options = page.eval_on_selector_all(
            '[name="session_type"] option', "els => els.map(e => e.value)"
        )
        check(options == ["ssh", "telnet", "sftp", "ftp"], "editor offers all four types", str(options))
        page.select_option('[name="session_type"]', "ftp")
        page.wait_for_timeout(200)
        check(page.input_value('[name="port"]') == "21", "choosing FTP fills in port 21",
              page.input_value('[name="port"]'))
        check(page.is_disabled('[name="private_key"]'), "FTP disables the private key")
        check(not page.is_disabled('[name="password"]'), "FTP keeps the password")
        page.fill('[name="name"]', NAME)
        page.fill('[name="folder"]', "Lab")
        page.fill('[name="host"]', "127.0.0.1")
        page.fill('[name="port"]', "2121")
        page.fill('[name="username"]', "testuser")
        page.fill('[name="password"]', "testpass")
        page.click(".wapyt-form-button-primary")
        page.wait_for_selector(".wapyt-tree-row[data-branch='true']:has-text('Lab')", timeout=30000)
        expand_lab()

    leaf = page.locator(f".wapyt-tree-row:has-text('{NAME}')")
    icon = leaf.locator(".mdi").last.get_attribute("class") or ""
    check("mdi-folder-network-outline" in icon, "tree shows the FTP icon", icon)

    # ── connecting ───────────────────────────────────────────────────────────
    leaf.dblclick()
    page.wait_for_selector(".ix-pane", timeout=30000)
    pane = page.eval_on_selector(".ix-pane", "e => e.dataset.pane")
    page.wait_for_selector(f"#pane-files-{pane} td[data-column-id='name']", timeout=60000)

    check(not page.is_visible(f'.ix-pane-tab[data-pane="{pane}"][data-pane-tab="terminal"]'),
          "no Terminal button on an FTP pane")
    check(page.is_visible(f"#pane-files-{pane}"), "the pane opens on Files")
    check("FTP" in page.inner_text(f'.ix-pane[data-pane="{pane}"] .ix-pane-host'),
          "pane header names the protocol",
          page.inner_text(f'.ix-pane[data-pane="{pane}"] .ix-pane-host'))
    # "share" is ours; other smokes (large_upload) leave files on the same
    # target, so demand our folder, not an otherwise empty home.
    check("share" in rows(page, pane), "home directory listed over FTPS", str(rows(page, pane)))
    check(page.evaluate("window.__terminalSockets") == 0, "no terminal socket was opened")

    page.locator(f"#pane-files-{pane} td[data-column-id='name']:has-text('share')").dblclick()
    page.wait_for_function(
        """(pane) => [...document.querySelectorAll(`#pane-files-${pane} td[data-column-id='name']`)]
                 .some(e => e.innerText.includes('hello.txt'))""",
        arg=pane, timeout=30000,
    )
    check(sorted(rows(page, pane)) == ["docs", "hello.txt"], "navigated into a directory",
          str(rows(page, pane)))

    body = page.evaluate(
        """async () => {
             const ids = [...document.querySelectorAll('.wapyt-tree-row[data-node-id^="sess_"]')]
                           .filter(e => e.innerText.includes('ftps-box')).map(e => e.dataset.nodeId);
             for (const id of ids) {
               const r = await fetch(`/files/${id.slice(5)}/download?path=${encodeURIComponent('/share/hello.txt')}`);
               if (r.ok) return await r.text();
             }
             return null;
           }"""
    )
    check(body == "hello over ftps\n", "download streams through /files over FTPS", repr(body))

    # ── restore ──────────────────────────────────────────────────────────────
    page.wait_for_timeout(1200)  # let the debounced layout save land
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(".ix-reconnect-btn", timeout=180000)
    check(page.evaluate("window.__terminalSockets") == 0, "restore dials nothing")
    pane = page.eval_on_selector(".ix-pane", "e => e.dataset.pane")
    page.click(f'.ix-reconnect-btn[data-reconnect="{pane}"]')
    page.wait_for_function(
        """(pane) => [...document.querySelectorAll(`#pane-files-${pane} td[data-column-id='name']`)]
                 .some(e => e.innerText.includes('hello.txt'))""",
        arg=pane, timeout=60000,
    )
    check(True, "reconnect lands back in /share")
    check(not page.is_visible(f"#pane-term-{pane}"), "the placeholder panel is gone after reconnect")
    check(page.evaluate("window.__terminalSockets") == 0, "still no terminal socket")

    page.screenshot(path="/tmp/claude-1000/ftp_smoke.png")
    reset_workspace(page)
    check(not errors, "no console errors", "; ".join(errors[:3]))
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
