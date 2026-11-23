"""Cross-platform lock guarding the dedicated Betclic browser profile.

Two processes must never drive the same Chromium profile (``cs2bot up`` plus, say, ``cs2bot executor
login``): Chromium would hand the second launch over to the first browser and Playwright would fail in
confusing ways, or worse, two bots would share one logged-in session.

The lock is a JSON file created with ``O_CREAT | O_EXCL`` (atomic on Windows and POSIX) holding the PID,
host, a random token and timestamps. A daemon thread refreshes ``heartbeat_at``; a lock is *stale* (and
may be broken) when its heartbeat is older than ``stale_after_s`` or, on the same host, when its PID is
no longer running.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from cs2bot.executor.base import ExecutorError
from cs2bot.utils.logging import get_logger
from cs2bot.utils.time import utcnow

log = get_logger(__name__)

LOCK_FILENAME = "cs2bot-executor.lock"


def pid_alive(pid: int) -> bool:
    """Whether a process with this PID exists on this machine (best effort, never signals it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE  # pointer-sized; the default c_int would truncate
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return bool(ctypes.get_last_error() == error_access_denied)
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return bool(code.value == still_active)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0: existence check only (POSIX)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class LockInfo:
    pid: int
    host: str
    token: str
    created_at: datetime
    heartbeat_at: datetime

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "host": self.host,
                "token": self.token,
                "created_at": self.created_at.isoformat(),
                "heartbeat_at": self.heartbeat_at.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, text: str) -> LockInfo | None:
        try:
            data: dict[str, Any] = json.loads(text)
            created = datetime.fromisoformat(str(data["created_at"]))
            heartbeat = datetime.fromisoformat(str(data.get("heartbeat_at", data["created_at"])))
            if created.tzinfo is None or heartbeat.tzinfo is None:
                return None
            return cls(
                pid=int(data["pid"]),
                host=str(data["host"]),
                token=str(data["token"]),
                created_at=created,
                heartbeat_at=heartbeat,
            )
        except (ValueError, KeyError, TypeError):
            return None


class ProfileLockedError(ExecutorError):
    """Another live process holds the browser-profile lock."""

    def __init__(self, path: Path, holder: LockInfo | None) -> None:
        self.path = path
        self.holder = holder
        who = (
            f"PID {holder.pid} on {holder.host} (since {holder.created_at:%Y-%m-%d %H:%M:%S}Z, "
            f"last heartbeat {holder.heartbeat_at:%H:%M:%S}Z)"
            if holder
            else "another process"
        )
        super().__init__(
            f"the Betclic browser profile is in use by {who}; lock file {path}. Use the running bot "
            f"(dashboard) or stop it first. A crashed holder's lock expires automatically."
        )


