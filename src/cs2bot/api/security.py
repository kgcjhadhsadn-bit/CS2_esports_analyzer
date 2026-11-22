"""Request-level protections for a dashboard that runs on the owner's PC.

* :class:`LocalSecurityMiddleware` rejects requests whose ``Host`` header is not a known local name
  (blocks DNS-rebinding: a malicious site that re-points its domain to 127.0.0.1 still sends its
  own domain in ``Host``) and adds anti-framing / no-sniff / no-store headers.
* :class:`CSRFMiddleware` requires ``X-Requested-With: cs2bot`` on every state-changing request.
  Browsers only let a cross-site page add a custom header after a CORS preflight, and CORS only
  allows ``settings.api.cors_origins``, so a forged form post or ``fetch`` from another site fails.
  A present ``Origin`` header must also be one of the allowed origins.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cs2bot.config import APISettings
from cs2bot.utils.logging import get_logger

log = get_logger(__name__)

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "cs2bot"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::", ""})

_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_")
_IPV6_CHARS = frozenset("0123456789abcdef:.")


def split_host_header(value: str) -> tuple[str, int | None] | None:
    """Split a ``Host`` header into ``(hostname, port)``; ``None`` if it is malformed.

    The hostname is lower-cased and IPv6 brackets are removed (``"[::1]:8000"`` -> ``("::1", 8000)``).
    """
    text = value.strip().lower()
    if not text:
        return None
    port_text: str | None
    if text.startswith("["):
        end = text.find("]")
        if end <= 1:
            return None
        host = text[1:end]
        if not set(host) <= _IPV6_CHARS:
            return None
        rest = text[end + 1 :]
        if not rest:
            port_text = None
        elif rest.startswith(":"):
            port_text = rest[1:]
        else:
            return None
    else:
        if text.count(":") > 1:  # an IPv6 literal must be bracketed in a Host header
            return None
        host, sep, tail = text.partition(":")
        port_text = tail if sep else None
        if not host or not set(host) <= _HOST_CHARS:
            return None
    if port_text is None:
        return host, None
    if not port_text.isdigit() or len(port_text) > 5 or int(port_text) > 65535:
        return None
    return host, int(port_text)


def _machine_names() -> set[str]:
    """This PC's host name(s) and addresses - only used when the API binds to all interfaces."""
    names: set[str] = set()
    try:
        hostname = socket.gethostname()
        names.add(hostname.lower())
        _, aliases, addresses = socket.gethostbyname_ex(hostname)
        names.update(a.lower() for a in aliases)
        names.update(addresses)
    except OSError:  # pragma: no cover - depends on the machine's resolver
        pass
    return names


def _origin_port(url: str) -> tuple[str, str, int | None] | None:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    return parts.scheme.lower(), parts.hostname.lower(), port


@dataclass(frozen=True)
class AllowedHosts:
    """Host header allow-list: allowed host names x allowed ports (``None`` = no port given)."""

    hostnames: frozenset[str]
    ports: frozenset[int | None]

    def is_allowed(self, header: str | None) -> bool:
        if header is None:
            return False
        parsed = split_host_header(header)
        if parsed is None:
            return False
        host, port = parsed
        return host in self.hostnames and port in self.ports

    def origins(self) -> frozenset[str]:
        """``http://host[:port]`` origins of the dashboard itself (same-origin requests)."""
        out: set[str] = set()
        for host in self.hostnames:
            shown = f"[{host}]" if ":" in host else host
            for port in self.ports:
                out.add(f"http://{shown}" if port is None else f"http://{shown}:{port}")
        return frozenset(out)


def build_allowed_hosts(api: APISettings, extra: Iterable[str] = ()) -> AllowedHosts:
    """Loopback names, the configured host (when not loopback) and ``extra``; with or without port.

    Accepted ports: none, ``api.port``, and the ports of ``api.cors_origins`` (a Vite dev server
    that proxies ``/api`` without rewriting ``Host`` sends e.g. ``localhost:5173``).
    """
    hostnames: set[str] = set(LOOPBACK_HOSTNAMES)
    ports: set[int | None] = {None, api.port}
    configured = api.host.strip().lower().strip("[]")
    if configured in WILDCARD_BIND_HOSTS:
        hostnames |= _machine_names()
    else:
        hostnames.add(configured)
    for origin in api.cors_origins:
        parsed = _origin_port(origin)
        if parsed is not None and parsed[2] is not None:
            ports.add(parsed[2])
    for item in extra:
        parsed_host = split_host_header(item)
        if parsed_host is None:
            log.warning("ignoring malformed extra allowed host %r", item)
            continue
        hostnames.add(parsed_host[0])
        if parsed_host[1] is not None:
            ports.add(parsed_host[1])
    return AllowedHosts(frozenset(hostnames), frozenset(ports))


def safe_cors_origins(api: APISettings) -> list[str]:
    """CORS origins without the ``*`` wildcard (which would defeat the CSRF protection)."""
    out: list[str] = []
    for origin in api.cors_origins:
        cleaned = origin.strip().rstrip("/")
        if not cleaned:
            continue
        if cleaned == "*":
            log.warning("ignoring '*' in api.cors_origins: list the dashboard origins explicitly")
            continue
        out.append(cleaned)
    return out


def build_allowed_origins(api: APISettings, hosts: AllowedHosts) -> frozenset[str]:
    """Origins accepted on state-changing requests: CORS origins + the dashboard's own origins."""
    return frozenset({o.lower() for o in safe_cors_origins(api)} | hosts.origins())


async def _reject(scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
    response = JSONResponse({"detail": detail}, status_code=status)
    await response(scope, receive, send)


class LocalSecurityMiddleware:
    """Outermost middleware: Host allow-list + security headers on every response."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: AllowedHosts, api_prefix: str = "/api") -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.api_prefix = api_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not self.allowed_hosts.is_allowed(headers.get("host")):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await _reject(scope, receive, send, 400, "invalid host header")
            return
        path: str = scope.get("path", "")
        is_api = path == self.api_prefix or path.startswith(self.api_prefix + "/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out.setdefault("X-Content-Type-Options", "nosniff")
                out.setdefault("X-Frame-Options", "DENY")
                out.setdefault("Referrer-Policy", "no-referrer")
                out.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
                if is_api:
                    out.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class CSRFMiddleware:
    """Require ``X-Requested-With: cs2bot`` (and an allowed ``Origin``, if sent) on mutations."""

    def __init__(self, app: ASGIApp, *, allowed_origins: frozenset[str]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if headers.get(CSRF_HEADER, "").strip().lower() != CSRF_VALUE:
            await _reject(
                scope, receive, send, 403, f"CSRF check failed: send the header '{CSRF_HEADER}: {CSRF_VALUE}'"
            )
            return
        origin = headers.get("origin")
        if origin is not None and origin.strip().rstrip("/").lower() not in self.allowed_origins:
            await _reject(scope, receive, send, 403, "CSRF check failed: origin not allowed")
            return
        await self.app(scope, receive, send)
