"""
Transfer smoke test: SFTP toolbar, upload, download, queue.

Chromium's native pickers cannot be driven by Playwright, so this stubs
window.showSaveFilePicker / showDirectoryPicker with in-memory handles. That
still exercises everything on our side of the boundary — the activation
ordering, streaming, progress, the queue and the routes — and only replaces the
browser chrome we cannot automate.
"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8799"
APP = f"{BASE}/iguanaxterm"

errors: list[str] = []
results: list[tuple[bool, str]] = []


def check(ok, label, detail=""):
    results.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'}: {label}" + (f"  {detail}" if detail else ""))


# In-memory File System Access stand-ins. Everything written is kept so the
# test can assert on the actual bytes that came down the wire.
PICKER_STUB = """
window.__saved = {};
// Must be a genuine WritableStream: response.body.pipeTo() rejects anything
// else, and the real FileSystemWritableFileStream is one.
function makeWritable(path){
  const chunks = [];
  return new WritableStream({
    write(chunk){
      chunks.push(chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk));
    },
    close(){
      let n = 0; chunks.forEach(c => n += c.length);
      const out = new Uint8Array(n); let o = 0;
      chunks.forEach(c => { out.set(c, o); o += c.length; });
      window.__saved[path] = out;
    },
    abort(){ },
  });
}
class FakeFileHandle {
  constructor(path){ this.name = path.split('/').pop(); this.path = path; }
  async createWritable(){ return makeWritable(this.path); }
}
class FakeDirHandle {
  constructor(path){ this.name = path.split('/').pop() || 'dest'; this.path = path; }
  async getDirectoryHandle(name){ return new FakeDirHandle(this.path + '/' + name); }
  async getFileHandle(name){ return new FakeFileHandle(this.path + '/' + name); }
}
window.showSaveFilePicker = async (opts) => {
  window.__lastSuggested = (opts || {}).suggestedName || '';
  return new FakeFileHandle('/dest/' + (window.__lastSuggested || 'unnamed'));
};
window.showDirectoryPicker = async () => new FakeDirHandle('/dest');
window.showOpenFilePicker = undefined;   // force the <input> path for uploads
"""


def login(page):
    page.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in page.url:
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass123")
        page.click('input[type="submit"]')
        page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector(".ix-toolbar", timeout=180000)


def open_sftp(page):
    page.wait_for_selector(".wapyt-tree-row", timeout=30000)
    page.click(".wapyt-tree-row[data-branch='true']")
    page.wait_for_timeout(300)
    page.locator(".wapyt-tree-row").nth(1).click()
    page.click('.ix-toolbar-btn[data-action="sftp"]')
    page.wait_for_selector(".wapyt-datatable-table tbody tr", timeout=30000)


def row_for(page, name):
    rows = page.locator(".wapyt-datatable-table tbody tr")
    for i in range(rows.count()):
        if name in rows.nth(i).inner_text():
            return rows.nth(i)
    return None


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(PICKER_STUB)

        login(page)
        open_sftp(page)

        check(page.locator(".ix-sftp-btn").count() == 5, "SFTP toolbar rendered",
              f"{page.locator('.ix-sftp-btn').count()} buttons")
        check(not page.locator("#note-sftp_2").inner_text().strip()
              if page.locator("#note-sftp_2").count() else True,
              "no capability warning when pickers exist")

        # ── single-file download → Save dialog with a pre-filled name ────────
        target = row_for(page, "readme.txt")
        check(target is not None, "readme.txt present")
        target.click()
        page.click('.ix-sftp-btn[data-sftp="download"]')
        page.wait_for_timeout(3000)

        suggested = page.evaluate("() => window.__lastSuggested")
        check(suggested == "readme.txt", "save dialog pre-filled the filename",
              repr(suggested))

        saved = page.evaluate("""() => {
            const k = Object.keys(window.__saved);
            return k.map(p => [p, new TextDecoder().decode(window.__saved[p])]);
        }""")
        check(len(saved) == 1, "one file written", str([s[0] for s in saved]))
        check(saved and "hello from the container" in saved[0][1],
              "downloaded bytes are correct", repr(saved[0][1].strip()) if saved else "")

        check(page.locator(".ix-queue-row").count() == 1, "queue row created")
        check(page.locator('.ix-queue-row[data-state="done"]').count() == 1,
              "queue row marked done",
              page.locator(".ix-queue-status").first.inner_text())

        # ── multi-select download → folder picker, tree recreated ────────────
        page.evaluate("() => { window.__saved = {}; }")
        page.click(".ix-queue-clear")
        page.wait_for_timeout(300)

        # A plain click replaces the selection; ctrl-click then ADDS. Starting
        # with ctrl would toggle readme.txt off, since it is still selected
        # from the single-file step above.
        row_for(page, "readme.txt").click()
        logs = row_for(page, "logs")
        if logs:
            page.keyboard.down("Control")
            logs.click()
            page.keyboard.up("Control")
        selected = page.locator('.wapyt-datatable-table tbody tr[data-selected="true"]').count()
        check(selected == 2, "two rows selected for the folder download",
              f"{selected} selected")
        page.click('.ix-sftp-btn[data-sftp="download"]')
        page.wait_for_timeout(6000)

        paths = page.evaluate("() => Object.keys(window.__saved).sort()")
        check(any(p.endswith("/readme.txt") for p in paths),
              "folder download wrote the plain file", str(paths))
        check(any("logs/app.log" in p for p in paths),
              "folder download recreated the tree", str(paths))

        content = page.evaluate("""() => {
            const k = Object.keys(window.__saved).find(p => p.includes('app.log'));
            return k ? new TextDecoder().decode(window.__saved[k]) : null;
        }""")
        check(content and "line one" in content, "nested file content correct",
              repr(content))

        # ── upload ──────────────────────────────────────────────────────────
        page.click(".ix-queue-clear")
        page.wait_for_timeout(300)
        with page.expect_file_chooser() as fc:
            page.click('.ix-sftp-btn[data-sftp="upload"]')
        chooser = fc.value
        chooser.set_files({
            "name": "uploaded.txt",
            "mimeType": "text/plain",
            "buffer": b"round trip payload",
        })
        page.wait_for_timeout(6000)

        check(row_for(page, "uploaded.txt") is not None,
              "uploaded file appears in the listing")
        check(page.locator('.ix-queue-row[data-state="done"]').count() >= 1,
              "upload queue row marked done")

        page.screenshot(path="/tmp/ix_transfer.png")
        browser.close()

    print(f"\nconsole/page errors: {len(errors)}")
    for e in errors[:8]:
        print("  ", e[:220])
    failed = [l for ok, l in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
