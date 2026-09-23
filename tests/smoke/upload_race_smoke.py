"""
Regression test for the folder-upload race.

Reproduces the real-browser sequence that broke a 22-file folder upload:

  1. the picker closes, so the window regains focus immediately
  2. Chrome interposes "Upload N files to this site?"
  3. `change` finally fires, seconds later, with the selected files

The old picker armed a 700ms focus timer as a cancellation fallback, which won
that race and resolved "cancelled" while the real selection was still in
flight. The upload then never started and nothing appeared on screen.

The stub below fires focus straight away and delays `change` well past that
window, so it fails against the old implementation and passes against the new
one.

Playwright's own `set_files()` cannot catch this: it fires `change` synchronously
with no focus cycle and no confirmation dialog — which is exactly why the
original transfer suite went green while the feature was broken.
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


# Replaces the native dialog: focus now, files later.
SLOW_PICKER = """
window.__pickDelayMs = 1800;
window.__stamp = String(Date.now()).slice(-6);
// Force the <input> path, which is where the race lives. Headless Chromium
// exposes showOpenFilePicker but cannot drive its native dialog.
window.showOpenFilePicker = undefined;
const realClick = HTMLInputElement.prototype.click;
HTMLInputElement.prototype.click = function () {
  if (this.type !== "file") return realClick.call(this);
  // The dialog closes and focus returns before anything is delivered.
  window.dispatchEvent(new Event("focus"));
  setTimeout(() => {
    const dt = new DataTransfer();
    for (let i = 1; i <= 3; i += 1) {
      dt.items.add(new File([`payload ${i}`],
        `shot_${window.__stamp}_${i}.txt`, {type: "text/plain"}));
    }
    Object.defineProperty(this, "files", {value: dt.files, configurable: true});
    // webkitRelativePath is read-only on File, so the app's folder handling is
    // covered by the main transfer suite; this test is about the race only.
    this.dispatchEvent(new Event("change", {bubbles: true}));
  }, window.__pickDelayMs);
};
"""


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(SLOW_PICKER)

        page.goto(APP, wait_until="domcontentloaded", timeout=30000)
        if "/login" in page.url:
            page.fill('input[name="email"]', "admin")
            page.fill('input[name="password"]', "testpass123")
            page.click('input[type="submit"]')
            page.wait_for_load_state("domcontentloaded")

        page.wait_for_selector(".ix-toolbar", timeout=180000)
        reset_workspace(page)
        page.wait_for_selector(".wapyt-tree-row", timeout=30000)
        session_leaf(page).click()
        page.click('.ix-toolbar-btn[data-action="sftp"]')
        page.wait_for_selector(".wapyt-datatable-table tbody tr", timeout=30000)

        # "Upload folder" is the button that uses the <input> element.
        page.click('.ix-sftp-btn[data-sftp="upload-folder"]')

        # The selection arrives 1.8s in; give the uploads time after that.
        queued = False
        for _ in range(40):
            if page.locator(".ix-queue-row").count() >= 3:
                queued = True
                break
            page.wait_for_timeout(500)
        check(queued, "late selection still starts the upload",
              f"{page.locator('.ix-queue-row').count()} queue rows")

        done = False
        for _ in range(40):
            if page.locator('.ix-queue-row[data-state="done"]').count() >= 3:
                done = True
                break
            page.wait_for_timeout(500)
        check(done, "all three uploads completed",
              f"{page.locator('.ix-queue-row[data-state=\"done\"]').count()} done")

        stamp = page.evaluate("() => window.__stamp")
        page.click('.ix-sftp-btn[data-sftp="refresh"]')
        page.wait_for_timeout(2500)
        names = page.locator(".wapyt-datatable-table tbody tr td:nth-child(3)").all_inner_texts()
        mine = [n for n in names if f"shot_{stamp}_" in n]
        check(len(mine) == 3, "uploaded files are on the remote host", str(mine))

        browser.close()

    print(f"\nconsole/page errors: {len(errors)}")
    for e in errors[:6]:
        print("  ", e[:220])
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
