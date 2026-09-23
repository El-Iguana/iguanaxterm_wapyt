"""
IguanaXterm — browser UI.

Runs in Pyodide. Everything here is client-side Python; anything touching a
host, a credential or the database goes through the BFF services in
``services/``, whose implementations are stripped from the module that reaches
the browser.

Two conventions that are easy to get wrong:

* **BFF calls use the generated ``*_async`` name.** ``SessionService().list()``
  is a blocking XHR (deprecated); ``await SessionService().list_async()`` is the
  awaitable one. Streaming exports are the exception — they are already async
  generators under their own name.
* **UI is built in ``load_ui``, not ``__init__``.** wapyt's ``LoadUICaller``
  metaclass calls ``load_ui()`` after construction; building in ``__init__`` as
  well renders everything twice.
"""
from __future__ import annotations

import asyncio
import html
import traceback

import js
from pyodide.ffi import create_proxy

from wapyt import filetransfer
from wapyt import (
    CellConfig,
    ColumnConfig,
    DataTable,
    DataTableConfig,
    FieldConfig,
    Form,
    FormConfig,
    LayoutConfig,
    MainWindow,
    ModalConfig,
    ModalWindow,
    SelectOption,
    TabConfig,
    TableAction,
    TabWidget,
    TabWidgetConfig,
    Terminal,
    TerminalConfig,
    TerminalTheme,
    Tree,
    TreeAction,
    TreeConfig,
    TreeItem,
)

from services.layout_service import LayoutService
from services.paths import (
    SESSION_TYPES,
    breadcrumbs,
    format_size,
    parent_path,
    session_caps,
    session_icon,
)
from services.session_service import SessionService
from services.sftp_service import SFTPService
from services.user_service import UserService

# pytincture resolves the browser entrypoint by AST, and its MainWindow
# detection is hardcoded to dhxpyt.layout.MainWindow — it never matches a wapyt
# base. Without this declaration the app starts with HTTP 422.
APP_ENTRYPOINT = "IguanaXterm"

# Browser tab icon. pytincture reads this literal from the app module and
# resolves it against modules_path; .webp is among the types it serves.
APP_FAVICON = "static/el_iguana_avatar.webp"

UNSORTED = "Ungrouped"

TERMINAL_THEME = TerminalTheme(
    background="#1a1a2e",
    foreground="#e0e0e0",
    cursor="#00ff88",
    selection_background="#33467c",
)

_TOOLBAR_BUTTONS = (
    ("new", "New", "mdi-plus-circle"),
    ("edit", "Edit", "mdi-pencil"),
    ("delete", "Delete", "mdi-delete"),
    ("|", "", ""),
    ("connect", "Connect", "mdi-connection"),
    ("sftp", "Files", "mdi-folder-network"),
    ("|", "", ""),
    ("users", "Users", "mdi-account-group"),
    ("password", "Password", "mdi-key"),
)


def _csrf_token() -> str:
    """
    pytincture's CSRF cookie. Readable from script on purpose — echoing it back
    in a header is what makes it a CSRF defence.
    """
    for part in str(js.document.cookie).split(";"):
        name, _, value = part.strip().partition("=")
        if "csrf" in name.lower():
            return str(js.decodeURIComponent(value))
    return ""


def _report(label: str) -> None:
    """
    Surface a failure from a fire-and-forget coroutine.

    ``asyncio.ensure_future`` swallows exceptions. The dhxpyt rewrite launched
    every BFF call that way with no handler, so its failures were completely
    silent — an empty tree and dead buttons, nothing in the console.
    """
    js.console.error(f"[iguanaxterm] {label} failed:\n{traceback.format_exc()}")


def _spawn(coro, label: str):
    """Run a coroutine detached, but never silently."""

    async def _guarded():
        try:
            await coro
        except Exception:  # noqa: BLE001 - last line of defence
            _report(label)

    return asyncio.ensure_future(_guarded())


