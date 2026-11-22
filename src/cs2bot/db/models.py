"""SQLAlchemy ORM models - the persistent schema (PLAN.md section 4.5).

Conventions
-----------
* All timestamps are UTC (``UTCDateTime`` rejects naive datetimes).
* Enum columns store the enum *value* strings.
* ``extra`` JSON columns hold source-specific fields that do not deserve a column yet.
* Orientation: ``team1`` / ``team2`` always refer to ``matches.team1_id`` / ``team2_id``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cs2bot.db.base import Base, Money, OddsNum, TimestampMixin, UTCDateTime, str_enum
from cs2bot.domain.enums import (
    BetStatus,
    Bookmaker,
    BuyType,
    ContextFlagType,
    DataSource,
    DemoStatus,
    JobStatus,
    LedgerKind,
    LinkStatus,
    MatchFormat,
    MatchStatus,
    PlayerRole,
    RoundEndReason,
    Side,
    Tier,
    VetoAction,
)

# --------------------------------------------------------------------------------------------
# Entities: teams, players, rosters, tournaments
# --------------------------------------------------------------------------------------------


class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    slug: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    logo_url: Mapped[str | None] = mapped_column(String(512))
    pandascore_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    hltv_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    hltv_rank: Mapped[int | None] = mapped_column(Integer)
    hltv_rank_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)

    aliases: Mapped[list[TeamAlias]] = relationship(back_populates="team", cascade="all, delete-orphan")


class TeamAlias(Base):
    """Normalised names under which a team appears in each source (for cross-source linking)."""

    __tablename__ = "team_aliases"
    __table_args__ = (UniqueConstraint("alias", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(128), index=True)  # normalize_team_name() output
    source: Mapped[DataSource] = mapped_column(str_enum(DataSource))

    team: Mapped[Team] = relationship(back_populates="aliases")


class Player(TimestampMixin, Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    nickname: Mapped[str] = mapped_column(String(64), index=True)
    real_name: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    pandascore_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    hltv_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    current_team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
    primary_role: Mapped[PlayerRole | None] = mapped_column(str_enum(PlayerRole))
    role_confidence: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)


class RosterPeriod(Base):
    __tablename__ = "roster_periods"
    __table_args__ = (UniqueConstraint("team_id", "player_id", "started_on"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    started_on: Mapped[date] = mapped_column(Date)
    ended_on: Mapped[date | None] = mapped_column(Date)
    is_standin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_coach: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[DataSource] = mapped_column(str_enum(DataSource))


class Tournament(TimestampMixin, Base):
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    slug: Mapped[str | None] = mapped_column(String(256))
    tier: Mapped[Tier] = mapped_column(str_enum(Tier), default=Tier.UNRANKED)
    is_lan: Mapped[bool | None] = mapped_column(Boolean)
    location: Mapped[str | None] = mapped_column(String(128))
    prize_pool_usd: Mapped[int | None] = mapped_column(Integer)
    start_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    pandascore_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    pandascore_serie_id: Mapped[int | None] = mapped_column(Integer)
    hltv_event_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)


class TeamRanking(Base):
    __tablename__ = "team_rankings"
    __table_args__ = (UniqueConstraint("team_id", "source", "ranked_on"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    source: Mapped[DataSource] = mapped_column(str_enum(DataSource))
    rank: Mapped[int] = mapped_column(Integer)
    points: Mapped[int | None] = mapped_column(Integer)
    ranked_on: Mapped[date] = mapped_column(Date, index=True)


# --------------------------------------------------------------------------------------------
# Matches, maps, vetoes, lineups
# --------------------------------------------------------------------------------------------


class Match(TimestampMixin, Base):
    __tablename__ = "matches"
    __table_args__ = (Index("ix_matches_status_scheduled", "status", "scheduled_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int | None] = mapped_column(
        ForeignKey("tournaments.id", ondelete="SET NULL"), index=True
    )
    team1_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    team2_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    status: Mapped[MatchStatus] = mapped_column(str_enum(MatchStatus), default=MatchStatus.SCHEDULED)
    format: Mapped[MatchFormat | None] = mapped_column(str_enum(MatchFormat))
    team1_score: Mapped[int | None] = mapped_column(Integer)
    team2_score: Mapped[int | None] = mapped_column(Integer)
    winner_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    is_lan: Mapped[bool | None] = mapped_column(Boolean)
    tier: Mapped[Tier] = mapped_column(str_enum(Tier), default=Tier.UNRANKED)
    stage: Mapped[str | None] = mapped_column(String(128))
    pandascore_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    hltv_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    hltv_url: Mapped[str | None] = mapped_column(String(512))
    in_scope: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    data_completeness: Mapped[float | None] = mapped_column(Float)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)

    tournament: Mapped[Tournament | None] = relationship()
    team1: Mapped[Team | None] = relationship(foreign_keys=[team1_id])
    team2: Mapped[Team | None] = relationship(foreign_keys=[team2_id])
    maps: Mapped[list[MapPlayed]] = relationship(
        back_populates="match", cascade="all, delete-orphan", order_by="MapPlayed.map_number"
    )
    vetoes: Mapped[list[Veto]] = relationship(
        back_populates="match", cascade="all, delete-orphan", order_by="Veto.step"
    )
    lineups: Mapped[list[MatchLineup]] = relationship(back_populates="match", cascade="all, delete-orphan")


class MatchLineup(Base):
    __tablename__ = "match_lineups"
    __table_args__ = (UniqueConstraint("match_id", "player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    is_standin: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[DataSource] = mapped_column(str_enum(DataSource))

    match: Mapped[Match] = relationship(back_populates="lineups")


class MapPlayed(TimestampMixin, Base):
    __tablename__ = "maps_played"
    __table_args__ = (UniqueConstraint("match_id", "map_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    map_number: Mapped[int] = mapped_column(Integer)
    map_name: Mapped[str] = mapped_column(String(32), index=True)  # normalised, e.g. "mirage"
    status: Mapped[MatchStatus] = mapped_column(str_enum(MatchStatus), default=MatchStatus.SCHEDULED)
    team1_rounds: Mapped[int | None] = mapped_column(Integer)
    team2_rounds: Mapped[int | None] = mapped_column(Integer)
    winner_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    team1_start_side: Mapped[Side | None] = mapped_column(str_enum(Side, 8))
    team1_ct_rounds: Mapped[int | None] = mapped_column(Integer)
    team1_t_rounds: Mapped[int | None] = mapped_column(Integer)
    team2_ct_rounds: Mapped[int | None] = mapped_column(Integer)
    team2_t_rounds: Mapped[int | None] = mapped_column(Integer)
    overtime_rounds: Mapped[int] = mapped_column(Integer, default=0)
    picked_by_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    is_decider: Mapped[bool] = mapped_column(Boolean, default=False)
    hltv_mapstats_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    played_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)

    match: Mapped[Match] = relationship(back_populates="maps")


class Veto(Base):
    __tablename__ = "vetoes"
    __table_args__ = (UniqueConstraint("match_id", "step"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    step: Mapped[int] = mapped_column(Integer)  # 1-based
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    action: Mapped[VetoAction] = mapped_column(str_enum(VetoAction, 16))
    map_name: Mapped[str] = mapped_column(String(32))

    match: Mapped[Match] = relationship(back_populates="vetoes")


class PlayerMapStats(Base):
    __tablename__ = "player_map_stats"
    __table_args__ = (UniqueConstraint("map_played_id", "player_id", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    map_played_id: Mapped[int] = mapped_column(ForeignKey("maps_played.id", ondelete="CASCADE"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    source: Mapped[DataSource] = mapped_column(str_enum(DataSource))
    kills: Mapped[int] = mapped_column(Integer)
    deaths: Mapped[int] = mapped_column(Integer)
    assists: Mapped[int] = mapped_column(Integer, default=0)
    headshots: Mapped[int | None] = mapped_column(Integer)
    rounds_played: Mapped[int | None] = mapped_column(Integer)
    adr: Mapped[float | None] = mapped_column(Float)
    kast: Mapped[float | None] = mapped_column(Float)  # 0..100
    rating: Mapped[float | None] = mapped_column(Float)
    impact: Mapped[float | None] = mapped_column(Float)
    first_kills: Mapped[int | None] = mapped_column(Integer)
    first_deaths: Mapped[int | None] = mapped_column(Integer)
    clutches_won: Mapped[int | None] = mapped_column(Integer)
    multi_kill_rounds: Mapped[int | None] = mapped_column(Integer)
    flash_assists: Mapped[int | None] = mapped_column(Integer)
    utility_damage: Mapped[float | None] = mapped_column(Float)
    trade_kills: Mapped[int | None] = mapped_column(Integer)
    traded_deaths: Mapped[int | None] = mapped_column(Integer)
    awp_kills: Mapped[int | None] = mapped_column(Integer)
    ct_kills: Mapped[int | None] = mapped_column(Integer)
    ct_deaths: Mapped[int | None] = mapped_column(Integer)
    t_kills: Mapped[int | None] = mapped_column(Integer)
    t_deaths: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)


# --------------------------------------------------------------------------------------------
# Demo-derived detail
# --------------------------------------------------------------------------------------------


class Demo(TimestampMixin, Base):
    __tablename__ = "demos"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    hltv_demo_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    url: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[DemoStatus] = mapped_column(str_enum(DemoStatus), default=DemoStatus.PENDING, index=True)
    archive_path: Mapped[str | None] = mapped_column(String(1024))
    dem_files: Mapped[list[Any]] = mapped_column(default=list)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    parser_version: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    parsed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Round(Base):
    __tablename__ = "rounds"
    __table_args__ = (UniqueConstraint("map_played_id", "round_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    map_played_id: Mapped[int] = mapped_column(ForeignKey("maps_played.id", ondelete="CASCADE"), index=True)
    round_number: Mapped[int] = mapped_column(Integer)  # 1-based
    team1_side: Mapped[Side] = mapped_column(str_enum(Side, 8))
    winner_side: Mapped[Side] = mapped_column(str_enum(Side, 8))
    winner_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    end_reason: Mapped[RoundEndReason] = mapped_column(str_enum(RoundEndReason), default=RoundEndReason.OTHER)
    ct_equipment_value: Mapped[int | None] = mapped_column(Integer)
    t_equipment_value: Mapped[int | None] = mapped_column(Integer)
    ct_money_start: Mapped[int | None] = mapped_column(Integer)
    t_money_start: Mapped[int | None] = mapped_column(Integer)
    ct_buy_type: Mapped[BuyType | None] = mapped_column(str_enum(BuyType, 16))
    t_buy_type: Mapped[BuyType | None] = mapped_column(str_enum(BuyType, 16))
    bomb_planted: Mapped[bool | None] = mapped_column(Boolean)
    bomb_site: Mapped[str | None] = mapped_column(String(8))
    ct_survivors: Mapped[int | None] = mapped_column(Integer)
    t_survivors: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)


class Kill(Base):
    __tablename__ = "kills"
    __table_args__ = (Index("ix_kills_map_round", "map_played_id", "round_number"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    map_played_id: Mapped[int] = mapped_column(ForeignKey("maps_played.id", ondelete="CASCADE"))
    round_number: Mapped[int] = mapped_column(Integer)
    tick: Mapped[int | None] = mapped_column(Integer)
    seconds_into_round: Mapped[float | None] = mapped_column(Float)
    attacker_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id", ondelete="SET NULL"), index=True
    )
    victim_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id", ondelete="SET NULL"), index=True
    )
    assister_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="SET NULL"))
    attacker_side: Mapped[Side | None] = mapped_column(str_enum(Side, 8))
    victim_side: Mapped[Side | None] = mapped_column(str_enum(Side, 8))
    weapon: Mapped[str | None] = mapped_column(String(32))
    headshot: Mapped[bool] = mapped_column(Boolean, default=False)
    penetrated: Mapped[bool | None] = mapped_column(Boolean)
    through_smoke: Mapped[bool | None] = mapped_column(Boolean)
    noscope: Mapped[bool | None] = mapped_column(Boolean)
    attacker_blind: Mapped[bool | None] = mapped_column(Boolean)
    flash_assisted: Mapped[bool | None] = mapped_column(Boolean)
    attacker_place: Mapped[str | None] = mapped_column(String(48))
    victim_place: Mapped[str | None] = mapped_column(String(48))
    attacker_x: Mapped[float | None] = mapped_column(Float)
    attacker_y: Mapped[float | None] = mapped_column(Float)
    attacker_z: Mapped[float | None] = mapped_column(Float)
    victim_x: Mapped[float | None] = mapped_column(Float)
    victim_y: Mapped[float | None] = mapped_column(Float)
    victim_z: Mapped[float | None] = mapped_column(Float)
    distance: Mapped[float | None] = mapped_column(Float)
    is_opening: Mapped[bool] = mapped_column(Boolean, default=False)
    is_trade: Mapped[bool] = mapped_column(Boolean, default=False)  # avenged a teammate within the window
    was_traded: Mapped[bool] = mapped_column(Boolean, default=False)  # victim's death was avenged


class PlayerRoundStats(Base):
    __tablename__ = "player_round_stats"
    __table_args__ = (UniqueConstraint("map_played_id", "round_number", "player_id"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    map_played_id: Mapped[int] = mapped_column(ForeignKey("maps_played.id", ondelete="CASCADE"), index=True)
    round_number: Mapped[int] = mapped_column(Integer)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    side: Mapped[Side] = mapped_column(str_enum(Side, 8))
    kills: Mapped[int] = mapped_column(Integer, default=0)
    deaths: Mapped[int] = mapped_column(Integer, default=0)
    assists: Mapped[int] = mapped_column(Integer, default=0)
    damage: Mapped[int] = mapped_column(Integer, default=0)
    utility_damage: Mapped[int] = mapped_column(Integer, default=0)
    enemies_flashed: Mapped[int] = mapped_column(Integer, default=0)
    flashes_thrown: Mapped[int] = mapped_column(Integer, default=0)
    smokes_thrown: Mapped[int] = mapped_column(Integer, default=0)
    he_thrown: Mapped[int] = mapped_column(Integer, default=0)
    molotovs_thrown: Mapped[int] = mapped_column(Integer, default=0)
    equipment_value: Mapped[int | None] = mapped_column(Integer)
    money_start: Mapped[int | None] = mapped_column(Integer)
    survived: Mapped[bool] = mapped_column(Boolean, default=False)
    kast: Mapped[bool | None] = mapped_column(Boolean)
    opening_kill: Mapped[bool] = mapped_column(Boolean, default=False)
    opening_death: Mapped[bool] = mapped_column(Boolean, default=False)
    clutch_situation: Mapped[str | None] = mapped_column(String(8))  # e.g. "1v2"
    clutch_won: Mapped[bool | None] = mapped_column(Boolean)
    primary_weapon: Mapped[str | None] = mapped_column(String(32))


class PlayerPositionSample(Base):
    """Where each player is N seconds after freeze time ends (role / setup inference)."""

    __tablename__ = "player_position_samples"
    __table_args__ = (Index("ix_possamples_map_player", "map_played_id", "player_id"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    map_played_id: Mapped[int] = mapped_column(ForeignKey("maps_played.id", ondelete="CASCADE"))
    round_number: Mapped[int] = mapped_column(Integer)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    side: Mapped[Side] = mapped_column(str_enum(Side, 8))
    seconds_into_round: Mapped[int] = mapped_column(Integer)
    place: Mapped[str | None] = mapped_column(String(48))
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    z: Mapped[float | None] = mapped_column(Float)
    is_alive: Mapped[bool] = mapped_column(Boolean, default=True)


class PositionAggregate(Base):
    __tablename__ = "positions_agg"
    __table_args__ = (UniqueConstraint("player_id", "map_name", "side", "place", "window_end"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    map_name: Mapped[str] = mapped_column(String(32))
    side: Mapped[Side] = mapped_column(str_enum(Side, 8))
    place: Mapped[str] = mapped_column(String(48))
    window_start: Mapped[date] = mapped_column(Date)
    window_end: Mapped[date] = mapped_column(Date)
    rounds_sampled: Mapped[int] = mapped_column(Integer, default=0)
    presence_share: Mapped[float] = mapped_column(Float, default=0.0)
    kills: Mapped[int] = mapped_column(Integer, default=0)
    deaths: Mapped[int] = mapped_column(Integer, default=0)
    opening_kills: Mapped[int] = mapped_column(Integer, default=0)
    opening_deaths: Mapped[int] = mapped_column(Integer, default=0)


class DuelAggregate(Base):
    __tablename__ = "duels_agg"
    __table_args__ = (UniqueConstraint("player_id", "opponent_player_id", "map_name", "window_end"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    opponent_player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    map_name: Mapped[str] = mapped_column(String(32))  # "*" = all maps
    window_end: Mapped[date] = mapped_column(Date)
    kills: Mapped[int] = mapped_column(Integer, default=0)
    deaths: Mapped[int] = mapped_column(Integer, default=0)
    opening_kills: Mapped[int] = mapped_column(Integer, default=0)
    opening_deaths: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------------------------
# News & context
# --------------------------------------------------------------------------------------------


class NewsItem(TimestampMixin, Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    team_ids: Mapped[list[Any]] = mapped_column(default=list)
    player_ids: Mapped[list[Any]] = mapped_column(default=list)
    triage_label: Mapped[str | None] = mapped_column(String(32))
    relevance: Mapped[float | None] = mapped_column(Float)
    triaged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ContextFlagRow(Base):
    __tablename__ = "context_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    flag_type: Mapped[ContextFlagType] = mapped_column(str_enum(ContextFlagType))
    severity: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    logit_shift: Mapped[float] = mapped_column(Float, default=0.0)  # team1 perspective
    veto: Mapped[bool] = mapped_column(Boolean, default=False)
    rationale: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[Any]] = mapped_column(default=list)
    model: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# --------------------------------------------------------------------------------------------
# Bookmaker data
# --------------------------------------------------------------------------------------------


class BookmakerEvent(TimestampMixin, Base):
    __tablename__ = "bookmaker_events"
    __table_args__ = (UniqueConstraint("bookmaker", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bookmaker: Mapped[Bookmaker] = mapped_column(str_enum(Bookmaker))
    external_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256))
    team1_raw: Mapped[str] = mapped_column(String(128))
    team2_raw: Mapped[str] = mapped_column(String(128))
    competition_raw: Mapped[str | None] = mapped_column(String(256))
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    url: Mapped[str | None] = mapped_column(String(1024))
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id", ondelete="SET NULL"), index=True)
    teams_swapped: Mapped[bool] = mapped_column(Boolean, default=False)  # bookmaker team1 == our team2
    link_confidence: Mapped[float | None] = mapped_column(Float)
    link_status: Mapped[LinkStatus] = mapped_column(str_enum(LinkStatus), default=LinkStatus.UNLINKED)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)

    markets: Mapped[list[BookmakerMarket]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class BookmakerMarket(Base):
    __tablename__ = "bookmaker_markets"
    __table_args__ = (UniqueConstraint("event_id", "raw_name", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bookmaker_events.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), default="")
    raw_name: Mapped[str] = mapped_column(String(256))
    market_type: Mapped[str | None] = mapped_column(String(48))  # MarketType value when mapped
    spec: Mapped[dict[str, Any] | None] = mapped_column()  # MarketSpec.model_dump(mode="json")
    spec_key: Mapped[str | None] = mapped_column(String(256), index=True)
    is_mapped: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime())

    event: Mapped[BookmakerEvent] = relationship(back_populates="markets")
    selections: Mapped[list[BookmakerSelection]] = relationship(
        back_populates="market", cascade="all, delete-orphan"
    )


class BookmakerSelection(Base):
    __tablename__ = "bookmaker_selections"
    __table_args__ = (UniqueConstraint("market_id", "raw_name", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("bookmaker_markets.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), default="")
    raw_name: Mapped[str] = mapped_column(String(256))
    selection_key: Mapped[str | None] = mapped_column(String(64))  # our orientation (team1/over/...)
    is_mapped: Mapped[bool] = mapped_column(Boolean, default=False)
    last_odds: Mapped[Decimal | None] = mapped_column(OddsNum)
    last_odds_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)

    market: Mapped[BookmakerMarket] = relationship(back_populates="selections")


class OddsSnapshot(Base):
    """Price history. A row is written only when the price/availability changes."""

    __tablename__ = "odds_snapshots"
    __table_args__ = (Index("ix_odds_selection_time", "selection_id", "captured_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    selection_id: Mapped[int] = mapped_column(ForeignKey("bookmaker_selections.id", ondelete="CASCADE"))
    odds: Mapped[Decimal] = mapped_column(OddsNum)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


# --------------------------------------------------------------------------------------------
# Models, predictions, decisions, bets, money
# --------------------------------------------------------------------------------------------


class ModelRun(Base):
    __tablename__ = "model_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # train / predict / backtest / calibrate
    model_version: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    artifact_path: Mapped[str | None] = mapped_column(String(1024))
    notes: Mapped[str | None] = mapped_column(Text)


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (Index("ix_predictions_lookup", "match_id", "spec_key", "selection_key", "is_latest"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    model_run_id: Mapped[int | None] = mapped_column(ForeignKey("model_runs.id", ondelete="SET NULL"))
    model_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    market_type: Mapped[str] = mapped_column(String(48))
    spec: Mapped[dict[str, Any]] = mapped_column()
    spec_key: Mapped[str] = mapped_column(String(256))
    selection_key: Mapped[str] = mapped_column(String(64))
    p_win: Mapped[float] = mapped_column(Float)
    p_push: Mapped[float] = mapped_column(Float, default=0.0)
    p_void: Mapped[float] = mapped_column(Float, default=0.0)
    sigma: Mapped[float] = mapped_column(Float)
    data_completeness: Mapped[float | None] = mapped_column(Float)
    components: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)


class MatchAnalysis(Base):
    """Full MatchPrediction payload + Claude rationale, for the dashboard analysis page."""

    __tablename__ = "match_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    model_version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column()  # MatchPrediction.model_dump(mode="json")
    rationale: Mapped[str | None] = mapped_column(Text)
    rationale_model: Mapped[str | None] = mapped_column(String(64))
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)


class BetDecisionRow(Base):
    __tablename__ = "bet_decisions"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id", ondelete="SET NULL"))
    selection_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookmaker_selections.id", ondelete="SET NULL")
    )
    spec_key: Mapped[str] = mapped_column(String(256))
    selection_key: Mapped[str] = mapped_column(String(64))
    odds: Mapped[Decimal] = mapped_column(OddsNum)
    odds_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    accepted: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list[Any]] = mapped_column(default=list)
    p_win: Mapped[float] = mapped_column(Float)
    sigma: Mapped[float] = mapped_column(Float)
    ev: Mapped[float] = mapped_column(Float)
    kelly_fraction: Mapped[float] = mapped_column(Float, default=0.0)
    proposed_stake: Mapped[Decimal | None] = mapped_column(Money)
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Bet(TimestampMixin, Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("bet_decisions.id", ondelete="SET NULL"))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id", ondelete="SET NULL"), index=True)
    selection_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookmaker_selections.id", ondelete="SET NULL")
    )
    bookmaker: Mapped[Bookmaker] = mapped_column(str_enum(Bookmaker), default=Bookmaker.BETCLIC)
    external_event_id: Mapped[str] = mapped_column(String(64))
    market_raw: Mapped[str] = mapped_column(String(256))
    selection_raw: Mapped[str] = mapped_column(String(256))
    spec_key: Mapped[str] = mapped_column(String(256))
    selection_key: Mapped[str] = mapped_column(String(64))
    requested_odds: Mapped[Decimal] = mapped_column(OddsNum)
    min_acceptable_odds: Mapped[Decimal] = mapped_column(OddsNum)
    placed_odds: Mapped[Decimal | None] = mapped_column(OddsNum)
    stake: Mapped[Decimal] = mapped_column(Money)  # gross stake incl. tax (what leaves the balance)
    tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    potential_payout: Mapped[Decimal | None] = mapped_column(Money)
    status: Mapped[BetStatus] = mapped_column(str_enum(BetStatus), default=BetStatus.PENDING, index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    external_bet_id: Mapped[str | None] = mapped_column(String(64))
    screenshot_path: Mapped[str | None] = mapped_column(String(1024))
    error: Mapped[str | None] = mapped_column(Text)
    placed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    payout: Mapped[Decimal | None] = mapped_column(Money)
    pnl: Mapped[Decimal | None] = mapped_column(Money)
    closing_odds: Mapped[Decimal | None] = mapped_column(OddsNum)
    clv: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)


class BankrollLedger(Base):
    __tablename__ = "bankroll_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    kind: Mapped[LedgerKind] = mapped_column(str_enum(LedgerKind))
    amount: Mapped[Decimal] = mapped_column(Money)  # signed: + money in, - money out
    balance_after: Mapped[Decimal] = mapped_column(Money)
    bet_id: Mapped[int | None] = mapped_column(ForeignKey("bets.id", ondelete="SET NULL"), index=True)
    note: Mapped[str | None] = mapped_column(String(512))


# --------------------------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------------------------


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_by: Mapped[str | None] = mapped_column(String(64))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    items: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
