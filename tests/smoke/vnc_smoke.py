"""
Remote desktops: noVNC in a pane, direct and through an SSH tunnel.

Needs the VNC target and the app on :8799 (see README.md):

    podman build -t iguanaxterm-vnctest -f tests/smoke/Containerfile.vnctarget tests/smoke
    podman run -d --name ix-vnctest -p 127.0.0.1:5900:5900 -p 127.0.0.1:2223:22 iguanaxterm-vnctest

The target's screen is two known colours, so the checks read real pixels off
noVNC's canvas rather than trusting that "connected" means "showing".

What it pins:
  - a VNC profile opens a Desktop pane (no Terminal, no Files) and shows the
    remote screen: the root colour and the xterm's colour where they belong
  - input reaches the remote: clicking into the xterm and typing an
    `xsetroot` command changes the root colour, and the canvas follows
  - the VNC password never appears in the page or in anything it sends
  - a desktop that listens only on its own localhost is reachable through an
    SSH session, and not directly
  - a wrong password is refused with the server's own reason, and the pane
    offers Reconnect
"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace, session_leaf  # noqa: E402
from transfer_smoke import login  # noqa: E402

PASSWORD = "vncpass"
ROOT, XTERM, CHANGED = (0x2a, 0x6f, 0x97), (0xf2, 0xc1, 0x4e), (0x00, 0xaa, 0x00)
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def close(a, b, slack=12):
    return a is not None and all(abs(x - y) <= slack for x, y in zip(a, b))


def new_profile(page, name, kind, host, port, password="", via=None, username=""):
    page.click('.ix-toolbar-btn[data-action="new"]')
    form = page.locator(".wapyt-modal-body:visible .wapyt-form-body")
    form.wait_for(timeout=30000)
    form.locator('[name="session_type"]').select_option(kind)
    form.locator('[name="name"]').fill(name)
    form.locator('[name="host"]').fill(host)
    form.locator('[name="port"]').fill(str(port))
    if username:
        form.locator('[name="username"]').fill(username)
    if password:
        form.locator('[name="password"]').fill(password)
    if via:
        form.locator('[name="via_session_id"]').select_option(label=f"Through SSH session “{via}”")
    page.locator(".wapyt-modal-body:visible .wapyt-form-button-primary").click()
    page.wait_for_timeout(1500)


def pixel(page, pane, x, y):
    """One framebuffer pixel, read off noVNC's canvas (unscaled coordinates)."""
    return page.evaluate(
        """([pane, x, y]) => {
             const c = document.querySelector(`#pane-term-${pane} .ix-vnc canvas`);
             if (!c || !c.width) return null;
             const d = c.getContext('2d').getImageData(x, y, 1, 1).data;
             return [d[0], d[1], d[2]];
           }""", [pane, x, y])


def wait_pixel(page, pane, x, y, want, timeout=20000):
    deadline = page.evaluate("Date.now()") + timeout
    got = None
    while page.evaluate("Date.now()") < deadline:
        got = pixel(page, pane, x, y)
        if close(got, want):
            return got
        page.wait_for_timeout(250)
    return got


