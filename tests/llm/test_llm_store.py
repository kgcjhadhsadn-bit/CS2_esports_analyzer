"""Persistence helpers: flags round-trip, triage storage, news selection."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.db.models import ContextFlagRow, NewsItem
from cs2bot.domain.enums import ContextFlagType, TeamSlot
from cs2bot.llm.combine import combine_flags
from cs2bot.llm.schemas import TriageLabel
from cs2bot.llm.store import (
    llm_extra,
    load_active_flags,
    merge_llm_extra,
    news_row_to_input,
    persist_flags,
    select_news_for_match,
    store_triage,
)
from tests.llm.llm_factories import (
    NOW,
    TEAM1_ID,
    TEAM2_ID,
    add_news,
    make_flag,
    make_settings,
    seed_match,
    triage_item,
)

TEAMS = (TEAM1_ID, TEAM2_ID)


def _rows(session: Session) -> list[ContextFlagRow]:
    return list(session.scalars(select(ContextFlagRow).order_by(ContextFlagRow.id)))


# ---------------------------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------------------------


def test_flags_round_trip(session: Session) -> None:
    seed_match(session)
    flags = [
        make_flag(
            team=TeamSlot.TEAM2, logit_shift=0.12, confidence=0.8, expires_at=NOW + timedelta(hours=40)
        ),
        make_flag(flag_type=ContextFlagType.ILLNESS, team=TeamSlot.TEAM1, logit_shift=-0.05, severity=0.3),
        make_flag(
            flag_type=ContextFlagType.OTHER, team=None, veto=True, logit_shift=0.0, evidence=["context"]
        ),
    ]
    rows = persist_flags(session, 10, flags, model="claude-opus-5-5", team_ids=TEAMS, now=NOW)

    assert [r.team_id for r in rows] == [TEAM2_ID, TEAM1_ID, None]
    assert all(r.active and r.model == "claude-opus-5-5" and r.created_at == NOW for r in rows)
    loaded = load_active_flags(session, 10, now=NOW)
    assert loaded == flags
    assert combine_flags(loaded, make_settings().llm, now=NOW) == combine_flags(
        flags, make_settings().llm, now=NOW
    )


def test_persist_replaces_previous_active_flags(session: Session) -> None:
    seed_match(session)
    persist_flags(session, 10, [make_flag(rationale="old")], model="m1", team_ids=TEAMS, now=NOW)
    persist_flags(
        session, 10, [make_flag(rationale="new")], model="m1", team_ids=TEAMS, now=NOW + timedelta(hours=1)
    )
    rows = _rows(session)
    assert [(r.rationale, r.active) for r in rows] == [("old", False), ("new", True)]
    assert [f.rationale for f in load_active_flags(session, 10, now=NOW + timedelta(hours=1))] == ["new"]


def test_persist_is_idempotent_for_identical_flags(session: Session) -> None:
    seed_match(session)
    flags = [make_flag(expires_at=NOW + timedelta(hours=3)), make_flag(flag_type=ContextFlagType.BOOTCAMP)]
    first = persist_flags(session, 10, flags, model="m1", team_ids=TEAMS, now=NOW)
    second = persist_flags(
        session, 10, list(reversed(flags)), model="m1", team_ids=TEAMS, now=NOW + timedelta(hours=1)
    )
    assert [r.id for r in second] == [r.id for r in first]
    assert len(_rows(session)) == 2 and all(r.created_at == NOW for r in _rows(session))
    # A different model (e.g. a fallback served the request) is recorded as a new generation.
    persist_flags(session, 10, flags, model="m2", team_ids=TEAMS, now=NOW)
    assert [r.active for r in _rows(session)] == [False, False, True, True]


def test_persisting_an_empty_list_deactivates_everything(session: Session) -> None:
    seed_match(session)
    persist_flags(session, 10, [make_flag(veto=True)], model="m", team_ids=TEAMS, now=NOW)
    assert persist_flags(session, 10, [], model="m", team_ids=TEAMS, now=NOW) == []
    assert [r.active for r in _rows(session)] == [False]
    assert load_active_flags(session, 10, now=NOW) == []
    assert persist_flags(session, 10, [], model="m", team_ids=TEAMS, now=NOW) == []  # no-op


def test_load_skips_expired_flags_with_the_combine_rule(session: Session) -> None:
    seed_match(session)
    flags = [
        make_flag(rationale="expired", expires_at=NOW - timedelta(minutes=1)),
        make_flag(rationale="at-now", flag_type=ContextFlagType.OTHER, expires_at=NOW),
        make_flag(rationale="open", flag_type=ContextFlagType.BOOTCAMP),
    ]
    persist_flags(session, 10, flags, model=None, team_ids=TEAMS, now=NOW - timedelta(hours=1))
    assert [f.rationale for f in load_active_flags(session, 10, now=NOW)] == ["at-now", "open"]
    assert [f.rationale for f in load_active_flags(session, 10, now=NOW + timedelta(seconds=1))] == ["open"]


def test_load_handles_unknown_match_foreign_team_and_long_model(session: Session) -> None:
    seed_match(session)
    assert load_active_flags(session, 999, now=NOW) == []
    persist_flags(
        session, 10, [make_flag(team=TeamSlot.TEAM1)], model="x" * 100, team_ids=(3, TEAM2_ID), now=NOW
    )
    row = _rows(session)[0]
    assert row.team_id == 3 and row.model is not None and len(row.model) == 64
    assert load_active_flags(session, 10, now=NOW)[0].team is None  # team 3 does not play match 10


def test_flags_of_other_matches_are_untouched(session: Session) -> None:
    from cs2bot.db.models import Match
    from cs2bot.domain.enums import MatchFormat

    seed_match(session)
    session.add(Match(id=11, team1_id=TEAM2_ID, team2_id=TEAM1_ID, format=MatchFormat.BO1))
    session.flush()
    persist_flags(
        session, 11, [make_flag(team=TeamSlot.TEAM1)], model="m", team_ids=(TEAM2_ID, TEAM1_ID), now=NOW
    )
    persist_flags(session, 10, [], model="m", team_ids=TEAMS, now=NOW)
    other = load_active_flags(session, 11, now=NOW)
    assert len(other) == 1 and other[0].team is TeamSlot.TEAM1


def test_persisted_veto_is_seen_by_the_decision_engine(session: Session) -> None:
    from cs2bot.engine.decision import load_match_decision_info

    seed_match(session)
    persist_flags(
        session, 10, [make_flag(veto=True, rationale="forfeit risk")], model="m", team_ids=TEAMS, now=NOW
    )
    info = load_match_decision_info(session, match_id=10, now=NOW)
    assert info.context_veto and "forfeit risk" in (info.context_veto_reason or "")
    persist_flags(session, 10, [], model="m", team_ids=TEAMS, now=NOW)
    assert load_match_decision_info(session, match_id=10, now=NOW).context_veto is False


# ---------------------------------------------------------------------------------------------
# Triage storage
# ---------------------------------------------------------------------------------------------


def test_store_triage_sets_fields_and_merges_teams(session: Session) -> None:
    seed_match(session)
    row = add_news(session, 1, team_ids=[TEAM1_ID], extra={"hltv": {"news_id": 77}})
    item = triage_item(
        row.id,
        label=TriageLabel.VISA_TRAVEL,
        relevance=0.7,
        team_ids=[TEAM2_ID, 999],
        team_names=["Bravo Foxes"],
        summary="Bravo Foxes arrive late.",
    )
    assert store_triage(session, row.id, item, now=NOW, model="claude-haiku-5-5") is True

    session.expire_all()
    stored = session.get(NewsItem, row.id)
    assert stored is not None
    assert stored.triage_label == "visa_travel" and stored.relevance == pytest.approx(0.7)
    assert stored.triaged_at == NOW
    assert stored.team_ids == [TEAM1_ID, TEAM2_ID]  # union; unknown team 999 ignored
    assert stored.extra["hltv"] == {"news_id": 77}  # other packages' data untouched
    assert stored.extra["llm"]["triage"] == {
        "summary": "Bravo Foxes arrive late.",
        "team_names": ["Bravo Foxes"],
        "team_ids": [TEAM2_ID],
        "model": "claude-haiku-5-5",
    }


def test_store_triage_is_idempotent_and_handles_missing_rows(session: Session) -> None:
    seed_match(session)
    row = add_news(session, 1, team_ids=[TEAM2_ID])
    item = triage_item(row.id, team_ids=[TEAM2_ID])
    store_triage(session, row.id, item, now=NOW)
    store_triage(session, row.id, item, now=NOW)
    assert row.team_ids == [TEAM2_ID] and row.triage_label == "stand_in"
    assert store_triage(session, 424242, triage_item(424242), now=NOW) is False


def test_extra_helpers() -> None:
    original = {"hltv": {"a": 1}, "llm": {"x": 1}}
    merged = merge_llm_extra(original, {"y": 2})
    assert merged == {"hltv": {"a": 1}, "llm": {"x": 1, "y": 2}} and original["llm"] == {"x": 1}
    assert merge_llm_extra(None, {"y": 2}) == {"llm": {"y": 2}}
    assert llm_extra({"llm": "corrupt"}) == {} and llm_extra(None) == {}


# ---------------------------------------------------------------------------------------------
# News selection
# ---------------------------------------------------------------------------------------------


def _titles(items: list) -> list[str]:
    return [item.title for item in items]


def test_select_news_filters_by_mention_relevance_and_recency(session: Session) -> None:
    ctx = seed_match(session)
    add_news(session, 1, title="team2 relevant", team_ids=[TEAM2_ID], label="stand_in", relevance=0.9)
    add_news(session, 2, title="player relevant", player_ids=[103], label="injury_illness", relevance=0.6)
    add_news(session, 3, title="other team", team_ids=[3], label="stand_in", relevance=0.9)
    add_news(session, 4, title="irrelevant label", team_ids=[TEAM1_ID], label="irrelevant", relevance=0.9)
    add_news(session, 5, title="low relevance", team_ids=[TEAM1_ID], label="bootcamp", relevance=0.1)
    add_news(
        session, 6, title="too old", team_ids=[TEAM1_ID], label="stand_in", relevance=0.9, hours_ago=15 * 24
    )
    add_news(session, 7, title="untriaged", team_ids=[TEAM1_ID])
    add_news(session, 8, title="unrelated player", player_ids=[999], label="stand_in", relevance=0.9)

    selected = select_news_for_match(session, ctx, limit=25, now=NOW)
    assert sorted(_titles(selected)) == ["player relevant", "team2 relevant", "untriaged"]
    assert _titles(select_news_for_match(session, ctx, limit=25, now=NOW, include_untriaged=False)) == [
        "team2 relevant",
        "player relevant",
    ]
    assert "low relevance" in _titles(
        select_news_for_match(session, ctx, limit=25, now=NOW, min_relevance=0.05)
    )
    assert "too old" in _titles(select_news_for_match(session, ctx, days=30, limit=25, now=NOW))


def test_select_news_never_looks_ahead(session: Session) -> None:
    ctx = seed_match(session)
    add_news(session, 1, title="known", team_ids=[TEAM1_ID], label="stand_in", relevance=0.9, hours_ago=5)
    add_news(session, 2, title="future", team_ids=[TEAM1_ID], label="stand_in", relevance=0.9, hours_ago=-2)
    assert _titles(select_news_for_match(session, ctx, limit=25, now=NOW)) == ["known"]
    assert _titles(select_news_for_match(session, ctx, limit=25, now=NOW + timedelta(hours=3))) == [
        "known",
        "future",
    ]
    assert select_news_for_match(session, ctx, limit=25, now=NOW - timedelta(hours=6)) == []


def test_select_news_is_chronological_and_limited_by_relevance(session: Session) -> None:
    ctx = seed_match(session)
    add_news(session, 1, title="r90 old", team_ids=[TEAM1_ID], label="stand_in", relevance=0.9, hours_ago=50)
    add_news(session, 2, title="r40", team_ids=[TEAM1_ID], label="bootcamp", relevance=0.4, hours_ago=30)
    add_news(session, 3, title="r80", team_ids=[TEAM2_ID], label="roster_change", relevance=0.8, hours_ago=20)
    add_news(session, 4, title="untriaged", team_ids=[TEAM2_ID], hours_ago=10)
    add_news(session, 5, title="r35", team_ids=[TEAM2_ID], label="coach_change", relevance=0.35, hours_ago=1)

    assert _titles(select_news_for_match(session, ctx, limit=25, now=NOW)) == [
        "r90 old",
        "r40",
        "r80",
        "untriaged",
        "r35",
    ]
    # limit 3: the two most relevant triaged items + the untriaged one (ranked 0.5), oldest first.
    assert _titles(select_news_for_match(session, ctx, limit=3, now=NOW)) == ["r90 old", "r80", "untriaged"]
    assert select_news_for_match(session, ctx, limit=0, now=NOW) == []


def test_select_news_adds_a_few_global_meta_items(session: Session) -> None:
    ctx = seed_match(session)
    add_news(session, 1, title="team", team_ids=[TEAM1_ID], label="stand_in", relevance=0.9, hours_ago=8)
    for n, hours in ((2, 40), (3, 30), (4, 20), (5, 10)):
        add_news(session, n, title=f"patch {n}", label="meta_patch", relevance=0.7, hours_ago=hours)
    add_news(session, 6, title="format news", label="tournament_format", relevance=0.9, hours_ago=3)
    add_news(session, 7, title="weak patch", label="meta_patch", relevance=0.1, hours_ago=2)

    assert _titles(select_news_for_match(session, ctx, limit=25, now=NOW)) == [
        "patch 3",
        "patch 4",
        "patch 5",
        "team",
    ]
    assert _titles(select_news_for_match(session, ctx, limit=25, now=NOW, max_global=0)) == ["team"]
    assert _titles(select_news_for_match(session, ctx, limit=2, now=NOW, max_global=1)) == ["patch 5", "team"]


def test_select_news_uses_ingestion_time_when_unpublished_and_tolerates_bad_json(session: Session) -> None:
    ctx = seed_match(session)
    add_news(session, 1, title="no date recent", team_ids=[TEAM1_ID], published=False, hours_ago=2)
    add_news(session, 2, title="no date old", team_ids=[TEAM1_ID], published=False, hours_ago=20 * 24)
    weird = add_news(session, 3, title="weird ids", team_ids=[TEAM1_ID])
    weird.team_ids = ["1", None, True, TEAM1_ID]
    weird.player_ids = {"not": "a list"}  # type: ignore[assignment]
    session.flush()
    selected = select_news_for_match(session, ctx, limit=25, now=NOW)
    assert sorted(_titles(selected)) == ["no date recent", "weird ids"]
    weird_in = next(item for item in selected if item.title == "weird ids")
    assert weird_in.team_ids == [TEAM1_ID] and weird_in.player_ids == []


def test_news_row_to_input_falls_back_to_the_lead(session: Session) -> None:
    seed_match(session)
    row = add_news(session, 1, body=None, extra={"hltv": {"lead": "Short lead."}}, team_ids=[TEAM1_ID])
    item = news_row_to_input(row)
    assert item.body == "Short lead." and item.url == row.url and item.published_at == row.published_at
    assert news_row_to_input(add_news(session, 2, body=None)).body is None


def test_select_news_still_finds_triage_teams_after_the_scraper_rewrites_team_ids(session: Session) -> None:
    """The HLTV sync overwrites ``team_ids`` when it refreshes an article; the verdict's ids survive."""
    ctx = seed_match(session)
    row = add_news(session, 1, title="visa trouble", team_ids=[])
    verdict = triage_item(row.id, label=TriageLabel.VISA_TRAVEL, relevance=0.8, team_ids=[TEAM2_ID])
    store_triage(session, row.id, verdict, now=NOW)
    assert _titles(select_news_for_match(session, ctx, limit=25, now=NOW)) == ["visa trouble"]

    row.team_ids = []  # what a refresh by the scraper does when its own detection finds nothing
    session.flush()
    session.expire_all()
    assert _titles(select_news_for_match(session, ctx, limit=25, now=NOW)) == ["visa trouble"]

    row.extra = {"llm": {"triage": "corrupted"}}  # malformed section: ignored, not fatal
    session.flush()
    assert select_news_for_match(session, ctx, limit=25, now=NOW) == []
