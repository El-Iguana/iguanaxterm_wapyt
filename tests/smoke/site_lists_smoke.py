"""
User tiers and site lists, end to end (#38, #39, #40, #41).

Needs the SSH target on :2222 and the app on :8799 with a FRESH data dir
(see README.md): it creates its own accounts, and a second run against the
same database finds them taken.

What it pins:
  - #40: a Power User goes from nothing to a User who can connect, entirely
    in the UI: Users > add pam's User, Site lists > new list > tick the site
    and the User. An Admin does the same, assigning the list straight from the
    Create user form.
  - #38: the User reaches the assigned SSH site through the real UI and gets a
    working shell; a WebSocket to a site that isn't assigned is refused 4404.
  - #39: the User's tree is only "Shared · <list>" with Connect and no Edit,
    Delete, Forget host key or Browse files (files are off); a Power User with
    an Admin's list sees their own folder and the shared group; the Admin's
    tree is their own sites only.
  - #41: with the User's terminal open, the owner takes the site out of the
    list, and the terminal says "Access to this site was removed" within a few
    seconds.
"""
import re
import sys as _sys, os as _os

from playwright.sync_api import sync_playwright

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from harness import dismiss_password_nag, record_toasts, session_leaf, toast_text  # noqa: E402

BASE = "http://127.0.0.1:8799"
APP = f"{BASE}/iguanaxterm/"
ADMIN_PASSWORD = _os.environ.get("IX_ADMIN_PASS", "testpass123")
SSH_HOST, SSH_PORT = "127.0.0.1", "2222"
REVOKED = "Access to this site was removed"

results = []


def check(ok, label, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}: {label}{('  -- ' + str(detail)[:300]) if detail else ''}")


def sign_in(browser, user, password):
    context = browser.new_context(viewport={"width": 1500, "height": 950})
    record_toasts(context)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(APP, wait_until="domcontentloaded", timeout=30000)
    page.fill('input[name="email"]', user)
    page.fill('input[name="password"]', password)
    page.click('input[type="submit"]')
    page.wait_for_selector(".wapyt-toolbar", timeout=180000)
    dismiss_password_nag(page)
    page.wait_for_timeout(1500)
    return page, errors


def toolbar(page, button):
    page.click(f'.wapyt-toolbar-btn[data-id="{button}"]')


def modal_form(page):
    """The form in the topmost open dialog."""
    return page.locator(".wapyt-modal-body:visible").last


def add_site(page, name, folder="Lab"):
    toolbar(page, "new")
    page.wait_for_selector(".wapyt-modal-body:visible .wapyt-form-body", timeout=30000)
    form = modal_form(page)
    form.locator('[name="name"]').fill(name)
    form.locator('[name="folder"]').fill(folder)
    form.locator('[name="host"]').fill(SSH_HOST)
    form.locator('[name="port"]').fill(SSH_PORT)
    form.locator('[name="username"]').fill("testuser")
    form.locator('[name="password"]').fill("testpass")
    form.locator(".wapyt-form-button-primary").click()
    # A new site lands in a folder that may be collapsed, so wait for the
    # dialog to close and then open folders until the site shows.
    page.wait_for_selector(".wapyt-modal-body:visible .wapyt-form-body", state="hidden", timeout=30000)
    page.wait_for_timeout(800)
    tree_text(page)
    page.wait_for_function(
        "n => [...document.querySelectorAll('.wapyt-tree-row')].some(r => r.textContent.includes(n))",
        arg=name, timeout=30000,
    )
    page.wait_for_timeout(300)


def tick(form, label):
    """Tick the checkbox_group entry with this label text."""
    form.locator("label", has_text=label).first.click()


def add_user(page, name, password, role=None, lists=()):
    toolbar(page, "users")
    page.wait_for_selector("#ix-admin-form:visible input", timeout=30000)
    page.wait_for_timeout(600)
    form = page.locator("#ix-admin-form:visible")
    form.locator('[name="username"]').fill(name)
    form.locator('[name="password"]').fill(password)
    if role:
        form.locator("select").select_option(role)
    for list_name in lists:
        tick(form, list_name)
    form.locator("button", has_text="Add user").click()
    page.wait_for_function(
        "n => [...document.querySelectorAll('.ix-admin .wapyt-datatable-table')].pop()?.innerText.includes(n)",
        arg=name, timeout=30000,
    )
    page.wait_for_timeout(500)
    text = page.locator(".ix-admin:visible .wapyt-datatable-table").inner_text()
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    return text


