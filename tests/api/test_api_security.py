"""Host-header allow-list, CORS, security headers, session auth + rate limiting, SPA serving, server."""

from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import itsdangerous.timed
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cs2bot.api import ApiOptions, create_app, make_server, run
from cs2bot.config import Settings
from cs2bot.db.base import Base
from cs2bot.db.models import AuditLog
from cs2bot.db.session import make_engine
from tests.api.api_seed import BASE_URL, CSRF, Seed, make_settings

# ------------------------------------------------------------------ Host header (DNS rebinding)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("127.0.0.1:8000", True),
        ("127.0.0.1", True),
        ("localhost:8000", True),
        ("LOCALHOST", True),
        ("[::1]:8000", True),
        ("[::1]", True),
        ("localhost:5173", True),  # Vite dev proxy (port of a CORS origin)
        ("localhost:9999", False),
        ("evil.example", False),
        ("evil.example:8000", False),
        ("127.0.0.1.nip.io:8000", False),
        ("localhost.", False),
        ("127.0.0.1:8000@evil.example", False),
        ("[::1", False),
        ("", False),
    ],
)
def test_host_header_allow_list(client: TestClient, host: str, allowed: bool) -> None:
    resp = client.get("/api/ping", headers={"host": host})
    if allowed:
        assert resp.status_code == 200
    else:
        assert resp.status_code == 400 and resp.json() == {"detail": "invalid host header"}


def test_host_check_applies_to_mutations_and_static(client: TestClient) -> None:
    resp = client.post("/api/control/pause", headers={**CSRF, "host": "attacker.example:8000"})
    assert resp.status_code == 400
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 400


def test_configured_lan_host_allowed(app_factory, seeded: Seed, tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        api={"host": "192.168.1.50", "password": SecretStr("pw"), "session_secret": SecretStr("k" * 32)},
    )
    with TestClient(app_factory(settings=settings), base_url="http://192.168.1.50:8000") as c:
        assert c.get("/api/ping").status_code == 200
        assert c.get("/api/ping", headers={"host": "192.168.1.51:8000"}).status_code == 400
        assert c.get("/api/ping", headers={"host": "localhost:8000"}).status_code == 200


def test_extra_allowed_hosts_option(app_factory, seeded: Seed) -> None:
    app = app_factory(options=ApiOptions(extra_allowed_hosts=("mypc.lan:8443", "bad host!")))
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/api/ping", headers={"host": "mypc.lan:8443"}).status_code == 200
        assert c.get("/api/ping", headers={"host": "mypc.lan"}).status_code == 200
        assert c.get("/api/ping", headers={"host": "other.lan"}).status_code == 400


# ------------------------------------------------------------------ headers / CORS / docs


def test_security_headers(client: TestClient) -> None:
    api = client.get("/api/matches", params={"limit": 1})
    assert api.headers["x-frame-options"] == "DENY"
    assert api.headers["x-content-type-options"] == "nosniff"
    assert api.headers["cache-control"] == "no-store"
    assert api.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in api.headers["content-security-policy"]
    root = client.get("/")
    assert root.headers["x-frame-options"] == "DENY" and "cache-control" not in root.headers