class IguanaXterm(MainWindow):
    # No __init__: the LoadUICaller metaclass calls load_ui() after
    # construction, so defining both builds the UI twice.

    def load_ui(self) -> None:
        self.set_theme("dark")
        # pytincture's bootstrap page leaves the tab title empty once the app
        # takes over, so the window identifies itself here.
        js.document.title = "IguanaXterm"

        self._sessions: list[dict] = []
        self._selected_id: int | None = None
        # A pane is one connection: its own terminal and its own file browser,
        # behind a two-entry tab strip. The workspace host (tabs today, a grid
        # later) only decides where a pane's root element lives.
        self._panes: dict[str, dict] = {}       # pane id -> pane state
        # JS ResizeObserver per pane, disconnected when the pane closes.
        self._crumb_observers: dict = {}
        self._sftp_tabs: dict[str, dict] = {}   # pane id -> {table, session_id, path}
        self._tab_counter = 0
        self._mode = "tabbed"      # or "tiled"
        self._restoring = False    # suppresses saves while rebuilding
        self._save_task = None
        self._grid = None          # the GridStack instance, once loaded
        self._grid_proxies: list = []
        self._maximized: str | None = None
        self._me: dict = {}

        self._build_chrome()
        _spawn(self._load_identity(), "identity load")
        _spawn(self._boot_workspace(), "workspace boot")

    async def _boot_workspace(self) -> None:
        # The layout names session ids, so the session list has to be in hand
        # before it can be validated against -- hence one coroutine rather than
        # two racing ones.
        await self._reload_sessions()
        await self._restore_layout()

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def _build_chrome(self) -> None:
        self.attach_html("mainwindow_header", self._toolbar_html())
        self._wire_toolbar()

        body = self.add_layout(
            "mainwindow",
            LayoutConfig(
                type="line",
                cols=[
                    CellConfig(id="sidebar", width="280px"),
                    CellConfig(id="workspace", width="100%"),
                ],
            ),
        )
        self.body = body

        # The sidebar is a brand header above the session tree. Built as HTML
        # because it is two static elements, then the Tree is mounted into the
        # host div below it.
        sidebar = body.get_cell("sidebar")
        sidebar_el = (
            sidebar.getContainer() if hasattr(sidebar, "getContainer") else sidebar
        )
        sidebar_el.innerHTML = (
            '<div class="ix-sidebar">'
            '  <div class="ix-brand">'
            # 128px WebP cropped to the head: the full portrait is 783KB and
            # would be fetched in its entirety to render a 40px avatar.
            '    <img class="ix-brand-logo" src="/static/el_iguana_avatar.webp"'
            '         alt="" width="40" height="40" decoding="async">'
            '    <div class="ix-brand-text">'
            '      <span class="ix-brand-name">IguanaXterm</span>'
            '      <span class="ix-brand-sub">SSH &middot; Telnet &middot; SFTP &middot; FTP</span>'
            '    </div>'
            '  </div>'
            '  <div class="ix-sidebar-tree" id="ix-tree-host"></div>'
            f"</div><style>{_SIDEBAR_CSS}</style>"
        )

        self.tree = Tree(
            TreeConfig(
                filterable=True,
                filter_placeholder="Filter sessions",
                empty_text="No saved sessions yet.\nUse New to add one.",
                context_actions=[
                    TreeAction("connect", "Connect", "mdi-connection", scope="leaf"),
                    TreeAction("sftp", "Browse files", "mdi-folder-network", scope="leaf"),
                    TreeAction(separator=True),
                    TreeAction("edit", "Edit", "mdi-pencil", scope="leaf"),
                    TreeAction("forget_key", "Forget host key", "mdi-key-remove", scope="leaf"),
                    TreeAction("delete", "Delete", "mdi-delete", scope="leaf", danger=True),
                ],
            ),
            root="#ix-tree-host",
        )
        self.tree.on_select(self._on_tree_select)
        self.tree.on_activate(lambda payload: self._connect(payload.get("id")))
        self.tree.on_action(self._on_tree_action)

        # Two hosts, one workspace. The TabWidget always holds a tab per pane
        # and is the source of truth for which panes exist; the grid mirrors it
        # while tiled. Switching modes moves pane roots between them and
        # nothing else, which is safe because a reparented xterm keeps its
        # buffer (tests/smoke/reparent_spike.py).
        workspace = body.get_cell("workspace")
        workspace_el = (
            workspace.getContainer() if hasattr(workspace, "getContainer") else workspace
        )
        workspace_el.innerHTML = (
            '<div class="ix-workspace">'
            '  <div class="ix-host" id="ix-tabs-host"></div>'
            '  <div class="ix-host" id="ix-grid-host" hidden>'
            '    <div class="grid-stack"></div>'
            '  </div>'
            "</div>"
        )

        self.tabs = TabWidget(
            TabWidgetConfig(tabs=[]),
            container=js.document.getElementById("ix-tabs-host"),
        )
        self.tabs.on_change(self._on_tab_change)
        self.tabs.on_close(self._on_tab_close)

        # The hint lives on the empty panel area rather than in a placeholder
        # tab, so it comes back on its own when the last tab is closed. The pane
        # and SFTP rules go up once here rather than with every pane built.
        hint = js.document.createElement("style")
        hint.textContent = _WORKSPACE_CSS + _GRID_CSS + _PANE_CSS + _SFTP_CSS
        js.document.head.appendChild(hint)

    def _toolbar_html(self) -> str:
        parts = ['<div class="ix-toolbar">']
        for key, label, icon in _TOOLBAR_BUTTONS:
            if key == "|":
                parts.append('<span class="ix-toolbar-sep"></span>')
                continue
            parts.append(
                f'<button type="button" class="ix-toolbar-btn" data-action="{key}">'
                f'<span class="mdi {icon}"></span><span>{label}</span></button>'
            )
        parts.append('<span class="ix-toolbar-spacer"></span>')
        parts.append(
            '<span class="ix-mode-switch" role="group" aria-label="Workspace layout">'
            '<button type="button" class="ix-mode-btn" data-mode="tabbed" '
            'aria-selected="true" title="One connection at a time, in tabs">'
            '<span class="mdi mdi-table-row"></span></button>'
            '<button type="button" class="ix-mode-btn" data-mode="tiled" '
            'aria-selected="false" title="Tile connections in a resizable grid">'
            '<span class="mdi mdi-view-grid"></span></button>'
            '</span>'
        )
        parts.append('<span class="ix-toolbar-user" id="ix-user"></span>')
        parts.append(
            '<button type="button" class="ix-toolbar-btn" data-action="logout" '
            'title="Sign out">'
            '<span class="mdi mdi-logout"></span><span>Logout</span></button>'
        )
        parts.append("</div>")
        parts.append(f"<style>{_TOOLBAR_CSS}</style>")
        return "".join(parts)

    def _wire_toolbar(self) -> None:
        def _on_click(event) -> None:
            # A DOM miss arrives as JsNull, not None: `x is None` is always
            # False for it and the next attribute access raises. JsNull is
            # falsy, so test truthiness for anything coming back over the FFI.
            button = event.target.closest(".ix-toolbar-btn")
            if button:
                self._on_toolbar(button.dataset.action)
                return
            # One delegated listener covers the per-tab SFTP toolbars too, so
            # each new tab does not add another document-level handler.
            sftp_button = event.target.closest("[data-sftp]")
            if sftp_button:
                self._sftp_click(sftp_button.dataset.sftp, sftp_button.dataset.tab)
                return

            pane_tab = event.target.closest("[data-pane-tab]")
            if pane_tab:
                self._pane_select(pane_tab.dataset.pane, pane_tab.dataset.paneTab)
                return

            maximizer = event.target.closest("[data-pane-max]")
            if maximizer:
                self._toggle_maximize(maximizer.dataset.paneMax)
                return

            closer = event.target.closest("[data-pane-close]")
            if closer:
                self._close_pane(closer.dataset.paneClose)
                return

            reconnect = event.target.closest("[data-reconnect]")
            if reconnect:
                self._connect_pane(reconnect.dataset.reconnect)
                return

            mode_button = event.target.closest("[data-mode]")
            if mode_button:
                self._set_mode(mode_button.dataset.mode)

        self._toolbar_proxy = create_proxy(_on_click)
        js.document.addEventListener("click", self._toolbar_proxy)

        def _on_key(event) -> None:
            # Escape restores a maximized tile -- but never when the focus is
            # in a terminal. Escape belongs to the remote there: it is how you
            # leave insert mode in vim, and stealing it would make the editor
            # people actually use unusable. Use the button in that case.
            if event.key != "Escape" or not self._maximized:
                return
            target = event.target
            if target and hasattr(target, "closest") and target.closest(".wapyt-terminal"):
                return
            self._toggle_maximize(self._maximized)

        self._key_proxy = create_proxy(_on_key)
        js.document.addEventListener("keydown", self._key_proxy)

    # ------------------------------------------------------------------
    # Identity and session list
    # ------------------------------------------------------------------

    async def _load_identity(self) -> None:
        self._me = await UserService().me_async()
        label = js.document.getElementById("ix-user")
        if label:  # JsNull, not None, when absent
            suffix = " · admin" if self._me.get("is_admin") else ""
            label.textContent = f"{self._me.get('username', '')}{suffix}"

    async def _reload_sessions(self) -> None:
        self._sessions = await SessionService().list_async()
        self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        """
        Rebuild the folder tree from the flat session list.

        The widget keys expansion by node id and keeps it across set_items, so
        reloading after a save does not collapse what the user had open.
        """
        folders: dict[str, list[dict]] = {}
        for session in self._sessions:
            folders.setdefault(session.get("folder") or UNSORTED, []).append(session)

        items = []
        for folder in sorted(folders, key=str.lower):
            children = [
                TreeItem(
                    id=f"sess_{session['id']}",
                    label=session["name"],
                    icon=session_icon(session.get("type", "ssh")),
                    tooltip=self._session_tooltip(session),
                )
                for session in folders[folder]
            ]
            items.append(
                TreeItem(
                    id=f"folder_{folder}",
                    label=folder,
                    badge=len(children),
                    items=children,
                )
            )
        self.tree.set_items(items)

    @staticmethod
    def _session_tooltip(session: dict) -> str:
        user = session.get("username") or ""
        prefix = f"{user}@" if user else ""
        detail = f"{prefix}{session['host']}:{session['port']}"
        description = session.get("description") or ""
        return f"{detail}\n{description}".strip()

    def _session(self, session_id: int | None) -> dict | None:
        return next((s for s in self._sessions if s["id"] == session_id), None)

    # ------------------------------------------------------------------
    # Tree and toolbar events
    # ------------------------------------------------------------------

    def _on_tree_select(self, payload: dict) -> None:
        node_id = str(payload.get("id") or "")
        self._selected_id = (
            int(node_id[5:]) if node_id.startswith("sess_") else None
        )

    def _on_tree_action(self, payload: dict) -> None:
        node_id = str(payload.get("id") or "")
        if not node_id.startswith("sess_"):
            return
        session_id = int(node_id[5:])
        self._selected_id = session_id
        action = payload.get("action")

        if action == "connect":
            self._connect(node_id)
        elif action == "sftp":
            self._open_sftp(session_id)
        elif action == "edit":
            _spawn(self._session_editor(session_id), "session editor")
        elif action == "delete":
            _spawn(self._confirm_delete(session_id), "session delete")
        elif action == "forget_key":
            _spawn(self._forget_host_key(session_id), "forget host key")

    def _on_toolbar(self, action: str) -> None:
        if action == "logout":
            _spawn(self._logout(), "logout")
        elif action == "new":
            _spawn(self._session_editor(None), "session editor")
        elif action == "users":
            _spawn(self._admin_panel(), "admin panel")
        elif action == "password":
            self._password_dialog()
        elif self._selected_id is None:
            self._toast("Select a session first.")
        elif action == "edit":
            _spawn(self._session_editor(self._selected_id), "session editor")
        elif action == "delete":
            _spawn(self._confirm_delete(self._selected_id), "session delete")
        elif action == "connect":
            self._connect(f"sess_{self._selected_id}")
        elif action == "sftp":
            self._open_sftp(self._selected_id)

    # ------------------------------------------------------------------
    # Terminals
    # ------------------------------------------------------------------

    async def _logout(self) -> None:
        """
        End the session and return to the login page.

        pytincture's logout is a POST that validates the CSRF header, so a link
        or a form submit cannot do it — the token has to travel in
        X-CSRF-Token, which only fetch can set. The cookie is deliberately not
        HttpOnly so the page can read it back.
        """
        open_panes = len(self._panes)
        if open_panes and not js.confirm(
            f"Sign out and close {open_panes} open connection(s)?"
        ):
            return

        application = str(js.window.location.pathname).strip("/").split("/")[0]
        options = js.Object.new()
        options.method = "POST"
        options.credentials = "same-origin"
        headers = js.Object.new()
        setattr(headers, "X-CSRF-Token", _csrf_token())
        options.headers = headers

        try:
            response = await js.fetch(f"/{application}/auth/logout", options)
        except Exception:
            self._toast("Could not sign out.")
            return
        if not response.ok:
            self._toast("Could not sign out.")
            return

        js.window.location.assign(f"/{application}/login")

    def _connect(self, node_id: str | None) -> None:
        """
        Open a pane for a session.

        A pane is one connection, not one saved session: connecting to the same
        host twice gives two panes with two shells, the way two terminal windows
        would.
        """
        if not node_id or not str(node_id).startswith("sess_"):
            return
        self._open_pane(int(str(node_id)[5:]))

    def _open_pane(
        self,
        session_id: int,
        focus: str = "terminal",
        *,
        connect: bool = True,
        restore: dict | None = None,
    ) -> str | None:
        session = self._session(session_id)
        if session is None:
            return None

        caps = session_caps(session.get("type"))
        if focus == "files" and not caps["files"]:
            self._toast("This connection has no file browser.")
            focus = "terminal"
        if focus == "terminal" and not caps["terminal"]:
            focus = "files"  # SFTP and FTP profiles are files only

        self._tab_counter += 1
        pane_id = f"pane_{self._tab_counter}"
        self.tabs.add_tab(
            TabConfig(
                id=pane_id,
                title=session["name"],
                icon=session_icon(session.get("type", "ssh")),
                closable=True,
            )
        )

        cell = self.tabs.get_cell(pane_id)
        container = cell.getContainer() if hasattr(cell, "getContainer") else cell
        container.innerHTML = self._pane_html(pane_id, session, caps)

        self._panes[pane_id] = {
            "session_id": session_id,
            "name": session["name"],
            "terminal": None,
            "has_terminal": caps["terminal"],
            "has_files": caps["files"],
            # Set for real by _pane_select once the pane is dialled.
            "tab": "terminal" if caps["terminal"] else "files",
            "files_mounted": False,
            # A brand-new pane carries a size but no position, so GridStack
            # auto-places it. Giving it x=0,y=0 -- which a default geometry
            # does -- stacks every new tile under the last one instead of
            # filling the row. Only a restored pane names a position.
            "geometry": (
                {
                    "x": int(restore.get("x") or 0),
                    "y": int(restore.get("y") or 0),
                    "w": int(restore.get("w") or 6),
                    "h": int(restore.get("h") or 7),
                }
                if restore
                else {"w": 6, "h": 7}
            ),
            # Where a restored pane should end up once it is dialled.
            "restore": restore or None,
        }

        self.tabs.set_active(pane_id)
        if self._mode == "tiled":
            self._attach_to_grid(pane_id)

        if connect:
            self._connect_pane(pane_id)
            if focus == "files" and caps["terminal"]:
                self._pane_select(pane_id, "files")
        else:
            self._show_reconnect(pane_id, session)

        self._schedule_layout_save()
        return pane_id

    def _connect_pane(self, pane_id: str) -> None:
        """Dial a pane's terminal, replacing any reconnect placeholder."""
        pane = self._panes.get(pane_id)
        if pane is None or pane["terminal"] is not None:
            return

        host = js.document.getElementById(f"pane-term-{pane_id}")
        if not host:
            return
        host.innerHTML = ""

        if not pane["has_terminal"]:
            # A files-only pane "dials" by opening its file browser. The
            # terminal panel only ever held the Reconnect placeholder.
            restore = pane.pop("restore", None)
            if restore:
                pane["restore_path"] = restore.get("path") or ""
            self._pane_select(pane_id, "files")
            return

        terminal = Terminal(
            TerminalConfig(
                ws_url=f"/ws/terminal/{pane['session_id']}",
                theme=TERMINAL_THEME,
                search=True,
                reconnect=True,
                # A tile can be dragged, and every frame that crosses a column
                # boundary is a TIOCSWINSZ on the remote: one measured 1.3s
                # drag sent 41 of them. Wait for the drag to settle instead.
                fit_debounce_ms=120,
            ),
            container=host,
        )
        terminal.on_error(lambda payload: self._toast(payload.get("message", "Error")))
        pane["terminal"] = terminal

        restore = pane.pop("restore", None)
        if restore and restore.get("tab") == "files" and pane["has_files"]:
            pane["restore_path"] = restore.get("path") or ""
            self._pane_select(pane_id, "files")
        else:
            terminal.fit()
            terminal.focus()

    def _show_reconnect(self, pane_id: str, session: dict) -> None:
        """
        A restored pane, not yet dialled.

        Restoring a layout deliberately does not connect: N simultaneous SSH
        dials on page load is the pattern that trips MaxStartups and produces
        the banner resets ssh.py's retry exists to survive.
        """
        host = js.document.getElementById(f"pane-term-{pane_id}")
        if not host:
            return
        label = f"{session.get('username') or ''}@{session.get('host', '')}".lstrip("@")
        host.innerHTML = (
            f'<div class="ix-reconnect">'
            f'  <span class="mdi mdi-power-plug-off ix-reconnect-icon"></span>'
            f'  <div class="ix-reconnect-name">{session["name"]}</div>'
            f'  <div class="ix-reconnect-host">{label}</div>'
            f'  <button type="button" class="ix-reconnect-btn" data-reconnect="{pane_id}">'
            f'    <span class="mdi mdi-connection"></span><span>Reconnect</span></button>'
            f"</div>"
        )

    def _pane_html(self, pane_id: str, session: dict, caps: dict) -> str:
        """
        A pane's own chrome: a two-entry tab strip over two stacked panels.

        The panels are absolutely positioned siblings rather than one swapped
        element, so the terminal's host is created once and never replaced —
        remounting an xterm is what loses a buffer. A hidden panel has no size,
        so ``fit()`` skips it and the widget's ResizeObserver re-fits it on the
        way back in.
        """
        # The tab strip names the pane while tabbed; tiled, it is hidden and
        # this is the only place the connection's name appears.
        name = html.escape(session.get("name", ""))
        host = session.get("host", "")
        port = session.get("port")
        user = session.get("username") or ""
        label = f"{user}@{host}" if user else host
        if port and int(port) != caps["port"]:
            label = f"{label}:{port}"
        if not caps["terminal"]:
            label = f"{session.get('type', '').upper()} {label}"

        files_attrs = (
            "" if caps["files"] else ' disabled title="This connection has no file browser."'
        )
        # A files-only pane keeps the Terminal panel (the Reconnect placeholder
        # lives there) but not a button that could switch to it.
        term_attrs = "" if caps["terminal"] else " hidden"
        return (
            f'<div class="ix-pane" data-pane="{pane_id}">'
            f'  <div class="ix-pane-tabs">'
            f'    <span class="ix-pane-grip mdi mdi-drag-vertical" title="Drag to move"></span>'
            f'    <span class="ix-pane-name" title="{name}">'
            f'      <span class="mdi {session_icon(session.get("type", "ssh"))}"></span>'
            f'      <span class="ix-pane-name-text">{name}</span></span>'
            f'    <button type="button" class="ix-pane-tab" data-pane-tab="terminal"'
            f'            data-pane="{pane_id}" aria-selected="true"{term_attrs}>'
            f'      <span class="mdi mdi-console-line"></span><span>Terminal</span></button>'
            f'    <button type="button" class="ix-pane-tab" data-pane-tab="files"'
            f'            data-pane="{pane_id}" aria-selected="false"{files_attrs}>'
            f'      <span class="mdi mdi-folder-network"></span><span>Files</span></button>'
            f'    <span class="ix-pane-spacer"></span>'
            f'    <span class="ix-pane-host" title="{label}">{label}</span>'
            f'    <button type="button" class="ix-pane-max" data-pane-max="{pane_id}"'
            f'            title="Maximize this pane" aria-pressed="false">'
            f'      <span class="mdi mdi-arrow-expand"></span></button>'
            f'    <button type="button" class="ix-pane-close" data-pane-close="{pane_id}"'
            f'            title="Close this connection">&times;</button>'
            f'  </div>'
            f'  <div class="ix-pane-body">'
            f'    <div class="ix-pane-panel" data-panel="terminal" id="pane-term-{pane_id}"></div>'
            f'    <div class="ix-pane-panel" data-panel="files" id="pane-files-{pane_id}" hidden></div>'
            f'  </div>'
            f"</div>"
        )

    def _pane_select(self, pane_id: str, which: str) -> None:
        pane = self._panes.get(pane_id)
        if pane is None or which not in ("terminal", "files"):
            return
        if which == "files" and not pane["has_files"]:
            self._toast("This connection has no file browser.")
            return
        if which == "terminal" and not pane["has_terminal"]:
            return

        pane["tab"] = which
        for name, element_id in (
            ("terminal", f"pane-term-{pane_id}"),
            ("files", f"pane-files-{pane_id}"),
        ):
            button = js.document.querySelector(
                f'.ix-pane-tab[data-pane="{pane_id}"][data-pane-tab="{name}"]'
            )
            if button:
                button.setAttribute("aria-selected", "true" if name == which else "false")
            panel = js.document.getElementById(element_id)
            if panel:
                panel.hidden = name != which

        if which == "terminal":
            if pane["terminal"] is not None:
                pane["terminal"].fit()
                pane["terminal"].focus()
        elif not pane["files_mounted"]:
            # Mounted on first use, so a pane that is only ever a terminal never
            # dials SFTP at all.
            pane["files_mounted"] = True
            _spawn(self._mount_files(pane_id), "sftp mount")
        self._schedule_layout_save()

    async def _mount_files(self, pane_id: str) -> None:
        pane = self._panes.get(pane_id)
        if pane is None:
            return
        session_id = pane["session_id"]

        # Registering the hold before the first listing means two panes on one
        # host both count, and closing either leaves the other's channel up.
        result = await SFTPService().retain_async(session_id)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not open SFTP"))
            # Leave the pane on its terminal rather than on an empty panel, and
            # let a later click try again.
            pane["files_mounted"] = False
            self._pane_select(pane_id, "terminal")
            return
        pane["sftp_held"] = True

        panel = self._build_sftp_panel(
            js.document.getElementById(f"pane-files-{pane_id}"), pane_id
        )
        table = DataTable(
            DataTableConfig(
                columns=[
                    ColumnConfig(id="icon", header="", type="icon", width=34, sortable=False),
                    ColumnConfig(id="name", header="Name"),
                    ColumnConfig(id="size", header="Size", width=100, align="right", sort_by="size_bytes"),
                    ColumnConfig(id="modified", header="Modified", width=150, sort_by="mtime"),
                    ColumnConfig(id="permissions", header="Perms", width=80),
                ],
                id_field="id",
                selection="multi",
                filterable=True,
                filter_placeholder="Filter files",
                group_dirs_first="is_dir",
                sort_by="name",
                drop_upload=True,
                empty_text="Empty directory",
                context_actions=[
                    TableAction("download", "Download", "mdi-download"),
                    TableAction("rename", "Rename", "mdi-rename-box"),
                    TableAction(separator=True),
                    TableAction("delete", "Delete", "mdi-delete", danger=True),
                ],
            ),
            container=panel["table_cell"],
        )

        start_path = pane.pop("restore_path", "")
        self._sftp_tabs[pane_id] = {
            "table": table,
            "session_id": session_id,
            "path": start_path,
        }
        table.on_activate(lambda payload: self._sftp_activate(pane_id, payload))
        table.on_action(lambda payload: self._sftp_action(pane_id, payload))
        table.on_drop(lambda payload: self._sftp_upload(pane_id, payload))

        await self._sftp_navigate(pane_id, start_path)

    def _on_tab_change(self, payload: dict) -> None:
        pane_id = payload.get("id") if isinstance(payload, dict) else payload
        pane = self._panes.get(pane_id)
        if pane is None or pane["tab"] != "terminal" or pane["terminal"] is None:
            return
        # A background pane is hidden, not unmounted, so its terminal has no
        # dimensions while inactive and skips fitting. Re-fit on the way back in
        # or the grid stays at whatever size it last measured.
        pane["terminal"].fit()
        pane["terminal"].focus()

    def _on_tab_close(self, payload: dict) -> None:
        pane_id = payload.get("id") if isinstance(payload, dict) else payload
        if self._maximized == pane_id:
            self._maximized = None
        pane = self._panes.pop(pane_id, None)
        if pane is None:
            return

        # Closing the pane must close the socket. The dhxpyt rewrite left every
        # terminal it ever opened running on the server. A restored pane that
        # was never dialled has nothing to close.
        if pane["terminal"] is not None:
            pane["terminal"].destroy()

        sftp = self._sftp_tabs.pop(pane_id, None)
        if sftp is not None:
            sftp["table"].destroy()
        observer = self._crumb_observers.pop(pane_id, None)
        if observer is not None:
            observer.disconnect()
        self._remove_grid_item(pane_id)
        self._schedule_layout_save()
        if pane.get("sftp_held"):
            # Drops this pane's hold only. Another pane on the same host keeps
            # the channel alive.
            _spawn(
                SFTPService().disconnect_async(pane["session_id"]),
                "sftp release",
            )

    # ------------------------------------------------------------------
    # Workspace mode: tabbed or tiled
    # ------------------------------------------------------------------

    async def _load_asset(self, tag: str, attrs: dict) -> bool:
        """Append a <script>/<link> and wait for it, without blocking the loop."""
        future = asyncio.get_event_loop().create_future()
        element = js.document.createElement(tag)
        for name, value in attrs.items():
            setattr(element, name, value)

        def _settle(ok: bool):
            def _handler(*_args) -> None:
                if not future.done():
                    future.set_result(ok)
            return create_proxy(_handler)

        element.addEventListener("load", _settle(True))
        element.addEventListener("error", _settle(False))
        js.document.head.appendChild(element)
        return await future

    async def _ensure_gridstack(self) -> bool:
        """
        Load the vendored GridStack the first time it is needed.

        Deferred rather than loaded at boot: someone who never tiles never pays
        for the 92KB, and the tabbed workspace is the default.
        """
        if getattr(js.window, "GridStack", None):
            return True
        await self._load_asset(
            "link", {"rel": "stylesheet", "href": "/gridstack/gridstack.min.css"}
        )
        loaded = await self._load_asset("script", {"src": "/gridstack/gridstack-all.js"})
        if not loaded or not getattr(js.window, "GridStack", None):
            self._toast("Could not load the tiling library.")
            return False
        return True

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode or mode not in ("tabbed", "tiled"):
            return
        if mode == "tiled":
            _spawn(self._go_tiled(), "workspace tile")
        else:
            self._go_tabbed()

    async def _go_tiled(self) -> None:
        if not await self._ensure_gridstack():
            return
        if self._mode == "tiled":
            return

        js.document.getElementById("ix-tabs-host").hidden = True
        js.document.getElementById("ix-grid-host").hidden = False

        if self._grid is None:
            options = js.Object.new()
            options.column = 12
            options.cellHeight = 56
            options.margin = 6
            options.float = False
            options.animate = False
            # Only the grip drags. The strip also holds the Terminal/Files
            # buttons, and a drag starting on one of those would eat the click.
            handle = js.Object.new()
            handle.handle = ".ix-pane-grip"
            options.draggable = handle
            self._grid = js.GridStack.init(
                options, js.document.querySelector("#ix-grid-host .grid-stack")
            )
            # Fitting during the drag is wasted work; the pane is mid-flight and
            # every intermediate size is thrown away.
            for event in ("resizestop", "dragstop", "change"):
                proxy = create_proxy(lambda *_args: self._on_grid_change())
                self._grid_proxies.append(proxy)
                self._grid.on(event, proxy)

        self._mode = "tiled"
        for pane_id in list(self._panes):
            self._attach_to_grid(pane_id)
        self._sync_mode_buttons()
        self._fit_visible_panes()
        self._schedule_layout_save()

    def _go_tabbed(self) -> None:
        # A maximized tile means nothing in a tabbed workspace, and leaving the
        # flag set would restore into a pane that is no longer in the grid.
        if self._maximized:
            self._toggle_maximize(self._maximized)
        for pane_id in list(self._panes):
            root = js.document.querySelector(f'.ix-pane[data-pane="{pane_id}"]')
            cell = self.tabs.get_cell(pane_id)
            target = cell.getContainer() if hasattr(cell, "getContainer") else cell
            if root and target:
                target.appendChild(root)
            self._remove_grid_item(pane_id)

        js.document.getElementById("ix-grid-host").hidden = True
        js.document.getElementById("ix-tabs-host").hidden = False
        self._mode = "tabbed"
        self._sync_mode_buttons()
        self._fit_visible_panes()
        self._schedule_layout_save()

    def _toggle_maximize(self, pane_id: str | None) -> None:
        """
        Zoom one tile to fill the workspace, or restore it.

        Deliberately presentational: the tile is overlaid with CSS and the grid
        model is not touched, so every other tile keeps its position and the
        saved layout is unaffected. Resizing the item to full width instead
        would reflow its neighbours and persist that reflow.
        """
        if pane_id not in self._panes:
            return
        target = None if self._maximized == pane_id else pane_id

        for candidate in self._panes:
            item = js.document.querySelector(
                f'#ix-grid-host .grid-stack-item[data-pane="{candidate}"]'
            )
            button = js.document.querySelector(f'[data-pane-max="{candidate}"]')
            on = candidate == target
            if item:
                if on:
                    item.dataset.maximized = "true"
                else:
                    item.removeAttribute("data-maximized")
            if button:
                button.setAttribute("aria-pressed", "true" if on else "false")
                button.title = "Restore this pane" if on else "Maximize this pane"
                icon = button.querySelector(".mdi")
                if icon:
                    icon.className = (
                        "mdi mdi-arrow-collapse" if on else "mdi mdi-arrow-expand"
                    )

        self._maximized = target
        if target:
            # A grid taller than its host can be scrolled; the maximized tile
            # is pinned to the top of it, so scroll there or it opens offscreen.
            host = js.document.getElementById("ix-grid-host")
            if host:
                host.scrollTop = 0
        self._fit_visible_panes()

    def _close_pane(self, pane_id: str) -> None:
        """
        Close from the pane's own button.

        The TabWidget emits `close` from its close button, not from
        removeTab, so the teardown has to be called directly or a tiled close
        would leak the terminal and its socket.
        """
        self._on_tab_close({"id": pane_id})
        self.tabs.remove_tab(pane_id)

    def _attach_to_grid(self, pane_id: str) -> None:
        """Give a pane a grid item and move its root inside it."""
        if self._grid is None:
            return
        root = js.document.querySelector(f'.ix-pane[data-pane="{pane_id}"]')
        if not root:
            return

        widget = js.document.createElement("div")
        widget.className = "grid-stack-item"
        geometry = self._panes.get(pane_id, {}).get("geometry") or {}
        widget.setAttribute("gs-w", str(geometry.get("w") or 6))
        widget.setAttribute("gs-h", str(geometry.get("h") or 7))
        if geometry.get("x") is not None and geometry.get("y") is not None:
            widget.setAttribute("gs-x", str(geometry["x"]))
            widget.setAttribute("gs-y", str(geometry["y"]))
        else:
            widget.setAttribute("gs-auto-position", "true")
        widget.dataset.pane = pane_id
        content = js.document.createElement("div")
        content.className = "grid-stack-item-content"
        widget.appendChild(content)

        # Move the pane root in first, then hand the finished element to
        # GridStack. `addWidget(HTMLElement)` was dropped in v11 — it warns and
        # builds its own element, which would leave this pane in a detached
        # node. `makeWidget` adopts an element that is already in the grid.
        content.appendChild(root)
        js.document.querySelector("#ix-grid-host .grid-stack").appendChild(widget)
        self._grid.makeWidget(widget)

    def _remove_grid_item(self, pane_id: str) -> None:
        if self._grid is None:
            return
        item = js.document.querySelector(
            f'#ix-grid-host .grid-stack-item[data-pane="{pane_id}"]'
        )
        if item:
            # removeDOM, but the pane root has already been moved out by the
            # caller when switching modes.
            self._grid.removeWidget(item, True)

    def _on_grid_change(self) -> None:
        self._fit_visible_panes()
        self._schedule_layout_save()

    def _fit_visible_panes(self) -> None:
        for pane in self._panes.values():
            if pane["tab"] == "terminal" and pane["terminal"] is not None:
                pane["terminal"].fit()

    # ------------------------------------------------------------------
    # Layout persistence
    # ------------------------------------------------------------------

    def _schedule_layout_save(self) -> None:
        """
        Coalesce saves.

        A grid drag fires `change` continuously and every pane tab click is a
        layout change too; without this, one drag would be a burst of BFF
        writes for a value only the last of which matters.
        """
        if self._restoring:
            return
        if self._save_task is not None:
            self._save_task.cancel()
        self._save_task = asyncio.ensure_future(self._save_layout_soon())

    async def _save_layout_soon(self) -> None:
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        self._save_task = None
        try:
            await LayoutService().save_async(
                mode=self._mode, panes=self._collect_layout()
            )
        except Exception as error:  # a failed save must never break the UI
            print(f"[layout] save failed: {error}", flush=True)

    def _collect_layout(self) -> list:
        panes = []
        for pane_id, pane in self._panes.items():
            geometry = dict(pane.get("geometry") or {})
            item = js.document.querySelector(
                f'#ix-grid-host .grid-stack-item[data-pane="{pane_id}"]'
            )
            # gridstackNode is where GridStack keeps the live position; the
            # gs-* attributes lag behind a drag.
            node = getattr(item, "gridstackNode", None) if item else None
            if node:
                geometry = {
                    "x": int(node.x or 0),
                    "y": int(node.y or 0),
                    "w": int(node.w or 6),
                    "h": int(node.h or 7),
                }
                pane["geometry"] = geometry

            sftp = self._sftp_tabs.get(pane_id) or {}
            panes.append(
                {
                    "session_id": pane["session_id"],
                    "x": geometry.get("x", 0),
                    "y": geometry.get("y", 0),
                    "w": geometry.get("w", 6),
                    "h": geometry.get("h", 7),
                    "tab": pane["tab"],
                    "path": sftp.get("path", ""),
                }
            )
        return panes

    async def _restore_layout(self) -> None:
        result = await LayoutService().get_async()
        if not result.get("ok"):
            return
        specs = result.get("panes") or []
        if not specs:
            return

        self._restoring = True
        try:
            if result.get("mode") == "tiled":
                await self._go_tiled()
            for spec in specs:
                self._open_pane(
                    int(spec["session_id"]),
                    focus=spec.get("tab", "terminal"),
                    connect=False,
                    restore=spec,
                )
        finally:
            self._restoring = False

        restored = len(self._panes)
        if restored:
            self._toast(
                f"Restored {restored} connection(s). Click Reconnect to dial."
            )

    def _sync_mode_buttons(self) -> None:
        for mode in ("tiled", "tabbed"):
            button = js.document.querySelector(f'[data-mode="{mode}"]')
            if button:
                button.setAttribute(
                    "aria-selected", "true" if mode == self._mode else "false"
                )

    # ------------------------------------------------------------------
    # SFTP
    # ------------------------------------------------------------------

    def _open_sftp(self, session_id: int) -> None:
        """
        Show a session's files.

        Reuses an open pane for that session rather than dialling a second
        connection just to browse; only when nothing is open does it start one.
        """
        session = self._session(session_id)
        if session is None:
            return
        if not session_caps(session.get("type"))["files"]:
            self._toast("This connection has no file browser.")
            return

        existing = next(
            (
                pane_id
                for pane_id, pane in self._panes.items()
                if pane["session_id"] == session_id
            ),
            None,
        )
        if existing:
            self.tabs.set_active(existing)
            self._pane_select(existing, "files")
            return

        self._open_pane(session_id, focus="files")

    def _build_sftp_panel(self, cell, tab_id: str) -> dict:
        """
        Build the SFTP panel chrome inside a pane's Files panel.

        The breadcrumb bar is plain HTML because wapyt has no breadcrumb widget
        and one path strip does not justify inventing one.
        """
        container = cell.getContainer() if hasattr(cell, "getContainer") else cell
        container.innerHTML = (
            f'<div class="ix-sftp">'
            f'  <div class="ix-sftp-bar">'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="upload" data-tab="{tab_id}"'
            f'            title="Browse and pick individual files. Ctrl-click or '
            f'shift-click to select several.">'
            f'      <span class="mdi mdi-file-upload"></span><span>Upload files…</span></button>'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="upload-folder" data-tab="{tab_id}"'
            f'            title="Upload an entire folder and everything inside it, '
            f'keeping its structure.">'
            f'      <span class="mdi mdi-folder-upload"></span><span>Upload folder…</span></button>'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="download" data-tab="{tab_id}"'
            f'            title="Download the selected files or folders.">'
            f'      <span class="mdi mdi-download"></span><span>Download</span></button>'
            f'    <span class="ix-sftp-sep"></span>'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="mkdir" data-tab="{tab_id}"'
            f'            title="Create a folder here.">'
            f'      <span class="mdi mdi-folder-plus"></span><span>New folder</span></button>'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="refresh" data-tab="{tab_id}"'
            f'            title="Re-read this directory.">'
            f'      <span class="mdi mdi-refresh"></span><span>Refresh</span></button>'
            f'    <span class="ix-sftp-spacer"></span>'
            f'    <span class="ix-sftp-note" id="note-{tab_id}">'
            f'      <span class="mdi mdi-alert ix-sftp-note-icon"></span>'
            f'      <span class="ix-sftp-note-text"></span></span>'
            f'  </div>'
            f'  <div class="ix-crumbs" id="crumbs-{tab_id}"></div>'
            f'  <div class="ix-sftp-table" id="table-{tab_id}"></div>'
            f'  <div class="ix-queue" id="queue-{tab_id}" hidden>'
            f'    <div class="ix-queue-head">'
            f'      <span class="ix-queue-title">Transfers</span>'
            f'      <button type="button" class="ix-queue-clear" data-sftp="clear-queue" '
            f'              data-tab="{tab_id}">Clear finished</button>'
            f'    </div>'
            f'    <div class="ix-queue-rows" id="queue-rows-{tab_id}"></div>'
            f'  </div>'
            f"</div>"
        )
        crumbs = js.document.getElementById(f"crumbs-{tab_id}")

        # Scrolling to the tail on navigation is not enough once panes can be
        # resized: a narrower pane keeps the old scroll offset and leaves the
        # middle of the path showing. Re-pin on resize, which is what a grid
        # drag does continuously.
        def _pin_crumbs(*_args) -> None:
            crumbs.scrollLeft = crumbs.scrollWidth

        observer = js.ResizeObserver.new(create_proxy(_pin_crumbs))
        observer.observe(crumbs)
        self._crumb_observers[tab_id] = observer

        return {
            "crumbs": crumbs,
            "table_cell": js.document.getElementById(f"table-{tab_id}"),
        }

    async def _sftp_navigate(self, tab_id: str, path: str) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        entry["table"].set_busy(True)
        try:
            result = await SFTPService().list_dir_async(entry["session_id"], path)
        finally:
            entry["table"].set_busy(False)

        if result.get("ok") is False:
            entry["table"].set_rows([])
            entry["table"].set_empty_text(result.get("error", "Could not list directory"))
            self._toast(result.get("error", "Could not list directory"))
            return

        entry["path"] = result["path"]
        entry["table"].set_rows(result["entries"])
        self._schedule_layout_save()
        self._render_crumbs(tab_id, result["path"])
        self._show_picker_note(tab_id)

    def _show_picker_note(self, tab_id: str) -> None:
        """
        Say up front when downloads cannot choose a destination.

        The text lives in its own span so a narrow pane can drop it and leave
        the warning icon, which carries the same words as a tooltip. Losing the
        warning entirely would be worse than losing the space.
        """
        note = js.document.getElementById(f"note-{tab_id}")
        if not note:  # JsNull, not None, when absent
            return
        text_el = note.querySelector(".ix-sftp-note-text")
        if not text_el or text_el.textContent:
            return
        caps = filetransfer.capabilities()
        if caps.pickers:
            return
        message = (
            "This browser cannot choose a download location — files go to your "
            "downloads folder. Chrome or Edge can."
            if caps.secure_context
            else "Destination picking needs HTTPS; downloads go to your "
                 "downloads folder."
        )
        text_el.textContent = message
        note.title = message
        note.dataset.shown = "true"

    def _render_crumbs(self, tab_id: str, path: str) -> None:
        holder = js.document.getElementById(f"crumbs-{tab_id}")
        if not holder:  # JsNull, not None, when absent
            return

        holder.innerHTML = ""
        up = js.document.createElement("button")
        up.className = "ix-crumb ix-crumb-up"
        up.innerHTML = '<span class="mdi mdi-arrow-up"></span>'
        up.title = "Parent directory"
        up.addEventListener(
            "click",
            create_proxy(
                lambda _event: _spawn(
                    self._sftp_navigate(tab_id, parent_path(path)), "sftp navigate"
                )
            ),
        )
        holder.appendChild(up)

        for crumb in breadcrumbs(path):
            button = js.document.createElement("button")
            button.className = "ix-crumb"
            # textContent, not innerHTML: a remote directory can be named
            # anything at all.
            button.textContent = crumb["label"]
            target = crumb["path"]
            button.addEventListener(
                "click",
                create_proxy(
                    lambda _event, target=target: _spawn(
                        self._sftp_navigate(tab_id, target), "sftp navigate"
                    )
                ),
            )
            holder.appendChild(button)

        # The strip scrolls rather than wrapping, so a deep path in a narrow
        # pane costs one line instead of four. Scroll to the end: the directory
        # you are actually in is the last crumb, and it is the one worth seeing.
        holder.scrollLeft = holder.scrollWidth

    def _sftp_activate(self, tab_id: str, payload: dict) -> None:
        row = payload.get("row") or {}
        if row.get("is_dir"):
            _spawn(self._sftp_navigate(tab_id, row["id"]), "sftp navigate")
        else:
            self._sftp_download(tab_id, [row["id"]])

    def _sftp_action(self, tab_id: str, payload: dict) -> None:
        action = payload.get("action")
        selected = payload.get("selected") or []
        if not selected:
            return
        if action == "download":
            self._sftp_download(tab_id, selected)
        elif action == "delete":
            _spawn(self._sftp_delete(tab_id, selected), "sftp delete")
        elif action == "rename":
            self._sftp_rename(tab_id, selected[0])

    # ── Transfers ──────────────────────────────────────────────────────────
    #
    # Bytes never cross the FFI. wapyt.filetransfer pipes the network stream
    # straight into a chosen file handle, and uploads go from a File object to
    # XMLHttpRequest. Python picks what moves and renders progress.

    def _sftp_click(self, action: str, tab_id: str) -> None:
        """
        SFTP toolbar dispatch.

        The picker calls below MUST be the first await in their coroutine: the
        File System Access API needs transient user activation, and the first
        await consumes it. Anything fetched before the picker — a listing, a BFF
        call — loses the activation and the browser raises SecurityError on a
        perfectly valid setup.
        """
        if action == "upload":
            _spawn(self._upload_picked(tab_id, directory=False), "sftp upload")
        elif action == "upload-folder":
            _spawn(self._upload_picked(tab_id, directory=True), "sftp upload folder")
        elif action == "download":
            entry = self._sftp_tabs.get(tab_id)
            selected = entry["table"].get_selected_ids() if entry else []
            if not selected:
                self._toast("Select something to download first.")
                return
            _spawn(self._download(tab_id, selected), "sftp download")
        elif action == "mkdir":
            self._sftp_mkdir(tab_id)
        elif action == "refresh":
            entry = self._sftp_tabs.get(tab_id)
            if entry:
                _spawn(self._sftp_navigate(tab_id, entry["path"]), "sftp refresh")
        elif action == "clear-queue":
            self._queue_clear_finished(tab_id)

    def _download_url(self, session_id: int, path: str) -> str:
        return (
            f"/files/{session_id}/download?path={js.encodeURIComponent(path)}"
        )

    async def _download(self, tab_id: str, paths: list) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return

        rows = [entry["table"].get_row(p) or {} for p in paths]
        files = [r for r in rows if r and not r.get("is_dir")]
        folders = [r for r in rows if r and r.get("is_dir")]
        caps = filetransfer.capabilities()

        # No pickers (Firefox, or a misconfigured non-secure origin): fall back
        # to the browser's download directory and say so, rather than silently
        # doing something different. The original app learned this the hard way
        # — a silent fallback produced one Save-As dialog per file.
        if not caps.pickers:
            if folders:
                self._toast(
                    "This browser cannot choose a destination folder, so whole "
                    "folders cannot be downloaded. Chrome or Edge can."
                )
                return
            for row in files:
                filetransfer.download_via_anchor(
                    self._download_url(entry["session_id"], row["id"]), row["name"]
                )
            self._toast(f"Sent {len(files)} file(s) to your downloads folder.")
            return

        # One plain file gets a Save dialog with a pre-filled name; anything
        # else needs a destination folder to write into.
        if len(files) == 1 and not folders:
            row = files[0]
            chosen = await filetransfer.pick_save_file(row["name"])
            if not chosen.ok:
                if not chosen.cancelled:
                    self._toast(chosen.error or "Could not open the save dialog.")
                return
            await self._run_download_queue(
                tab_id, [(row["id"], row["name"], chosen.id, None)]
            )
            filetransfer.release(chosen.id)
            return

        chosen = await filetransfer.pick_folder()
        if not chosen.ok:
            if not chosen.cancelled:
                self._toast(chosen.error or "Could not open the folder picker.")
            return

        jobs = [(row["id"], row["name"], None, row["name"]) for row in files]
        for folder in folders:
            jobs.extend(await self._expand_folder(entry["session_id"], folder))

        if not jobs:
            self._toast("Nothing to download.")
            filetransfer.release(chosen.id)
            return

        await self._run_download_queue(tab_id, jobs, folder_id=chosen.id)
        filetransfer.release(chosen.id)

    async def _expand_folder(self, session_id: int, folder: dict) -> list:
        """
        Walk a remote folder, streaming entries as they are found.

        SFTPService.walk is a @bff_stream, so the count moves while the remote
        walk is still running instead of hanging on a silent traversal.
        """
        parent = folder["id"].rstrip("/")
        base = folder["name"]
        jobs: list = []
        async for item in SFTPService().walk(session_id, folder["id"]):
            if item.get("error"):
                self._toast(item["error"].get("message", "Walk failed"))
                continue
            if item.get("done"):
                break
            remote = item["path"]
            relative = remote[len(parent) + 1:] if remote.startswith(parent + "/") else remote.rsplit("/", 1)[-1]
            jobs.append((remote, relative.rsplit("/", 1)[-1], None, f"{base}/{relative}"))
            self._toast(f"Scanning {base}… {len(jobs)} file(s)")
        return jobs

    async def _run_download_queue(
        self, tab_id: str, jobs: list, folder_id: str = ""
    ) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        session_id = entry["session_id"]
        done = 0

        for remote, label, file_handle, relative in jobs:
            transfer_id = self._queue_add(tab_id, label, "download")
            url = self._download_url(session_id, remote)
            progress = self._queue_progress(tab_id, transfer_id)

            if file_handle:
                outcome = await filetransfer.save_file(
                    file_handle, url, transfer_id, progress
                )
            else:
                outcome = await filetransfer.save_into(
                    folder_id, relative, url, transfer_id, progress
                )

            if outcome.ok:
                done += 1
                self._queue_finish(tab_id, transfer_id, "done")
            elif outcome.cancelled:
                self._queue_finish(tab_id, transfer_id, "cancelled")
            else:
                self._queue_finish(tab_id, transfer_id, "failed", outcome.error)

        self._toast(f"Downloaded {done} of {len(jobs)} file(s).")

    # ── Upload ─────────────────────────────────────────────────────────────

    async def _upload_picked(self, tab_id: str, directory: bool) -> None:
        # First await in the handler, while user activation still holds.
        chosen = await filetransfer.pick_files(multiple=True, directory=directory)
        if not chosen.ok:
            if not chosen.cancelled:
                self._toast(chosen.error or "Could not open the file picker.")
            return
        await self._run_upload_queue(tab_id, chosen.files)

    def _sftp_upload(self, tab_id: str, payload: dict) -> None:
        """Drag-and-drop upload: the table already holds the File handles."""
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        dropped = entry["table"].get_dropped_files()
        count = int(dropped.length) if hasattr(dropped, "length") else len(dropped)
        picked = [
            filetransfer.PickedFile(
                id=filetransfer.adopt(dropped[i]),
                name=dropped[i].name,
                size=int(dropped[i].size),
                path=dropped[i].name,
            )
            for i in range(count)
        ]
        _spawn(self._run_upload_queue(tab_id, picked), "sftp drop upload")

    async def _run_upload_queue(self, tab_id: str, files: list) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None or not files:
            return
        url = f"/files/{entry['session_id']}/upload"
        done = 0

        for picked in files:
            if not picked.id:
                continue
            transfer_id = self._queue_add(tab_id, picked.name, "upload")
            fields = {"path": entry["path"]}
            # A directory pick carries the folder structure; the route recreates
            # it remotely rather than flattening everything into one directory.
            if picked.path and picked.path != picked.name:
                fields["relative_path"] = picked.path

            outcome = await filetransfer.upload(
                url, picked.id, fields, transfer_id,
                self._queue_progress(tab_id, transfer_id),
            )
            filetransfer.release(picked.id)

            if outcome.ok:
                done += 1
                self._queue_finish(tab_id, transfer_id, "done")
            elif outcome.cancelled:
                self._queue_finish(tab_id, transfer_id, "cancelled")
            else:
                self._queue_finish(tab_id, transfer_id, "failed", outcome.error)

        self._toast(f"Uploaded {done} of {len(files)} file(s).")
        await self._sftp_navigate(tab_id, entry["path"])

    def _sftp_mkdir(self, tab_id: str) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        name = js.prompt("New folder name:")
        if not name:
            return

        async def _run() -> None:
            result = await SFTPService().mkdir_async(
                entry["session_id"], entry["path"], name
            )
            if not result.get("ok"):
                self._toast(result.get("error", "Could not create folder"))
            await self._sftp_navigate(tab_id, entry["path"])

        _spawn(_run(), "sftp mkdir")

    # ── Transfer queue ─────────────────────────────────────────────────────
    #
    # Hand-built rather than a DataTable: progress updates land several times a
    # second, and DataTable re-renders every row on set_rows, which would thrash
    # the whole table. Here each row's own bar is mutated in place.

    def _queue_add(self, tab_id: str, label: str, kind: str) -> str:
        self._transfer_seq = getattr(self, "_transfer_seq", 0) + 1
        transfer_id = f"tx{self._transfer_seq}"

        holder = js.document.getElementById(f"queue-{tab_id}")
        rows = js.document.getElementById(f"queue-rows-{tab_id}")
        if not holder or not rows:
            return transfer_id
        holder.hidden = False

        row = js.document.createElement("div")
        row.className = "ix-queue-row"
        row.id = f"{tab_id}-{transfer_id}"

        icon = js.document.createElement("span")
        icon.className = "mdi mdi-" + ("upload" if kind == "upload" else "download")
        row.appendChild(icon)

        name = js.document.createElement("span")
        name.className = "ix-queue-name"
        name.textContent = label          # remote-controlled: never innerHTML
        name.title = label
        row.appendChild(name)

        track = js.document.createElement("span")
        track.className = "ix-queue-track"
        bar = js.document.createElement("span")
        bar.className = "ix-queue-bar"
        bar.id = f"bar-{tab_id}-{transfer_id}"
        track.appendChild(bar)
        row.appendChild(track)

        status = js.document.createElement("span")
        status.className = "ix-queue-status"
        status.id = f"st-{tab_id}-{transfer_id}"
        status.textContent = "starting…"
        row.appendChild(status)

        stop = js.document.createElement("button")
        stop.type = "button"
        stop.className = "ix-queue-cancel"
        stop.textContent = "×"
        stop.title = "Cancel"
        stop.addEventListener(
            "click", create_proxy(lambda _e: filetransfer.cancel(transfer_id))
        )
        row.appendChild(stop)

        rows.appendChild(row)
        return transfer_id

    def _queue_progress(self, tab_id: str, transfer_id: str):
        def _update(seen: int, total: int) -> None:
            bar = js.document.getElementById(f"bar-{tab_id}-{transfer_id}")
            status = js.document.getElementById(f"st-{tab_id}-{transfer_id}")
            if total:
                pct = max(0, min(100, int(seen * 100 / total)))
                if bar:
                    bar.style.width = f"{pct}%"
                if status:
                    status.textContent = f"{pct}%  {format_size(seen)}"
            elif status:
                # No Content-Length: report bytes moved instead of a percentage.
                status.textContent = format_size(seen)

        return _update

    def _queue_finish(
        self, tab_id: str, transfer_id: str, state: str, detail: str = ""
    ) -> None:
        row = js.document.getElementById(f"{tab_id}-{transfer_id}")
        bar = js.document.getElementById(f"bar-{tab_id}-{transfer_id}")
        status = js.document.getElementById(f"st-{tab_id}-{transfer_id}")
        if row:
            row.dataset.state = state
        if bar and state == "done":
            bar.style.width = "100%"
        if status:
            status.textContent = detail or state
            if detail:
                status.title = detail

    def _queue_clear_finished(self, tab_id: str) -> None:
        rows = js.document.getElementById(f"queue-rows-{tab_id}")
        holder = js.document.getElementById(f"queue-{tab_id}")
        if not rows:
            return
        for row in list(rows.children):
            if row.dataset.state:
                row.remove()
        if holder and rows.children.length == 0:
            holder.hidden = True

    async def _sftp_delete(self, tab_id: str, paths: list) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        count = len(paths)
        label = paths[0].rsplit("/", 1)[-1] if count == 1 else f"{count} items"
        if not js.confirm(f"Delete {label}? This cannot be undone."):
            return
        result = await SFTPService().delete_async(entry["session_id"], paths)
        if result.get("failed"):
            self._toast(f"{len(result['failed'])} item(s) could not be deleted.")
        await self._sftp_navigate(tab_id, entry["path"])

    def _sftp_rename(self, tab_id: str, path: str) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        current = path.rsplit("/", 1)[-1]
        new_name = js.prompt("New name:", current)
        if not new_name or new_name == current:
            return

        async def _run() -> None:
            result = await SFTPService().rename_async(
                entry["session_id"], path, new_name
            )
            if not result.get("ok"):
                self._toast(result.get("error", "Rename failed"))
            await self._sftp_navigate(tab_id, entry["path"])

        _spawn(_run(), "sftp rename")

    # ------------------------------------------------------------------
    # Session editor
    # ------------------------------------------------------------------

    async def _session_editor(self, session_id: int | None) -> None:
        existing: dict = {}
        if session_id:
            existing = await SessionService().get_async(session_id)
            if not existing:
                self._toast("Session not found.")
                return

        modal = ModalWindow(
            ModalConfig(
                title="Edit session" if session_id else "New session",
                width=560,
                # What the nine fields plus the action row actually measure.
                # At 640 the Save button sat below the fold; short screens
                # still clamp to max-height and scroll.
                height=740,
            )
        )

        has_password = existing.get("has_password")
        has_key = existing.get("has_private_key")
        secret_help = (
            "Leave blank to keep the stored value."
            if (has_password or has_key)
            else None
        )

        form = Form(
            FormConfig(
                columns=2,
                submit_text="Save",
                cancel_text="Cancel",
                fields=[
                    FieldConfig(id="name", label="Name", required=True,
                                value=existing.get("name", ""), span=2),
                    FieldConfig(id="folder", label="Folder",
                                value=existing.get("folder", ""),
                                placeholder="Ungrouped"),
                    FieldConfig(id="session_type", label="Type", type="select",
                                value=existing.get("type", "ssh"),
                                options=[SelectOption(key, caps["label"])
                                         for key, caps in SESSION_TYPES.items()]),
                    FieldConfig(id="host", label="Host", required=True,
                                value=existing.get("host", "")),
                    FieldConfig(id="port", label="Port", type="number",
                                value=existing.get("port", 22), min=1, max=65535),
                    FieldConfig(id="username", label="Username",
                                value=existing.get("username", ""), span=2,
                                autocomplete="off"),
                    FieldConfig(id="password", label="Password", type="password",
                                span=2, autocomplete="new-password",
                                help=secret_help,
                                placeholder="••••••" if has_password else ""),
                    FieldConfig(id="private_key", label="Private key (PEM)",
                                type="textarea", rows=5, span=2,
                                # Not DSA: paramiko 5 dropped DSSKey, and
                                # ssh.py builds its loader list from what the
                                # installed paramiko actually exposes.
                                help="RSA, Ed25519 or ECDSA."
                                     + (" Stored key in place." if has_key else ""),
                                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                                            if not has_key else "Stored — leave blank to keep"),
                    FieldConfig(id="description", label="Description",
                                value=existing.get("description", ""), span=2),
                ],
            ),
            container=modal.body,
        )

        def _on_type_change(payload: dict) -> None:
            if payload.get("id") != "session_type":
                return
            # Each type has its own default port. Telnet has no user or key
            # auth, and FTP has a user and password but no key.
            session_type = payload.get("value") or "ssh"
            form.set_values({"port": session_caps(session_type)["port"]})
            for field in ("password", "username"):
                form.set_field_disabled(field, session_type == "telnet")
            form.set_field_disabled("private_key", session_type in ("telnet", "ftp"))

        form.on_change(_on_type_change)
        form.on_cancel(lambda _payload: modal.hide())

        def _on_submit(values: dict) -> None:
            _spawn(_save(values), "session save")

        async def _save(values: dict) -> None:
            form.set_busy(True)
            try:
                payload = {
                    "session_id": session_id,
                    "name": values.get("name", ""),
                    "host": values.get("host", ""),
                    "port": int(values.get("port") or 22),
                    "username": values.get("username") or "",
                    "session_type": values.get("session_type", "ssh"),
                    "folder": values.get("folder") or "",
                    "description": values.get("description") or "",
                }
                # An empty secret field means "unchanged" when one is stored,
                # and the service's sentinel default expresses that by omission.
                if values.get("password"):
                    payload["password"] = values["password"]
                elif not has_password:
                    payload["password"] = ""
                if values.get("private_key"):
                    payload["private_key"] = values["private_key"]
                elif not has_key:
                    payload["private_key"] = ""

                result = await SessionService().save_async(**payload)
                if not result.get("ok"):
                    if result.get("errors"):
                        form.set_errors(result["errors"])
                    else:
                        form.set_error(None, result.get("error", "Could not save"))
                    return
                modal.hide()
                await self._reload_sessions()
            finally:
                form.set_busy(False)

        form.on_submit(_on_submit)
        modal.show()
        form.focus_first()

    async def _confirm_delete(self, session_id: int) -> None:
        session = self._session(session_id)
        if session is None:
            return
        if not js.confirm(f"Delete session “{session['name']}”?"):
            return
        result = await SessionService().delete_async(session_id)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not delete"))
            return
        self._selected_id = None
        await self._reload_sessions()

    async def _forget_host_key(self, session_id: int) -> None:
        await SessionService().clear_host_key_async(session_id)
        self._toast("Host key cleared; it will be re-pinned on next connect.")
        await self._reload_sessions()

    # ------------------------------------------------------------------
    # Account dialogs
    # ------------------------------------------------------------------

    def _password_dialog(self) -> None:
        modal = ModalWindow(ModalConfig(title="Change password", width=440, height=360))
        form = Form(
            FormConfig(
                submit_text="Change password",
                cancel_text="Cancel",
                fields=[
                    FieldConfig(id="current_password", label="Current password",
                                type="password", required=True,
                                autocomplete="current-password"),
                    FieldConfig(id="new_password", label="New password",
                                type="password", required=True, min_length=8,
                                autocomplete="new-password"),
                    FieldConfig(id="confirm", label="Confirm new password",
                                type="password", required=True,
                                matches="new_password",
                                matches_message="Passwords do not match",
                                autocomplete="new-password"),
                ],
            ),
            container=modal.body,
        )
        form.on_cancel(lambda _payload: modal.hide())

        async def _submit(values: dict) -> None:
            form.set_busy(True)
            try:
                result = await UserService().change_own_password_async(
                    values["current_password"], values["new_password"]
                )
                if not result.get("ok"):
                    if result.get("errors"):
                        form.set_errors(result["errors"])
                    else:
                        form.set_error(None, result.get("error", "Could not change"))
                    return
                modal.hide()
                self._toast("Password changed.")
            finally:
                form.set_busy(False)

        form.on_submit(lambda values: _spawn(_submit(values), "password change"))
        modal.show()
        form.focus_first()

    async def _admin_panel(self) -> None:
        if not self._me.get("is_admin"):
            self._toast("Administrator access required.")
            return

        modal = ModalWindow(ModalConfig(title="Users", width=760, height=560))
        container = modal.body
        container.innerHTML = (
            f'<div class="ix-admin">'
            f'  <div class="ix-admin-table" id="ix-admin-table"></div>'
            f'  <div class="ix-admin-form" id="ix-admin-form"></div>'
            f"</div><style>{_ADMIN_CSS}</style>"
        )

        table = DataTable(
            DataTableConfig(
                columns=[
                    ColumnConfig(id="username", header="Username"),
                    ColumnConfig(id="role", header="Role", width=90),
                    ColumnConfig(id="session_count", header="Sessions", width=90, align="right"),
                    ColumnConfig(id="created_at", header="Created", width=110),
                ],
                selection="single",
                empty_text="No users",
                context_actions=[
                    TableAction("toggle_admin", "Toggle admin", "mdi-shield-account"),
                    TableAction("reset", "Reset password", "mdi-lock-reset"),
                    TableAction(separator=True),
                    TableAction("delete", "Delete user", "mdi-delete", danger=True),
                ],
            ),
            container=js.document.getElementById("ix-admin-table"),
        )

        async def _refresh() -> None:
            users = await UserService().list_async()
            table.set_rows(
                [{**user, "role": "admin" if user["is_admin"] else "user"} for user in users]
            )

        def _on_action(payload: dict) -> None:
            user_id = payload.get("id")
            row = payload.get("row") or {}
            action = payload.get("action")

            async def _run() -> None:
                service = UserService()
                if action == "delete":
                    if not js.confirm(
                        f"Delete “{row.get('username')}” and all their saved sessions?"
                    ):
                        return
                    result = await service.delete_async(int(user_id))
                elif action == "toggle_admin":
                    result = await service.set_admin_async(
                        int(user_id), not row.get("is_admin")
                    )
                elif action == "reset":
                    new_password = js.prompt(
                        f"New password for {row.get('username')} (min 8 chars):"
                    )
                    if not new_password:
                        return
                    result = await service.reset_password_async(
                        int(user_id), new_password
                    )
                else:
                    return

                if not result.get("ok"):
                    self._toast(
                        result.get("error")
                        or "; ".join((result.get("errors") or {}).values())
                        or "Action failed"
                    )
                await _refresh()

            _spawn(_run(), f"admin {action}")

        table.on_action(_on_action)

        create_form = Form(
            FormConfig(
                columns=3,
                submit_text="Add user",
                fields=[
                    FieldConfig(id="username", label="New user", required=True),
                    FieldConfig(id="password", label="Password", type="password",
                                required=True, min_length=8),
                    FieldConfig(id="is_admin", label="Administrator", type="checkbox"),
                ],
            ),
            container=js.document.getElementById("ix-admin-form"),
        )

        async def _create(values: dict) -> None:
            create_form.set_busy(True)
            try:
                result = await UserService().create_async(
                    values["username"], values["password"], bool(values.get("is_admin"))
                )
                if not result.get("ok"):
                    if result.get("errors"):
                        create_form.set_errors(result["errors"])
                    else:
                        create_form.set_error(None, result.get("error", "Failed"))
                    return
                create_form.set_values({"username": "", "password": "", "is_admin": False})
                await _refresh()
            finally:
                create_form.set_busy(False)

        create_form.on_submit(lambda values: _spawn(_create(values), "create user"))

        modal.show()
        await _refresh()

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def _toast(self, message: str) -> None:
        holder = js.document.getElementById("ix-toast")
        if not holder:  # JsNull, not None, when absent
            holder = js.document.createElement("div")
            holder.id = "ix-toast"
            holder.className = "ix-toast"
            js.document.body.appendChild(holder)
            style = js.document.createElement("style")
            style.textContent = _TOAST_CSS
            js.document.head.appendChild(style)
        holder.textContent = message
        holder.dataset.visible = "true"
        js.window.clearTimeout(getattr(self, "_toast_timer", 0) or 0)
        self._toast_timer = js.window.setTimeout(
            create_proxy(lambda: holder.removeAttribute("data-visible")), 4000
        )


