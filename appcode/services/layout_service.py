"""
BFF: the workspace layout, one per user.

Stored as opaque JSON. The shape belongs to the UI and will change with it, so
the database holds a blob and this service only enforces the things that are
actually invariants: it is yours, it is small, and every session it names is
one you own.
"""
from __future__ import annotations

import json
from typing import Any

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import get_db

# A layout is a handful of panes. This is a guard against a runaway client, not
# a real limit -- 64 open connections would be remarkable.
_MAX_PANES = 64
_MODES = ("tabbed", "tiled")


@backend_for_frontend
@bff_policy(application="iguanaxterm")
class LayoutService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    def get(self) -> dict:
        """
        The saved layout, with dead panes already dropped.

        Sessions get deleted between visits, and a pane pointing at one that no
        longer exists -- or at someone else's -- must never come back. Filtering
        on read rather than on write means a session deleted while the layout
        sat untouched is still caught.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not signed in"}

        with get_db() as conn:
            row = conn.execute(
                "SELECT payload FROM layouts WHERE user_id = ?", (self._user_id,)
            ).fetchone()
            owned = {
                int(r[0])
                for r in conn.execute(
                    "SELECT id FROM sessions WHERE user_id = ?", (self._user_id,)
                )
            }

        if row is None or not row[0]:
            return {"ok": True, "mode": "tabbed", "panes": []}

        try:
            saved = json.loads(row[0])
        except (ValueError, TypeError):
            # A corrupt blob is not worth an error page; an empty workspace is
            # a fine thing to fall back to.
            return {"ok": True, "mode": "tabbed", "panes": []}

        panes = [
            pane
            for pane in (saved.get("panes") or [])
            if isinstance(pane, dict) and int(pane.get("session_id") or 0) in owned
        ]
        mode = saved.get("mode")
        return {
            "ok": True,
            "mode": mode if mode in _MODES else "tabbed",
            "panes": panes[:_MAX_PANES],
        }

    def save(self, mode: str, panes: Any) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not signed in"}
        if mode not in _MODES:
            return {"ok": False, "error": "Unknown layout mode"}

        cleaned = []
        for pane in (panes or [])[:_MAX_PANES]:
            if not isinstance(pane, dict):
                continue
            try:
                session_id = int(pane.get("session_id"))
            except (TypeError, ValueError):
                continue
            cleaned.append(
                {
                    "session_id": session_id,
                    "x": int(pane.get("x") or 0),
                    "y": int(pane.get("y") or 0),
                    "w": int(pane.get("w") or 6),
                    "h": int(pane.get("h") or 7),
                    "tab": "files" if pane.get("tab") == "files" else "terminal",
                    # Only ever a path we handed out; still capped, since it
                    # comes back from the browser.
                    "path": str(pane.get("path") or "")[:4096],
                }
            )

        payload = json.dumps({"mode": mode, "panes": cleaned})
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO layouts (user_id, payload, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE
                   SET payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (self._user_id, payload),
            )
        return {"ok": True, "panes": len(cleaned)}

    def clear(self) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not signed in"}
        with get_db() as conn:
            conn.execute("DELETE FROM layouts WHERE user_id = ?", (self._user_id,))
        return {"ok": True}
