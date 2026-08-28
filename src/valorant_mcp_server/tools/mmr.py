"""
MCP tools for Valorant MMR (rank/rating) data via the Henrik Dev API.

Endpoints used:
  GET /valorant/v3/mmr/{region}/{platform}/{name}/{tag}
  GET /valorant/v3/by-puuid/mmr/{region}/{platform}/{puuid}
  GET /valorant/v3/by-puuid/mmr/history/{region}/{platform}/{puuid}
"""

from typing import Any

from valorant_mcp_server import client
from valorant_mcp_server.literals import Platform, Region
from valorant_mcp_server.riot_id import riot_id_error, riot_id_path


async def get_mmr(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
) -> dict[str, Any]:
    """Retrieve current MMR/rank details for a player by Riot ID.

    Args:
        region: Server region. One of: eu, na, latam, br, ap, kr.
        name: The player's in-game name (e.g. 'TenZ').
        tag: The player's tag line without '#' (e.g. 'SEN').
        platform: Platform to query. Either 'pc' (default) or 'console'.

    Returns:
        account, peak, currentposition
        and seasonal mmr info for the given player#tag.
    """
    try:
        safe_name, safe_tag = riot_id_path(name, tag)
    except ValueError as exc:
        return riot_id_error(exc, name=name, tag=tag)
    data = await client.get(
        f"/valorant/v3/mmr/{region}/{platform}/{safe_name}/{safe_tag}"
    )
    return data.get("data", data)


async def get_mmr_by_puuid(
    region: Region,
    puuid: str,
    platform: Platform = "pc",
) -> dict[str, Any]:
    """Retrieve current MMR/rank details for a player by PUUID.

    Args:
        region: Server region. One of: eu, na, latam, br, ap, kr.
        puuid: The player's unique PUUID identifier.
        platform: Platform to query. Either 'pc' (default) or 'console'.

    Returns:
        Same structure as get_mmr.
    """
    data = await client.get(f"/valorant/v3/by-puuid/mmr/{region}/{platform}/{puuid}")
    return data.get("data", data)


async def get_mmr_history(
    region: Region,
    puuid: str,
    platform: Platform = "pc",
) -> dict[str, Any]:
    """Retrieve ranked rating (RR) change history for a player by PUUID.

    Args:
        region: Server region. One of: eu, na, latam, br, ap, kr.
        puuid: The player's unique PUUID identifier.
        platform: Platform to query. Either 'pc' (default) or 'console'.

    Returns:
        A list of MMR history entries. Each entry includes match_id,
        tier, ranking_in_tier (RR), rr_change_to_last_game, date, and map.
    """
    data = await client.get(
        f"/valorant/v2/by-puuid/mmr-history/{region}/{platform}/{puuid}"
    )
    return data.get("data", data)