_WORKSPACE_CSS = """
/* :empty matches when the tab strip holds no panels — pseudo-elements do not
   count as children — so the hint appears with no state to track. */
.wapyt-tabwidget-panels:empty::after{
  content:"Double-click a session in the sidebar to open a terminal.";
  display:flex;align-items:center;justify-content:center;height:100%;
  color:#64748b;font:14px system-ui,sans-serif;text-align:center;padding:0 24px;
}
.wapyt-tabwidget-tabs:empty{display:none;}
"""

_TOOLBAR_CSS = """
.ix-toolbar{display:flex;align-items:center;gap:4px;padding:6px 10px;height:100%;
  background:#111827;border-bottom:1px solid #1f2937;font:13px system-ui,sans-serif;}
.ix-toolbar-btn{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;
  color:#cbd5f5;background:transparent;border:1px solid transparent;border-radius:6px;
  cursor:pointer;font:inherit;}
.ix-toolbar-btn:hover{background:#1f2937;border-color:#334155;}
.ix-toolbar-btn .mdi{font-size:16px;}
.ix-toolbar-sep{width:1px;height:20px;margin:0 6px;background:#334155;}
.ix-toolbar-spacer{flex:1 1 auto;}
.ix-toolbar-user{color:#64748b;font-size:12px;padding-right:6px;}
"""

