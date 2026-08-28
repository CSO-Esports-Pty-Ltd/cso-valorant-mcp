"""Shared Literal aliases and validation patterns used across Valorant MCP tools."""

import re
from typing import Literal

Region = Literal["eu", "na", "latam", "br", "ap", "kr"]
Platform = Literal["pc", "console"]

GameMode = Literal[
    "competitive",
    "custom",
    "deathmatch",
    "escalation",
    "teamdeathmatch",
    "newmap",
    "replication",
    "snowballfight",
    "spikerush",
    "swiftplay",
    "unrated",
]

# Map names are deliberately NOT a Literal: Riot ships new maps regularly and
# a stale enum would reject them. Tools accept any string and pass it through
# to the Henrik API (e.g. 'Ascent', 'Bind', 'Corrode').

# Season short codes: 'e{episode}a{act}' for the Episode era (e1a1 .. e9a3)
# and 'v{year}a{act}' (e.g. 'v25a1') for the V25+ season format. Validated by
# pattern instead of a Literal so new seasons work without a code change.
SEASON_SHORT_PATTERN = re.compile(r"^(e\d+a\d+|v\d{2,4}a\d+)$", re.IGNORECASE)


def is_valid_season_short(value: str) -> bool:
    """Return True when value looks like a Henrik season short code."""
    return bool(SEASON_SHORT_PATTERN.match(str(value).strip()))

EsportsRegion = Literal[
    "international",
    "north america",
    "emea",
    "brazil",
    "japan",
    "korea",
    "latin_america",
    "latin_america_south",
    "latin_america_north",
    "southeast_asia",
    "vietnam",
    "oceania",
]

League = Literal[
    "vct_americas",
    "challengers_na",
    "game_changers_na",
    "vct_emea",
    "vct_pacific",
    "challengers_br",
    "challengers_jpn",
    "challengers_kr",
    "challengers_latam",
    "challengers_latam_n",
    "challengers_latam_s",
    "challengers_apac",
    "challengers_sea_id",
    "challengers_sea_ph",
    "challengers_sea_sg_and_my",
    "challengers_sea_th",
    "challengers_sea_hk_and_tw",
    "challengers_sea_vn",
    "valorant_oceania_tour",
    "challengers_south_asia",
    "game_changers_sea",
    "game_changers_series_brazil",
    "game_changers_east_asia",
    "game_changers_emea",
    "game_changers_jpn",
    "game_changers_kr",
    "game_changers_latam",
    "game_changers_championship",
    "masters",
    "last_chance_qualifier_apac",
    "last_chance_qualifier_east_asia",
    "last_chance_qualifier_emea",
    "last_chance_qualifier_na",
    "last_chance_qualifier_br_and_latam",
    "vct_lock_in",
    "champions",
    "vrl_spain",
    "vrl_northern_europe",
    "vrl_dach",
    "vrl_france",
    "vrl_east",
    "vrl_turkey",
    "vrl_cis",
    "mena_resilence",
    "challengers_italy",
    "challengers_portugal",
]
