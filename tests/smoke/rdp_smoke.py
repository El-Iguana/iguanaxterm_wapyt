"""
RDP desktops, end to end (#57-#62).

Needs (see README.md, "RDP smoke"):
  - the RDP target: podman run -d --name ix-rdptest
        -p 127.0.0.1:3389:3389 -p 127.0.0.1:2224:22 iguanaxterm-rdptest
  - guacd bound to loopback on the host network, e.g. on :4823
  - the app on :8799 with GANXTERM_GUACD pointing at that guacd, and a FRESH
    data dir (GANXTERM_DATA_DIR, also given to this script as IX_DATA_DIR so
    it can read the pinned certificate)

What it pins:
  - an RDP site is saved from the editor, connects, and draws the desktop
  - the certificate is pinned (rdp-sha256:) on first connect
  - typing reaches the session; Ctrl+Alt+Del reaches it (the target binds it
    to a marker file)
  - the remote desktop follows a resize of the pane
  - clipboard both ways: the desktop's copy lands here; Paste to desktop and
    Ctrl+V put this computer's text there
  - sound (#73): noise played in the session reaches the pane; Mute drops it,
    is remembered for the site, and unmuting plays again
  - performance (#74): the editor shows colour depth and effects only for
    Custom; Slow link and Best quality sites connect and draw; on a 2x screen
    "Use every pixel" gets a desktop with twice the pixels, and off (the
    default) a CSS-sized one
  - gateway (#75): the editor shows the gateway's own login only when "same
    login" is off, and refuses a gateway together with an SSH tunnel (no RD
    Gateway here: tests/test_rdp_gateway_live.py covers guacd's side)
  - drive (#76, when IX_DRIVE_DIR is set): a file uploaded in the pane's Files
    tab is on the desktop's drive; a file the desktop writes is listed and
    downloads; the desktop is still connected afterwards
  - printing (#77): xrdp can't redirect a printer, so guacd's own print
    instructions (file, blobs, end) are fed to the pane: it acks each step,
    the PDF downloads under its job name, and a site without printing, or a
    file that isn't a PDF, is refused
  - a refused login (NLA against xrdp) is explained
  - the tunnel: a site reachable only through the SSH target connects
  - a changed certificate is refused with the host-key message, and
    Forget host key lets it connect again
"""
import os
import sqlite3
import subprocess
import sys as _sys
import time

from playwright.sync_api import sync_playwright

_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import dismiss_password_nag, record_toasts, reset_workspace, session_leaf, toast_text  # noqa: E402

BASE = "http://127.0.0.1:8799"
APP = f"{BASE}/iguanaxterm/"
ADMIN_PASSWORD = os.environ.get("IX_ADMIN_PASS", "testpass123")
DATA_DIR = os.environ.get("IX_DATA_DIR", "")
TARGET = os.environ.get("IX_RDP_CONTAINER", "ix-rdptest")
# Where the app (guacd, really) reaches the target: 127.0.0.1 with host
# networking; the target's name on a compose-style network otherwise.
RDP_HOST = os.environ.get("IX_RDP_HOST", "127.0.0.1")
RDP_PORT = os.environ.get("IX_RDP_PORT", "3389")
SSH_HOST = os.environ.get("IX_RDP_SSH_HOST", "127.0.0.1")
SSH_PORT = os.environ.get("IX_RDP_SSH_PORT", "2224")
# The app's GANXTERM_DRIVE_DIR (#76); the drive checks run only with it. xrdp
# mounts a redirected drive with FUSE as ~/thinclient_drives/GUACFS, so the
# target needs --device /dev/fuse --cap-add SYS_ADMIN.
DRIVE_DIR = os.environ.get("IX_DRIVE_DIR", "")

results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + str(detail)[:300]) if detail else ''}")


def target(*command, user="testuser"):
    """Run a command in the RDP target, in the RDP session's X display."""
    shell = " ".join(command)
    proc = subprocess.run(
        ["podman", "exec", "-u", user, TARGET, "sh", "-c", f"DISPLAY=:10 {shell}"],
        capture_output=True, text=True, timeout=20,
    )
    return proc.stdout.strip()


