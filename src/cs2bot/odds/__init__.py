"""cs2bot.odds - Betclic odds collection, market mapping, event linking and quotes.

See docs/ARCHITECTURE.md (P4) for the contract and :mod:`cs2bot.odds.sources` for how Betclic's
web apps load odds. Typical wiring::

    mapper = MarketMapper.from_yaml(settings.betclic.market_rules_file)
    source = BetclicBrowserSource(settings)          # or BetclicApiSource(settings)
    with session_scope() as session:
        report = collect_once(session, source, mapper, settings=settings)
        quotes = latest_quotes(session, match_id, max_age_s=settings.betclic.odds_max_age_s)
"""

from cs2bot.odds.api import BetclicApiError, BetclicApiSource
from cs2bot.odds.browser import (
    BetclicBrowserSource,
    BetclicSource,
    BetclicSourceError,
    run_coroutine_blocking,
)
from cs2bot.odds.collector import (
    CollectorConfig,
    CollectReport,
    collect_once,
    original_market_external_id,
    remap_event,
)
from cs2bot.odds.event_matching import (
    LinkCandidate,
    LinkConfig,
    LinkResult,
    link_event,
    store_betclic_aliases,
)
from cs2bot.odds.market_mapping import (
    MarketMapper,
    PlayerResolver,
    lineup_player_resolver,
    list_unmapped,
    list_unmapped_selections,
    make_name_resolver,
)
from cs2bot.odds.payload_parser import PayloadKeys, extract_events, extract_json_documents
from cs2bot.odds.quotes import closing_odds, latest_quote, latest_quotes, odds_history
from cs2bot.odds.source_config import BetclicSourceConfig
from cs2bot.odds.sources import (
    FetchInfo,
    OddsSource,
    RawPayloadReplaySource,
    RawPayloadStore,
    StaticOddsSource,
    SupportsFetchInfo,
)

__all__ = [
    "BetclicApiError",
    "BetclicApiSource",
    "BetclicBrowserSource",
    "BetclicSource",
    "BetclicSourceConfig",
    "BetclicSourceError",
    "CollectReport",
    "CollectorConfig",
    "FetchInfo",
    "LinkCandidate",
    "LinkConfig",
    "LinkResult",
    "MarketMapper",
    "OddsSource",
    "PayloadKeys",
    "PlayerResolver",
    "RawPayloadReplaySource",
    "RawPayloadStore",
    "StaticOddsSource",
    "SupportsFetchInfo",
    "closing_odds",
    "collect_once",
    "extract_events",
    "extract_json_documents",
    "latest_quote",
    "latest_quotes",
    "lineup_player_resolver",
    "link_event",
    "list_unmapped",
    "list_unmapped_selections",
    "make_name_resolver",
    "odds_history",
    "original_market_external_id",
    "remap_event",
    "run_coroutine_blocking",
    "store_betclic_aliases",
]
