"""Tests for collab primitives (LWW + FileLockManager)."""
from __future__ import annotations

import time

import pytest

from cowork.collab import FileLockManager, LWWMap, LWWText, LockError


# ---------------------------------------------------------------------------
# LWWMap convergence
# ---------------------------------------------------------------------------

def test_lwwmap_basic_set_get():
    m = LWWMap("a")
    m.set("k", 1)
    assert m.get("k") == 1


def test_lwwmap_merge_converges():
    a = LWWMap("a")
    b = LWWMap("b")
    a.set("x", "from-a")
    b.set("x", "from-b")  # concurrent
    # Merge each into the other.
    snap_a = a.snapshot()
    snap_b = b.snapshot()
    a.merge(snap_b)
    b.merge(snap_a)
    # Both must agree on the same winner.
    assert a.get("x") == b.get("x")


def test_lwwmap_higher_clock_wins():
    a = LWWMap("a")
    b = LWWMap("b")
    a.set("k", 1)
    # b makes two writes -> higher clock
    b.set("k", 2)
    b.set("k", 3)
    a.merge(b.snapshot())
    assert a.get("k") == 3


def test_lwwmap_delete_tombstone():
    m = LWWMap("a")
    m.set("k", 1)
    m.delete("k")
    assert m.get("k") is None
    assert ("k", 1) not in m.items()


def test_lwwmap_requires_replica_id():
    with pytest.raises(ValueError):
        LWWMap("")


# ---------------------------------------------------------------------------
# LWWText
# ---------------------------------------------------------------------------

def test_lww_text_set_get_merge():
    a = LWWText("a", initial="hello")
    b = LWWText("b")
    b.merge(a.snapshot())
    assert b.get() == "hello"
    a.set("hi")
    b.set("hey")
    a.merge(b.snapshot())
    b.merge(a.snapshot())
    assert a.get() == b.get()


# ---------------------------------------------------------------------------
# FileLockManager
# ---------------------------------------------------------------------------

def test_lock_acquire_and_release():
    m = FileLockManager(default_ttl=5)
    lock = m.acquire("w1", "a.py", "agent1")
    assert m.is_held("w1", "a.py")
    assert m.release(lock) is True
    assert not m.is_held("w1", "a.py")


def test_lock_conflict_raises():
    m = FileLockManager(default_ttl=5)
    m.acquire("w1", "a.py", "agent1")
    with pytest.raises(LockError):
        m.acquire("w1", "a.py", "agent2")


def test_lock_same_holder_reacquires():
    m = FileLockManager(default_ttl=5)
    l1 = m.acquire("w1", "a.py", "agent1")
    l2 = m.acquire("w1", "a.py", "agent1")
    # Releasing the latest token releases the lock.
    assert m.release(l2) is True
    # Old token no longer matches.
    assert m.release(l1) is False


def test_lock_expires_after_ttl():
    m = FileLockManager(default_ttl=0.05)
    m.acquire("w1", "a.py", "agent1")
    time.sleep(0.1)
    assert not m.is_held("w1", "a.py")
    # A different holder can now acquire.
    m.acquire("w1", "a.py", "agent2")


def test_lock_refresh_extends_ttl():
    m = FileLockManager(default_ttl=0.05)
    lock = m.acquire("w1", "a.py", "agent1")
    time.sleep(0.03)
    m.refresh(lock, ttl=5)
    time.sleep(0.05)
    assert m.is_held("w1", "a.py")


def test_lock_refresh_after_expiry_raises():
    m = FileLockManager(default_ttl=0.02)
    lock = m.acquire("w1", "a.py", "agent1")
    time.sleep(0.05)
    # Another holder takes over the path.
    m.acquire("w1", "a.py", "agent2")
    with pytest.raises(LockError):
        m.refresh(lock)


def test_list_for_workspace_excludes_other_workspaces():
    m = FileLockManager(default_ttl=5)
    m.acquire("w1", "a.py", "h1")
    m.acquire("w2", "b.py", "h1")
    locks = m.list_for_workspace("w1")
    assert len(locks) == 1 and locks[0].path == "a.py"
