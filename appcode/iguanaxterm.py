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

from services.paths import breadcrumbs, format_size, parent_path, session_icon
from services.session_service import SessionService
from services.sftp_service import SFTPService
from services.user_service import UserService

# pytincture resolves the browser entrypoint by AST, and its MainWindow
# detection is hardcoded to dhxpyt.layout.MainWindow — it never matches a wapyt
# base. Without this declaration the app starts with HTTP 422.
APP_ENTRYPOINT = "IguanaXterm"

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

        self._sessions: list[dict] = []
        self._selected_id: int | None = None
        self._terminals: dict[str, dict] = {}   # tab id -> {terminal, session_id}
        self._sftp_tabs: dict[str, dict] = {}   # tab id -> {table, session_id, path}
        self._tab_counter = 0
        self._me: dict = {}

        self._build_chrome()
        _spawn(self._load_identity(), "identity load")
        _spawn(self._reload_sessions(), "session load")

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
            '      <span class="ix-brand-sub">SSH &middot; Telnet &middot; SFTP</span>'
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

        self.tabs = TabWidget(
            TabWidgetConfig(
                tabs=[TabConfig(id="welcome", title="Welcome", icon="mdi-home")],
                active="welcome",
            ),
            container=body.get_cell("workspace"),
        )
        self.tabs.on_change(self._on_tab_change)
        self.tabs.on_close(self._on_tab_close)
        self.tabs.attach_html("welcome", self._welcome_html())

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
        parts.append('<span class="ix-toolbar-user" id="ix-user"></span>')
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

        self._toolbar_proxy = create_proxy(_on_click)
        js.document.addEventListener("click", self._toolbar_proxy)

    @staticmethod
    def _welcome_html() -> str:
        return (
            '<div style="display:flex;flex-direction:column;align-items:center;'
            'justify-content:center;height:100%;gap:10px;color:#94a3b8;'
            'font:14px system-ui,sans-serif;">'
            '<span class="mdi mdi-console" style="font-size:44px;opacity:.5"></span>'
            "<div>Double-click a session to open a terminal.</div></div>"
        )

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
        if action == "new":
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

    def _connect(self, node_id: str | None) -> None:
        if not node_id or not str(node_id).startswith("sess_"):
            return
        session_id = int(str(node_id)[5:])
        session = self._session(session_id)
        if session is None:
            return

        self._tab_counter += 1
        tab_id = f"term_{self._tab_counter}"
        self.tabs.add_tab(
            TabConfig(
                id=tab_id,
                title=session["name"],
                icon=session_icon(session.get("type", "ssh")),
                closable=True,
            )
        )

        terminal = Terminal(
            TerminalConfig(
                ws_url=f"/ws/terminal/{session_id}",
                theme=TERMINAL_THEME,
                search=True,
                reconnect=True,
            ),
            container=self.tabs.get_cell(tab_id),
        )
        terminal.on_error(lambda payload: self._toast(payload.get("message", "Error")))

        self._terminals[tab_id] = {"terminal": terminal, "session_id": session_id}
        self.tabs.set_active(tab_id)

    def _on_tab_change(self, payload: dict) -> None:
        tab_id = payload.get("id") if isinstance(payload, dict) else payload
        entry = self._terminals.get(tab_id)
        if entry is None:
            return
        # A keep-alive tab is hidden, not unmounted, so its terminal has no
        # dimensions while inactive and skips fitting. Re-fit on the way back in
        # or the grid stays at whatever size it last measured.
        entry["terminal"].fit()
        entry["terminal"].focus()

    def _on_tab_close(self, payload: dict) -> None:
        tab_id = payload.get("id") if isinstance(payload, dict) else payload

        entry = self._terminals.pop(tab_id, None)
        if entry is not None:
            # Closing the tab must close the socket. The dhxpyt rewrite left
            # every terminal it ever opened running on the server.
            entry["terminal"].destroy()

        sftp = self._sftp_tabs.pop(tab_id, None)
        if sftp is not None:
            sftp["table"].destroy()
            _spawn(
                SFTPService().disconnect_async(sftp["session_id"]),
                "sftp disconnect",
            )

    # ------------------------------------------------------------------
    # SFTP
    # ------------------------------------------------------------------

    def _open_sftp(self, session_id: int) -> None:
        session = self._session(session_id)
        if session is None:
            return
        if session.get("type") == "telnet":
            self._toast("SFTP needs an SSH session.")
            return

        existing = next(
            (
                tab_id
                for tab_id, entry in self._sftp_tabs.items()
                if entry["session_id"] == session_id
            ),
            None,
        )
        if existing:
            self.tabs.set_active(existing)
            return

        self._tab_counter += 1
        tab_id = f"sftp_{self._tab_counter}"
        self.tabs.add_tab(
            TabConfig(
                id=tab_id,
                title=f"{session['name']} files",
                icon="mdi-folder-network",
                closable=True,
            )
        )

        cell = self.tabs.get_cell(tab_id)
        panel = self._build_sftp_panel(cell, tab_id)
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

        entry = {"table": table, "session_id": session_id, "path": ""}
        self._sftp_tabs[tab_id] = entry

        table.on_activate(lambda payload: self._sftp_activate(tab_id, payload))
        table.on_action(lambda payload: self._sftp_action(tab_id, payload))
        table.on_drop(lambda payload: self._sftp_upload(tab_id, payload))

        self.tabs.set_active(tab_id)
        _spawn(self._sftp_navigate(tab_id, ""), "sftp listing")

    def _build_sftp_panel(self, cell, tab_id: str) -> dict:
        """
        Build the SFTP panel chrome inside a tab cell.

        The breadcrumb bar is plain HTML because wapyt has no breadcrumb widget
        and one path strip does not justify inventing one.
        """
        container = cell.getContainer() if hasattr(cell, "getContainer") else cell
        container.innerHTML = (
            f'<div class="ix-sftp">'
            f'  <div class="ix-sftp-bar">'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="upload" data-tab="{tab_id}">'
            f'      <span class="mdi mdi-upload"></span><span>Upload</span></button>'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="upload-folder" data-tab="{tab_id}">'
            f'      <span class="mdi mdi-folder-upload"></span><span>Upload folder</span></button>'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="download" data-tab="{tab_id}">'
            f'      <span class="mdi mdi-download"></span><span>Download</span></button>'
            f'    <span class="ix-sftp-sep"></span>'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="mkdir" data-tab="{tab_id}">'
            f'      <span class="mdi mdi-folder-plus"></span><span>New folder</span></button>'
            f'    <button type="button" class="ix-sftp-btn" data-sftp="refresh" data-tab="{tab_id}">'
            f'      <span class="mdi mdi-refresh"></span><span>Refresh</span></button>'
            f'    <span class="ix-sftp-spacer"></span>'
            f'    <span class="ix-sftp-note" id="note-{tab_id}"></span>'
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
            f"</div><style>{_SFTP_CSS}</style>"
        )
        return {
            "crumbs": js.document.getElementById(f"crumbs-{tab_id}"),
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
        self._render_crumbs(tab_id, result["path"])
        self._show_picker_note(tab_id)

    def _show_picker_note(self, tab_id: str) -> None:
        """Say up front when downloads cannot choose a destination."""
        note = js.document.getElementById(f"note-{tab_id}")
        if not note or note.textContent:
            return
        caps = filetransfer.capabilities()
        if caps.pickers:
            return
        note.textContent = (
            "This browser cannot choose a download location — files go to your "
            "downloads folder. Chrome or Edge can."
            if caps.secure_context
            else "Destination picking needs HTTPS; downloads go to your "
                 "downloads folder."
        )

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
                height=640,
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
                                options=[SelectOption("ssh", "SSH"),
                                         SelectOption("telnet", "Telnet")]),
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
                                help="RSA, Ed25519, ECDSA or DSA."
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
            # Telnet's default port is 23, and it has no key or user auth.
            is_telnet = payload.get("value") == "telnet"
            form.set_values({"port": 23 if is_telnet else 22})
            for field in ("password", "private_key", "username"):
                form.set_field_disabled(field, is_telnet)

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
.ix-sftp-note{color:#fbbf24;font:11px system-ui,sans-serif;padding-right:6px;
  max-width:46ch;text-align:right;line-height:1.3;}
.ix-queue{flex:0 0 auto;max-height:210px;display:flex;flex-direction:column;
  background:#0f172a;border-top:1px solid #1f2937;}
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
.ix-crumbs{display:flex;flex-wrap:wrap;align-items:center;gap:2px;padding:6px 8px;
  background:#111827;border-bottom:1px solid #1f2937;}
.ix-crumb{padding:3px 8px;color:#cbd5f5;background:transparent;border:none;
  border-radius:4px;cursor:pointer;font:12px system-ui,sans-serif;}
.ix-crumb:hover{background:#1f2937;}
.ix-crumb-up{color:#38bdf8;}
.ix-sftp-table{flex:1 1 auto;min-height:0;}
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
