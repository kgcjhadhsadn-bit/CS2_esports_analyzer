"""Name normalisation and fuzzy matching for teams, players, maps across sources.

PandaScore, HLTV and Betclic spell names differently ("Natus Vincere" / "NAVI" / "Natus Vincere
Esports"). Everything that links entities across sources goes through these helpers.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

# Words that carry no identity. NOTE: "academy", "junior", "youth", "female", "fe", "2" etc. are
# deliberately NOT stripped - academy rosters are different teams.
_NOISE_WORDS = {"team", "esports", "esport", "e-sports", "gaming", "club", "gg", "clan", "cs", "cs2", "csgo"}

# Canonical aliases (normalised form -> canonical normalised form).
_TEAM_ALIASES: dict[str, str] = {
    "navi": "natus vincere",
    "na vi": "natus vincere",
    "natus vincere": "natus vincere",
    "mousesports": "mouz",
    "mouz": "mouz",
    "faze": "faze",
    "g2": "g2",
    "vitality": "vitality",
    "liquid": "liquid",
    "spirit": "spirit",
    "virtus pro": "virtus pro",
    "vp": "virtus pro",
    "heroic": "heroic",
    "astralis": "astralis",
    "fnatic": "fnatic",
    "complexity": "complexity",
    "col": "complexity",
    "nip": "ninjas in pyjamas",
    "ninjas in pyjamas": "ninjas in pyjamas",
    "big": "big",
    "ence": "ence",
    "furia": "furia",
    "the mongolz": "mongolz",
    "mongolz": "mongolz",
    "3dmax": "3dmax",
    "falcons": "falcons",
    "eternal fire": "eternal fire",
    "pain": "pain",
    "mibr": "mibr",
    "imperial": "imperial",
    "saw": "saw",
    "gamerlegion": "gamerlegion",
    "aurora": "aurora",
    "b8": "b8",
    "9ine": "9ine",
    "monte": "monte",
    "apeks": "apeks",
    "og": "og",
    "cloud9": "cloud9",
    "c9": "cloud9",
    "tyloo": "tyloo",
    "lynn vision": "lynn vision",
    "m80": "m80",
    "wildcard": "wildcard",
    "nrg": "nrg",
    "bestia": "bestia",
}

# CS2 map name normalisation: "de_mirage", "Mirage", "MIRAGE" -> "mirage"
_MAP_ALIASES: dict[str, str] = {
    "dust ii": "dust2",
    "dust 2": "dust2",
    "dust2": "dust2",
    "d2": "dust2",
    "mirage": "mirage",
    "inferno": "inferno",
    "nuke": "nuke",
    "ancient": "ancient",
    "anubis": "anubis",
    "vertigo": "vertigo",
    "overpass": "overpass",
    "train": "train",
    "cache": "cache",
    "cobblestone": "cobblestone",
    "tuscan": "tuscan",
    "season": "season",
}


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_text(text: str) -> str:
    text = strip_accents(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_team_name(name: str) -> str:
    """Canonical comparable form of a team name."""
    base = normalize_text(name)
    if base in _TEAM_ALIASES:
        return _TEAM_ALIASES[base]
    words = [w for w in base.split(" ") if w not in _NOISE_WORDS]
    stripped = " ".join(words) or base
    return _TEAM_ALIASES.get(stripped, stripped)


def normalize_player_name(name: str) -> str:
    return normalize_text(name).replace(" ", "")


def normalize_map_name(name: str) -> str:
    """'de_dust2' / 'Dust II' / 'DUST2' -> 'dust2'. Unknown maps are returned normalised."""
    base = normalize_text(name)
    if base.startswith("de "):
        base = base[3:]
    if base in _MAP_ALIASES:
        return _MAP_ALIASES[base]
    return base.replace(" ", "")


def team_name_similarity(a: str, b: str) -> float:
    """0..100 similarity of two team names after normalisation."""
    na, nb = normalize_team_name(a), normalize_team_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    return float(max(fuzz.token_set_ratio(na, nb), fuzz.ratio(na, nb)))