class ProfileLock:
    """Exclusive, heartbeat-refreshed lock file for one browser-profile directory."""

    def __init__(
        self,
        profile_dir: Path,
        *,
        stale_after_s: float = 600.0,
        heartbeat_s: float | None = 30.0,
        filename: str = LOCK_FILENAME,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        if heartbeat_s is not None and heartbeat_s * 2 > stale_after_s:
            raise ValueError("heartbeat_s must be at most half of stale_after_s")
        self.profile_dir = Path(profile_dir)
        self.path = self.profile_dir / filename
        self.stale_after_s = stale_after_s
        self.heartbeat_s = heartbeat_s
        self._clock = clock
        self._token = uuid.uuid4().hex
        self._host = socket.gethostname()
        self._held = False
        self._lost = False
        self._created_at: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._io_lock = threading.Lock()

    # ------------------------------------------------------------------ public API

    @property
    def held(self) -> bool:
        return self._held

    @property
    def lost(self) -> bool:
        """True if our lock file was removed/replaced by someone else while we held it."""
        return self._lost

    def read_info(self) -> LockInfo | None:
        try:
            return LockInfo.from_json(self.path.read_text(encoding="utf-8"))
        except OSError:
            return None

    def acquire(self) -> None:
        """Take the lock or raise :class:`ProfileLockedError` (stale locks are broken first).

        A no-op while we hold the lock. If our lock was *lost* (the file was deleted, or another process
        took it over after judging it stale - e.g. the PC slept longer than ``stale_after_s``), it is
        taken again from scratch: that fails with :class:`ProfileLockedError` when someone else holds it.
        """
        if self._held and not self._lost:
            return
        if self._held:  # lost: forget the old lock (release() never deletes a file that is not ours)
            self.release()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            now = self._clock()
            info = LockInfo(os.getpid(), self._host, self._token, now, now)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                holder = self.read_info()
                if self._is_stale(holder):
                    self._break_stale(holder)
                    continue
                raise ProfileLockedError(self.path, holder) from None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(info.to_json())
                fh.flush()
                os.fsync(fh.fileno())
            self._held = True
            self._lost = False
            self._created_at = now
            self._start_heartbeat()
            log.info("acquired browser-profile lock %s", self.path)
            return
        raise ProfileLockedError(self.path, self.read_info())

    def refresh(self) -> None:
        """Rewrite ``heartbeat_at`` (called by the heartbeat thread; safe to call manually)."""
        with self._io_lock:
            if not self._held:
                return
            current = self.read_info()
            if current is None or current.token != self._token:
                if not self._lost:
                    log.error("browser-profile lock %s was taken over by another process", self.path)
                self._lost = True
                return
            now = self._clock()
            info = LockInfo(os.getpid(), self._host, self._token, self._created_at or now, now)
            tmp = self.path.with_name(f"{self.path.name}.{self._token[:8]}.tmp")
            try:
                tmp.write_text(info.to_json(), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:  # Windows: the file may be open in another process; retry next tick
                log.debug("lock heartbeat failed: %s", exc)
                with contextlib.suppress(OSError):
                    tmp.unlink()

    def verify(self) -> bool:
        """Re-read the lock file now: ``True`` only if we hold the lock and the file is still ours."""
        self.refresh()
        return self._held and not self._lost

    def release(self) -> None:
        """Stop the heartbeat and delete the lock file if it is still ours."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._io_lock:
            if not self._held:
                return
            self._held = False
            current = self.read_info()
            if current is not None and current.token == self._token:
                try:
                    self.path.unlink()
                    log.info("released browser-profile lock %s", self.path)
                except OSError as exc:
                    log.warning("could not delete lock file %s: %s", self.path, exc)

    def __enter__(self) -> ProfileLock:
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()

    # ------------------------------------------------------------------ internals

    def _age_s(self, holder: LockInfo | None) -> float | None:
        if holder is not None:
            return (self._clock() - holder.heartbeat_at).total_seconds()
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return None
        return self._clock().timestamp() - datetime.fromtimestamp(mtime, tz=UTC).timestamp()

    def _is_stale(self, holder: LockInfo | None) -> bool:
        same_host_other_pid = holder is not None and holder.host == self._host and holder.pid != os.getpid()
        if same_host_other_pid and holder is not None and not pid_alive(holder.pid):
            return True
        age = self._age_s(holder)
        if age is None:  # the file vanished meanwhile -> just retry
            return True
        return age > self.stale_after_s

    def _break_stale(self, holder: LockInfo | None) -> None:
        """Move the stale lock aside (atomic rename) and make sure we moved the one we judged."""
        grave = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex[:8]}")
        try:
            os.rename(self.path, grave)
        except FileNotFoundError:
            return  # someone else removed it; retry the exclusive create
        except OSError as exc:
            log.warning("could not break stale lock %s: %s", self.path, exc)
            return
        moved = None
        with contextlib.suppress(OSError):
            moved = LockInfo.from_json(grave.read_text(encoding="utf-8"))
        if holder is not None and (moved is None or moved.token != holder.token):
            # A racing process re-created the lock between our read and rename: put it back.
            try:
                os.link(grave, self.path)
            except OSError:
                log.error("lost a race while breaking stale lock %s", self.path)
            with contextlib.suppress(OSError):
                grave.unlink()
            raise ProfileLockedError(self.path, moved)
        with contextlib.suppress(OSError):
            grave.unlink()
        if holder is not None:
            log.warning(
                "broke stale browser-profile lock of PID %s on %s (last heartbeat %s)",
                holder.pid,
                holder.host,
                holder.heartbeat_at.isoformat(),
            )
        else:
            log.warning("broke stale/corrupt browser-profile lock %s", self.path)

    def _start_heartbeat(self) -> None:
        if self.heartbeat_s is None or self.heartbeat_s <= 0:
            return
        self._stop = threading.Event()
        interval = self.heartbeat_s
        stop = self._stop

        def _loop() -> None:
            while not stop.wait(interval):
                try:
                    self.refresh()
                except Exception:  # never let the heartbeat thread die silently
                    log.exception("browser-profile lock heartbeat failed")

        self._thread = threading.Thread(target=_loop, name="cs2bot-profile-lock", daemon=True)
        self._thread.start()
