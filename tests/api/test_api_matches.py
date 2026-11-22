"""Matches list / detail / odds history, teams and jobs endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.api_seed import BASE_URL, HCP, MW, TOTAL, Seed


def _ids(resp_json: dict) -> list[int]:
    return [m["id"] for m in resp_json["items"]]


# ------------------------------------------------------------------ list


def test_list_matches_sorted_and_paginated(client: TestClient) -> None:
    body = client.get("/api/matches").json()
    assert body["total"] == 27 and body["limit"] == 50 and body["offset"] == 0
    times = [m["scheduled_at"] for m in body["items"]]
    assert times == sorted(times)
    assert body["items"][-3:][0]["id"] == 103  # 103 starts in 3h, then 101 (+1d), then 102 (+2d)
    assert _ids(body)[-3:] == [103, 101, 102]

    page = client.get("/api/matches", params={"limit": 5, "offset": 25}).json()
    assert page["total"] == 27 and _ids(page) == [101, 102]
    desc = client.get("/api/matches", params={"order": "desc", "limit": 2}).json()
    assert _ids(desc) == [102, 101]
    assert client.get("/api/matches", params={"limit": 100000}).json()["limit"] == 500


def test_list_matches_filters(client: TestClient) -> None:
    def ids(**params: object) -> list[int]:
        resp = client.get("/api/matches", params=params)
        assert resp.status_code == 200, resp.text
        return _ids(resp.json())

    assert ids(status="scheduled") == [103, 101, 102]
    assert ids(status="scheduled,live") == [103, 101, 102]
    assert len(ids(status="finished")) == 24
    assert ids(status="scheduled", in_scope="false") == [102]
    assert ids(status="scheduled", in_scope="true") == [103, 101]
    assert ids(linked="true") == [101]  # 103 only has a pending-review link
    assert ids(status="scheduled", linked="false") == [103, 102]
    assert ids(**{"from": "2026-11-21T00:00:00Z", "to": "2026-11-21T23:59:59Z"}) == [101]
    assert ids(**{"from": "2026-11-21"}) == [101, 102]  # date only = midnight UTC
    assert ids(status="scheduled", team_id=5) == [103]
    assert client.get("/api/matches", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/matches", params={"order": "sideways"}).status_code == 422


def test_match_summary_fields(client: TestClient, seeded: Seed) -> None:
    items = {m["id"]: m for m in client.get("/api/matches", params={"status": "scheduled"}).json()["items"]}
    m101 = items[101]
    assert m101["team1"]["name"] == "Synth Team 1" and m101["team2"]["id"] == 2
    assert m101["tournament"] == {
        "id": m101["tournament"]["id"],
        "name": "IEM Test Masters",
        "tier": "s",
        "is_lan": True,
    }
    assert m101["format"] == "bo3" and m101["tier"] == "s" and m101["data_completeness"] == 0.97
    assert m101["betclic"]["id"] == seeded.events["main"] and m101["betclic"]["link_status"] == "auto"
    assert m101["accepted_decisions"] == 1
    # best EV = team1 match winner: 0.80 * 1.95 * 0.88 - 1
    assert m101["best_ev"] == pytest.approx(0.80 * 1.95 * 0.88 - 1, abs=1e-6)
    assert m101["has_analysis"] is True and m101["scheduled_at"] == "2026-11-21T12:00:00Z"
    m103 = items[103]
    assert m103["betclic"]["link_status"] == "pending_review"
    assert m103["best_ev"] is None and m103["accepted_decisions"] == 0 and m103["has_analysis"] is False
    assert items[102]["betclic"] is None


# ------------------------------------------------------------------ detail


def test_match_detail(client: TestClient, seeded: Seed) -> None:
    resp = client.get("/api/matches/101")
    assert resp.status_code == 200
    d = resp.json()
    assert d["match"]["id"] == 101 and d["tax_rate"] == "0.12"
    assert sorted(p["player_id"] for p in d["lineups"]["team1"]) == [1, 2, 3, 4, 5]
    standins = [p for p in d["lineups"]["team2"] if p["is_standin"]]
    assert [p["player_id"] for p in standins] == [30] and len(d["lineups"]["team2"]) == 5
    assert d["lineups"]["team1"][0]["nickname"].startswith("s1_")
    assert [(v["step"], v["team"], v["action"], v["map_name"]) for v in d["vetoes"]] == [
        (1, "team1", "ban", "nuke"),
        (2, "team2", "ban", "train"),
    ]
    (flag,) = d["context_flags"]
    assert flag["flag_type"] == "stand_in" and flag["team"] == "team2" and flag["veto"] is False
    analysis = d["analysis"]
    assert analysis["model_version"] == "test-1" and analysis["rationale"].startswith("Team 1")
    assert analysis["payload"]["match_id"] == 101 and analysis["payload"]["central"]["p_team1_win"] == 0.8
    assert [e["external_id"] for e in d["betclic_events"]] == ["ev-101"]
    assert d["unmapped_selections"] == 1

    rows = {(r["spec_key"], r["selection_key"]): r for r in d["markets"]}
    t1 = rows[(MW.key(), "team1")]
    assert t1["p_win"] == 0.8  # the non-latest 0.70 prediction is ignored
    assert t1["odds"] == "1.95" and t1["selection_id"] == seeded.selections["team1"]
    assert t1["ev"] == pytest.approx(0.3728, abs=1e-6)
    assert t1["effective_odds"] == "1.716" and t1["fair_odds"] == "1.25" and t1["breakeven_odds"] == "1.42"
    assert t1["odds_stale"] is False and t1["odds_age_s"] == 60.0 and t1["is_available"] is True
    assert t1["decision"]["accepted"] is True and t1["decision"]["id"] == seeded.decisions["accepted"]
    assert t1["decision"]["proposed_stake"] == "15.00"
    assert t1["bet"]["id"] == seeded.bets["open"] and t1["bet"]["status"] == "placed"
    assert t1["label"] == "Match winner: Synth Team 1"
    t2 = rows[(MW.key(), "team2")]
    assert t2["ev"] == pytest.approx(0.2 * 1.80 * 0.88 - 1, abs=1e-6) and t2["decision"] is None
    h2 = rows[(HCP.key(), "team2")]
    assert h2["label"] == "Map handicap: Synth Team 2 +1.5"
    assert rows[(HCP.key(), "team1")]["decision"]["reasons"] == ["p_win 0.45 < 0.75"]
    over = rows[(TOTAL.key(), "over")]
    assert (
        over["p_win"] is None and over["ev"] is None and over["odds"] == "2.20" and over["odds_stale"] is True
    )
    cms = rows[("correct_map_score", "2-0")]
    assert (
        cms["odds"] is None
        and cms["p_win"] == 0.35
        and cms["label"] == "Correct map score: Synth Team 1 2-0 Synth Team 2"
    )
    # taxonomy order: match winner rows first
    assert d["markets"][0]["market_type"] == "match_winner"


def test_match_detail_tax_free_promo(app_factory, seeded: Seed) -> None:
    with TestClient(app_factory(betclic={"tax_free_promo": True}), base_url=BASE_URL) as c:
        d = c.get("/api/matches/101").json()
    assert d["tax_rate"] == "0"
    row = next(r for r in d["markets"] if r["spec_key"] == MW.key() and r["selection_key"] == "team1")
    assert row["ev"] == pytest.approx(0.8 * 1.95 - 1, abs=1e-6)
    assert d["match"]["best_ev"] == pytest.approx(0.56, abs=1e-6)


def test_finished_match_detail_maps(client: TestClient) -> None:
    d = client.get("/api/matches/1").json()
    assert d["match"]["status"] == "finished" and d["maps"]
    for m in d["maps"]:
        assert m["winner"] in ("team1", "team2") and m["status"] == "finished"
        hi, lo = max(m["team1_rounds"], m["team2_rounds"]), min(m["team1_rounds"], m["team2_rounds"])
        assert hi >= 13 > lo or m["overtime_rounds"] > 0
    assert d["analysis"] is None and d["betclic_events"] == []
    # calibration predictions exist for match 1, but there are no bookmaker prices
    assert len(d["markets"]) == 3 and all(r["odds"] is None and r["ev"] is None for r in d["markets"])


def test_match_404s(client: TestClient) -> None:
    assert client.get("/api/matches/99999").status_code == 404
    assert client.get("/api/matches/99999/odds-history").status_code == 404
    assert client.get("/api/matches/abc").status_code == 422


# ------------------------------------------------------------------ odds history


def test_odds_history(client: TestClient, seeded: Seed) -> None:
    sel = seeded.selections["team1"]
    body = client.get("/api/matches/101/odds-history", params={"selection_id": sel}).json()
    (series,) = body["series"]
    assert series["selection_id"] == sel and series["current_odds"] == "1.95"
    assert [p["odds"] for p in series["points"]] == ["2.05", "2.00", "1.95"]
    assert series["points"][0]["captured_at"] == "2026-11-20T09:00:00Z"
    assert series["label"] == "Match winner: Synth Team 1"
    since = client.get(
        "/api/matches/101/odds-history", params={"selection_id": sel, "since": "2026-11-20T09:30:00Z"}
    ).json()
    assert [p["odds"] for p in since["series"][0]["points"]] == ["2.00", "1.95"]

    every = client.get("/api/matches/101/odds-history").json()["series"]
    assert len(every) == 6  # the mapped selections of the linked event (the unmapped one is skipped)
    assert every[0]["spec_key"] == MW.key()
    # a selection of another match's event is not reachable through match 101
    other = client.get("/api/matches/103/odds-history").json()["series"][0]["selection_id"]
    assert client.get("/api/matches/101/odds-history", params={"selection_id": other}).status_code == 404


# ------------------------------------------------------------------ teams / jobs


def test_team_detail(client: TestClient) -> None:
    resp = client.get("/api/teams/1", params={"window_days": 3650})
    assert resp.status_code == 200
    d = resp.json()
    assert d["team"]["name"] == "Synth Team 1" and d["team"]["aliases"] == ["synth 1"]
    assert [p["nickname"] for p in d["players"]] == sorted(p["nickname"] for p in d["players"])
    assert len(d["players"]) == 5
    assert all(r["result"] in ("W", "L") for r in d["recent_results"])
    assert d["form"] == [r["result"] for r in d["recent_results"][:10]]
    times = [r["scheduled_at"] for r in d["recent_results"]]
    assert times == sorted(times, reverse=True)
    assert [u["id"] for u in d["upcoming"]] == [101]
    played = sum(m["played"] for m in d["maps"])
    maps_in_results = sum(len(r["maps"]) for r in d["recent_results"])
    assert played >= maps_in_results > 0
    for m in d["maps"]:
        assert 0 <= m["won"] <= m["played"]
    # nuke was banned by team 1 in match 101's veto
    assert any(m["map_name"] == "nuke" and m["bans"] >= 1 for m in d["maps"])
    player = d["players"][0]
    assert player["maps"] > 0 and player["kpr"] is not None


def test_team_404(client: TestClient) -> None:
    assert client.get("/api/teams/999").status_code == 404


def test_jobs_list(client: TestClient) -> None:
    body = client.get("/api/jobs").json()
    assert body["total"] == 9
    starts = [j["started_at"] for j in body["items"]]
    assert starts == sorted(starts, reverse=True)
    running = next(j for j in body["items"] if j["status"] == "running")
    assert running["finished_at"] is None and running["duration_s"] is None
    failed = client.get("/api/jobs", params={"status": "failed"}).json()
    assert failed["total"] == 3 and {j["job_name"] for j in failed["items"]} == {"settle_bets"}
    named = client.get("/api/jobs", params={"name": "odds_collect", "limit": 1}).json()
    assert named["total"] == 2 and len(named["items"]) == 1 and named["items"][0]["duration_s"] == 60.0