def open_desktop(page, name):
    before = page.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
    session_leaf(page, name).dblclick()
    page.wait_for_function("n => document.querySelectorAll('.ix-pane').length > n", arg=len(before))
    after = page.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
    return [p for p in after if p not in before][0]


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    errors, sent = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error"
            and "WebSocket" not in m.text else None)
    page.on("websocket", lambda ws: ws.on("framesent", lambda f: sent.append(
        f if isinstance(f, (bytes, str)) else f.get("payload", ""))))
    page.on("request", lambda r: sent.append(r.post_data or ""))
    login(page)
    reset_workspace(page)

    if not session_leaf(page, "desk-direct").count():
        new_profile(page, "vnc-ssh", "ssh", "127.0.0.1", 2223, "testpass", username="testuser")
        new_profile(page, "desk-direct", "vnc", "127.0.0.1", 5900, PASSWORD)
        new_profile(page, "desk-tunnel", "vnc", "localhost", 5901, via="vnc-ssh")
        new_profile(page, "desk-nowhere", "vnc", "127.0.0.1", 5901)       # not published
        new_profile(page, "desk-badpw", "vnc", "127.0.0.1", 5900, "wrongpw")
    sent.clear()  # the profile passwords went out in the saves above, as they must

    # A rebuilt target has new SSH host keys, and the tunnel -- correctly --
    # refuses a changed key. Forget the pin, as a person would after a rebuild.
    session_leaf(page, "vnc-ssh").click(button="right")
    page.locator(".wapyt-tree-menu :text('Forget host key'), [role=menu] :text('Forget host key')").first.click()
    page.wait_for_timeout(800)

    # ── direct, with a password ──────────────────────────────────────────────
    pane = open_desktop(page, "desk-direct")
    tabs = page.evaluate("""(p) => [...document.querySelectorAll(`.ix-pane-tab[data-pane="${p}"]`)]
        .filter(b => !b.hidden).map(b => b.innerText.trim())""", pane)
    check(tabs == ["Desktop"], "a VNC pane has a Desktop tab and nothing else", str(tabs))
    got = wait_pixel(page, pane, 5, 5, ROOT)
    check(close(got, ROOT), "the remote root window is on the canvas", str(got))
    got = pixel(page, pane, 200, 150)
    check(close(got, XTERM), "and the xterm is where the remote put it", str(got))
    host_label = page.inner_text(f'.ix-pane[data-pane="{pane}"] .ix-pane-host')
    check("127.0.0.1" in host_label, "header names the desktop", host_label)

    # Input: click into the xterm (no window manager, so focus follows the
    # pointer) and type a command that repaints the root window.
    box = page.locator(f"#pane-term-{pane} .ix-vnc canvas").bounding_box()
    scale = box["width"] / 1024
    page.mouse.click(box["x"] + 200 * scale, box["y"] + 150 * scale)
    page.keyboard.type("xsetroot -solid '#00aa00'\n", delay=15)
    got = wait_pixel(page, pane, 5, 5, CHANGED)
    check(close(got, CHANGED), "typing into the remote xterm repaints its root window", str(got))

    page.click(f'[data-pane-cad="{pane}"]')   # must not throw; nothing to observe
    check(not any(PASSWORD in (s.decode("latin-1") if isinstance(s, bytes) else str(s))
                  for s in sent)
          and PASSWORD not in page.content(),
          "the VNC password is not in the page or anything it sent")

    # ── through SSH, to a desktop that only listens on its own localhost ────
    pane_t = open_desktop(page, "desk-tunnel")
    got = wait_pixel(page, pane_t, 5, 5, CHANGED)
    check(close(got, CHANGED), "the tunnelled desktop shows the same screen", str(got))
    check("via vnc-ssh" in page.inner_text(f'.ix-pane[data-pane="{pane_t}"] .ix-pane-host'),
          "header says which SSH session it rides")

    pane_n = open_desktop(page, "desk-nowhere")
    page.wait_for_selector(f"#pane-term-{pane_n} .ix-reconnect-btn", timeout=30000)
    check(True, "the same port directly is unreachable, and the pane offers Reconnect")

    # ── wrong password ───────────────────────────────────────────────────────
    page.wait_for_timeout(4500)   # let the previous toast expire
    pane_b = open_desktop(page, "desk-badpw")
    page.wait_for_selector(f"#pane-term-{pane_b} .ix-reconnect-btn", timeout=30000)
    toast = page.inner_text("#ix-toast")
    check("rejected the password" in toast, "a wrong password is refused, with the reason",
          repr(toast))

    # Put the root back for the next run.
    page.mouse.click(box["x"] + 200 * scale, box["y"] + 150 * scale)
    page.keyboard.type("xsetroot -solid '#2a6f97'\n", delay=15)
    page.screenshot(path="/tmp/claude-1000/vnc_smoke.png")
    reset_workspace(page)
    # noVNC logs every refused connection itself; the two provoked above are
    # expected. Anything else is not.
    unexpected = [e for e in errors if "Security negotiation failed" not in e]
    check(not unexpected, "no console errors beyond the refusals provoked",
          "; ".join(unexpected[:3]))
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
