"""
MCP tools for Valorant ranked leaderboard data via the Henrik Dev API.

Endpoint used:
  GET /valorant/v3/leaderboard/{region}/{platform}

The v3 endpoint paginates with `size` and `start_index` query params.
"""

from typing import Any

from valorant_mcp_server import client
from valorant_mcp_server.literals import Platform, Region, is_valid_season_short
from valorant_mcp_server.riot_id import parse_riot_id, riot_id_error

DEFAULT_LEADERBOARD_SIZE = 100
MAX_LEADERBOARD_SIZE = 1000


def _clamped_size(size: int | None) -> int:
    try:
        requested = int(size) if size is not None else DEFAULT_LEADERBOARD_SIZE
    except Exception:
        requested = DEFAULT_LEADERBOARD_SIZE
    return max(1, min(requested, MAX_LEADERBOARD_SIZE))


async def get_leaderboard(
    region: Region,
    platform: Platform = "pc",
    name: str | None = None,
    tag: str | None = None,
    puuid: str | None = None,
    season_short: str | None = None,
    size: int | None = None,
    start: int | None = None,
) -> dict[str, Any]:
    """Retrieve the competitive leaderboard for a given region and platform.

    You can filter by player (name+tag OR puuid — not both) and optionally
    restrict to a specific season.

    Args:
        region: Server region. One of: eu, na, latam, br, ap, kr.
        platform: Platform to query — 'pc' (default) or 'console'.
        name: Filter leaderboard to show a specific player name (requires tag).
        tag: Filter leaderboard to show a specific player tag (requires name).
        puuid: Filter by PUUID instead of name/tag (mutually exclusive with name/tag).
        season_short: Season code such as 'e9a3' or 'v25a1' for historical data.
        size: Number of leaderboard entries to return (default 100, max 1000).
        start: 0-indexed entry offset, sent as the v3 `start_index` param.

    Returns:
        A dictionary containing the leaderboard entries. Each entry includes
        puuid, name, tag, leaderboard_rank, rr, wins, and tier. On failure a
        structured error dict ({'error': True, ...}) is returned instead.
    """
    params: dict[str, Any] = {}
    if name or tag:
        if not (name and tag):
            return {
                "error": True,
                "message": "Provide name and tag together to filter by Riot ID.",
                "received": {"name": name, "tag": tag},
            }
        try:
            clean_name, clean_tag = parse_riot_id(name, tag)
        except ValueError as exc:
            return riot_id_error(exc, name=name, tag=tag)
        params["name"] = clean_name
        params["tag"] = clean_tag
    if puuid:
        params["puuid"] = puuid
    if season_short:
        normalized_season = str(season_short).strip().lower()
        if not is_valid_season_short(normalized_season):
            return {
                "error": True,
                "message": (
                    "Invalid season_short format. Expected 'e{episode}a{act}' "
                    "(e.g. 'e9a3') or 'v{year}a{act}' (e.g. 'v25a1')."
                ),
                "received": {"season_short": season_short},
            }
        params["season_short"] = normalized_season
    params["size"] = _clamped_size(size)
    if start is not None:
        params["start_index"] = max(0, int(start))

    data = await client.get(
        f"/valorant/v3/leaderboard/{region}/{platform}", params=params
    )
    return data.get("data", data)
