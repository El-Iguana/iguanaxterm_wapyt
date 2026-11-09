"""Taking access away closes what is open (#41)."""
from __future__ import annotations

import pytest

from _world import make_list, svc, world  # noqa: F401  (fixture)


@pytest.fixture()
def tracked(world):
    """ula on pam-ssh with a terminal, a desktop and a folder download open."""
    from services import live

    _db, ids = world
    lab = make_list(ids["pam"], "power", "Lab",
                    [ids["pam-ssh"], ids["pam-vnc"]], [ids["ula"]], files=True)
    closed: list = []
    tokens = [
        live.register(ids["ula"], ids["pam-ssh"], "terminal", lambda r: closed.append(("terminal", r))),
        live.register(ids["ula"], ids["pam-vnc"], "desktop", lambda r: closed.append(("desktop", r))),
        live.register(ids["ula"], ids["pam-ssh"], "job", lambda r: closed.append(("job", r))),
        # The owner's own terminal on the same site must survive everything.
        live.register(ids["pam"], ids["pam-ssh"], "terminal", lambda r: closed.append(("owner", r))),
    ]
    yield ids, lab, closed
    for token in tokens:
        live.unregister(token)


def lists(ids):
    from services.site_list_service import SiteListService

    return svc(SiteListService, ids["pam"], "power")


def kinds(closed) -> set:
    return {kind for kind, _reason in closed}


def test_nothing_closes_when_nothing_changes(tracked):
    from services import live

    _ids, _lab, closed = tracked
    assert live.recheck() == 0 and closed == []


def test_removing_the_member_closes_everything_of_theirs(tracked):
    from services.live import REVOKED

    ids, lab, closed = tracked
    assert lists(ids).set_members(lab, [])["ok"]
    assert kinds(closed) == {"terminal", "desktop", "job"}
    assert all(reason == REVOKED for _kind, reason in closed)


def test_removing_a_site_closes_only_that_site(tracked):
    ids, lab, closed = tracked
    assert lists(ids).set_items(lab, [ids["pam-vnc"]])["ok"]
    assert kinds(closed) == {"terminal", "job"}


def test_switching_files_off_closes_only_file_work(tracked):
    ids, lab, closed = tracked
    assert lists(ids).save(list_id=lab, name="Lab", allow_files=False)["ok"]
    assert kinds(closed) == {"job"}


def test_deleting_the_list(tracked):
    ids, lab, closed = tracked
    assert lists(ids).delete(lab)["ok"]
    assert kinds(closed) == {"terminal", "desktop", "job"}


def test_deleting_the_site_closes_the_owners_too(tracked):
    from services.session_service import SessionService

    ids, _lab, closed = tracked
    assert svc(SessionService, ids["pam"], "power").delete(ids["pam-ssh"])["ok"]
    assert kinds(closed) == {"terminal", "job", "owner"}


def test_deleting_the_user(tracked):
    from services.user_service import UserService

    ids, _lab, closed = tracked
    assert svc(UserService, ids["pam"], "power").delete(ids["ula"])["ok"]
    assert kinds(closed) == {"terminal", "desktop", "job"}


def test_a_revoked_entry_is_closed_once(tracked):
    from services import live

    ids, lab, closed = tracked
    lists(ids).set_members(lab, [])
    before = len(closed)
    live.recheck()
    assert len(closed) == before


def test_pooled_file_channels_are_released(world):
    from services import live
    from services.pool import sftp_pool, transfer_pool

    _db, ids = world
    lab = make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]], files=True)

    class _Client:
        closed = 0

        def close(self):
            _Client.closed += 1

    from services.pool import _PooledConnection

    for pool in (sftp_pool, transfer_pool):
        pool._connections[(ids["ula"], ids["pam-ssh"])] = _PooledConnection(_Client(), object())
        pool._connections[(ids["pam"], ids["pam-ssh"])] = _PooledConnection(_Client(), object())
    try:
        lists_svc = svc(__import__("services.site_list_service", fromlist=["x"]).SiteListService,
                        ids["pam"], "power")
        lists_svc.save(list_id=lab, name="Lab", allow_files=False)
        for pool in (sftp_pool, transfer_pool):
            assert pool.keys() == [(ids["pam"], ids["pam-ssh"])]
        assert _Client.closed == 2
    finally:
        sftp_pool.close_all()
        transfer_pool.close_all()
    assert live.count() == 0
