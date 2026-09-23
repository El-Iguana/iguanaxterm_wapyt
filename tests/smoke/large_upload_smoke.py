"""
Large uploads, over SFTP and FTPS.

pytincture caps every request body at 2 MiB, so every photo bigger than that
came back 413 before the upload route ran. The route is now exempt and
streams; everything else must still be capped.

Needs the SSH target on :2222 and ftp_target.py on :2121 (see README.md), and
the app on :8799. Creates its own profiles if they are missing.

What it pins:
  - a file well past 2 MiB uploads over SFTP and over FTPS, and downloads
    back byte-identical (SHA-256 compared in the page)
  - a >2 MiB body to any *other* path is still 413
  - an upload without the CSRF header is refused
  - a cancelled upload leaves no half-written file under the real name
"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace  # noqa: E402


APP = "http://127.0.0.1:8799/iguanaxterm"
SIZE_MB = 40
PROFILES = {
    "alpine-box": {"type": "ssh", "port": "2222", "home": "/home/testuser"},
    "ftps-box": {"type": "ftp", "port": "2121", "home": "/"},
}
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


HELPERS = """
window.__ix = {
  csrf() {
    const m = document.cookie.match(/(?:^|;\\s*)pytincture[^=]*csrf=([^;]+)/i);
    return m ? decodeURIComponent(m[1]) : "";
  },
  blob(mb, seed) {
    // Distinct bytes throughout, so a dropped or repeated chunk changes the hash.
    const bytes = new Uint8Array(mb * 1024 * 1024);
    let x = seed >>> 0;
    for (let i = 0; i < bytes.length; i++) { x = (x * 1664525 + 1013904223) >>> 0; bytes[i] = x >>> 24; }
    return new Blob([bytes]);
  },
  async sha(buf) {
    const d = await crypto.subtle.digest("SHA-256", buf);
    return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
  },
  form(path, blob, name) {
    const f = new FormData();
    f.append("path", path);
    f.append("file", blob, name);
    return f;
  },
};
"""


def expand_all(page):
    """Open collapsed folders only; clicking an open one would collapse it."""
    for branch in page.locator(".wapyt-tree-row[data-branch='true']").all():
        if "\u25b8" in branch.inner_text():  # ▸ = collapsed
            branch.click()
            page.wait_for_timeout(150)


def session_ids(page):
    return page.evaluate(
        """() => Object.fromEntries([...document.querySelectorAll('.wapyt-tree-row[data-node-id^="sess_"]')]
             .map(e => [e.innerText.trim(), e.dataset.nodeId.slice(5)]))"""
    )


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.add_init_script(HELPERS)
    page.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in page.url:
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass123")
        page.click('input[type="submit"]')
    page.wait_for_selector(".ix-toolbar", timeout=180000)
    reset_workspace(page)

    for name, spec in PROFILES.items():
        expand_all(page)
        if name in session_ids(page):
            continue
        page.click('.ix-toolbar-btn[data-action="new"]')
        # A closed dialog's form stays in the DOM, hidden; address the open one.
        form = page.locator(".wapyt-modal-body:visible .wapyt-form-body")
        form.wait_for(timeout=30000)
        form.locator('[name="session_type"]').select_option(spec["type"])
        form.locator('[name="name"]').fill(name)
        form.locator('[name="host"]').fill("127.0.0.1")
        form.locator('[name="port"]').fill(spec["port"])
        form.locator('[name="username"]').fill("testuser")
        form.locator('[name="password"]').fill("testpass")
        page.locator(".wapyt-modal-body:visible .wapyt-form-button-primary").click()
        page.wait_for_timeout(1500)

    expand_all(page)
    ids = session_ids(page)
    check(all(name in ids for name in PROFILES), "both profiles present", str(ids))

    for name, spec in PROFILES.items():
        sid, home = ids[name], spec["home"]
        outcome = page.evaluate(
            """async ([sid, home, mb]) => {
                 const blob = __ix.blob(mb, 1234);
                 const want = await __ix.sha(await blob.arrayBuffer());
                 const t0 = performance.now();
                 const up = await fetch(`/files/${sid}/upload`, {
                   method: "POST", headers: {"X-CSRF-Token": __ix.csrf()},
                   body: __ix.form(home, blob, "big.bin"),
                 });
                 const upBody = await up.text();
                 const seconds = (performance.now() - t0) / 1000;
                 const path = (home.endsWith("/") ? home : home + "/") + "big.bin";
                 const down = await fetch(`/files/${sid}/download?path=${encodeURIComponent(path)}`);
                 const got = down.ok ? await __ix.sha(await down.arrayBuffer()) : null;
                 return {up: up.status, upBody: upBody.slice(0, 200), down: down.status,
                         same: got === want, seconds: Math.round(seconds * 10) / 10};
               }""",
            [sid, home, SIZE_MB],
        )
        check(outcome["up"] == 200, f"{name}: {SIZE_MB} MB upload accepted",
              f"{outcome['up']} {outcome['upBody']}")
        check(outcome["same"], f"{name}: downloads back byte-identical",
              f"{outcome['seconds']}s upload")

        # A folder upload, as from "Upload folder…": the route must recreate
        # Amber/2026/ on the far side. This is the shape of the failure that
        # prompted all this -- a folder of photos, one of them over 2 MiB.
        nested = page.evaluate(
            """async ([sid, home]) => {
                 const blob = __ix.blob(5, 7);
                 const want = await __ix.sha(await blob.arrayBuffer());
                 const f = new FormData();
                 f.append("path", home);
                 f.append("relative_path", "Amber/2026/IMG_0042.jpg");
                 f.append("file", blob, "IMG_0042.jpg");
                 const up = await fetch(`/files/${sid}/upload`, {
                   method: "POST", headers: {"X-CSRF-Token": __ix.csrf()}, body: f,
                 });
                 const path = (home.endsWith("/") ? home : home + "/") + "Amber/2026/IMG_0042.jpg";
                 const down = await fetch(`/files/${sid}/download?path=${encodeURIComponent(path)}`);
                 const got = down.ok ? await __ix.sha(await down.arrayBuffer()) : null;
                 return {up: up.status, same: got === want};
               }""",
            [sid, home],
        )
        check(nested["up"] == 200 and nested["same"],
              f"{name}: folder upload recreates Amber/2026/ with a 5 MB photo", str(nested))

        # Cancel partway: the remote must not keep a truncated big.bin-cut.
        cancelled = page.evaluate(
            """([sid, home]) => new Promise((resolve) => {
                 const x = new XMLHttpRequest();
                 x.open("POST", `/files/${sid}/upload`);
                 x.setRequestHeader("X-CSRF-Token", __ix.csrf());
                 let aborted = false;
                 x.upload.onprogress = (e) => {
                   if (!aborted && e.loaded > 8 * 1024 * 1024) { aborted = true; x.abort(); }
                 };
                 x.onabort = () => resolve("aborted");
                 x.onload = () => resolve("finished " + x.status);
                 x.send(__ix.form(home, __ix.blob(60, 99), "cut.bin"));
               })""",
            [sid, home],
        )
        page.wait_for_timeout(2500)
        leftover = page.evaluate(
            """async ([sid, home]) => {
                 const path = (home.endsWith("/") ? home : home + "/") + "cut.bin";
                 return (await fetch(`/files/${sid}/download?path=${encodeURIComponent(path)}`)).status;
               }""",
            [sid, home],
        )
        check(cancelled == "aborted" and leftover == 404,
              f"{name}: a cancelled upload leaves no partial file",
              f"{cancelled}, cut.bin -> {leftover}")

    sid = ids["ftps-box"]
    guards = page.evaluate(
        """async (sid) => {
             const big = new Blob([new Uint8Array(3 * 1024 * 1024)]);
             const other = await fetch("/iguanaxterm/classcall/services/session_service.py/SessionService/list", {
               method: "POST", headers: {"X-CSRF-Token": __ix.csrf(), "Content-Type": "application/json"},
               body: big,
             });
             const noCsrf = await fetch(`/files/${sid}/upload`, {
               method: "POST", body: __ix.form("/", new Blob(["x"]), "nocsrf.bin"),
             });
             return {other: other.status, noCsrf: noCsrf.status};
           }""",
        sid,
    )
    check(guards["other"] == 413, "a >2 MiB body to any other path is still 413", str(guards["other"]))
    check(guards["noCsrf"] == 403, "an upload without the CSRF header is refused", str(guards["noCsrf"]))

    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