def new_list(page, name, sites=(), members=(), files=False):
    toolbar(page, "lists")
    page.wait_for_selector("#ix-lists-form:visible input", timeout=30000)
    page.wait_for_timeout(500)
    page.locator("#ix-lists-form:visible").locator('[name="name"]').fill(name)
    page.locator("#ix-lists-form:visible").locator("button", has_text="New list").click()
    page.wait_for_selector(f".wapyt-modal-title:visible:has-text('Site list: {name}')", timeout=30000)
    page.wait_for_timeout(500)
    editor = modal_form(page)
    for site in sites:
        tick(editor, site)
    for member in members:
        tick(editor, member)
    if files:
        editor.locator('[name="allow_files"]').check()
    editor.locator("button", has_text="Save").click()
    page.wait_for_function(
        "n => [...document.querySelectorAll('#ix-lists-table')].pop()?.innerText.includes(n)",
        arg=name, timeout=30000,
    )
    page.wait_for_timeout(500)
    text = page.locator("#ix-lists-table:visible").inner_text()
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    return text


def tree_text(page):
    for branch in page.locator(".wapyt-tree-row[data-branch='true']").all():
        if "▸" in branch.inner_text():
            branch.click()
            page.wait_for_timeout(150)
    return page.locator("#ix-tree-host").inner_text()


def menu_for(page, name):
    session_leaf(page, name).click(button="right")
    page.wait_for_timeout(400)
    items = [t.strip() for t in page.locator(".wapyt-tree-menu-item:visible").all_inner_texts()]
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    return items


def term_text(page, pane):
    return page.evaluate(
        "(p) => document.querySelector(`#pane-term-${p} .xterm-rows`)?.innerText || ''", pane
    )


def open_terminal(page, name):
    before = page.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
    session_leaf(page, name).dblclick()
    page.wait_for_function("n => document.querySelectorAll('.ix-pane').length > n", arg=len(before))
    after = page.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
    pane = [x for x in after if x not in before][0]
    page.wait_for_selector(f"#pane-term-{pane} .xterm-rows", timeout=60000)
    page.wait_for_function(
        "p => (document.querySelector(`#pane-term-${p} .xterm-rows`)?.innerText || '').includes('$')",
        arg=pane, timeout=60000,
    )
    return pane


def ws_refused(page, session_id):
    """
    Open a terminal WebSocket by hand. The server refuses before accepting,
    which a browser can only report as 1006 (the 4404 itself is pinned by
    tests/test_access.py), so what counts is: closed, and nothing sent.
    """
    return page.evaluate(
        """(id) => new Promise((resolve) => {
            let messages = 0;
            const ws = new WebSocket(`${location.origin.replace('http', 'ws')}/ws/terminal/${id}`);
            ws.onopen = () => resolve({opened: true, messages});
            ws.onmessage = () => { messages += 1; };
            ws.onclose = (e) => resolve({opened: false, code: e.code, messages});
            setTimeout(() => resolve({timeout: true}), 10000);
        })""",
        session_id,
    )


