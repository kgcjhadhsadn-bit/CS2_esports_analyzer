"""Cross-platform browser-profile lock: exclusivity, stale detection, heartbeat, safe release."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cs2bot.executor.lock import LOCK_FILENAME, LockInfo, ProfileLock, ProfileLockedError, pid_alive


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 22, 12, 0, tzinfo=UTC))


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_lock(path: Path, *, pid: int, host: str, heartbeat: datetime, token: str = "other") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "host": host,
                "token": token,
                "created_at": heartbeat.isoformat(),
                "heartbeat_at": heartbeat.isoformat(),
            }
        ),
        encoding="utf-8",
    )


def test_acquire_writes_pid_and_timestamp_and_release_removes(tmp_path: Path, clock: Clock) -> None:
    lock = ProfileLock(tmp_path / "profile", heartbeat_s=None, clock=clock)
    lock.acquire()
    assert lock.held
    info = lock.read_info()
    assert info is not None
    assert info.pid == os.getpid() and info.host == socket.gethostname()
    assert info.created_at == clock.now and info.heartbeat_at == clock.now
    assert lock.path == tmp_path / "profile" / LOCK_FILENAME
    lock.acquire()  # re-entrant no-op
    lock.release()
    assert not lock.held and not lock.path.exists()
    lock.release()  # idempotent


def test_second_lock_on_same_profile_is_refused(tmp_path: Path, clock: Clock) -> None:
    first = ProfileLock(tmp_path, heartbeat_s=None, clock=clock)
    second = ProfileLock(tmp_path, heartbeat_s=None, clock=clock)
    first.acquire()
    with pytest.raises(ProfileLockedError) as err:
        second.acquire()
    assert err.value.holder is not None and err.value.holder.pid == os.getpid()
    assert "in use by PID" in str(err.value)
    first.release()
    second.acquire()  # free again
    second.release()


def test_live_lock_from_another_process_is_respected(tmp_path: Path, clock: Clock) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    parent_pid = os.getppid()  # certainly alive
    _write_lock(lock_path, pid=parent_pid, host=socket.gethostname(), heartbeat=clock.now)
    with pytest.raises(ProfileLockedError):
        ProfileLock(tmp_path, heartbeat_s=None, clock=clock).acquire()


def test_lock_of_a_dead_process_is_broken_immediately(tmp_path: Path, clock: Clock) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    _write_lock(lock_path, pid=_dead_pid(), host=socket.gethostname(), heartbeat=clock.now)
    lock = ProfileLock(tmp_path, heartbeat_s=None, clock=clock)
    lock.acquire()
    info = lock.read_info()
    assert info is not None and info.pid == os.getpid()
    assert not list(tmp_path.glob(f"{LOCK_FILENAME}.stale-*"))  # the moved stale file is cleaned up
    lock.release()


def test_lock_with_old_heartbeat_is_stale(tmp_path: Path, clock: Clock) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    # another host: the PID cannot be checked, only the heartbeat age counts
    _write_lock(lock_path, pid=12345, host="other-pc", heartbeat=clock.now)
    lock = ProfileLock(tmp_path, stale_after_s=600, heartbeat_s=None, clock=clock)
    clock.advance(599)
    with pytest.raises(ProfileLockedError):
        lock.acquire()
    clock.advance(2)
    lock.acquire()
    assert lock.held
    lock.release()


def test_corrupt_lock_file_uses_file_age(tmp_path: Path) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    lock_path.write_text("{not json", encoding="utf-8")
    fresh = ProfileLock(tmp_path, stale_after_s=600, heartbeat_s=None)
    with pytest.raises(ProfileLockedError):
        fresh.acquire()  # might be a lock being written right now
    old = datetime.now(UTC).timestamp() - 3600
    os.utime(lock_path, (old, old))
    fresh.acquire()
    assert fresh.held
    fresh.release()


def test_refresh_updates_heartbeat_and_detects_takeover(tmp_path: Path, clock: Clock) -> None:
    lock = ProfileLock(tmp_path, heartbeat_s=None, clock=clock)
    lock.acquire()
    clock.advance(120)
    lock.refresh()
    info = lock.read_info()
    assert info is not None and info.heartbeat_at == clock.now and info.created_at < clock.now
    # someone else replaced the file: we notice and stop touching it, and release leaves it alone
    _write_lock(lock.path, pid=1, host="other-pc", heartbeat=clock.now, token="thief")
    lock.refresh()
    assert lock.lost
    lock.release()
    assert lock.path.exists()
    stolen = LockInfo.from_json(lock.path.read_text(encoding="utf-8"))
    assert stolen is not None and stolen.token == "thief"


def test_heartbeat_thread_keeps_the_lock_fresh(tmp_path: Path) -> None:
    lock = ProfileLock(tmp_path, stale_after_s=0.4, heartbeat_s=0.05)
    with lock:
        first = lock.read_info()
        assert first is not None
        deadline = datetime.now(UTC) + timedelta(seconds=3)
        while datetime.now(UTC) < deadline:
            info = lock.read_info()
            if info is not None and info.heartbeat_at > first.heartbeat_at:
                break
        else:  # pragma: no cover - would mean the heartbeat thread is dead
            pytest.fail("heartbeat did not refresh the lock")
    assert not lock.path.exists()


def test_constructor_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ProfileLock(tmp_path, stale_after_s=0)
    with pytest.raises(ValueError):
        ProfileLock(tmp_path, stale_after_s=10, heartbeat_s=6)


def test_lock_info_rejects_naive_or_broken_json() -> None:
    assert LockInfo.from_json("[]") is None
    assert (
        LockInfo.from_json('{"pid": 1, "host": "h", "token": "t", "created_at": "2026-01-01T00:00:00"}')
        is None
    )


def test_pid_alive() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(-5)
    assert not pid_alive(_dead_pid())
