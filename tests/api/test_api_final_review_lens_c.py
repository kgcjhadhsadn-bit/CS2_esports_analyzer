"""Final review, Lens C: confidence tiers and the new decision switches through the dashboard API."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.api_seed import CSRF


def test_config_shows_the_staking_tiers_and_the_new_switches(client: TestClient) -> None:
    body = client.get("/api/control/config").json()
    bank, dec = body["effective"]["bankroll"], body["effective"]["decision"]
    assert bank["staking_mode"] == "confidence_tiers"
    assert bank["confidence_tiers"] == [[0.75, 0.01], [0.85, 0.015], [0.92, 0.02]]
    assert dec["min_ev"] == -1.0
    assert dec["require_profitable_win"] is True and dec["use_confidence_guard"] is True


def test_valid_tiers_can_be_edited(client: TestClient) -> None:
    resp = client.put(
        "/api/control/config",
        json={"bankroll": {"confidence_tiers": [[0.78, 0.005], [0.88, 0.01], [0.95, 0.02]]}},
        headers=CSRF,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective"]["bankroll"]["confidence_tiers"] == [
        [0.78, 0.005],
        [0.88, 0.01],
        [0.95, 0.02],
    ]


@pytest.mark.parametrize(
    ("bankroll", "field", "message"),
    [
        ({"confidence_tiers": [[0.85, 0.01], [0.75, 0.015]]}, "confidence_tiers", "ascending"),
        ({"confidence_tiers": [[0.75, 0.05]]}, "confidence_tiers", "max_stake_fraction"),
        ({"confidence_tiers": []}, "confidence_tiers", "must not be empty"),
        # lowering the max stake below the 2 % tier is caught too
        ({"max_stake_fraction": 0.015}, "max_stake_fraction", "max_stake_fraction"),
        ({"staking_mode": "martingale"}, "staking_mode", ""),
    ],
)
def test_invalid_tiers_are_rejected(
    client: TestClient, bankroll: dict[str, Any], field: str, message: str
) -> None:
    resp = client.put("/api/control/config", json={"bankroll": bankroll}, headers=CSRF)
    assert resp.status_code == 422, resp.text
    items = resp.json()["detail"]
    assert any(item["loc"][:3] == ["body", "bankroll", field] and message in item["msg"] for item in items), (
        items
    )


def test_kelly_mode_and_switches_can_be_set(client: TestClient) -> None:
    resp = client.put(
        "/api/control/config",
        json={
            "bankroll": {"staking_mode": "kelly"},
            "decision": {"require_profitable_win": False, "use_confidence_guard": False},
        },
        headers=CSRF,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["effective"]["bankroll"]["staking_mode"] == "kelly"
    assert body["overrides"]["decision"] == {"require_profitable_win": False, "use_confidence_guard": False}


# ------------------------------------------------------------------ behind Caddy (VPS)


def _login(c: TestClient, password: str, forwarded_for: str) -> Any:
    return c.post(
        "/api/auth/login",
        json={"username": "admin", "password": password},
        headers={**CSRF, "X-Forwarded-For": forwarded_for, "X-Forwarded-Proto": "https"},
    )


def test_behind_the_trusted_proxy_the_rate_limit_and_cookie_see_the_real_client(
    app_factory: Any, seeded: Any, tmp_path: Any
) -> None:
    """Before: every request came from Caddy's address, so 5 wrong passwords from anyone locked the
    owner out for 5 minutes, the audit log showed Caddy, and the cookie was never ``Secure``."""
    from pydantic import SecretStr

    from cs2bot.api import ApiOptions
    from tests.api.api_seed import BASE_URL, make_settings

    trusted = make_settings(
        tmp_path,
        api={
            "password": SecretStr("s3cret-pass"),
            "session_secret": SecretStr("x" * 32),
            "forwarded_allow_ips": ["testclient"],  # the TestClient's peer address = "Caddy"
        },
    )
    app = app_factory(settings=trusted, options=ApiOptions(login_max_failures=3, login_window_s=60))
    with TestClient(app, base_url=BASE_URL) as c:
        for _ in range(3):
            assert _login(c, "wrong", "203.0.113.7").status_code == 401
        assert _login(c, "wrong", "203.0.113.7").status_code == 429  # the attacker is blocked ...
        ok = _login(c, "s3cret-pass", "198.51.100.20")  # ... the owner is not
        assert ok.status_code == 200
        assert "secure" in ok.headers["set-cookie"].lower()


def test_forwarding_headers_are_ignored_unless_the_proxy_is_configured(
    app_factory: Any, seeded: Any, auth_settings: Any
) -> None:
    from cs2bot.api import ApiOptions
    from tests.api.api_seed import BASE_URL

    assert auth_settings.api.forwarded_allow_ips == []
    app = app_factory(settings=auth_settings, options=ApiOptions(login_max_failures=3, login_window_s=60))
    with TestClient(app, base_url=BASE_URL) as c:
        for i in range(3):
            assert _login(c, "wrong", f"203.0.113.{i}").status_code == 401  # spoofed, one client
        assert _login(c, "s3cret-pass", "198.51.100.20").status_code == 429
