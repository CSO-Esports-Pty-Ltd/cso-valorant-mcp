"""
MCP tools for Valorant Esports Games via Henrik Dev API.

Endpoints used:
    GET /valorant/v1/esports/schedule
"""

from typing import Any

from valorant_mcp_server import client
from valorant_mcp_server.literals import EsportsRegion, League


async def get_esports_schedule(
    region: EsportsRegion | None = None,
    league: list[League] | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Retrieve the current and upcoming schedule for Valorant esports matches.

    Can be filtered by a specific broader region or by an explicit list
    of leagues/tournaments.

    Args:
        region: Optional region to filter by (e.g., 'international', 'north america', 'emea').
        league: Optional list of specific leagues to filter by (e.g., ['vct_americas', 'vct_emea']).

    Returns:
        List of esports scheduled matches, scores, and details. On failure a
        structured error dict ({'error': True, ...}) is returned instead.
    """
    params: dict[str, Any] = {}
    if region:
        params["region"] = region
    if league:
        params["league"] = ",".join(league)

    data = await client.get("/valorant/v1/esports/schedule", params=params)
    return data.get("data", data)