_SIDEBAR_CSS = """
.ix-sidebar{display:flex;flex-direction:column;height:100%;min-height:0;}
.ix-brand{display:flex;align-items:center;gap:10px;padding:10px 12px;flex:0 0 auto;
  background:#111827;border-bottom:1px solid #1f2937;}
.ix-brand-logo{flex:0 0 auto;width:40px;height:40px;border-radius:50%;
  object-fit:cover;border:1px solid #334155;background:#0f172a;}
.ix-brand-text{display:flex;flex-direction:column;min-width:0;line-height:1.25;}
.ix-brand-name{font:600 14px system-ui,sans-serif;color:#e2e8f0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ix-brand-sub{font:10.5px system-ui,sans-serif;color:#64748b;letter-spacing:.03em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ix-sidebar-tree{flex:1 1 auto;min-height:0;}
"""

_GRID_CSS = """
.ix-workspace{position:relative;height:100%;min-height:0;}
.ix-host{position:absolute;inset:0;min-height:0;}
.ix-host[hidden]{display:none;}
#ix-grid-host{overflow:auto;background:#0b1220;}
#ix-grid-host .grid-stack{min-height:100%;}
/* The item content is the pane's parent, so it needs the same min-height:0
   discipline as a layout cell or a terminal pushes it wider than its cell. */
.grid-stack-item-content{display:flex;flex-direction:column;min-width:0;
  min-height:0;overflow:hidden;background:#0f172a;border:1px solid #1f2937;
  border-radius:8px;}
.grid-stack-item-content .ix-pane{height:100%;}
/* Grip and close only mean anything while tiled: in tabbed mode the tab strip
   already moves and closes a pane. */
.ix-pane-grip,.ix-pane-close,.ix-pane-max{display:none;}
.grid-stack .ix-pane-grip{display:inline-flex;align-items:center;color:#475569;
  cursor:move;font-size:16px;padding:0 2px;}
.grid-stack .ix-pane-grip:hover{color:#94a3b8;}
.grid-stack .ix-pane-close{display:inline-flex;align-items:center;
  justify-content:center;width:20px;height:20px;padding:0;color:#64748b;
  background:transparent;border:none;border-radius:4px;cursor:pointer;
  font-size:16px;line-height:1;}
.grid-stack .ix-pane-close:hover{background:#7f1d1d;color:#fecaca;}
.grid-stack .ix-pane-max{display:inline-flex;align-items:center;
  justify-content:center;width:20px;height:20px;padding:0;color:#64748b;
  background:transparent;border:none;border-radius:4px;cursor:pointer;
  font-size:14px;}
.grid-stack .ix-pane-max:hover{background:#1f2937;color:#cbd5f5;}
.grid-stack .ix-pane-max[aria-pressed="true"]{color:#38bdf8;}

/* Maximize is presentational: the tile is overlaid on the grid rather than
   resized in it, so its neighbours keep their positions and the saved layout
   is untouched. GridStack sets width/height inline as calc() over its CSS
   variables, hence !important. The grid is position:relative and stretched to
   the host by min-height, so inset:0 fills the workspace. */
#ix-grid-host .grid-stack-item[data-maximized]{
  position:absolute !important;
  inset:0 !important;
  width:auto !important;
  height:auto !important;
  margin:0 !important;
  transform:none !important;
  z-index:30;
}
/* Dragging or resizing a tile that is pretending to fill the workspace would
   move it in the grid model behind the overlay. */
#ix-grid-host .grid-stack-item[data-maximized] .ui-resizable-handle,
#ix-grid-host .grid-stack-item[data-maximized] .ix-pane-grip{display:none;}
.ix-reconnect{display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:6px;height:100%;padding:20px;text-align:center;}
.ix-reconnect-icon{font-size:30px;color:#334155;}
.ix-reconnect-name{color:#e2e8f0;font:600 14px system-ui,sans-serif;}
.ix-reconnect-host{color:#64748b;font:11px ui-monospace,Menlo,Consolas,monospace;}
.ix-reconnect-btn{display:inline-flex;align-items:center;gap:6px;margin-top:8px;
  padding:7px 15px;color:#e2e8f0;background:#1e293b;border:1px solid #334155;
  border-radius:7px;cursor:pointer;font:13px system-ui,sans-serif;}
.ix-reconnect-btn:hover{background:#334155;border-color:#475569;}
.ix-mode-switch{display:inline-flex;gap:2px;margin-right:10px;padding:2px;
  background:#0b1220;border:1px solid #1f2937;border-radius:7px;}
.ix-mode-btn{display:inline-flex;align-items:center;justify-content:center;
  width:28px;height:24px;padding:0;color:#64748b;background:transparent;
  border:none;border-radius:5px;cursor:pointer;font-size:15px;}
.ix-mode-btn:hover{color:#cbd5f5;background:#1f2937;}
.ix-mode-btn[aria-selected="true"]{color:#e2e8f0;background:#1e293b;}
"""