def session_id_of(page, name):
    node = page.locator(f".wapyt-tree-row[data-node-id^='sess_']:has-text('{name}')").first
    return int(node.get_attribute("data-node-id")[5:])


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

    # ── Admin: a Power User, an own site, and a list made at Create user ──────
    admin, admin_errors = sign_in(browser, "admin", ADMIN_PASSWORD)
    text = add_user(admin, "pam", "pams-password", role="power")
    check("Power User" in text, "admin creates Power User pam", text)
    add_site(admin, "admin-box", folder="Admin")
    lists = new_list(admin, "Ops", sites=["admin-box"], members=["pam"])
    check("Ops" in lists, "admin builds list Ops (admin-box, assigned to pam)", lists)
    lists = new_list(admin, "Desk")      # empty for now; assigned at Create user below
    text = add_user(admin, "ada", "adas-password", lists=["Desk"])
    check(re.search(r"ada\s+User\s+admin\s+Desk", text), "admin creates User ada with list Desk", text)
    # Put the admin's site in Desk so ada has something to open.
    toolbar(admin, "lists")
    admin.wait_for_selector("#ix-lists-table:visible tbody tr", timeout=30000)
    admin.locator("#ix-lists-table:visible tbody tr", has_text="Desk").dblclick()
    admin.wait_for_selector(".wapyt-modal-title:visible:has-text('Site list: Desk')", timeout=30000)
    admin.wait_for_timeout(400)
    tick(modal_form(admin), "admin-box")
    modal_form(admin).locator("button", has_text="Save").click()
    admin.wait_for_timeout(1200)
    admin.keyboard.press("Escape")
    admin_tree = tree_text(admin)
    check("admin-box" in admin_tree and "Shared" not in admin_tree,
          "admin's tree: own sites only", admin_tree.replace("\n", " | "))

    # ── Power User: own site, a User, and a list for them ─────────────────────
    pam, pam_errors = sign_in(browser, "pam", "pams-password")
    add_site(pam, "lab-box")
    add_site(pam, "secret-box")
    text = add_user(pam, "ula", "ulas-password")
    check("ula" in text and "admin" not in text, "pam creates User ula (sees only her)", text)
    lists = new_list(pam, "Lab servers", sites=["lab-box"], members=["ula"])
    check("Lab servers" in lists, "pam builds list Lab servers", lists)
    pam_tree = tree_text(pam)
    check("lab-box" in pam_tree and "Shared · Ops" in pam_tree and "admin-box" in pam_tree,
          "pam's tree: her folder plus Shared · Ops", pam_tree.replace("\n", " | "))
    check(set(menu_for(pam, "admin-box")) == {"Connect"},
          "pam's menu on a shared site: Connect only", menu_for(pam, "admin-box"))
    secret_id = session_id_of(pam, "secret-box")

    # ── User ula: the shared site, through the real UI ───────────────────────
    ula, ula_errors = sign_in(browser, "ula", "ulas-password")
    ula_tree = tree_text(ula)
    check("Shared · Lab servers" in ula_tree and "lab-box" in ula_tree
          and "secret-box" not in ula_tree, "ula's tree: Shared · Lab servers only",
          ula_tree.replace("\n", " | "))
    items = menu_for(ula, "lab-box")
    check(items == ["Connect"], "ula's menu: Connect only (no files, no edit)", items)
    pane = open_terminal(ula, "lab-box")
    ula.click(f"#pane-term-{pane} .xterm-screen")
    ula.keyboard.type("echo SHARED_$((40+2))\n")
    ula.wait_for_timeout(1500)
    check("SHARED_42" in term_text(ula, pane), "ula gets a working shell on lab-box")
    check(ula.locator(f'.ix-pane[data-pane="{pane}"] .ix-pane-tab[data-pane-tab="files"]:not([hidden]):not([disabled])').count() == 0,
          "ula's pane has no usable Files tab")
    refused = ws_refused(ula, secret_id)
    check(refused.get("opened") is False and refused.get("messages") == 0,
          "ula's WebSocket to unassigned secret-box is refused", refused)

    # ── ada (made by the admin) connects too ─────────────────────────────────
    ada, ada_errors = sign_in(browser, "ada", "adas-password")
    ada_tree = tree_text(ada)
    check("Shared · Desk" in ada_tree and "admin-box" in ada_tree, "ada sees Shared · Desk",
          ada_tree.replace("\n", " | "))
    ada_pane = open_terminal(ada, "admin-box")
    check("$" in term_text(ada, ada_pane), "ada gets a shell on admin-box")

    # ── #41: pam takes lab-box out of the list; ula's terminal closes ───────
    toolbar(pam, "lists")
    pam.wait_for_selector("#ix-lists-table:visible tbody tr", timeout=30000)
    pam.locator("#ix-lists-table:visible tbody tr", has_text="Lab servers").dblclick()
    pam.wait_for_selector(".wapyt-modal-title:visible:has-text('Site list: Lab servers')", timeout=30000)
    pam.wait_for_timeout(400)
    tick(modal_form(pam), "lab-box")            # untick
    modal_form(pam).locator("button", has_text="Save").click()
    try:
        ula.wait_for_function(
            "([p, t]) => (document.querySelector(`#pane-term-${p} .xterm-rows`)?.innerText || '').includes(t)",
            arg=[pane, REVOKED], timeout=8000,
        )
        revoked = True
    except Exception:
        revoked = False
    check(revoked, "ula's open terminal closes: 'Access to this site was removed'",
          term_text(ula, pane)[-200:])
    ula.wait_for_timeout(1500)
    check("lab-box" not in tree_text(ula), "lab-box is gone from ula's tree")
    check(REVOKED in toast_text(ula), "ula is told why", toast_text(ula)[-200:])

    for who, errors in (("admin", admin_errors), ("pam", pam_errors),
                        ("ula", ula_errors), ("ada", ada_errors)):
        real = [e for e in errors if "WebSocket" not in e and "4404" not in e]
        check(not real, f"no console errors ({who})", "; ".join(real)[:300])
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
_sys.exit(0 if all(results) else 1)
