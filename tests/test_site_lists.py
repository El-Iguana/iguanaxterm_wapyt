"""Site lists: ownership rules, cascades, and no credentials out (#37)."""
from __future__ import annotations

import json

import pytest

from _world import make_list, svc, world  # noqa: F401  (fixture)


def lists_as(user_id: int, role: str):
    from services.site_list_service import SiteListService

    return svc(SiteListService, user_id, role)


def test_a_power_user_builds_a_list_of_their_sites_for_their_users(world):
    _db, ids = world
    list_id = make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]])
    got = lists_as(ids["pam"], "power").get(list_id)
    assert [s["name"] for s in got["sites"]] == ["pam-ssh"]
    assert [m["username"] for m in got["members"]] == ["ula"]
    assert got["allow_files"] is False  # off unless asked for


def test_a_power_user_cannot_add_someone_elses_site(world):
    _db, ids = world
    pam = lists_as(ids["pam"], "power")
    list_id = pam.save(name="Lab")["id"]
    assert not pam.set_items(list_id, [ids["pete-ssh"]])["ok"]
    assert pam.get(list_id)["sites"] == []


@pytest.mark.parametrize("who", ["vic", "pete", "admin"])
def test_a_power_user_assigns_only_their_own_users(world, who):
    _db, ids = world
    pam = lists_as(ids["pam"], "power")
    list_id = pam.save(name="Lab")["id"]
    assert not pam.set_members(list_id, [ids[who]])["ok"]
    assert pam.get(list_id)["members"] == []


def test_a_power_user_cannot_see_or_touch_another_power_users_list(world):
    _db, ids = world
    petes = make_list(ids["pete"], "power", "Pete's", [ids["pete-ssh"]], [ids["vic"]])
    pam = lists_as(ids["pam"], "power")
    assert pam.list() == []
    for result in (
        pam.get(petes),
        pam.save(list_id=petes, name="mine now"),
        pam.delete(petes),
        pam.set_items(petes, []),
        pam.set_members(petes, []),
        pam.delete_preview(petes),
    ):
        assert result == {"ok": False, "error": "List not found"}
    assert lists_as(ids["pete"], "power").get(petes)["name"] == "Pete's"


def test_an_admin_manages_every_list_with_any_site_and_any_account(world):
    _db, ids = world
    petes = make_list(ids["pete"], "power", "Pete's", [ids["pete-ssh"]], [ids["vic"]])
    admin = lists_as(1, "admin")
    assert [row["name"] for row in admin.list()] == ["Pete's"]
    assert admin.set_items(petes, [ids["pete-ssh"], ids["pam-ssh"]])["ok"]
    # Power Users too: they then see those sites next to their own.
    assert admin.set_members(petes, [ids["vic"], ids["pam"]])["ok"]
    got = admin.get(petes)
    assert {m["username"] for m in got["members"]} == {"vic", "pam"}


def test_users_have_no_list_powers(world):
    _db, ids = world
    ula = lists_as(ids["ula"], "user")
    assert ula.list() == []
    assert not ula.save(name="sneaky")["ok"]
    assert ula.candidates() == {"sites": [], "members": []}


def test_candidates_follow_the_rules(world):
    _db, ids = world
    pam = lists_as(ids["pam"], "power").candidates()
    assert {s["name"] for s in pam["sites"]} == {"pam-ssh", "pam-ftp", "pam-vnc"}
    assert {m["username"] for m in pam["members"]} == {"ula", "una"}
    admin = lists_as(1, "admin").candidates()
    assert len(admin["sites"]) == 4 and len(admin["members"]) == 6


def test_a_demoted_power_user_loses_their_lists_at_once(world):
    db, ids = world
    list_id = make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]])
    with db.get_db() as conn:
        conn.execute("UPDATE users SET role = 'user' WHERE id = ?", (ids["pam"],))
    pam = lists_as(ids["pam"], "power")  # the session still says power
    assert pam.list() == [] and not pam.delete(list_id)["ok"]


def test_cascades(world):
    db, ids = world
    from services.session_service import SessionService
    from services.user_service import UserService

    list_id = make_list(ids["pam"], "power", "Lab",
                        [ids["pam-ssh"], ids["pam-ftp"]], [ids["ula"], ids["una"]])
    # A deleted site drops out of the list.
    assert svc(SessionService, ids["pam"], "power").delete(ids["pam-ftp"])["ok"]
    # A deleted member drops out of the list.
    assert svc(UserService, ids["pam"], "power").delete(ids["una"])["ok"]
    got = lists_as(ids["pam"], "power").get(list_id)
    assert [s["name"] for s in got["sites"]] == ["pam-ssh"]
    assert [m["username"] for m in got["members"]] == ["ula"]
    # A deleted list takes its items and members with it.
    assert lists_as(ids["pam"], "power").delete(list_id)["ok"]
    with db.get_db() as conn:
        for table in ("site_list_items", "site_list_members"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_deleting_a_power_user_moves_their_lists_to_the_admin(world):
    db, ids = world
    from services.user_service import UserService

    list_id = make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]])
    admin = svc(UserService, 1, "admin")
    preview = admin.delete_preview(ids["pam"])
    assert (preview["lists"], preview["users"], preview["sites"]) == (1, 2, 3)
    assert admin.delete(ids["pam"])["ok"]
    got = lists_as(1, "admin").get(list_id)
    # The list survives, now the Admin's; pam's own sites went with her.
    assert got["owner_id"] == 1 and got["sites"] == []
    assert [m["username"] for m in got["members"]] == ["ula"]


def test_assign_from_the_users_screen(world):
    _db, ids = world
    lab = make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]])
    ops = make_list(ids["pam"], "power", "Ops", [ids["pam-ftp"]])
    petes = make_list(ids["pete"], "power", "Pete's", [ids["pete-ssh"]], [ids["vic"]])
    pam = lists_as(ids["pam"], "power")
    assert pam.assign(ids["ula"], [lab, ops])["ok"]
    assert pam.lists_for(ids["ula"])["assigned"] == [lab, ops]
    assert pam.assign(ids["ula"], [ops])["ok"]
    assert pam.lists_for(ids["ula"])["assigned"] == [ops]
    # Not her list, not her User.
    assert not pam.assign(ids["ula"], [petes])["ok"]
    assert not pam.assign(ids["vic"], [lab])["ok"]
    assert not pam.lists_for(ids["vic"])["ok"]


def test_no_method_returns_a_credential(world):
    _db, ids = world
    list_id = make_list(ids["pam"], "power", "Lab",
                        [ids["pam-ssh"], ids["pam-vnc"]], [ids["ula"]], files=True)
    pam = lists_as(ids["pam"], "power")
    out = json.dumps([
        pam.list(), pam.get(list_id), pam.candidates(), pam.delete_preview(list_id),
        pam.lists_for(ids["ula"]), lists_as(1, "admin").candidates(),
    ])
    for word in ("password", "private_key", "passphrase", "s3cret-pw", "vnc-pw", "host_key"):
        assert word not in out, word


def test_emptying_a_list_removes_everyone_and_everything(world):
    _db, ids = world
    list_id = make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"], ids["una"]])
    pam = lists_as(ids["pam"], "power")
    assert pam.set_members(list_id, [])["ok"] and pam.set_items(list_id, [])["ok"]
    got = pam.get(list_id)
    assert got["members"] == [] and got["sites"] == []
