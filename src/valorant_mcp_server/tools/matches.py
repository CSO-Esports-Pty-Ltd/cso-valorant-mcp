"""
MCP tools for Valorant match history and match details via the Henrik Dev API.

Endpoints used:
  GET /valorant/v4/matches/{region}/{platform}/{name}/{tag}
  GET /valorant/v4/match/{region}/{matchid}
"""

import asyncio
import os
import time
from collections import OrderedDict
from typing import Any

from valorant_mcp_server import client
from valorant_mcp_server.literals import GameMode, Platform, Region
from valorant_mcp_server.riot_id import riot_id_error, riot_id_path


_MATCH_CACHE_TTL_SECONDS = max(
    0.0, float(os.getenv("HENRIK_MATCH_CACHE_TTL_SECONDS", "300"))
)
_MATCH_CACHE_MAX_ENTRIES = max(
    1, int(os.getenv("HENRIK_MATCH_CACHE_MAX_ENTRIES", "256"))
)
_MATCH_DETAIL_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_MATCH_DETAIL_LOCKS: dict[str, asyncio.Lock] = {}


def clear_match_detail_cache() -> None:
    _MATCH_DETAIL_CACHE.clear()
    _MATCH_DETAIL_LOCKS.clear()


def _cached_match(cache_key: str) -> dict[str, Any] | None:
    entry = _MATCH_DETAIL_CACHE.get(cache_key)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at <= time.monotonic():
        _MATCH_DETAIL_CACHE.pop(cache_key, None)
        return None
    _MATCH_DETAIL_CACHE.move_to_end(cache_key)
    return payload


def _store_match(cache_key: str, payload: dict[str, Any]) -> None:
    if _MATCH_CACHE_TTL_SECONDS <= 0:
        return
    _MATCH_DETAIL_CACHE[cache_key] = (
        time.monotonic() + _MATCH_CACHE_TTL_SECONDS,
        payload,
    )
    _MATCH_DETAIL_CACHE.move_to_end(cache_key)
    while len(_MATCH_DETAIL_CACHE) > _MATCH_CACHE_MAX_ENTRIES:
        _MATCH_DETAIL_CACHE.popitem(last=False)


async def get_match_history(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
    mode: GameMode | None = None,
    map_name: str | None = None,
    size: int | None = None,
    start: int | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Retrieve recent match history for a player by Riot ID.

    Args:
        region: Server region. One of: eu, na, latam, br, ap, kr.
        name: The player's in-game name.
        tag: The player's tag line without '#'.
        platform: Platform to query — 'pc' (default) or 'console'.
        mode: Optional game mode filter (e.g. 'competitive', 'unrated').
        map_name: Optional map name filter (e.g. 'Ascent', 'Bind'); passed
            through to the API unvalidated so newly released maps work.
        size: Number of matches to return (the API defaults to 10).
        start: Optional v4 pagination start index.

    Returns:
        A list of match summary objects on success. Each entry includes match
        metadata, teams, and per-player stats for that match. On failure a
        structured error dict ({'error': True, ...}) is returned instead of a
        list — callers must check the type before slicing.
    """
    try:
        safe_name, safe_tag = riot_id_path(name, tag)
    except ValueError as exc:
        return riot_id_error(exc, name=name, tag=tag)

    params: dict[str, Any] = {}
    if mode:
        params["mode"] = mode
    if map_name:
        params["map"] = map_name
    if size is not None:
        params["size"] = size
    if start is not None:
        params["start"] = max(0, start)

    data = await client.get(
        f"/valorant/v4/matches/{region}/{platform}/{safe_name}/{safe_tag}",
        params=params,
    )
    return data.get("data", data)


async def get_match(region: Region, match_id: str) -> dict[str, Any]:
    """Retrieve full details for a single Valorant match by match ID.

    Args:
        region: Server region. One of: eu, na, latam, br, ap, kr.
        match_id: The unique match UUID (e.g. '696848f3-f16f-45bf-af13-e2192f81a600').

    Returns:
        A dictionary with complete match data including metadata, all players,
        round results, kills, economy, and team outcomes.
    """
    cache_key = f"{str(region).lower()}:{match_id}"
    cached = _cached_match(cache_key)
    if cached is not None:
        return cached

    lock = _MATCH_DETAIL_LOCKS.setdefault(cache_key, asyncio.Lock())
    try:
        async with lock:
            cached = _cached_match(cache_key)
            if cached is not None:
                return cached

            data = await client.get(f"/valorant/v4/match/{region}/{match_id}")
            payload = data.get("data", data)
            if isinstance(payload, dict) and not payload.get("error"):
                _store_match(cache_key, payload)
            return payload
    finally:
        if _MATCH_DETAIL_LOCKS.get(cache_key) is lock:
            _MATCH_DETAIL_LOCKS.pop(cache_key, None)
