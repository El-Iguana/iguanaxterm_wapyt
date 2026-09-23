"""
State must survive pytincture re-executing a BFF module on every call.

pytincture loads a BFF module's source afresh for each call
(``backend/app.py``: ``prepare_call`` -> ``_load_source_module``). A pool or
registry defined at module level in such a file is therefore a new, empty one
per request. That silently turned the SFTP pool into "dial on every click"
(and never close).

These tests load the modules the way pytincture does, twice, and check that
what must persist is the same object both times.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

APPCODE = Path(__file__).resolve().parents[1] / "appcode"
sys.path.insert(0, str(APPCODE))

loading = pytest.importorskip("pytincture.backend.source_loading")


def _load_like_pytincture(relative: str, hint: str):
    return loading.load_source_module(str(APPCODE / relative), hint, str(APPCODE))


def test_the_sftp_pool_is_the_same_pool_on_every_call():
    first = _load_like_pytincture("services/sftp_service.py", "SFTPService")
    second = _load_like_pytincture("services/sftp_service.py", "SFTPService")
    assert first is not second, "pytincture really does re-execute the module"
    assert first.sftp_pool is second.sftp_pool
    assert first._pool is second._pool, "the walk executor too"

    from services.pool import sftp_pool
    assert first.sftp_pool is sftp_pool, "and it is the one session_service closes"


def test_no_bff_module_keeps_mutable_state_at_module_level():
    """
    A guard for next time: flag module-level containers, locks, pools and
    executors in any file that exports BFF classes.
    """
    import ast

    suspicious = []
    for path in (APPCODE / "services").glob("*.py"):
        tree = ast.parse(path.read_text())
        exports_bff = any(
            isinstance(node, ast.ClassDef)
            and any(getattr(d, "id", None) == "backend_for_frontend" for d in node.decorator_list)
            for node in tree.body
        )
        if not exports_bff:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if isinstance(value, (ast.Dict, ast.List, ast.Set)) or (
                isinstance(value, ast.Call)
                and getattr(value.func, "id", getattr(value.func, "attr", "")) not in ("APIRouter",)
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [getattr(t, "id", "?") for t in targets]
                suspicious.append(f"{path.name}: {', '.join(names)}")
    assert suspicious == [], "state in a BFF module is rebuilt on every call -- move it to a plain module"
