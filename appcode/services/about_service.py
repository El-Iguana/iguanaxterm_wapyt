"""
BFF: whether a newer IguanaXterm release is out, for the About dialog and the
toolbar badge. The check and its cache are in ``release_check`` (a BFF module
is re-executed on every call, so it cannot keep them).
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id


@backend_for_frontend
@bff_policy(application="iguanaxterm")
class AboutService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    def latest(self) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        from services.release_check import latest_release

        return {"ok": True, **latest_release()}