_PANE_CSS = """
.ix-pane{display:flex;flex-direction:column;height:100%;min-height:0;
  container-type:inline-size;}
.ix-pane-tabs{display:flex;align-items:center;gap:2px;flex:0 0 auto;
  padding:4px 6px;background:#0b1220;border-bottom:1px solid #1f2937;}
.ix-pane-tab{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;
  color:#94a3b8;background:transparent;border:1px solid transparent;
  border-radius:6px;cursor:pointer;font:12px system-ui,sans-serif;}
.ix-pane-tab:hover:not(:disabled){background:#1f2937;color:#cbd5f5;}
.ix-pane-tab[aria-selected="true"]{background:#1e293b;color:#e2e8f0;
  border-color:#334155;}
.ix-pane-tab:disabled{opacity:.4;cursor:not-allowed;}
.ix-pane-tab .mdi{font-size:15px;}
.ix-pane-spacer{flex:1 1 auto;}
/* Tiled only, like the grip. Shrinks before anything else in the strip, and
   outlives the host label in a narrow tile since it is the one that says
   which connection this is. */
.ix-pane-name{display:none;}
.grid-stack .ix-pane-name{display:inline-flex;align-items:center;gap:5px;
  flex:0 1 auto;min-width:0;margin:0 8px 0 2px;color:#e2e8f0;
  font:600 12px system-ui,sans-serif;}
.ix-pane-name .mdi{font-size:14px;color:#94a3b8;flex:0 0 auto;}
.ix-pane-name-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ix-pane-host{color:#64748b;font:11px ui-monospace,Menlo,Consolas,monospace;
  padding-right:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  max-width:40%;}
/* Both panels are always mounted and stacked; only visibility changes, so the
   terminal's host element is created once and never replaced. */
.ix-pane-body{position:relative;flex:1 1 auto;min-height:0;}
.ix-pane-panel{position:absolute;inset:0;min-width:0;min-height:0;}
.ix-pane-panel[hidden],.ix-pane-tab[hidden]{display:none;}
/* A narrow pane drops the tab labels to icons. */
@container (max-width: 420px){
  .ix-pane-tab span:not(.mdi){display:none;}
  .ix-pane-host{display:none;}
}
"""

