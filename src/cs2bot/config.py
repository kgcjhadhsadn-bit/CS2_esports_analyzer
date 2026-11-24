"""Application configuration.

Every runtime knob lives here. Values are resolved (highest priority first) from:

1. environment variables, prefix ``CS2BOT_``, nested sections joined with ``__``
   (example: ``CS2BOT_BANKROLL__KELLY_FRACTION=0.25``),
2. a ``.env`` file in the working directory,
3. the defaults below.

A few values can additionally be overridden at runtime from the dashboard; those overrides are
stored in the ``system_state`` table (key ``config_overrides``) and merged by
:func:`cs2bot.db.state.effective_decision_settings` / ``effective_bankroll_settings``.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


def _blank_to_none(value: Any) -> Any:
    """Treat empty strings from .env files (``KEY=``) as "not set"."""
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return value if value.get_secret_value().strip() else None
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalSecret = Annotated[SecretStr | None, BeforeValidator(_blank_to_none)]
OptionalStr = Annotated[str | None, BeforeValidator(_blank_to_none)]

# The CS2 Active Duty pool changes a few times per year. This is only the *default*; the meta
# module detects the pool actually being played from recent Tier 1/2 results, and
# config/patches.yaml holds the pool history (whose latest entry differs from this list - see
# docs/dev/reports/integration.md; the synthetic universe and many tests are pinned to this one).
DEFAULT_ACTIVE_MAP_POOL: list[str] = ["ancient", "anubis", "dust2", "inferno", "mirage", "nuke", "train"]


class DatabaseSettings(BaseModel):
    # Matches docker-compose.yml / .env.example; 127.0.0.1 avoids an IPv6 (::1) attempt on Windows.
    url: str = "postgresql+psycopg://cs2bot:change-me@127.0.0.1:5432/cs2bot"
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10


class ScopeSettings(BaseModel):
    """Which matches are in scope (PLAN.md section 4.1)."""

    pandascore_tiers: list[str] = Field(default_factory=lambda: ["s", "a", "b"])
    hltv_top_n: int = 50
    active_map_pool: list[str] = Field(default_factory=lambda: list(DEFAULT_ACTIVE_MAP_POOL))
    backfill_start: date = date(2023, 9, 27)  # CS2 release
    max_hours_ahead: int = 72


class PandaScoreSettings(BaseModel):
    api_token: OptionalSecret = None
    base_url: str = "https://api.pandascore.co"
    videogame: str = "csgo"  # PandaScore still serves CS2 under the csgo slug
    max_requests_per_hour: int = 1000
    page_size: int = 100
    timeout_s: float = 30.0


class HLTVSettings(BaseModel):
    base_url: str = "https://www.hltv.org"
    fetch_mode: Literal["browser", "http"] = "browser"
    min_delay_s: float = 4.0
    max_delay_s: float = 9.0
    proxy_url: OptionalSecret = None
    cache_dir: Path = Path("data/cache/hltv")
    cache_ttl_minutes: int = 360
    finished_match_cache_ttl_minutes: int = 60 * 24 * 30
    user_agent: OptionalStr = None
    headless: bool = True
    chromium_executable: OptionalStr = None
    navigation_timeout_s: float = 60.0
    # Persistent browser profile: cookies survive restarts, so a challenge the owner passes by hand
    # once in a visible window (CS2BOT_HLTV__HEADLESS=false) keeps working afterwards.
    browser_profile_dir: Path | None = Path("data/browser/hltv")
    challenge_wait_s: float = 20.0
    cooldown_base_s: float = 900.0  # first cool-down after a block; doubles on each repeat
    cooldown_max_s: float = 43_200.0
    block_resources: bool = True  # skip images / media / fonts (less traffic for HLTV and us)


class DemoSettings(BaseModel):
    storage_dir: Path = Path("data/demos")
    keep_archives: bool = False
    keep_dem_files: bool = False
    max_concurrent_downloads: int = 1
    position_sample_seconds: list[int] = Field(default_factory=lambda: [10, 20, 30, 45, 60])
    trade_window_s: float = 5.0
    download_timeout_s: float = 900.0


class BetclicSettings(BaseModel):
    base_url: str = "https://www.betclic.pl"
    # Listing page for CS2 events. Unverified from outside Poland - confirm on the owner's PC.
    cs2_listing_path: str = "/e-sport-s102"
    offering_api_base: OptionalStr = None
    site_code: str = "plpl"
    country_code: str = "pl"
    language: str = "pl"
    poll_interval_s: int = 180
    tax_rate: Decimal = Decimal("0.12")
    tax_free_promo: bool = False
    big_win_tax_threshold: Decimal = Decimal("2280")
    big_win_tax_rate: Decimal = Decimal("0.10")
    market_rules_file: Path = Path("config/betclic_markets.yaml")
    selectors_file: Path = Path("config/betclic_selectors.yaml")
    username: OptionalSecret = None
    password: OptionalSecret = None
    # Where Betclic credentials come from (they are optional: the owner logs in by hand once and
    # the persistent browser profile keeps the session). "keyring" = Windows Credential Manager.
    credentials_source: Literal["none", "env", "keyring"] = "none"
    keyring_service: str = "cs2bot-betclic"
    browser_profile_dir: Path = Path("data/browser/betclic")
    chromium_executable: OptionalStr = None
    headless: bool = True  # odds collector only (public pages, no login, separate browser context)
    proxy_url: OptionalSecret = None
    min_stake: Decimal = Decimal("2.00")
    odds_max_age_s: int = 600
    # Settlement-rule assumptions; adjust once real Betclic settlements have been observed.
    void_refunds_stake_tax: bool = True  # a voided/pushed bet returns the full gross stake
    winnings_tax_rounding: Literal["zloty", "grosz"] = "zloty"
    h2h_tie_rule: Literal["push", "lose"] = "push"  # player head-to-head kills ending level
    totals_include_overtime: bool = True  # round totals / handicaps count overtime rounds


class LLMSettings(BaseModel):
    enabled: bool = True
    api_key: OptionalSecret = None  # falls back to the SDK's own ANTHROPIC_API_KEY resolution
    context_model: str = "claude-opus-5-5"
    triage_model: str = "claude-haiku-5-5"
    context_effort: Effort = "high"
    triage_effort: Effort = "low"
    use_server_fallbacks: bool = True
    max_total_logit_shift: float = 0.35
    max_flag_logit_shift: float = 0.20
    request_timeout_s: float = 600.0
    max_news_items_per_match: int = 25


class ModelSettings(BaseModel):
    artifacts_dir: Path = Path("data/models")
    n_map_simulations: int = 20_000
    random_seed: int | None = 20231
    # Not read by any model (final review): the rating model's time decay and priors live in
    # ``cs2bot.models.ratings.RatingConfig`` (defaults, or the result of ``cs2bot models tune``).
    rating_half_life_days: float = 180.0
    min_maps_for_rating: int = 8
    calibration_holdout_days: int = 90
    sigma_floor: float = 0.01
    scenario_n_simulations: int | None = None  # low/high scenarios; None = max(2000, n_map_simulations // 2)
    ot_start_money: int = 10_000  # per player at each overtime half (some organisers use 12 500)
    round_model_half_life_days: float = 365.0
    kill_model_half_life_days: float = 120.0
    top_fragger_samples: int = 20_000


class MarketThresholds(BaseModel):
    """Optional per-market override of the global decision thresholds."""

    min_probability: float | None = None
    min_ev: float | None = None
    max_sigma: float | None = None


class DecisionSettings(BaseModel):
    """Bet only if ALL checks pass (PLAN.md section 6)."""

    min_probability: float = 0.75
    # Owner decision 2026-11-24: bet on confidence only, the odds are not a criterion. -1.0 disables the
    # EV check (EV per unit staked can never be below -1). Set 0.0 to restore a break-even guard.
    min_ev: float = -1.0
    max_sigma: float = 0.05
    min_data_completeness: float = 0.95
    min_event_link_confidence: float = 0.90
    min_odds: Decimal = Decimal("1.01")
    max_odds: Decimal = Decimal("15.0")
    min_minutes_before_start: int = 10
    max_hours_before_start: int = 48
    prefer_known_veto: bool = True
    market_overrides: dict[str, MarketThresholds] = Field(default_factory=dict)
    disabled_markets: list[str] = Field(default_factory=list)
    # Require the calibration layer's per-market "trusted" flag (enough history, calibrated within
    # tolerance in the 0.75-1.0 range) before betting a market type. Only enforced when the caller
    # passes the trusted set to ``engine.decision.evaluate`` (see docs/dev/reports/cloud_autonomy.md).
    require_trusted_calibration: bool = False


class BankrollSettings(BaseModel):
    starting_bankroll: Decimal = Decimal("1000.00")
    currency: str = "PLN"
    kelly_fraction: float = 0.25
    max_stake_fraction: float = 0.02
    max_match_exposure_fraction: float = 0.04
    daily_loss_limit_fraction: float = 0.05
    weekly_loss_limit_fraction: float = 0.10
    max_bets_per_day: int = 10
    stake_step: Decimal = Decimal("0.10")
    # How stakes are sized. "confidence_tiers" (owner decision 2026-11-24) ignores the odds: the stake is
    # a fraction of the bankroll chosen by the calibrated probability; "kelly" is fractional Kelly.
    staking_mode: Literal["confidence_tiers", "kelly"] = "confidence_tiers"
    # (minimum probability, fraction of bankroll); the highest tier whose minimum is met applies.
    confidence_tiers: list[tuple[float, float]] = Field(
        default_factory=lambda: [(0.75, 0.01), (0.85, 0.015), (0.92, 0.02)]
    )


class ExecutorSettings(BaseModel):
    enabled: bool = True
    dry_run: bool = True  # set CS2BOT_EXECUTOR__DRY_RUN=false to place real bets
    odds_tolerance: float = 0.02  # max relative odds drop accepted at placement time
    action_delay_min_s: float = 1.5
    action_delay_max_s: float = 4.0
    navigation_timeout_s: float = 45.0
    headless: bool = False  # visible browser on the owner's desktop, logged-in persistent profile
    screenshot_dir: Path = Path("data/screenshots")
    reconcile_tolerance: Decimal = Decimal("0.05")
    # Browser automation tuning (see cs2bot.executor.tuning.ExecutorTuning).
    element_timeout_s: float = 10.0
    optional_timeout_s: float = 1.5
    slip_update_timeout_s: float = 8.0
    confirmation_timeout_s: float = 45.0  # receipt / error after the final click, else UNCERTAIN
    min_seconds_between_bets: float = 20.0
    max_odds_change_rounds: int = 2
    viewport_width: int = 1440
    viewport_height: int = 900
    save_html_on_selector_error: bool = True
    health_check_event_page: bool = True
    lock_stale_after_s: float = 600.0
    lock_heartbeat_s: float = 30.0
    # Refuse a price this many times above the requested odds (likely a misread); None disables it.
    max_odds_rise_factor: float | None = 1.5
    # Plausible range of the slip's "potential win" / (stake x odds) before the final click.
    potential_win_min_ratio: float = 0.70
    potential_win_max_ratio: float = 1.05
    journal_retention_days: float = 60.0  # placement-journal records older than this are ignored
    # Linux server without a desktop: a visible (headless=false) browser needs an X display (Xvfb,
    # DISPLAY=:99 on the VPS). With no DISPLAY / WAYLAND_DISPLAY set the browser falls back to
    # headless instead of failing to start; false = fail loudly instead.
    headless_without_display: bool = True
    # Auto-login (cloud autonomy): seconds to wait for the outcome after submitting the login form.
    login_submit_timeout_s: float = 30.0


class APISettings(BaseModel):
    # Local-only by default (PLAN.md: dashboard reachable only from the owner's PC).
    host: str = "127.0.0.1"
    port: int = 8000
    serve_dashboard: bool = True
    dashboard_dist_dir: Path = Path("dashboard/dist")
    username: str = "admin"
    password: OptionalSecret = None
    session_secret: OptionalSecret = None
    session_max_age_s: int = 60 * 60 * 12
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    allowed_hosts: list[str] = Field(default_factory=list)  # extra Host-header names (non-loopback)
    login_max_failures: int = 5
    login_global_max_failures: int = 20
    login_window_s: float = 300.0

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")


AUTO_RESUME_DEFAULT_REASONS: list[str] = [
    "loss_limit_daily",
    "loss_limit_weekly",
    "uncertain_bet",
    "login_required",
    "2fa_required",
    "balance_mismatch",
    "selector_error",
    "executor_unavailable",
]


class AutonomySettings(BaseModel):
    """Unattended operation (PLAN.md 0b): auto-login, auto-resume of automatic pauses, circuit breaker.

    Disabled by default (tests, the Windows PC); the VPS ``.env`` turns it on. The kill switch and a
    manual pause are never lifted automatically, whatever is configured here.
    """

    enabled: bool = False
    # Fill AND submit the Betclic login form with the saved credentials (betclic.credentials_source
    # env/keyring) when the session is logged out. Stops at an SMS/2FA step (never automated).
    auto_login: bool = False
    # Master switch of the auto-resume policy and the pause reasons it may lift.
    auto_resume: bool = True
    auto_resume_reasons: list[str] = Field(default_factory=lambda: list(AUTO_RESUME_DEFAULT_REASONS))
    check_interval_min: float = 5.0
    # Circuit breaker: the same reason may auto-resume at most this many times per window; one more
    # and the bot stays paused (circuit_open) until the owner resumes.
    circuit_breaker_limit: int = 3
    circuit_breaker_window_h: float = 24.0
    login_retry_interval_min: float = 15.0
    # A login that stopped at the SMS/2FA step is retried this rarely (every try may send an SMS).
    twofa_retry_interval_min: float = 240.0
    # Re-read the Betclic balance before lifting a balance-mismatch pause (books any remaining
    # difference as a RECONCILIATION entry). Without a readable balance the entry booked when the
    # mismatch was found is accepted.
    balance_recheck: bool = True


class TelegramSettings(BaseModel):
    """Send-only notifications + owner commands through a Telegram bot (Bot API over HTTPS).

    Create the bot with @BotFather (token) and read your chat id with @userinfobot. Commands are
    accepted only from ``chat_id``; everything else is ignored and logged.
    """

    enabled: bool = False
    bot_token: OptionalSecret = None
    chat_id: OptionalStr = None  # numeric id (negative for groups) or @channelname
    api_base: str = "https://api.telegram.org"
    daily_report_time: time = time(21, 0)  # Europe/Warsaw
    # which events to send
    notify_bets: bool = True
    notify_dry_run_bets: bool = True
    notify_settlements: bool = True
    notify_control: bool = True  # pause / resume / auto-resume / circuit breaker / kill switch / login
    notify_errors: bool = True  # failing jobs (rate-limited per job), executor problems
    notify_lifecycle: bool = True  # startup / shutdown
    daily_report: bool = True
    commands_enabled: bool = True
    # transport
    request_timeout_s: float = 15.0
    max_retries: int = 3
    min_send_interval_s: float = 1.1  # Telegram allows about one message per second per chat
    poll_timeout_s: int = 5  # getUpdates long-poll timeout (short: shutdown waits for it)
    poll_session_s: float = 600.0  # one scheduler run of the command poller lasts this long
    notify_interval_s: int = 30  # how often new events are looked up in the database
    error_alert_cooldown_min: float = 360.0


class SchedulerSettings(BaseModel):
    """Built-in scheduler used by ``cs2bot up``. Missed runs are caught up after a restart."""

    enabled: bool = True
    tick_s: float = 5.0
    odds_poll_s: int = 180
    upcoming_sync_min: int = 30
    results_sync_min: int = 60
    news_poll_min: int = 30
    predict_min: int = 15
    settle_min: int = 30
    demo_process_min: int = 60
    retrain_cron: str = "0 5 * * *"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CS2BOT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "test", "prod"] = "dev"
    timezone: str = "Europe/Warsaw"
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    log_json: bool = False

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    scope: ScopeSettings = Field(default_factory=ScopeSettings)
    pandascore: PandaScoreSettings = Field(default_factory=PandaScoreSettings)
    hltv: HLTVSettings = Field(default_factory=HLTVSettings)
    demos: DemoSettings = Field(default_factory=DemoSettings)
    betclic: BetclicSettings = Field(default_factory=BetclicSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    decision: DecisionSettings = Field(default_factory=DecisionSettings)
    bankroll: BankrollSettings = Field(default_factory=BankrollSettings)
    executor: ExecutorSettings = Field(default_factory=ExecutorSettings)
    api: APISettings = Field(default_factory=APISettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    autonomy: AutonomySettings = Field(default_factory=AutonomySettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if not 0 < self.bankroll.kelly_fraction <= 1:
            raise ValueError("bankroll.kelly_fraction must be in (0, 1]")
        if not 0 < self.decision.min_probability < 1:
            raise ValueError("decision.min_probability must be in (0, 1)")
        if not self.api.is_loopback and (self.api.password is None or self.api.session_secret is None):
            raise ValueError(
                "The API listens on a non-loopback address: set CS2BOT_API__PASSWORD and "
                "CS2BOT_API__SESSION_SECRET (or keep CS2BOT_API__HOST=127.0.0.1)"
            )
        return self

    @property
    def effective_tax_rate(self) -> Decimal:
        """Stake tax actually applied (0 during Betclic 'bez podatku' promos)."""
        return Decimal("0") if self.betclic.tax_free_promo else self.betclic.tax_rate


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Forget the cached settings (tests / after changing env vars)."""
    get_settings.cache_clear()