def pinned(name):
    if not DATA_DIR:
        return ""
    with sqlite3.connect(os.path.join(DATA_DIR, "iguanaxterm.db")) as conn:
        row = conn.execute("SELECT host_key FROM sessions WHERE name = ?", (name,)).fetchone()
    return row[0] if row else ""


def site_id(name):
    with sqlite3.connect(os.path.join(DATA_DIR, "iguanaxterm.db")) as conn:
        return conn.execute("SELECT id FROM sessions WHERE name = ?", (name,)).fetchone()[0]


def visible_form(page):
    return page.locator(".wapyt-modal-body:visible").last


def add_site(page, name, host, port, session_type="rdp", security=None, via=None,
             username="testuser", password="testpass", performance=None, hidpi=None,
             drive=None, printing=None):
    page.click('.wapyt-toolbar-btn[data-id="new"]')
    page.wait_for_selector(".wapyt-modal-body:visible .wapyt-form-body", timeout=30000)
    form = visible_form(page)
    form.locator('[name="name"]').fill(name)
    form.locator('[name="folder"]').fill("Desktops")
    form.locator('[name="session_type"]').select_option(session_type)
    page.wait_for_timeout(200)
    form.locator('[name="host"]').fill(host)
    form.locator('[name="port"]').fill(str(port))
    form.locator('[name="username"]').fill(username)
    form.locator('[name="password"]').fill(password)
    if security:
        form.locator('[name="security"]').select_option(security)
    if via:
        form.locator('[name="via_session_id"]').select_option(label=f"Through SSH session “{via}”")
    if performance:
        form.locator('[name="performance"]').select_option(performance)
    if hidpi is not None:
        form.locator('[name="hidpi"]').set_checked(hidpi)
    if drive is not None:
        form.locator('[name="drive"]').set_checked(drive)
    if printing is not None:
        form.locator('[name="printing"]').set_checked(printing)
    form.locator(".wapyt-form-button-primary").click()
    page.wait_for_selector(".wapyt-modal-body:visible .wapyt-form-body", state="hidden", timeout=30000)
    page.wait_for_timeout(600)


def open_desktop(page, name):
    before = page.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
    session_leaf(page, name).dblclick()
    page.wait_for_function("n => document.querySelectorAll('.ix-pane').length > n", arg=len(before))
    after = page.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
    return [x for x in after if x not in before][0]


DRAWN = """
(pane) => {
  const root = document.querySelector(`#pane-term-${pane} .ix-rdp`);
  if (!root) return -1;
  let lit = 0;
  for (const canvas of root.querySelectorAll('canvas')) {
    if (!canvas.width || !canvas.height) continue;
    const ctx = canvas.getContext('2d');
    const data = ctx.getImageData(0, 0, Math.min(canvas.width, 400), Math.min(canvas.height, 300)).data;
    for (let i = 0; i < data.length; i += 4 * 97) if (data[i + 3] && (data[i] + data[i + 1] + data[i + 2]) > 30) lit++;
  }
  return lit;
}
"""


def wait_drawn(page, pane, timeout_s=30):
    end = time.time() + timeout_s
    lit = 0
    while time.time() < end:
        lit = page.evaluate(DRAWN, pane)
        if lit > 20:
            return lit
        page.wait_for_timeout(500)
    return lit