_SFTP_CSS = """
.ix-sftp{display:flex;flex-direction:column;height:100%;min-height:0;}
.ix-sftp-bar{display:flex;align-items:center;gap:4px;padding:6px 8px;
  background:#111827;border-bottom:1px solid #1f2937;}
.ix-sftp-btn{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;
  color:#cbd5f5;background:transparent;border:1px solid transparent;border-radius:6px;
  cursor:pointer;font:12px system-ui,sans-serif;}
.ix-sftp-btn:hover{background:#1f2937;border-color:#334155;}
.ix-sftp-btn .mdi{font-size:15px;}
.ix-sftp-sep{width:1px;height:18px;margin:0 5px;background:#334155;}
.ix-sftp-spacer{flex:1 1 auto;}
.ix-sftp-note{display:none;align-items:center;gap:5px;color:#fbbf24;
  font:11px system-ui,sans-serif;padding-right:6px;max-width:46ch;
  text-align:right;line-height:1.3;}
.ix-sftp-note[data-shown]{display:inline-flex;}
.ix-sftp-note-icon{font-size:14px;flex:0 0 auto;}
/* A short grid cell cannot spare 210px of queue. The percentage resolves
   because .ix-sftp sits in an absolutely positioned pane panel, so its height
   is definite. */
.ix-queue{flex:0 0 auto;max-height:min(210px, 45%);display:flex;
  flex-direction:column;background:#0f172a;border-top:1px solid #1f2937;}
.ix-queue[hidden]{display:none;}
.ix-queue-head{display:flex;align-items:center;justify-content:space-between;
  padding:5px 10px;border-bottom:1px solid #1f2937;}
.ix-queue-title{font:600 11px system-ui,sans-serif;color:#94a3b8;
  letter-spacing:.04em;text-transform:uppercase;}
.ix-queue-clear{padding:2px 8px;color:#94a3b8;background:transparent;border:none;
  border-radius:4px;cursor:pointer;font:11px system-ui,sans-serif;}
.ix-queue-clear:hover{background:#1f2937;color:#cbd5f5;}
.ix-queue-rows{overflow:auto;min-height:0;}
.ix-queue-row{display:flex;align-items:center;gap:8px;padding:5px 10px;
  font:12px system-ui,sans-serif;color:#cbd5f5;}
.ix-queue-row .mdi{font-size:14px;opacity:.7;flex:0 0 auto;}
.ix-queue-name{flex:0 1 240px;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;}
.ix-queue-track{flex:1 1 auto;height:5px;min-width:60px;border-radius:3px;
  background:#1e293b;overflow:hidden;}
.ix-queue-bar{display:block;height:100%;width:0;border-radius:3px;background:#38bdf8;
  transition:width .15s linear;}
.ix-queue-status{flex:0 0 auto;min-width:96px;text-align:right;color:#94a3b8;
  font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ix-queue-cancel{flex:0 0 auto;width:20px;height:20px;padding:0;color:#94a3b8;
  background:transparent;border:none;border-radius:4px;cursor:pointer;font-size:14px;}
.ix-queue-cancel:hover{background:#1f2937;color:#f87171;}
.ix-queue-row[data-state] .ix-queue-cancel{visibility:hidden;}
.ix-queue-row[data-state="done"] .ix-queue-bar{background:#34d399;}
.ix-queue-row[data-state="failed"] .ix-queue-bar{background:#f87171;}
.ix-queue-row[data-state="failed"] .ix-queue-status{color:#f87171;}
.ix-queue-row[data-state="cancelled"]{opacity:.55;}
/* nowrap + scroll: wrapping turned a deep path into four stacked lines that
   ate the listing in a narrow pane. */
.ix-crumbs{display:flex;flex-wrap:nowrap;align-items:center;gap:2px;
  padding:6px 8px;background:#111827;border-bottom:1px solid #1f2937;
  overflow-x:auto;scrollbar-width:none;}
.ix-crumbs::-webkit-scrollbar{height:0;}
.ix-crumb{flex:0 0 auto;max-width:22ch;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;}
.ix-crumb{padding:3px 8px;color:#cbd5f5;background:transparent;border:none;
  border-radius:4px;cursor:pointer;font:12px system-ui,sans-serif;}
.ix-crumb:hover{background:#1f2937;}
.ix-crumb-up{color:#38bdf8;}
.ix-sftp-table{flex:1 1 auto;min-height:0;}

/* Narrow panes. The natural toolbar is 469px wide, so it starts clipping just
   under that; the tiers below give it somewhere to go. Container queries, not
   media queries -- a pane is narrow because the grid cell is narrow, which has
   nothing to do with the size of the window. */
@container (max-width: 700px){
  /* Keep the warning, drop its sentence; the icon carries it as a tooltip. */
  .ix-sftp-note-text{display:none;}
}
/* Below ~500px the fixed columns (icon 34 + size 100 + modified 150 + perms
   80, plus the checkbox) squeezed Name to zero and the filename -- the one
   column that matters -- disappeared behind a horizontal scrollbar. Secondary
   columns give way instead; they are still in the context menu and the
   tooltip. */
@container (max-width: 700px){
  .ix-sftp-table th[data-column-id="permissions"],
  .ix-sftp-table td[data-column-id="permissions"]{display:none;}
}
@container (max-width: 560px){
  .ix-sftp-table th[data-column-id="modified"],
  .ix-sftp-table td[data-column-id="modified"]{display:none;}
  .ix-sftp-btn span:not(.mdi){display:none;}
  .ix-sftp-btn{padding:5px 7px;}
  .ix-sftp-bar{gap:2px;padding:6px;}
  .ix-sftp-sep{margin:0 3px;}
}
@container (max-width: 420px){
  .ix-crumb{max-width:12ch;}
  .ix-queue-name{flex:0 1 120px;}
  .ix-queue-status{min-width:64px;}
}
"""

_ADMIN_CSS = """
.ix-admin{display:flex;flex-direction:column;height:100%;min-height:0;gap:10px;}
.ix-admin-table{flex:1 1 auto;min-height:0;border:1px solid var(--wapyt-border);
  border-radius:6px;overflow:hidden;}
.ix-admin-form{flex:0 0 auto;}
"""

_TOAST_CSS = """
.ix-toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(12px);
  padding:10px 18px;border-radius:8px;background:#1e293b;color:#e2e8f0;
  font:13px system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.4);
  opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;z-index:10000;}
.ix-toast[data-visible]{opacity:1;transform:translateX(-50%) translateY(0);}
"""
