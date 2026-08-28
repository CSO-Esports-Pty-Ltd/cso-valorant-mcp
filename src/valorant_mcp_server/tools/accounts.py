"""
MCP tools for Valorant account lookups via the Henrik Dev API.

Endpoints used:
  GET /valorant/v2/account/{name}/{tag}
  GET /valorant/v2/by-puuid/account/{puuid}
"""

from typing import Any

from valorant_mcp_server import client
from valorant_mcp_server.riot_id import riot_id_error, riot_id_path


async def get_account(
    name: str, tag: str, force_update: bool = False
) -> dict[str, Any]:
    """Retrieve Valorant account details by Riot ID (name + tag).

    Args:
        name: The player's in-game name (e.g. 'TenZ').
        tag: The player's tag line without '#' (e.g. 'SEN').
        force_update: If True, forces the API to refresh cached data from Riot.

    Returns:
        A dictionary containing:
          - puuid (str)
          - region (str)
          - account_level (int)
          - name (str)
          - tag (str)
          - card (uuid)
          - title (uuid)
          - platforms (list[str]): platforms the account is linked to
          - updated_at (str): human-readable update timestamp
    """
    try:
        safe_name, safe_tag = riot_id_path(name, tag)
    except ValueError as exc:
        return riot_id_error(exc, name=name, tag=tag)
    params: dict[str, Any] = {}
    if force_update:
        params["force"] = "true"
    data = await client.get(
        f"/valorant/v2/account/{safe_name}/{safe_tag}", params=params
    )
    return data.get("data", data)


async def get_account_by_puuid(
    puuid: str, force_update: bool = False
) -> dict[str, Any]:
    """Retrieve Valorant account details by PUUID.

    Args:
        puuid: The player's unique PUUID identifier.
        force_update: If True, forces the API to refresh cached data from Riot.

    Returns:
        Same structure as get_account.
    """
    params: dict[str, Any] = {}
    if force_update:
        params["force"] = "true"
    data = await client.get(f"/valorant/v2/by-puuid/account/{puuid}", params=params)
    return data.get("data", data)
