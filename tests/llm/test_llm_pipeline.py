"""Pipeline helpers: triage of pending news and the cost-guarded match-context refresh."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.db.models import ContextFlagRow, Match, NewsItem
from cs2bot.domain.enums import ContextFlagType
from cs2bot.llm.context import ContextAnalyzer, LLMStatus
from cs2bot.llm.pipeline import load_known_teams, refresh_match_context, triage_pending_news
from cs2bot.llm.schemas import TriageLabel
from cs2bot.llm.tuning import LLMTuning
from tests.llm.llm_factories import (
    NOW,
    START,
    TEAM1_ID,
    TEAM2_ID,
    FakeClient,
    add_news,
    api_status_error,
    context_result,
    fake_response,
    make_settings,
    refusal,
    seed_match,
    suggestion,
    triage_batch,
    triage_item,
)


def _flag_rows(session: Session) -> list[ContextFlagRow]:
    return list(session.scalars(select(ContextFlagRow).order_by(ContextFlagRow.id)))


def _ok(*flags: Any) -> Any:
    return fake_response(context_result(*flags), model="claude-opus-5-5")


# ---------------------------------------------------------------------------------------------
# refresh_match_context
# ---------------------------------------------------------------------------------------------


def test_refresh_analyses_persists_and_marks_the_match(session: Session) -> None:
    ctx = seed_match(session)
    news = add_news(
        session, 1, title="Bravo Foxes stand-in", team_ids=[TEAM2_ID], label="stand_in", relevance=0.9
    )
    client = FakeClient([_ok(suggestion())])
    analyzer = ContextAnalyzer(make_settings(), client=client)

    result = refresh_match_context(session, analyzer, ctx, now=NOW)

    assert result.status is LLMStatus.OK and result.source == "llm" and result.n_news == 1
    assert [f.flag_type for f in result.flags] == [ContextFlagType.STAND_IN]
    assert result.flags[0].evidence == [news.url]
    rows = _flag_rows(session)
    assert (
        len(rows) == 1
        and rows[0].active
        and rows[0].team_id == TEAM2_ID
        and rows[0].model == "claude-opus-5-5"
    )
    marker = session.get(Match, 10).extra["llm"]["context"]  # type: ignore[union-attr]
    assert marker["fingerprint"] == result.fingerprint and marker["n_flags"] == 1 and marker["n_news"] == 1
    assert marker["analyzed_at"] == NOW.isoformat() and marker["note"] == "One stand-in reported."
    assert "Bravo Foxes stand-in" in client.user_text()


def test_refresh_skips_the_call_while_inputs_are_unchanged_even_after_a_restart(session: Session) -> None:
    ctx = seed_match(session)
    add_news(session, 1, team_ids=[TEAM2_ID], label="stand_in", relevance=0.9)
    first_client = FakeClient([_ok(suggestion())])
    first = refresh_match_context(
        session, ContextAnalyzer(make_settings(), client=first_client), ctx, now=NOW
    )

    # New analyzer = new process: the in-memory memo is empty, the database marker still applies.
    second_client = FakeClient()
    restarted = ContextAnalyzer(make_settings(), client=second_client)
    second = refresh_match_context(session, restarted, ctx, now=NOW + timedelta(hours=2))

    assert second_client.calls == []
    assert second.source == "db" and second.flags == first.flags and second.note == "One stand-in reported."
    assert len(_flag_rows(session)) == 1


def test_refresh_reanalyses_when_news_changes_or_the_interval_passes(session: Session) -> None:
    ctx = seed_match(session)
    add_news(session, 1, team_ids=[TEAM2_ID], label="stand_in", relevance=0.9)
    client = FakeClient([_ok(suggestion()), _ok(), _ok(suggestion(flag_type=ContextFlagType.ILLNESS))])
    analyzer = ContextAnalyzer(make_settings(), client=client)

    refresh_match_context(session, analyzer, ctx, now=NOW)
    add_news(session, 2, title="Stand-in story retracted", team_ids=[TEAM2_ID], hours_ago=-0.5)
    second = refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(hours=1))
    assert len(client.calls) == 2 and second.source == "llm" and second.flags == []
    assert [r.active for r in _flag_rows(session)] == [False]  # a successful empty analysis clears the flag

    third = refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(hours=8))
    assert len(client.calls) == 3 and [f.flag_type for f in third.flags] == [ContextFlagType.ILLNESS]


def test_refresh_uses_the_memo_when_the_db_marker_is_missing(session: Session) -> None:
    ctx = seed_match(session)
    client = FakeClient([_ok(suggestion(evidence=["context"]))])
    analyzer = ContextAnalyzer(make_settings(), client=client)
    refresh_match_context(session, analyzer, ctx, now=NOW)
    match = session.get(Match, 10)
    assert match is not None
    match.extra = {}  # e.g. another package overwrote the column
    session.flush()

    again = refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(minutes=30))
    assert len(client.calls) == 1 and again.source == "memo" and len(again.flags) == 1
    assert (
        match.extra["llm"]["context"]["analyzed_at"] == NOW.isoformat()
    )  # marker restored, original time kept
    assert len(_flag_rows(session)) == 1  # identical flags are not re-inserted


def test_refresh_force_bypasses_both_guards(session: Session) -> None:
    ctx = seed_match(session)
    client = FakeClient([_ok(), _ok()])
    analyzer = ContextAnalyzer(make_settings(), client=client)
    refresh_match_context(session, analyzer, ctx, now=NOW)
    refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(minutes=1), force=True)
    assert len(client.calls) == 2


def test_failed_analysis_keeps_the_stored_flags(session: Session) -> None:
    ctx = seed_match(session)
    add_news(session, 1, team_ids=[TEAM2_ID], label="stand_in", relevance=0.9)
    veto = suggestion(veto=True, confidence=0.9, flag_type=ContextFlagType.OTHER)
    client = FakeClient([_ok(veto), refusal(), api_status_error(500)])
    analyzer = ContextAnalyzer(make_settings(), client=client, tuning=LLMTuning(failure_backoff_s=60.0))
    refresh_match_context(session, analyzer, ctx, now=NOW)

    add_news(session, 2, team_ids=[TEAM1_ID], hours_ago=-1)  # inputs change -> new attempt, which is refused
    refused = refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(hours=2))
    assert refused.status is LLMStatus.REFUSED and refused.source == "stale"
    assert [f.veto for f in refused.flags] == [True]
    assert [r.active for r in _flag_rows(session)] == [True]
    # The refused inputs are not sent again until the re-analysis interval has passed.
    waiting = refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(hours=3))
    assert waiting.status is LLMStatus.BACKOFF and len(client.calls) == 2 and len(waiting.flags) == 1

    add_news(session, 3, team_ids=[TEAM1_ID], hours_ago=-3)  # inputs change again -> server error
    failed = refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(hours=4))
    assert failed.status is LLMStatus.FAILED and [f.veto for f in failed.flags] == [True]
    backoff = refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(hours=4, seconds=30))
    assert backoff.status is LLMStatus.BACKOFF and len(client.calls) == 3 and len(backoff.flags) == 1
    assert [r.active for r in _flag_rows(session)] == [True]


def test_refresh_when_disabled_returns_stored_flags_without_calls(session: Session) -> None:
    ctx = seed_match(session)
    refresh_match_context(
        session,
        ContextAnalyzer(make_settings(), client=FakeClient([_ok(suggestion(evidence=["context"]))])),
        ctx,
        now=NOW,
    )
    client = FakeClient()
    disabled = ContextAnalyzer(make_settings(enabled=False), client=client)
    result = refresh_match_context(session, disabled, ctx, now=NOW + timedelta(hours=7))
    assert result.status is LLMStatus.DISABLED and result.source == "stale" and len(result.flags) == 1
    assert client.calls == []


def test_refresh_drops_expired_flags_from_the_result(session: Session) -> None:
    ctx = seed_match(session)
    soon = (NOW + timedelta(hours=1)).isoformat()
    client = FakeClient([_ok(suggestion(evidence=["context"], expires_at=soon))])
    analyzer = ContextAnalyzer(make_settings(), client=client)
    assert len(refresh_match_context(session, analyzer, ctx, now=NOW).flags) == 1
    assert refresh_match_context(session, analyzer, ctx, now=NOW + timedelta(hours=2)).flags == []
    assert len(client.calls) == 1


def test_refresh_for_a_match_missing_from_the_db_returns_flags_without_persisting(session: Session) -> None:
    ctx = seed_match(session, match_id=555)
    client = FakeClient([_ok(suggestion(evidence=["context"]))])
    result = refresh_match_context(session, ContextAnalyzer(make_settings(), client=client), ctx, now=NOW)
    assert len(result.flags) == 1 and _flag_rows(session) == []


def test_refresh_near_start_uses_the_short_interval(session: Session) -> None:
    ctx = seed_match(session)
    client = FakeClient([_ok(), _ok()])
    analyzer = ContextAnalyzer(make_settings(), client=client)
    t0 = START - timedelta(hours=2, minutes=30)
    refresh_match_context(session, analyzer, ctx, now=t0)
    refresh_match_context(session, analyzer, ctx, now=t0 + timedelta(minutes=59))
    assert len(client.calls) == 1
    refresh_match_context(session, analyzer, ctx, now=t0 + timedelta(minutes=61))
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------------------------
# triage_pending_news
# ---------------------------------------------------------------------------------------------


def _answer_all(label: TriageLabel = TriageLabel.STAND_IN, skip: tuple[int, ...] = ()) -> Any:
    def script(endpoint: str, kwargs: dict[str, Any]) -> Any:
        ids = [int(x) for x in re.findall(r'<news_item news_id="(\d+)"', kwargs["messages"][0]["content"])]
        items = [triage_item(i, label=label, team_names=["Bravo Foxes"]) for i in ids if i not in skip]
        return fake_response(triage_batch(*items), model="claude-haiku-5-5")

    return script


def test_triage_pending_news_stores_verdicts(session: Session) -> None:
    seed_match(session)
    fresh = add_news(session, 1, title="Bravo Foxes field a stand-in")
    add_news(session, 2, label="irrelevant", relevance=0.0)  # already triaged
    add_news(session, 3, hours_ago=40 * 24)  # too old to matter
    client = FakeClient(_answer_all())
    analyzer = ContextAnalyzer(make_settings(), client=client)

    report = triage_pending_news(session, analyzer, now=NOW)

    assert (report.status, report.candidates, report.triaged, report.unanswered) == (LLMStatus.OK, 1, 1, 0)
    assert len(client.calls) == 1
    assert fresh.triage_label == "stand_in" and fresh.triaged_at == NOW
    assert fresh.team_ids == [TEAM2_ID]  # resolved from the returned team name
    assert fresh.extra["llm"]["triage"]["model"] == "claude-haiku-5-5"
    assert load_known_teams(session) == {1: "Alpha Wolves", 2: "Bravo Foxes", 3: "Charlie Owls"}
    # Second run: nothing left to do, no request.
    assert triage_pending_news(session, analyzer, now=NOW).candidates == 0
    assert len(client.calls) == 1


def test_triage_pending_news_respects_the_limit_newest_first(session: Session) -> None:
    seed_match(session)
    rows = [add_news(session, n) for n in range(1, 6)]
    analyzer = ContextAnalyzer(make_settings(), client=FakeClient(_answer_all()))
    report = triage_pending_news(session, analyzer, limit=2, now=NOW)
    assert report.triaged == 2
    assert [r.triaged_at is not None for r in rows] == [False, False, False, True, True]


def test_triage_gives_up_on_an_item_after_repeated_content_failures(session: Session) -> None:
    seed_match(session)
    stubborn = add_news(session, 1)
    fine = add_news(session, 2)
    client = FakeClient(_answer_all(skip=(stubborn.id,)))
    analyzer = ContextAnalyzer(make_settings(), client=client, tuning=LLMTuning(triage_max_attempts=2))

    first = triage_pending_news(session, analyzer, now=NOW)
    assert (first.triaged, first.unanswered) == (1, 1) and fine.triaged_at == NOW
    assert stubborn.extra["llm"]["triage_attempts"] == 1
    second = triage_pending_news(session, analyzer, now=NOW)
    assert second.unanswered == 1 and stubborn.extra["llm"]["triage_attempts"] == 2
    third = triage_pending_news(session, analyzer, now=NOW)
    assert third.candidates == 0 and len(client.calls) == 2
    assert session.get(NewsItem, stubborn.id).triaged_at is None  # type: ignore[union-attr]


def test_transient_failures_do_not_count_as_attempts(session: Session) -> None:
    seed_match(session)
    row = add_news(session, 1)
    analyzer = ContextAnalyzer(make_settings(), client=FakeClient([api_status_error(429)]))
    report = triage_pending_news(session, analyzer, now=NOW)
    assert (report.status, report.triaged, report.deferred) == (LLMStatus.FAILED, 0, 1)
    assert "llm" not in (row.extra or {})


def test_triage_pending_news_when_disabled(session: Session) -> None:
    seed_match(session)
    add_news(session, 1)
    client = FakeClient()
    report = triage_pending_news(
        session, ContextAnalyzer(make_settings(enabled=False), client=client), now=NOW
    )
    assert report.status is LLMStatus.DISABLED and report.candidates == 1 and client.calls == []


def test_given_up_items_do_not_starve_older_pending_news(session: Session) -> None:
    seed_match(session)
    older = [add_news(session, n, hours_ago=20 - n) for n in (1, 2)]
    for n in range(3, 13):  # ten newer items the model could never classify
        add_news(session, n, hours_ago=1, extra={"llm": {"triage_attempts": 3}})
    client = FakeClient(_answer_all())
    analyzer = ContextAnalyzer(make_settings(), client=client)

    report = triage_pending_news(session, analyzer, limit=2, now=NOW)

    assert (report.candidates, report.triaged) == (2, 2)
    assert all(row.triaged_at == NOW for row in older)


def test_rejected_requests_do_not_burn_triage_attempts(session: Session) -> None:
    """HTTP 400 (e.g. exhausted credit, unknown model) says nothing about the articles themselves."""
    seed_match(session)
    rows = [add_news(session, n) for n in (1, 2, 3)]
    client = FakeClient(lambda *_: api_status_error(400))
    analyzer = ContextAnalyzer(make_settings(), client=client, tuning=LLMTuning(triage_max_attempts=2))
    for _ in range(4):
        report = triage_pending_news(session, analyzer, now=NOW)
        assert (report.status, report.candidates, report.unanswered, report.deferred) == (
            LLMStatus.FAILED,
            3,
            0,
            3,
        )
    assert len(client.calls) == 4  # one request per run, no item-by-item retries
    assert all("llm" not in (row.extra or {}) for row in rows)
    # Once the account works again, everything is still waiting to be triaged.
    working = ContextAnalyzer(make_settings(), client=FakeClient(_answer_all()))
    assert triage_pending_news(session, working, now=NOW).triaged == 3
