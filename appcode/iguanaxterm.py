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

from services.paths import breadcrumbs, parent_path, session_icon
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
            container=body.get_cell("sidebar"),
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
            button = event.target.closest(".ix-toolbar-btn")
            # A DOM miss arrives as JsNull, not None: `button is None` is always
            # False and every click outside the toolbar would raise. JsNull is
            # falsy, so test truthiness for anything coming back over the FFI.
            if not button:
                return
            self._on_toolbar(button.dataset.action)

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
            f'  <div class="ix-crumbs" id="crumbs-{tab_id}"></div>'
            f'  <div class="ix-sftp-table" id="table-{tab_id}"></div>'
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

    def _sftp_download(self, tab_id: str, paths: list) -> None:
        """
        Pull files through the streaming transfer route.

        Not the BFF: bytes in JSON means base64, a third larger and resident in
        Pyodide's heap on the way past. An anchor click carries the same session
        cookie and streams straight to disk.
        """
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        for path in paths:
            row = entry["table"].get_row(path) or {}
            if row.get("is_dir"):
                self._toast("Folder download is not in this build yet.")
                continue
            url = (
                f"/files/{entry['session_id']}/download"
                f"?path={js.encodeURIComponent(path)}"
            )
            anchor = js.document.createElement("a")
            anchor.href = url
            anchor.download = row.get("name", "download")
            js.document.body.appendChild(anchor)
            anchor.click()
            anchor.remove()

    def _sftp_upload(self, tab_id: str, payload: dict) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        files = entry["table"].get_dropped_files()
        _spawn(self._do_upload(tab_id, files), "sftp upload")

    async def _do_upload(self, tab_id: str, files) -> None:
        entry = self._sftp_tabs.get(tab_id)
        if entry is None:
            return
        total = int(files.length) if hasattr(files, "length") else len(files)
        for index in range(total):
            handle = files[index]
            self._toast(f"Uploading {handle.name} ({index + 1}/{total})…")
            form = js.FormData.new()
            form.append("path", entry["path"])
            form.append("file", handle)
            options = js.Object.new()
            options.method = "POST"
            options.body = form
            options.credentials = "same-origin"
            response = await js.fetch(
                f"/files/{entry['session_id']}/upload", options
            )
            if not response.ok:
                self._toast(f"Upload failed: {handle.name}")
                return
        self._toast(f"Uploaded {total} file(s).")
        await self._sftp_navigate(tab_id, entry["path"])

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

_SFTP_CSS = """
.ix-sftp{display:flex;flex-direction:column;height:100%;min-height:0;}
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