def test_cors_preflight(client: TestClient) -> None:
    allowed = client.options(
        "/api/control/pause",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with,content-type",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "x-requested-with" in allowed.headers["access-control-allow-headers"].lower()
    denied = client.options(
        "/api/control/pause",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert denied.status_code == 400 and "access-control-allow-origin" not in denied.headers
    simple = client.get("/api/ping", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


def test_cors_wildcard_is_ignored(app_factory, seeded: Seed) -> None:
    app = app_factory(api={"cors_origins": ["*", "http://localhost:3000/"]})
    with TestClient(app, base_url=BASE_URL) as c:
        resp = c.get("/api/ping", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in resp.headers
        ok = c.get("/api/ping", headers={"Origin": "http://localhost:3000"})
        assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
        forged = c.post("/api/control/pause", headers={**CSRF, "Origin": "https://evil.example"})
        assert forged.status_code == 403


def test_openapi_and_docs(client: TestClient) -> None:
    spec = client.get("/api/openapi.json").json()
    assert "/api/matches/{match_id}" in spec["paths"] and "/api/control/kill-switch" in spec["paths"]
    assert client.get("/api/docs").status_code == 200


# ------------------------------------------------------------------ auth


def _login(c: TestClient, password: str = "s3cret-pass", username: str = "admin") -> Any:
    return c.post("/api/auth/login", json={"username": username, "password": password}, headers=CSRF)


def test_auth_disabled(client: TestClient) -> None:
    assert client.get("/api/auth/me").json() == {
        "auth_enabled": False,
        "authenticated": True,
        "username": None,
    }
    assert client.get("/api/matches").status_code == 200
    assert _login(client).json()["auth_enabled"] is False
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200


def test_auth_login_flow(app_factory, seeded: Seed, auth_settings: Settings, session_factory) -> None:
    with TestClient(app_factory(settings=auth_settings), base_url=BASE_URL) as c:
        assert c.get("/api/matches").status_code == 401
        assert c.get("/api/health").status_code == 401
        assert c.post("/api/control/pause", headers=CSRF).status_code == 401
        assert c.get("/api/ping").status_code == 200
        assert c.get("/api/auth/me").json() == {
            "auth_enabled": True,
            "authenticated": False,
            "username": None,
        }
        assert (
            c.post("/api/auth/login", json={"username": "admin", "password": "s3cret-pass"}).status_code
            == 403
        )
        wrong = _login(c, "nope")
        assert wrong.status_code == 401 and "set-cookie" not in wrong.headers
        assert _login(c, username="root").status_code == 401

        ok = _login(c)
        assert ok.status_code == 200 and ok.json() == {
            "auth_enabled": True,
            "authenticated": True,
            "username": "admin",
        }
        raw_cookie = ok.headers["set-cookie"]
        morsel = SimpleCookie(raw_cookie)["cs2bot_session"]
        assert morsel["httponly"] is True and morsel["samesite"].lower() == "strict"
        assert morsel["path"] == "/" and int(morsel["max-age"]) == 43200
        assert "s3cret-pass" not in raw_cookie
        token = morsel.value

        assert c.get("/api/auth/me").json()["authenticated"] is True
        assert c.get("/api/matches").status_code == 200
        paused = c.post("/api/control/pause", headers=CSRF)
        assert paused.status_code == 200 and paused.json()["paused"]["updated_by"] == "dashboard:admin"
        assert c.post("/api/control/resume").status_code == 403  # CSRF still required when logged in

        assert c.post("/api/auth/logout", headers=CSRF).json()["authenticated"] is False
        assert c.get("/api/matches").status_code == 401
        c.cookies.set("cs2bot_session", token)  # replaying the old cookie after logout
        assert c.get("/api/matches").status_code == 401
        c.cookies.set("cs2bot_session", token[:-2] + "xx")
        assert c.get("/api/matches").status_code == 401

    with session_factory() as s:
        from sqlalchemy import select

        actions = [a.action for a in s.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert actions[0] == "auth_login" and "auth_logout" in actions and "pause" in actions


def test_validation_errors_do_not_echo_input(
    app_factory, seeded: Seed, auth_settings: Settings, client: TestClient
) -> None:
    bad_query = client.get("/api/matches", params={"limit": "lots"})
    assert bad_query.status_code == 422 and "lots" not in bad_query.text
    assert bad_query.json()["detail"][0]["loc"] == ["query", "limit"]
    with TestClient(app_factory(settings=auth_settings), base_url=BASE_URL) as c:
        resp = c.post("/api/auth/login", json={"password": "hunter2-secret"}, headers=CSRF)
        assert resp.status_code == 422
        assert "hunter2-secret" not in resp.text
        assert resp.json()["detail"] == [
            {"loc": ["body", "username"], "msg": "Field required", "type": "missing"}
        ]


def test_auth_rate_limit(app_factory, seeded: Seed, auth_settings: Settings) -> None:
    app = app_factory(settings=auth_settings, options=ApiOptions(login_max_failures=3, login_window_s=60))
    with TestClient(app, base_url=BASE_URL) as c:
        for _ in range(3):
            assert _login(c, "wrong").status_code == 401
        blocked = _login(c)  # even the right password is refused while blocked
        assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
        assert c.get("/api/matches").status_code == 401


def test_session_expiry(
    app_factory, seeded: Seed, auth_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app_factory(settings=auth_settings), base_url=BASE_URL) as c:
        assert _login(c).status_code == 200
        assert c.get("/api/matches").status_code == 200
        real_time = itsdangerous.timed.time

        class _Later:
            @staticmethod
            def time() -> float:
                return real_time.time() + 43200 + 5

        monkeypatch.setattr(itsdangerous.timed, "time", _Later)
        assert c.get("/api/matches").status_code == 401


def test_sessions_bound_to_credentials(app_factory, seeded: Seed, tmp_path: Path) -> None:
    def settings(password: str) -> Settings:
        return make_settings(
            tmp_path, api={"password": SecretStr(password), "session_secret": SecretStr("z" * 32)}
        )

    with TestClient(app_factory(settings=settings("first")), base_url=BASE_URL) as c:
        token = SimpleCookie(_login(c, "first").headers["set-cookie"])["cs2bot_session"].value
    with TestClient(app_factory(settings=settings("second")), base_url=BASE_URL) as c:
        c.cookies.set("cs2bot_session", token)
        assert c.get("/api/matches").status_code == 401  # password rotated -> old sessions invalid


def test_password_without_secret_uses_ephemeral_secret(app_factory, seeded: Seed, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api={"password": SecretStr("pw")})
    with TestClient(app_factory(settings=settings), base_url=BASE_URL) as c:
        assert _login(c, "pw").status_code == 200
        assert c.get("/api/matches").status_code == 200


# ------------------------------------------------------------------ static dashboard


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>cs2bot</title><div id=root></div>")
    (d / "assets" / "app-abc123.js").write_text("console.log('dashboard')")
    (d / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("top secret")
    return d


def test_spa_serving_and_fallback(app_factory, seeded: Seed, dist: Path) -> None:
    with TestClient(app_factory(api={"dashboard_dist_dir": str(dist)}), base_url=BASE_URL) as c:
        index = c.get("/")
        assert index.status_code == 200 and "<div id=root>" in index.text
        assert index.headers["cache-control"] == "no-cache"
        asset = c.get("/assets/app-abc123.js")
        assert asset.status_code == 200 and "dashboard" in asset.text
        assert "immutable" in asset.headers["cache-control"]
        assert c.get("/favicon.svg").text == "<svg/>"
        deep = c.get("/matches/101")
        assert deep.status_code == 200 and "<div id=root>" in deep.text
        assert c.get("/assets/missing-999.js").status_code == 404
        api_404 = c.get("/api/does-not-exist")
        assert api_404.status_code == 404 and api_404.json() == {"detail": "not found"}
        assert c.get("/api/matches/101").json()["match"]["id"] == 101  # API still wins
        for sneaky in ("/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
            resp = c.get(sneaky)
            assert "top secret" not in resp.text, sneaky


def test_dashboard_disabled_or_missing(app_factory, seeded: Seed, dist: Path) -> None:
    with TestClient(
        app_factory(api={"dashboard_dist_dir": str(dist), "serve_dashboard": False}), base_url=BASE_URL
    ) as c:
        hint = c.get("/").json()
        assert hint["name"] == "cs2bot API" and hint["api_docs"] == "/api/docs"
        assert c.get("/matches/101").status_code == 404
    with TestClient(app_factory(), base_url=BASE_URL) as c:  # dist dir missing
        assert "not built" in c.get("/").json()["dashboard"]


# ------------------------------------------------------------------ server helpers


def test_make_server_and_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session_factory) -> None:
    settings = make_settings(tmp_path, api={"port": 8123})
    server = make_server(settings, session_factory)
    assert isinstance(server, uvicorn.Server)
    assert server.config.host == "127.0.0.1" and server.config.port == 8123
    assert server.config.proxy_headers is False and server.config.server_header is False

    seen: dict[str, Any] = {}

    def fake_run(self: uvicorn.Server, sockets: Any = None) -> None:
        seen["host"], seen["port"] = self.config.host, self.config.port

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    run(settings, session_factory)
    assert seen == {"host": "127.0.0.1", "port": 8123}


def test_create_app_defaults_build_own_session_factory(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/fresh.db"
    app = create_app(make_settings(tmp_path, database={"url": url}))
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/api/ping").json() == {"status": "ok"}
        h = c.get("/api/health").json()  # reachable, but no schema yet
        assert h["db"]["ok"] is False and next(a["code"] for a in h["alerts"]) == "db_error"
        engine = make_engine(url)
        Base.metadata.create_all(engine)
        engine.dispose()
        assert c.get("/api/health").json()["db"]["ok"] is True


def test_database_errors_become_json(app_factory, seeded: Seed) -> None:
    from sqlalchemy.exc import IntegrityError, OperationalError

    app = app_factory()

    def duplicate() -> None:
        raise IntegrityError("INSERT INTO bets ...", {"idempotency_key": "k"}, Exception("duplicate key"))

    def gone() -> None:
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))

    app.add_api_route("/api/test-duplicate", duplicate)
    app.add_api_route("/api/test-gone", gone)
    with TestClient(app, base_url=BASE_URL) as c:
        dup = c.get("/api/test-duplicate")
        assert dup.status_code == 500 and dup.json() == {"detail": "database error"}
        down = c.get("/api/test-gone")
        assert down.status_code == 503 and down.json() == {"detail": "database unavailable"}