def close_pane(page, pane):
    """Tabbed mode: the pane's own close button is for tiles; close its tab."""
    page.locator(f'.wapyt-tab[data-tab-id="{pane}"] .wapyt-tab-close').click()
    page.wait_for_timeout(800)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(viewport={"width": 1400, "height": 900},
                                  permissions=["clipboard-read", "clipboard-write"])
    record_toasts(context)
    # Chromium's native open picker can't be driven; the <input> path can (#76).
    context.add_init_script("window.showOpenFilePicker = undefined;")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(APP, wait_until="domcontentloaded", timeout=30000)
    page.fill('input[name="email"]', "admin")
    page.fill('input[name="password"]', ADMIN_PASSWORD)
    page.click('input[type="submit"]')
    page.wait_for_selector(".wapyt-toolbar", timeout=180000)
    reset_workspace(page)

    # ── connect, draw, pin ─────────────────────────────────────────────────
    add_site(page, "win-box", RDP_HOST, RDP_PORT)
    target("rm -f /tmp/typed-ok /tmp/cad-pressed")
    pane = open_desktop(page, "win-box")
    lit = wait_drawn(page, pane)
    check(lit > 20, "the RDP desktop draws in the pane", f"{lit} lit samples")
    check(pinned("win-box").startswith("rdp-sha256:"), "the certificate is pinned on first connect",
          pinned("win-box"))

    # ── typing and Ctrl+Alt+Del ─────────────────────────────────────────────
    page.click(f"#pane-term-{pane} .ix-rdp")
    page.wait_for_timeout(500)
    # A previous run's Ctrl+V test leaves bash in quoted-insert, which takes
    # the next key literally: two Enters clear it and any half-typed line.
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    page.keyboard.type("touch /tmp/typed-ok\n", delay=30)
    page.wait_for_timeout(1500)
    check(target("test -f /tmp/typed-ok && echo yes") == "yes", "typed text reaches the session")
    page.click(f'[data-pane-cad="{pane}"]')
    page.wait_for_timeout(1500)
    check(target("test -f /tmp/cad-pressed && echo yes") == "yes", "Ctrl+Alt+Del reaches the session")

    # ── resize follows the pane ──────────────────────────────────────────────
    size = "p => { const r = document.querySelector(`#pane-term-${p} .ix-rdp`).ixRdp; return [r.display.getWidth(), r.display.getHeight()]; }"
    before = page.evaluate(size, pane)
    page.set_viewport_size({"width": 1100, "height": 760})
    page.wait_for_timeout(3000)
    after = page.evaluate(size, pane)
    check(before != after and after[0] < before[0], "the remote screen follows a resize", f"{before} -> {after}")
    page.set_viewport_size({"width": 1400, "height": 900})
    page.wait_for_timeout(1500)

    # ── clipboard both ways ──────────────────────────────────────────────────
    marker = f"from-desktop-{int(time.time())}"
    # xclip must keep owning the selection until xrdp asks for it: one left
    # in the background dies with `podman exec`. -quiet keeps it in the
    # foreground, serving, until we stop it.
    owner = subprocess.Popen(
        ["podman", "exec", "-u", "testuser", TARGET, "sh", "-c",
         f"printf %s {marker} | DISPLAY=:10 xclip -quiet -selection clipboard"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    got = ""
    for _ in range(20):
        page.wait_for_timeout(500)
        got = page.evaluate("() => navigator.clipboard.readText()")
        if got == marker:
            break
    owner.terminate()
    check(got == marker, "the desktop's copy lands on this computer's clipboard", got)

    here = f"from-here-{int(time.time())}"
    page.evaluate("t => navigator.clipboard.writeText(t)", here)
    page.click(f'[data-pane-clip-send="{pane}"]')
    page.wait_for_timeout(2000)
    remote = target("xclip -o -selection clipboard")
    check(remote == here, "Paste to desktop sets the desktop's clipboard", remote)

    again = f"ctrl-v-{int(time.time())}"
    page.evaluate("t => navigator.clipboard.writeText(t)", again)
    page.click(f"#pane-term-{pane} .ix-rdp")
    page.keyboard.press("Control+V")
    page.wait_for_timeout(2000)
    remote = target("xclip -o -selection clipboard")
    check(remote == again, "Ctrl+V sends this computer's clipboard first", remote)

    # ── sound (#73) ──────────────────────────────────────────────────────────
    audio = "p => ({...document.querySelector(`#pane-term-${p} .ix-rdp`).ixRdp.audio})"

    def play_noise(seconds=2):
        subprocess.run(["podman", "exec", "-u", "testuser", TARGET, "/play-noise.sh",
                        str(44100 * 4 * seconds)], capture_output=True, timeout=30)
        page.wait_for_timeout(1500)

    start = page.evaluate(audio, pane)
    play_noise()
    heard = page.evaluate(audio, pane)
    check(heard["streams"] >= 1 and heard["played"] > start["played"],
          "sound played in the session reaches the pane", f"{start} -> {heard}")
    mute = f'[data-pane-mute="{pane}"]'
    page.click(mute)
    page.wait_for_timeout(300)
    check(page.get_attribute(mute, "aria-pressed") == "true", "Mute shows as pressed")
    play_noise()
    muted = page.evaluate(audio, pane)
    check(muted["received"] > heard["received"] and muted["played"] == heard["played"],
          "while muted, sound arrives but isn't played", f"{heard} -> {muted}")
    check(page.evaluate("id => localStorage.getItem(`ix-rdp-muted-${id}`)", site_id("win-box")) == "1",
          "the mute is remembered for the site")
    page.click(mute)
    page.wait_for_timeout(300)
    play_noise()
    again_heard = page.evaluate(audio, pane)
    check(again_heard["played"] > muted["played"], "unmuting plays sound again",
          f"{muted} -> {again_heard}")
    close_pane(page, pane)

    # ── a refused login is explained ─────────────────────────────────────────
    add_site(page, "nla-box", RDP_HOST, RDP_PORT, security="nla")
    pane = open_desktop(page, "nla-box")
    page.wait_for_timeout(8000)
    toasts = toast_text(page)
    check("login was refused" in toasts, "NLA against xrdp: the refusal is explained", toasts[-200:])
    close_pane(page, pane)

    # ── through the SSH tunnel ───────────────────────────────────────────────
    add_site(page, "jump", SSH_HOST, SSH_PORT, session_type="ssh")
    add_site(page, "hidden-box", "127.0.0.1", "3390", via="jump")
    pane = open_desktop(page, "hidden-box")
    lit = wait_drawn(page, pane)
    check(lit > 20, "a desktop reachable only through SSH connects through the tunnel", f"{lit}")
    check(pinned("hidden-box").startswith("rdp-sha256:"), "its certificate is pinned through the tunnel")
    close_pane(page, pane)

    # ── a changed certificate is refused; Forget lets it connect ─────────────
    first_pin = pinned("win-box")
    subprocess.run(["podman", "exec", TARGET, "/rotate-cert.sh"], capture_output=True, timeout=30)
    time.sleep(2)
    pane = open_desktop(page, "win-box")
    page.wait_for_timeout(6000)
    toasts = toast_text(page)
    check("has changed" in toasts and wait_drawn(page, pane, 1) <= 20,
          "a changed certificate is refused", toasts[-200:])
    check(pinned("win-box") == first_pin, "the pin is not replaced")
    close_pane(page, pane)
    session_leaf(page, "win-box").click(button="right")
    page.wait_for_timeout(300)
    page.locator(".wapyt-tree-menu-item:visible", has_text="Forget host key").click()
    page.wait_for_timeout(1000)
    pane = open_desktop(page, "win-box")
    lit = wait_drawn(page, pane)
    check(lit > 20 and pinned("win-box") not in ("", first_pin),
          "after Forget, it connects and pins the new certificate", pinned("win-box"))
    close_pane(page, pane)

    # ── performance presets (#74) ────────────────────────────────────────────
    page.click('.wapyt-toolbar-btn[data-id="new"]')
    page.wait_for_selector(".wapyt-modal-body:visible .wapyt-form-body", timeout=30000)
    form = visible_form(page)
    form.locator('[name="session_type"]').select_option("rdp")
    page.wait_for_timeout(300)
    depth = form.locator('[name="color_depth"]')
    hidden_before = not depth.is_visible()
    form.locator('[name="performance"]').select_option("custom")
    page.wait_for_timeout(300)
    shown = depth.is_visible() and form.locator('[name="wallpaper"]').is_visible()
    form.locator('[name="performance"]').select_option("slow")
    page.wait_for_timeout(300)
    check(hidden_before and shown and not depth.is_visible(),
          "colour depth and effects show only for Custom")
    form.locator("button", has_text="Cancel").click()
    page.wait_for_timeout(500)

    # ── gateway editor (#75) ─────────────────────────────────────────────────
    page.click('.wapyt-toolbar-btn[data-id="new"]')
    page.wait_for_selector(".wapyt-modal-body:visible .wapyt-form-body", timeout=30000)
    form = visible_form(page)
    form.locator('[name="name"]').fill("gw-box")
    form.locator('[name="session_type"]').select_option("rdp")
    page.wait_for_timeout(300)
    form.locator('[name="host"]').fill("desk-07.corp.local")
    gw_user = form.locator('[name="gateway_username"]')
    hidden_first = not gw_user.is_visible()
    form.locator('[name="gateway_same_login"]').set_checked(False)
    page.wait_for_timeout(300)
    shown = gw_user.is_visible() and form.locator('[name="gateway_password"]').is_visible()
    check(hidden_first and shown, "the gateway's own login shows only when it differs")
    form.locator('[name="gateway_host"]').fill("rdgw.example.com")
    form.locator('[name="via_session_id"]').select_option(label="Through SSH session “jump”")
    form.locator(".wapyt-form-button-primary").click()
    page.wait_for_timeout(1500)
    body = form.inner_text()
    check("reached through the gateway" in body, "a gateway plus an SSH tunnel is refused",
          body[-200:])
    form.locator("button", has_text="Cancel").click()
    page.wait_for_timeout(500)

    for preset in ("slow", "best"):
        add_site(page, f"{preset}-box", RDP_HOST, RDP_PORT, performance=preset)
        pane = open_desktop(page, f"{preset}-box")
        lit = wait_drawn(page, pane)
        check(lit > 20, f"a {preset} preset site connects and draws", f"{lit}")
        close_pane(page, pane)

    # ── print to PDF (#77) ───────────────────────────────────────────────────
    # guacd's side, exactly: "file" announces the stream and waits for an ack,
    # then each "blob" waits for one, then "end". Acks are read off the tunnel.
    FEED = """([pane, index, mimetype, name, parts]) => {
      const rdp = document.querySelector(`#pane-term-${pane} .ix-rdp`).ixRdp;
      const acks = [];
      const send = rdp.tunnel.sendMessage.bind(rdp.tunnel);
      rdp.tunnel.sendMessage = (...m) => { if (m[0] === "ack") acks.push(m.slice(1).map(String)); return send(...m); };
      const say = (op, args) => rdp.tunnel.oninstruction(op, args.map(String));
      say("file", [index, mimetype, name]);
      for (const part of parts) say("blob", [index, btoa(part)]);
      say("end", [index]);
      rdp.tunnel.sendMessage = send;
      return acks;
    }"""
    add_site(page, "print-box", RDP_HOST, RDP_PORT, printing=True)
    pane = open_desktop(page, "print-box")
    wait_drawn(page, pane)
    pdf = ["%PDF-1.4\n", "1 0 obj << >> endobj\n", "%%EOF\n"]
    with page.expect_download() as download:
        acks = page.evaluate(FEED, [pane, 901, "application/pdf", "Quarterly report", pdf])
    saved = download.value
    check(saved.suggested_filename == "Quarterly report.pdf", "a print job downloads as a PDF",
          saved.suggested_filename)
    check(open(saved.path(), "rb").read() == "".join(pdf).encode(), "with the job's bytes")
    check(acks == [["901", "Ready", "0"]] + [["901", "OK", "0"]] * len(pdf),
          "every step of the job is acknowledged, so guacd keeps sending", str(acks))
    page.wait_for_timeout(500)
    check("Printed Quarterly report.pdf" in toast_text(page), "and a toast says so")
    acks = page.evaluate(FEED, [pane, 902, "text/plain", "x.txt", ["not a pdf"]])
    check(acks[:1] == [["902", "Not accepted", str(0x0303)]], "a file that isn't a PDF is refused",
          str(acks))
    close_pane(page, pane)
    pane = open_desktop(page, "win-box")
    wait_drawn(page, pane)
    acks = page.evaluate(FEED, [pane, 903, "application/pdf", "y.pdf", ["%PDF"]])
    check(acks[:1] == [["903", "Not accepted", str(0x0303)]],
          "a site without printing refuses print jobs", str(acks))
    close_pane(page, pane)

    # ── the shared drive (#76) ───────────────────────────────────────────────
    if DRIVE_DIR:
        add_site(page, "drive-box", RDP_HOST, RDP_PORT, drive=True)
        pane = open_desktop(page, "drive-box")
        check(wait_drawn(page, pane) > 20, "a site with a drive connects and draws")
        page.wait_for_timeout(2000)                      # xrdp mounts the drive
        page.click(f'.ix-pane-tab[data-pane="{pane}"][data-pane-tab="files"]')
        files = f"#pane-files-{pane}"
        page.wait_for_selector(f"{files} .wapyt-datatable", timeout=30000)
        payload = f"to the desktop {int(time.time())}"
        with page.expect_file_chooser() as chooser:
            page.click(f'{files} .ix-sftp-btn[data-sftp="upload"]')
        chooser.value.set_files({"name": "to-desktop.txt", "mimeType": "text/plain",
                                 "buffer": payload.encode()})
        page.wait_for_timeout(3000)
        there = target("cat ~/thinclient_drives/GUACFS/to-desktop.txt")
        check(there == payload, "a file uploaded in Files is on the desktop's drive", there)
        target("echo from the desktop > ~/thinclient_drives/GUACFS/from-desktop.txt")
        page.wait_for_timeout(1000)
        page.click(f'{files} .ix-sftp-btn[data-sftp="refresh"]')
        page.wait_for_timeout(1500)
        names = page.locator(f"{files} .wapyt-datatable-table tbody tr").all_inner_texts()
        check(any("from-desktop.txt" in n for n in names),
              "a file the desktop writes is listed in Files", str(names)[:200])
        got = page.evaluate(
            "id => fetch(`/files/${id}/download?path=/from-desktop.txt`).then(r => r.text())",
            site_id("drive-box"))
        check(got.strip() == "from the desktop", "and it downloads", got[:80])
        page.click(f'.ix-pane-tab[data-pane="{pane}"][data-pane-tab="terminal"]')
        page.wait_for_timeout(1500)
        check(wait_drawn(page, pane, 5) > 20, "the desktop is still connected after Files")
        close_pane(page, pane)
    else:
        print("SKIP: drive checks (set IX_DRIVE_DIR to the app's GANXTERM_DRIVE_DIR)")

    real = [e for e in errors if "WebSocket" not in e]
    check(not real, "no console errors", "; ".join(real)[:400])
    page.screenshot(path=os.environ.get("IX_SHOT", "/tmp/rdp_smoke.png"))

    # ── high DPI (#74), on a 2x screen ───────────────────────────────────────
    hi = browser.new_context(viewport={"width": 1400, "height": 900}, device_scale_factor=2)
    page = hi.new_page()
    page.goto(APP, wait_until="domcontentloaded", timeout=30000)
    page.fill('input[name="email"]', "admin")
    page.fill('input[name="password"]', ADMIN_PASSWORD)
    page.click('input[type="submit"]')
    page.wait_for_selector(".wapyt-toolbar", timeout=180000)
    reset_workspace(page)
    add_site(page, "sharp-box", RDP_HOST, RDP_PORT, hidpi=True)
    ratio = """p => {
      const root = document.querySelector(`#pane-term-${p} .ix-rdp`);
      return root.ixRdp.display.getWidth() / root.getBoundingClientRect().width;
    }"""
    for name, want in (("sharp-box", 2), ("win-box", 1)):
        pane = open_desktop(page, name)
        wait_drawn(page, pane)
        got = page.evaluate(ratio, pane)
        check(abs(got - want) < 0.1,
              f"{'sharp' if want == 2 else 'soft'}: desktop pixels per CSS pixel on a 2x screen",
              f"{got:.2f}, want {want}")
        close_pane(page, pane)
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
_sys.exit(0 if all(results) else 1)
