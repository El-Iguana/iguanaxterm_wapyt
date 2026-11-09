"""Sites shared with me, in SessionService.list (#39)."""
from __future__ import annotations

import json

from _world import make_list, svc, world  # noqa: F401  (fixture)


def listing(user_id: int, role: str) -> list:
    from services.session_service import SessionService

    return svc(SessionService, user_id, role).list()


def test_a_user_sees_assigned_sites_read_only_and_without_secrets(world):
    _db, ids = world
    make_list(ids["pam"], "power", "Lab servers", [ids["pam-ssh"], ids["pam-vnc"]], [ids["ula"]])
    rows = listing(ids["ula"], "user")
    assert [(r["name"], r["list_name"], r["shared"]) for r in rows] == [
        ("pam-ssh", "Lab servers", True), ("pam-vnc", "Lab servers", True),
    ]
    for row in rows:
        assert row["editable"] is False and row["can_forget"] is False
        assert row["files"] is False and row["owner"] == "pam"
        assert row["via_session_id"] == 0          # the owner's tunnel stays hidden
    text = json.dumps(rows)
    for word in ("password", "private_key", "passphrase", "s3cret-pw", "vnc-pw"):
        assert word not in text, word


def test_files_follow_the_list_and_the_site_type(world):
    _db, ids = world
    make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"], ids["pam-vnc"]], [ids["ula"]], files=True)
    files = {r["name"]: r["files"] for r in listing(ids["ula"], "user")}
    assert files == {"pam-ssh": True, "pam-vnc": False}   # a desktop has no files


def test_a_site_in_two_lists_shows_once_under_the_first(world):
    _db, ids = world
    make_list(ids["pam"], "power", "Zeta", [ids["pam-ssh"]], [ids["ula"]], files=True)
    make_list(ids["pam"], "power", "Alpha", [ids["pam-ssh"]], [ids["ula"]])
    rows = listing(ids["ula"], "user")
    assert [(r["name"], r["list_name"], r["files"]) for r in rows] == [("pam-ssh", "Alpha", True)]


def test_an_owned_site_shows_once_as_owned(world):
    _db, ids = world
    make_list(1, "admin", "Mixed", [ids["pam-ssh"], ids["pete-ssh"]], [ids["pam"]])
    rows = listing(ids["pam"], "power")
    names = [(r["name"], r["shared"]) for r in rows]
    assert names.count(("pam-ssh", False)) == 1 and ("pam-ssh", True) not in names
    assert ("pete-ssh", True) in names
    own = [r for r in rows if not r["shared"]]
    assert all(r["editable"] and r["can_forget"] for r in own)


def test_an_admin_with_a_shared_site_may_forget_its_key(world):
    _db, ids = world
    make_list(1, "admin", "Mine to look after", [ids["pete-ssh"]], [1])
    (row,) = listing(1, "admin")
    assert row["shared"] and row["can_forget"] and not row["editable"]


def test_a_user_with_nothing_shared_sees_nothing(world):
    _db, ids = world
    assert listing(ids["una"], "user") == []
