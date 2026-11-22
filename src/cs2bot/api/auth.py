"""Optional session-cookie authentication and login rate limiting.

Auth is enabled when ``settings.api.password`` is set. The session cookie holds an
``itsdangerous``-signed token (username, random session id, credential fingerprint) and is
``HttpOnly`` + ``SameSite=Strict``. Changing the username or password invalidates every session;
logout revokes the session id for the rest of the token's lifetime. Secrets are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import SecretStr

_SALT = "cs2bot.api.session"


@dataclass(frozen=True)
class SessionInfo:
    username: str
    sid: str


class SessionAuth:
    """Credential check + signed session tokens (stateless apart from a small revocation list)."""

    def __init__(
        self,
        *,
        username: str,
        password: SecretStr | None,
        secret: SecretStr | None,
        max_age_s: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = password is not None
        self.username = username
        self.max_age_s = max(60, int(max_age_s))
        self.ephemeral_secret = secret is None
        secret_value = secret.get_secret_value() if secret is not None else secrets.token_urlsafe(32)
        self._key = hashlib.sha256(("cs2bot-api:" + secret_value).encode()).digest()
        self._serializer = URLSafeTimedSerializer(secret_value, salt=_SALT)
        password_bytes = password.get_secret_value().encode() if password is not None else b""
        self._expected_user = self._digest(username.encode())
        self._expected_password = self._digest(password_bytes)
        # Sessions are bound to the current credentials: rotating either one logs everybody out.
        self._fingerprint = hmac.new(
            self._key, b"fp\x00" + username.encode() + b"\x00" + password_bytes, hashlib.sha256
        ).hexdigest()[:24]
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def _digest(self, value: bytes) -> bytes:
        # Comparing fixed-length keyed digests keeps the comparison constant-time even when the
        # submitted value has a different length than the real one.
        return hmac.new(self._key, value, hashlib.sha256).digest()

    def check_credentials(self, username: str, password: str) -> bool:
        if not self.enabled:
            return True
        user_ok = hmac.compare_digest(self._digest(username.encode()), self._expected_user)
        password_ok = hmac.compare_digest(self._digest(password.encode()), self._expected_password)
        return user_ok and password_ok

    def issue(self) -> str:
        """New signed session token for the configured user."""
        return str(
            self._serializer.dumps(
                {"u": self.username, "sid": secrets.token_urlsafe(16), "fp": self._fingerprint}
            )
        )

    def verify(self, token: str | None) -> SessionInfo | None:
        """Session for a cookie value, or ``None`` if missing, forged, expired, stale or revoked."""
        if not self.enabled or not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=self.max_age_s)
        except BadSignature:  # includes SignatureExpired
            return None
        if not isinstance(data, dict):
            return None
        sid = data.get("sid")
        if data.get("u") != self.username or data.get("fp") != self._fingerprint or not isinstance(sid, str):
            return None
        with self._lock:
            if sid in self._revoked:
                return None
        return SessionInfo(username=self.username, sid=sid)

    def revoke(self, sid: str) -> None:
        now = self._clock()
        with self._lock:
            self._revoked = {k: exp for k, exp in self._revoked.items() if exp > now}
            self._revoked[sid] = now + self.max_age_s


class LoginRateLimiter:
    """Sliding-window limit on failed logins, per client address and globally."""

    def __init__(
        self,
        *,
        max_failures: int,
        global_max_failures: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max(1, max_failures)
        self.global_max_failures = max(1, global_max_failures)
        self.window_s = max(1.0, window_s)
        self._clock = clock
        self._by_client: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window_s:
            q.popleft()

    def retry_after(self, client: str) -> float | None:
        """Seconds the client must wait before the next attempt, or ``None`` if it may try now."""
        now = self._clock()
        with self._lock:
            waits: list[float] = []
            q = self._by_client.get(client)
            if q is not None:
                self._prune(q, now)
                if len(q) >= self.max_failures:
                    waits.append(q[0] + self.window_s - now)
                if not q:
                    self._by_client.pop(client, None)
            self._prune(self._global, now)
            if len(self._global) >= self.global_max_failures:
                waits.append(self._global[0] + self.window_s - now)
        return max(1.0, max(waits)) if waits else None

    def record_failure(self, client: str) -> None:
        now = self._clock()
        with self._lock:
            self._by_client.setdefault(client, deque()).append(now)
            self._global.append(now)

    def reset(self, client: str) -> None:
        with self._lock:
            self._by_client.pop(client, None)
