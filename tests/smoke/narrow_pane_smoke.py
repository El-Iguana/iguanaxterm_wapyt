"""Phase 2: the SFTP panel stays usable in a grid-cell-sized pane."""
from playwright.sync_api import sync_playwright

APP = "http://127.0.0.1:8799/iguanaxterm"
results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + detail) if detail else ''}")


def set_width(pg, w):
    pg.evaluate("(w) => { document.querySelector('.ix-pane').style.width = w + 'px'; }", w)
    pg.wait_for_timeout(250)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

    pg.goto(APP, wait_until="domcontentloaded", timeout=30000)
    if "/login" in pg.url:
        pg.fill('input[name="email"]', "admin")
        pg.fill('input[name="password"]', "testpass123")
        pg.click('input[type="submit"]')
    pg.wait_for_selector(".ix-toolbar", timeout=180000)
    pg.wait_for_timeout(500)
    if pg.locator(".wapyt-tree-row").count() == 0:
        pg.click('.ix-toolbar-btn[data-action="new"]')
        pg.wait_for_selector(".wapyt-modal-body .wapyt-form-body", timeout=30000)
        pg.fill('[name="name"]', "alpine-box")
        pg.fill('[name="host"]', "127.0.0.1")
        pg.fill('[name="port"]', "2222")
        pg.fill('[name="username"]', "testuser")
        pg.fill('[name="password"]', "testpass")
        pg.click(".wapyt-form-button-primary")
        pg.wait_for_selector(".wapyt-tree-row", timeout=30000)
    pg.wait_for_timeout(400)
    br = pg.locator(".wapyt-tree-row[data-branch='true']")
    if br.count():
        br.first.click(); pg.wait_for_timeout(300)
    pg.locator(".wapyt-tree-row").last.dblclick()
    pg.wait_for_selector("#pane-term-pane_1 .xterm-rows", timeout=60000)
    pg.wait_for_timeout(2500)

    # A genuinely deep path, so the breadcrumbs have something to cope with.
    pg.click("#pane-term-pane_1 .xterm-screen")
    pg.keyboard.type("mkdir -p deep/nested/structure/with-a-long-name/final && echo MADE\n")
    pg.wait_for_timeout(1800)

    pg.click('.ix-pane-tab[data-pane="pane_1"][data-pane-tab="files"]')
    pg.wait_for_selector("#pane-files-pane_1 tr[data-row-id]", timeout=40000)

    # ── wide: labels present ────────────────────────────────────────────────
    set_width(pg, 1200)
    wide = pg.evaluate("""() => {
      const b = document.querySelector('.ix-sftp-btn');
      return { label: b.querySelector('span:not(.mdi)') &&
                      getComputedStyle(b.querySelector('span:not(.mdi)')).display };
    }""")
    check(wide["label"] != "none", "wide pane keeps the button labels", wide["label"])

    # ── narrow: icons only, still clickable ─────────────────────────────────
    set_width(pg, 380)
    narrow = pg.evaluate("""() => {
      const bar = document.querySelector('.ix-sftp-bar');
      const btns = [...document.querySelectorAll('.ix-sftp-btn')];
      const barR = bar.getBoundingClientRect();
      return {
        labelDisplay: getComputedStyle(btns[0].querySelector('span:not(.mdi)')).display,
        allInside: btns.every(x => x.getBoundingClientRect().right <= barR.right + 0.5),
        allHaveTitles: btns.every(x => (x.title || "").length > 0),
        iconsVisible: btns.every(x => x.querySelector('.mdi').getBoundingClientRect().width > 4),
        count: btns.length,
      };
    }""")
    check(narrow["labelDisplay"] == "none", "narrow pane drops the labels")
    check(narrow["allInside"], f"all {narrow['count']} buttons inside the bar")
    check(narrow["allHaveTitles"], "every icon-only button has a tooltip")
    check(narrow["iconsVisible"], "icons actually render")

    # Clicking an icon-only button still works.
    pg.click('#pane-files-pane_1 .ix-sftp-btn[data-sftp="refresh"]')
    pg.wait_for_timeout(2000)
    check(pg.locator("#pane-files-pane_1 tr[data-row-id]").count() > 0,
          "icon-only Refresh still drives the listing")

    # ── breadcrumbs on a deep path in a narrow pane ─────────────────────────
    for segment in ("deep", "nested", "structure", "with-a-long-name", "final"):
        pg.dblclick(f'#pane-files-pane_1 tr[data-row-id] >> text="{segment}"')
        pg.wait_for_timeout(900)
    crumbs = pg.evaluate("""() => {
      const c = document.querySelector('.ix-crumbs');
      const kids = [...c.children];
      const last = kids[kids.length - 1].getBoundingClientRect();
      const box = c.getBoundingClientRect();
      return { height: Math.round(box.height), crumbs: kids.length,
               oneLine: Math.round(box.height) < 48,
               lastInView: last.right <= box.right + 1 && last.left >= box.left - 1,
               scrollable: c.scrollWidth > c.clientWidth };
    }""")
    check(crumbs["oneLine"],
          "deep path stays on one line instead of wrapping",
          f"{crumbs['crumbs']} crumbs in {crumbs['height']}px")
    check(crumbs["lastInView"],
          "current directory scrolled into view",
          f"scrollable={crumbs['scrollable']}")

    # Resizing must keep the tail pinned -- a grid drag does this constantly,
    # and without the ResizeObserver the strip kept its old offset and showed
    # the middle of the path.
    for width in (1200, 640, 380, 900, 320):
        set_width(pg, width)
        r = pg.evaluate("""() => {
          const c = document.querySelector('.ix-crumbs');
          const kids = [...c.children];
          const last = kids[kids.length - 1].getBoundingClientRect();
          const box = c.getBoundingClientRect();
          return { ok: last.right <= box.right + 2 && last.left >= box.left - 2,
                   label: kids[kids.length - 1].textContent };
        }""")
        check(r["ok"], f"current directory still in view after resize to {width}px",
              repr(r["label"]))

    # ── the capability note, forced on ──────────────────────────────────────
    pg.evaluate("""() => {
      const n = document.querySelector('.ix-sftp-note');
      n.querySelector('.ix-sftp-note-text').textContent =
        'Destination picking needs HTTPS; downloads go to your downloads folder.';
      n.title = 'Destination picking needs HTTPS.';
      n.dataset.shown = 'true';
    }""")
    for width, want_text in ((1200, True), (640, False), (380, False)):
        set_width(pg, width)
        r = pg.evaluate("""() => {
          const n = document.querySelector('.ix-sftp-note');
          const bar = document.querySelector('.ix-sftp-bar');
          return { textShown: getComputedStyle(n.querySelector('.ix-sftp-note-text')).display !== 'none',
                   iconShown: n.querySelector('.ix-sftp-note-icon').getBoundingClientRect().width > 4,
                   overflow: bar.scrollWidth - bar.clientWidth };
        }""")
        check(r["textShown"] == want_text and r["iconShown"] and r["overflow"] <= 0,
              f"note at {width}px: {'sentence' if want_text else 'icon only'}, no overflow",
              f"overflow={r['overflow']}")

    set_width(pg, 380)
    pg.screenshot(path="/tmp/ix_narrow_pane_380.png")
    set_width(pg, 1200)
    pg.screenshot(path="/tmp/ix_narrow_pane_1200.png")

    print()
    print("console/page errors:", errs or "none")
    print("RESULT:", "ALL PASS" if all(results) and not errs else "FAILURES")
    b.close()
