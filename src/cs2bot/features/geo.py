"""Curated geography for the travel / time-zone features: country -> region and UTC offset.

Sources: standard (winter) UTC offsets of each country's capital or main esports hub; regions follow
the Valve Regional Standings split (Europe incl. CIS and Middle East, Americas split into North and
South, Asia, Oceania). Daylight-saving time is ignored on purpose: a +-1 h error does not change what
the features capture (a 7-hour jet lag vs none). Countries spanning several zones use the zone where
their teams and events usually are (USA: Central, Russia: Moscow, Brazil: Sao Paulo, Australia: Sydney,
Canada: Eastern, China: Beijing, Kazakhstan: Astana).

Inputs are messy (HLTV country names, ISO codes, "Cologne, Germany", "Europe (Online)"), so every
lookup is tolerant and returns ``None`` when unsure.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

EUROPE: Final[str] = "europe"
CIS: Final[str] = "cis"
NORTH_AMERICA: Final[str] = "north_america"
SOUTH_AMERICA: Final[str] = "south_america"
ASIA: Final[str] = "asia"
OCEANIA: Final[str] = "oceania"
MIDDLE_EAST: Final[str] = "middle_east"
AFRICA: Final[str] = "africa"

# country (lower-case, ASCII) -> (region, UTC offset hours, ISO-3166 alpha-2)
_COUNTRIES: Final[dict[str, tuple[str, float, str]]] = {
    "denmark": (EUROPE, 1, "dk"),
    "sweden": (EUROPE, 1, "se"),
    "norway": (EUROPE, 1, "no"),
    "finland": (EUROPE, 2, "fi"),
    "iceland": (EUROPE, 0, "is"),
    "germany": (EUROPE, 1, "de"),
    "france": (EUROPE, 1, "fr"),
    "belgium": (EUROPE, 1, "be"),
    "netherlands": (EUROPE, 1, "nl"),
    "luxembourg": (EUROPE, 1, "lu"),
    "united kingdom": (EUROPE, 0, "gb"),
    "ireland": (EUROPE, 0, "ie"),
    "spain": (EUROPE, 1, "es"),
    "portugal": (EUROPE, 0, "pt"),
    "italy": (EUROPE, 1, "it"),
    "malta": (EUROPE, 1, "mt"),
    "switzerland": (EUROPE, 1, "ch"),
    "austria": (EUROPE, 1, "at"),
    "poland": (EUROPE, 1, "pl"),
    "czech republic": (EUROPE, 1, "cz"),
    "slovakia": (EUROPE, 1, "sk"),
    "hungary": (EUROPE, 1, "hu"),
    "slovenia": (EUROPE, 1, "si"),
    "croatia": (EUROPE, 1, "hr"),
    "bosnia and herzegovina": (EUROPE, 1, "ba"),
    "serbia": (EUROPE, 1, "rs"),
    "montenegro": (EUROPE, 1, "me"),
    "north macedonia": (EUROPE, 1, "mk"),
    "albania": (EUROPE, 1, "al"),
    "kosovo": (EUROPE, 1, "xk"),
    "romania": (EUROPE, 2, "ro"),
    "bulgaria": (EUROPE, 2, "bg"),
    "greece": (EUROPE, 2, "gr"),
    "cyprus": (EUROPE, 2, "cy"),
    "estonia": (EUROPE, 2, "ee"),
    "latvia": (EUROPE, 2, "lv"),
    "lithuania": (EUROPE, 2, "lt"),
    "turkey": (EUROPE, 3, "tr"),
    "ukraine": (CIS, 2, "ua"),
    "russia": (CIS, 3, "ru"),
    "belarus": (CIS, 3, "by"),
    "kazakhstan": (CIS, 5, "kz"),
    "uzbekistan": (CIS, 5, "uz"),
    "kyrgyzstan": (CIS, 6, "kg"),
    "georgia": (CIS, 4, "ge"),
    "armenia": (CIS, 4, "am"),
    "azerbaijan": (CIS, 4, "az"),
    "moldova": (CIS, 2, "md"),
    "mongolia": (ASIA, 8, "mn"),
    "israel": (MIDDLE_EAST, 2, "il"),
    "saudi arabia": (MIDDLE_EAST, 3, "sa"),
    "united arab emirates": (MIDDLE_EAST, 4, "ae"),
    "qatar": (MIDDLE_EAST, 3, "qa"),
    "jordan": (MIDDLE_EAST, 3, "jo"),
    "lebanon": (MIDDLE_EAST, 2, "lb"),
    "egypt": (AFRICA, 2, "eg"),
    "south africa": (AFRICA, 2, "za"),
    "morocco": (AFRICA, 1, "ma"),
    "tunisia": (AFRICA, 1, "tn"),
    "algeria": (AFRICA, 1, "dz"),
    "united states": (NORTH_AMERICA, -6, "us"),
    "canada": (NORTH_AMERICA, -5, "ca"),
    "mexico": (NORTH_AMERICA, -6, "mx"),
    "brazil": (SOUTH_AMERICA, -3, "br"),
    "argentina": (SOUTH_AMERICA, -3, "ar"),
    "chile": (SOUTH_AMERICA, -4, "cl"),
    "uruguay": (SOUTH_AMERICA, -3, "uy"),
    "peru": (SOUTH_AMERICA, -5, "pe"),
    "colombia": (SOUTH_AMERICA, -5, "co"),
    "venezuela": (SOUTH_AMERICA, -4, "ve"),
    "paraguay": (SOUTH_AMERICA, -4, "py"),
    "bolivia": (SOUTH_AMERICA, -4, "bo"),
    "ecuador": (SOUTH_AMERICA, -5, "ec"),
    "guatemala": (NORTH_AMERICA, -6, "gt"),
    "china": (ASIA, 8, "cn"),
    "hong kong": (ASIA, 8, "hk"),
    "taiwan": (ASIA, 8, "tw"),
    "macau": (ASIA, 8, "mo"),
    "japan": (ASIA, 9, "jp"),
    "south korea": (ASIA, 9, "kr"),
    "singapore": (ASIA, 8, "sg"),
    "malaysia": (ASIA, 8, "my"),
    "indonesia": (ASIA, 7, "id"),
    "thailand": (ASIA, 7, "th"),
    "vietnam": (ASIA, 7, "vn"),
    "philippines": (ASIA, 8, "ph"),
    "india": (ASIA, 5.5, "in"),
    "pakistan": (ASIA, 5, "pk"),
    "bangladesh": (ASIA, 6, "bd"),
    "sri lanka": (ASIA, 5.5, "lk"),
    "nepal": (ASIA, 5.75, "np"),
    "iran": (MIDDLE_EAST, 3.5, "ir"),
    "iraq": (MIDDLE_EAST, 3, "iq"),
    "kuwait": (MIDDLE_EAST, 3, "kw"),
    "bahrain": (MIDDLE_EAST, 3, "bh"),
    "oman": (MIDDLE_EAST, 4, "om"),
    "australia": (OCEANIA, 10, "au"),
    "new zealand": (OCEANIA, 12, "nz"),
}

_ALIASES: Final[dict[str, str]] = {
    "usa": "united states",
    "us": "united states",
    "united states of america": "united states",
    "america": "united states",
    "uk": "united kingdom",
    "great britain": "united kingdom",
    "england": "united kingdom",
    "scotland": "united kingdom",
    "wales": "united kingdom",
    "northern ireland": "united kingdom",
    "czechia": "czech republic",
    "korea": "south korea",
    "republic of korea": "south korea",
    "uae": "united arab emirates",
    "emirates": "united arab emirates",
    "holland": "netherlands",
    "the netherlands": "netherlands",
    "russian federation": "russia",
    "bosnia": "bosnia and herzegovina",
    "macedonia": "north macedonia",
    "turkiye": "turkey",
    "ksa": "saudi arabia",
    "hongkong": "hong kong",
    "cis": "russia",  # HLTV uses "CIS" as a team flag for mixed CIS rosters
}

# event cities -> country (venues of Tier 1/2 CS events since 2023 and frequent hosts)
_CITIES: Final[dict[str, str]] = {
    "cologne": "germany",
    "koln": "germany",
    "berlin": "germany",
    "hamburg": "germany",
    "katowice": "poland",
    "krakow": "poland",
    "warsaw": "poland",
    "wroclaw": "poland",
    "copenhagen": "denmark",
    "aarhus": "denmark",
    "herning": "denmark",
    "odense": "denmark",
    "stockholm": "sweden",
    "jonkoping": "sweden",
    "malmo": "sweden",
    "paris": "france",
    "marseille": "france",
    "antwerp": "belgium",
    "amsterdam": "netherlands",
    "rotterdam": "netherlands",
    "utrecht": "netherlands",
    "london": "united kingdom",
    "birmingham": "united kingdom",
    "lisbon": "portugal",
    "madrid": "spain",
    "valencia": "spain",
    "barcelona": "spain",
    "bilbao": "spain",
    "malta": "malta",
    "st julian's": "malta",
    "belgrade": "serbia",
    "novi sad": "serbia",
    "bucharest": "romania",
    "sofia": "bulgaria",
    "budapest": "hungary",
    "prague": "czech republic",
    "vienna": "austria",
    "zurich": "switzerland",
    "basel": "switzerland",
    "milan": "italy",
    "rome": "italy",
    "kyiv": "ukraine",
    "kiev": "ukraine",
    "moscow": "russia",
    "saint petersburg": "russia",
    "minsk": "belarus",
    "astana": "kazakhstan",
    "almaty": "kazakhstan",
    "tbilisi": "georgia",
    "baku": "azerbaijan",
    "istanbul": "turkey",
    "riyadh": "saudi arabia",
    "jeddah": "saudi arabia",
    "abu dhabi": "united arab emirates",
    "dubai": "united arab emirates",
    "doha": "qatar",
    "austin": "united states",
    "dallas": "united states",
    "arlington": "united states",
    "atlanta": "united states",
    "los angeles": "united states",
    "new york": "united states",
    "chicago": "united states",
    "boston": "united states",
    "las vegas": "united states",
    "fort worth": "united states",
    "toronto": "canada",
    "montreal": "canada",
    "rio de janeiro": "brazil",
    "sao paulo": "brazil",
    "curitiba": "brazil",
    "buenos aires": "argentina",
    "santiago": "chile",
    "mexico city": "mexico",
    "shanghai": "china",
    "chengdu": "china",
    "beijing": "china",
    "shenzhen": "china",
    "chongqing": "china",
    "hangzhou": "china",
    "wuhan": "china",
    "macau": "macau",
    "hong kong": "hong kong",
    "singapore": "singapore",
    "kuala lumpur": "malaysia",
    "bangkok": "thailand",
    "jakarta": "indonesia",
    "manila": "philippines",
    "tokyo": "japan",
    "seoul": "south korea",
    "busan": "south korea",
    "melbourne": "australia",
    "sydney": "australia",
    "brisbane": "australia",
    "auckland": "new zealand",
}

_REGION_WORDS: Final[dict[str, str]] = {
    "europe": EUROPE,
    "eu": EUROPE,
    "cis": CIS,
    "north america": NORTH_AMERICA,
    "na": NORTH_AMERICA,
    "south america": SOUTH_AMERICA,
    "sa": SOUTH_AMERICA,
    "americas": NORTH_AMERICA,
    "asia": ASIA,
    "oceania": OCEANIA,
    "middle east": MIDDLE_EAST,
    "mena": MIDDLE_EAST,
    "africa": AFRICA,
}

# typical offset of a region (used for an "Online (Europe)" event or a region-only team flag)
_REGION_OFFSETS: Final[dict[str, float]] = {
    EUROPE: 1,
    CIS: 3,
    NORTH_AMERICA: -6,
    SOUTH_AMERICA: -3,
    ASIA: 8,
    OCEANIA: 10,
    MIDDLE_EAST: 3,
    AFRICA: 2,
}

_ISO: Final[dict[str, str]] = {code: name for name, (_r, _o, code) in _COUNTRIES.items()}
_ONLINE_RE: Final = re.compile(r"\bonline\b", re.IGNORECASE)


@dataclass(frozen=True)
class Place:
    """Where something is: a country (when known), its region and UTC offset; ``online`` events."""

    country: str | None
    region: str | None
    utc_offset: float | None
    online: bool = False


def _ascii(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower().strip()


def normalize_country(value: str | None) -> str | None:
    """Canonical country name (lower-case) from a name, alias or ISO alpha-2 code; None if unknown."""
    if not value:
        return None
    text = re.sub(r"\s+", " ", _ascii(value).replace("_", " ").replace("-", " ")).strip(" .")
    if not text:
        return None
    if text in _COUNTRIES:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    if len(text) == 2 and text in _ISO:
        return _ISO[text]
    if text in _CITIES:
        return _CITIES[text]
    return None


def country_place(country: str | None) -> Place | None:
    name = normalize_country(country)
    if name is None:
        return None
    region, offset, _ = _COUNTRIES[name]
    return Place(name, region, offset)


def parse_location(text: str | None) -> Place | None:
    """Parse an event location ("Cologne, Germany", "Online", "Europe (Online)", "Austin, TX, USA").

    Returns None when nothing is recognised. An online event gets ``online=True`` and, when a region
    word is present, that region (offset of the region's usual hub).
    """
    if not text or not text.strip():
        return None
    raw = _ascii(text)
    online = bool(_ONLINE_RE.search(raw))
    parts = [p.strip(" .()") for p in re.split(r"[,/|()\-]+", raw) if p.strip(" .()")]
    for part in reversed(parts):  # the country is usually last
        place = country_place(part)
        if place is not None:
            return Place(place.country, place.region, place.utc_offset, online)
    for part in parts:
        if part in _CITIES:
            place = country_place(_CITIES[part])
            if place is not None:
                return Place(place.country, place.region, place.utc_offset, online)
    for part in parts:
        region = _REGION_WORDS.get(part)
        if region is not None:
            return Place(None, region, _REGION_OFFSETS.get(region), online)
    if online:
        return Place(None, None, None, True)
    return None


def team_home(country: str | None, player_countries: tuple[str, ...] = ()) -> Place | None:
    """A team's home: its own country, else the most common country of its players.

    A region-only flag ("Europe", "CIS") gives the region with its typical offset.
    """
    place = country_place(country)
    if place is not None:
        return place
    if country:
        region = _REGION_WORDS.get(_ascii(country))
        if region is not None:
            return Place(None, region, _REGION_OFFSETS.get(region))
    counts: dict[str, int] = {}
    for c in player_countries:
        name = normalize_country(c)
        if name is not None:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None
    best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return country_place(best)


def tz_shift_hours(a: float | None, b: float | None) -> float | None:
    """Absolute time-zone difference in hours on the 24-hour circle (0..12); None if unknown."""
    if a is None or b is None:
        return None
    d = abs(a - b) % 24.0
    return min(d, 24.0 - d)
