"""
Refcounting in the SFTP pool.

A pane per connection means one saved session can be open several times over.
Closing one pane used to call close() and pull the shared channel out from
under the others; the observable damage is an in-flight transfer dying, because
a *subsequent* listing merely re-dials and looks fine. That is why this is
tested here rather than through the browser.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))


class _FakeTransport:
    def __init__(self) -> None:
        self.active = True

    def is_active(self) -> bool:
        return self.active


class _FakeClient:
    def __init__(self) -> None:
        self.closed = 0
        self._transport = _FakeTransport()

    def get_transport(self):
        return self._transport

    def close(self) -> None:
        self.closed += 1


@pytest.fixture()
def pool():
    from services.sftp_service import SFTPPool

    return SFTPPool()


def _seed(pool, user_id=1, session_id=7):
    """Put a live connection in the pool without dialling anything."""
    from services.sftp_service import _PooledConnection

    client = _FakeClient()
    conn = _PooledConnection(client, sftp=object())
    key = (user_id, session_id)
    pool._connections[key] = conn
    pool._keys[session_id] = key
    return client, key


def test_release_with_holds_left_keeps_the_channel(pool):
    client, key = _seed(pool)
    pool.retain(1, 7)
    pool.retain(1, 7)

    assert pool.release(1, 7) == 1
    assert key in pool._connections, "second pane's channel was closed"
    assert client.closed == 0, "client.close() called while a pane still held it"


def test_last_release_closes_the_channel(pool):
    client, key = _seed(pool)
    pool.retain(1, 7)

    assert pool.release(1, 7) == 0
    assert key not in pool._connections
    assert client.closed == 1


def test_release_without_a_hold_still_tears_down(pool):
    """A reload can leave the server holding a channel nobody retained."""
    client, key = _seed(pool)

    assert pool.release(1, 7) == 0
    assert key not in pool._connections
    assert client.closed == 1


def test_close_ignores_holds(pool):
    """Deleting a session or a user must not wait for panes to let go."""
    client, key = _seed(pool)
    pool.retain(1, 7)
    pool.retain(1, 7)

    pool.close(7)

    assert key not in pool._connections
    assert client.closed == 1
    assert pool._refs.get(key) is None, "refcount outlived the connection"


def test_holds_are_scoped_per_user(pool):
    """One user's hold must not keep another user's channel alive."""
    client_a, key_a = _seed(pool, user_id=1, session_id=7)
    client_b, key_b = _seed(pool, user_id=2, session_id=7)
    pool.retain(1, 7)

    assert pool.release(2, 7) == 0
    assert key_b not in pool._connections and client_b.closed == 1
    assert key_a in pool._connections and client_a.closed == 0


def test_idle_eviction_clears_a_stale_hold(pool):
    """A crashed browser never releases; eviction is the backstop."""
    from services import sftp_service

    client, key = _seed(pool)
    pool.retain(1, 7)
    pool._connections[key].last_used = time.monotonic() - sftp_service._IDLE_TIMEOUT_SECONDS - 1

    pool._evict_idle()

    assert key not in pool._connections
    assert pool._refs.get(key) is None, "stale hold would pin the next channel open"
    assert client.closed == 1


def test_idle_eviction_spares_a_channel_mid_transfer(pool):
    """
    last_used is stamped when a transfer starts, so a long one looks idle.
    Evicting it would cut a large upload off partway through.
    """
    from services import sftp_service

    client, key = _seed(pool)
    conn = pool._connections[key]
    conn.last_used = time.monotonic() - sftp_service._IDLE_TIMEOUT_SECONDS - 60

    conn.lock.acquire()
    try:
        pool._evict_idle()
        assert key in pool._connections, "evicted a channel with a transfer running"
        assert client.closed == 0
    finally:
        conn.lock.release()

    pool._evict_idle()
    assert key not in pool._connections, "an idle, unlocked channel should still go"


def test_discard_drops_only_the_connection_it_was_given(pool, monkeypatch):
    """
    A download cut short discards its channel (read-ahead replies may still be
    in flight). It must not take out a newer connection that replaced it, and
    it closes the old one only after a grace period.
    """
    from services import pool as pool_module
    from services.sftp_service import _PooledConnection

    monkeypatch.setattr(pool_module, "_DISCARD_GRACE_SECONDS", 0.05)
    client, key = _seed(pool)
    old = pool._connections[key]

    newer = _PooledConnection(_FakeClient(), sftp=object())
    pool._connections[key] = newer
    pool.discard(1, 7, old)
    assert pool._connections[key] is newer, "discarded a connection it was not given"

    pool.discard(1, 7, newer)
    assert key not in pool._connections
    assert newer.client.closed == 0, "closed before the grace period"
    time.sleep(0.2)
    assert newer.client.closed == 1
