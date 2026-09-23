"""
Shared setup for the smoke scripts.

Exists because the workspace layout persists: a test that logs in now inherits
whatever the previous run left open, so pane ids are already taken and the
first assertion about `pane_1` fails in a way that looks nothing like the real
cause.
"""
from __future__ import annotations


def reset_workspace(page, timeout_ms: int = 30000) -> int:
    """
    Close every open pane and return how many there were.

    Leaves the workspace tabbed and empty, and waits for the debounced layout
    save so the next reload starts clean too.
    """
    tabbed = page.locator('.ix-mode-btn[data-mode="tabbed"]')
    if tabbed.count():
        tabbed.click()
        page.wait_for_timeout(400)

    closed = 0
    for _ in range(64):
        closers = page.locator(".wapyt-tab-close")
        if closers.count() == 0:
            break
        closers.first.click()
        closed += 1
        page.wait_for_timeout(200)

    # The layout save is debounced by 500ms; outrunning it means the next run
    # restores panes this one just closed.
    page.wait_for_timeout(1200)

    if closed:
        # Closing is not enough on its own: the pane counter has already
        # advanced past the panes that were restored, so the next connection
        # would be pane_3 and every test that names pane_1 would miss. The
        # layout is empty now, so a reload starts the counter clean.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".ix-toolbar", timeout=180000)
        page.wait_for_timeout(800)
    return closed


def new_pane(page, leaf, timeout_ms: int = 60000) -> str:
    """Open a connection and return its pane id, whatever the counter is at."""
    before = page.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
    leaf.dblclick()
    page.wait_for_function(
        "n => document.querySelectorAll('.ix-pane').length > n", arg=len(before)
    )
    after = page.eval_on_selector_all(".ix-pane", "els => els.map(e => e.dataset.pane)")
    fresh = [x for x in after if x not in before][0]
    page.wait_for_selector(f"#pane-term-{fresh} .xterm-rows", timeout=timeout_ms)
    return fresh


def session_leaf(page, name: str = "alpine-box"):
    """
    The tree row for a saved session, by name.

    Not "the last row": the FTP and large-upload smokes add their own profiles
    to the same scratch database, and a folder that is already open collapses
    when clicked. Only collapsed folders are opened here.
    """
    for branch in page.locator(".wapyt-tree-row[data-branch='true']").all():
        if "▸" in branch.inner_text():  # ▸ = collapsed
            branch.click()
            page.wait_for_timeout(150)
    return page.locator(f".wapyt-tree-row[data-node-id^='sess_']:has-text('{name}')").first
