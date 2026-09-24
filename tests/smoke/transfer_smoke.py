"""
Transfer smoke test: SFTP toolbar, upload, download, queue.

Chromium's native pickers cannot be driven by Playwright, so this stubs
window.showSaveFilePicker / showDirectoryPicker with in-memory handles. That
still exercises everything on our side of the boundary — the activation
ordering, streaming, progress, the queue and the routes — and only replaces the
browser chrome we cannot automate.
"""
from playwright.sync_api import sync_playwright

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import reset_workspace, session_leaf  # noqa: E402


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
// Mirrors FileSystemWritableFileStream: a WritableStream (pipeTo needs one)
// that also has write/seek/truncate/close/abort methods and keeps a position.
// Nothing reaches __saved until close() -- like the real one, which writes a
// swap file and only commits on close, and discards everything on abort.
function makeWritable(path){
  let data = new Uint8Array(0);
  let position = 0;
  const put = (chunk) => {
    const bytes = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
    const end = position + bytes.length;
    if (end > data.length) { const grown = new Uint8Array(end); grown.set(data); data = grown; }
    data.set(bytes, position);
    position = end;
  };
  const stream = new WritableStream({
    write(chunk){ put(chunk); },
    close(){ window.__saved[path] = data.slice(); },
    abort(){ },
  });
  stream.write = async (chunk) => put(chunk);
  stream.seek = async (to) => { position = to; };
  stream.truncate = async (size) => { data = data.slice(0, size); position = Math.min(position, size); };
  stream.close = async () => { window.__saved[path] = data.slice(); };
  stream.abort = async () => { data = new Uint8Array(0); };
  return stream;
}
// What exists on the fake disk, path -> "file" | "dir", with the real API's
// rules: a lookup without {create: true} throws NotFoundError for a missing
// entry, and asking for the wrong kind throws TypeMismatchError. __writes
// counts commits per path, so any overwrite at all is visible to a test.
window.__entries = new Map([["/dest", "dir"]]);
window.__writes = {};
function lookup(path, kind, create){
  const found = window.__entries.get(path);
  if (found && found !== kind) throw new DOMException("wrong kind", "TypeMismatchError");
  if (!found && !create) throw new DOMException("missing", "NotFoundError");
  if (!found && kind === "dir") window.__entries.set(path, "dir");
}
class FakeFileHandle {
  constructor(path){ this.name = path.split('/').pop(); this.path = path; }
  async createWritable(){
    const writable = makeWritable(this.path);
    const commit = writable.close;
    const path = this.path;
    writable.close = async () => {
      await commit();
      window.__entries.set(path, "file");
      window.__writes[path] = (window.__writes[path] || 0) + 1;
    };
    return writable;
  }
}
class FakeDirHandle {
  constructor(path){ this.name = path.split('/').pop() || 'dest'; this.path = path; }
  async getDirectoryHandle(name, opts){
    const path = this.path + '/' + name;
    lookup(path, "dir", opts && opts.create);
    return new FakeDirHandle(path);
  }
  async getFileHandle(name, opts){
    const path = this.path + '/' + name;
    lookup(path, "file", opts && opts.create);
    return new FakeFileHandle(path);
  }
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
    reset_workspace(page)


def open_sftp(page):
    page.wait_for_selector(".wapyt-tree-row", timeout=30000)
    session_leaf(page).click()
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
        # "readme (2).txt", not "readme.txt": the single-file step already
        # saved one, and nothing already there is replaced (checked below).
        check(any(p.startswith("/dest/readme") for p in paths),
              "folder download wrote the plain file", str(paths))
        check(any("logs/app.log" in p for p in paths),
              "folder download recreated the tree", str(paths))

        content = page.evaluate("""() => {
            const k = Object.keys(window.__saved).find(p => p.includes('app.log'));
            return k ? new TextDecoder().decode(window.__saved[k]) : null;
        }""")
        check(content and "line one" in content, "nested file content correct",
              repr(content))

        # ── nothing already there is replaced ───────────────────────────────
        # The single-file step above saved /dest/readme.txt, so this folder
        # download met it: it must have become "readme (2).txt".
        check("/dest/readme (2).txt" in paths and "/dest/logs/app.log" in paths,
              "an existing file is kept; the new one is numbered", str(paths))
        toast = page.inner_text("#ix-toast")
        check("“readme.txt” as “readme (2).txt”" in toast, "and the toast says so", repr(toast))

        # The same selection again: now both the file and the folder clash.
        row_for(page, "readme.txt").click()
        page.keyboard.down("Control")
        row_for(page, "logs").click()
        page.keyboard.up("Control")
        page.click('.ix-sftp-btn[data-sftp="download"]')
        page.wait_for_timeout(6000)
        paths = page.evaluate("() => Object.keys(window.__saved).sort()")
        check("/dest/readme (3).txt" in paths and "/dest/logs (2)/app.log" in paths,
              "a second identical download is numbered, file and folder", str(paths))
        writes = page.evaluate("() => window.__writes")
        check(all(count == 1 for count in writes.values()),
              "no path was ever written twice", str(writes))
        check("2 items were already there" in page.inner_text("#ix-toast"),
              "and the toast counts them", repr(page.inner_text("#ix-toast")))

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
