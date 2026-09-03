"""
Valorant MCP Server — entry point.

Wraps the Henrik Dev Valorant API (https://docs.henrikdev.xyz/valorant/general)
as a set of MCP tools callable by any MCP-compatible AI client.

Environment variables:
  HENRIK_API_KEY  (required) Your Henrik Dev API key.

Usage:
  valorant-mcp-server              # run via installed script
  uv run valorant-mcp-server       # run via uv
  uv run mcp dev src/valorant_mcp_server/server.py  # MCP Inspector
"""

import asyncio
import hmac
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from mcp.types import ToolAnnotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from valorant_mcp_server.literals import (
    GameMode,
    Platform,
    Region,
    EsportsRegion,
    League,
)
from valorant_mcp_server.riot_id import (
    riot_id_error as _riot_id_error,
    riot_id_path as _riot_id_path,
)
from valorant_mcp_server.cso_utils import (
    cso_agent_counts_from_report as _cso_agent_counts_from_report,
    cso_role_from_agents as _cso_role_from_agents,
    extract_match_id as _extract_match_id,
    extract_match_length_seconds as _extract_match_length_seconds,
    extract_match_started_at as _extract_match_started_at,
    extract_queue_name as _extract_queue_name,
    format_hhmmss as _format_hhmmss,
    parse_iso_datetime as _parse_iso_datetime,
    playtime_window as _playtime_window,
)
from valorant_mcp_server.henrik import (
    content_slice as _content_slice,
    henrik_get as _henrik_get,
)
from valorant_mcp_server.match_utils import (
    agent_name as _agent_name,
    find_player_in_match as _find_player_in_match,
    map_name_from_match as _map_name_from_match,
    match_meta as _match_meta,
    player_identity as _player_identity,
    player_rows_from_match as _player_rows_from_match,
    player_stats as _player_stats,
    safe_get as _safe_get,
    team_won as _team_won,
)
from valorant_mcp_server.round_tools import (
    compact_events as _compact_events,
    one_round as _one_round,
    opening_duels as _opening_duels,
    player_impact_summary as _player_impact_summary,
    rollup_history as _rollup_history,
    rounds_summary as _rounds_summary,
    team_economy_summary as _team_economy_summary,
)
from valorant_mcp_server.tools import accounts, analytics, leaderboard, matches, mmr, esports

def _csv_env(name: str, defaults: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return defaults

    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or defaults


def _clamped_matchlist_size(size: int | None, *, default: int = 3, max_size: int = 5) -> int:
    try:
        requested = int(size) if size is not None else default
    except Exception:
        requested = default
    return max(1, min(requested, max_size))


def _player_identity_error(
    name: str | None, tag: str | None, puuid: str | None
) -> dict[str, Any] | None:
    """Validate that exactly one player identification form was supplied.

    Valid forms are name+tag together (without puuid) or puuid alone.
    Returns None when the combination is valid, otherwise a structured
    error dict suitable for returning directly as a tool response.
    """
    if puuid and not name and not tag:
        return None
    if name and tag and not puuid:
        return None
    return {
        "error": True,
        "message": (
            "Provide exactly one identification form: both name and tag "
            "together (without puuid), or puuid alone."
        ),
        "received": {"name": name, "tag": tag, "puuid": puuid},
    }


def _data_list(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get("matches") or data.get("history") or data.get("data")
    return [item for item in data or [] if isinstance(item, dict)] if isinstance(data, list) else []


def _display_name(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("name") or value.get("displayName") or value.get("id")
    return value


def _team_score_summary(item: dict[str, Any]) -> dict[str, Any] | None:
    teams = (
        _safe_get(item, "data", "teams", default=None)
        or item.get("teams")
        or _safe_get(item, "metadata", "teams", default=None)
    )
    if not teams:
        return None

    if isinstance(teams, dict):
        summary: dict[str, Any] = {}
        for key, value in teams.items():
            if not isinstance(value, dict):
                continue
            rounds = value.get("rounds") or {}
            summary[str(key)] = {
                "rounds_won": rounds.get("won") if isinstance(rounds, dict) else value.get("rounds_won"),
                "has_won": value.get("has_won"),
            }
        return summary or None

    if isinstance(teams, list):
        summary = {}
        for value in teams:
            if not isinstance(value, dict):
                continue
            team_id = value.get("team_id") or value.get("teamId") or value.get("team")
            if not team_id:
                continue
            rounds = value.get("rounds") or {}
            summary[str(team_id)] = {
                "rounds_won": rounds.get("won") if isinstance(rounds, dict) else value.get("rounds_won"),
                "has_won": value.get("has_won"),
            }
        return summary or None

    return None


def _compact_match_history_item(
    item: dict[str, Any],
    *,
    region: Region,
    platform: Platform | None = None,
    target_puuid: str | None = None,
) -> dict[str, Any]:
    meta = item.get("metadata") or item.get("meta") or _safe_get(item, "data", "metadata", default={}) or {}
    started_at = _extract_match_started_at(item)
    target_row = _find_player_in_match(item, puuid=target_puuid) if target_puuid else None

    compact: dict[str, Any] = {
        "match_id": _extract_match_id(item),
        "region": region,
        "platform": platform or meta.get("platform"),
        "map": _display_name(meta.get("map")) or meta.get("map_name") or meta.get("mapName"),
        "mode": _extract_queue_name(item),
        "started_at": started_at.isoformat() if started_at else meta.get("started_at") or meta.get("game_start_patched"),
        "game_length_seconds": _extract_match_length_seconds(item),
        "team_score": _team_score_summary(item),
    }

    if target_row:
        compact["player"] = {
            "puuid": target_row.get("puuid"),
            "name": _player_identity(target_row),
            "agent": _agent_name(target_row),
            "team": target_row.get("team") or target_row.get("team_id") or target_row.get("teamId"),
            "won": _team_won(target_row, item),
            **_player_stats(target_row),
        }

    return {key: value for key, value in compact.items() if value is not None}


def _compact_match_history_response(
    payload: dict[str, Any],
    *,
    region: Region,
    platform: Platform | None,
    requested_size: int,
    target_puuid: str | None = None,
    source_tool: str,
) -> dict[str, Any]:
    if payload.get("error"):
        return {
            "error": True,
            "source_tool": source_tool,
            "requested_size": requested_size,
            "message": payload.get("message"),
            "path": payload.get("path"),
            "status_code": payload.get("status_code"),
        }

    rows = _data_list(payload)
    trimmed = [
        _compact_match_history_item(
            item,
            region=region,
            platform=platform,
            target_puuid=target_puuid,
        )
        for item in rows[:requested_size]
    ]

    return {
        "source_tool": source_tool,
        "region": region,
        "platform": platform,
        "requested_size": requested_size,
        "matches_returned": len(trimmed),
        "matches": trimmed,
        "notes": [
            "Trimmed match-history response: small payload, at most 5 matches.",
            "Use get_match or the round-summary tools only for a selected match_id.",
        ],
    }


def _safe_int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _mmr_history_started_at(item: dict[str, Any]) -> datetime | None:
    raw = item.get("date_raw") or item.get("dateRaw") or item.get("timestamp")
    if raw is not None:
        try:
            timestamp = float(raw)
            if timestamp > 1_000_000_000_000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (TypeError, ValueError, OSError):
            pass

    parsed = _parse_iso_datetime(item.get("date"))
    if parsed:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    return None


def _current_rank_name(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None

    current = payload.get("current")
    if isinstance(current, dict):
        tier = current.get("tier")
        if isinstance(tier, dict):
            name = tier.get("name")
            if name:
                return str(name)

    peak = payload.get("peak")
    if isinstance(peak, dict):
        tier = peak.get("tier")
        if isinstance(tier, dict) and tier.get("name"):
            return str(tier.get("name"))

    return None


def _rank_tier_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None

    tier = value.get("tier")
    if isinstance(tier, dict):
        for key in ("name", "displayName", "display_name"):
            name = tier.get(key)
            if name:
                return str(name)

    for key in ("tier_name", "tierName", "rank", "name"):
        name = value.get(key)
        if name:
            return str(name)

    return None


def _peak_rank_name(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None

    peak = payload.get("peak")
    peak_name = _rank_tier_name(peak)
    if peak_name:
        return peak_name

    for key in ("peak_rank", "peakRank", "highest_rank", "highestRank"):
        value = payload.get(key)
        if isinstance(value, dict):
            name = _rank_tier_name(value)
            if name:
                return name
        elif value:
            return str(value)

    return None


async def _collect_player_rr_summary(
    *,
    region: Region,
    platform: Platform,
    name: str,
    tag: str,
    days: int = 7,
) -> dict[str, Any]:
    now, window_start = _playtime_window(max(1, int(days or 1)))
    del now

    output: dict[str, Any] = {
        "rr": None,
        "rr_delta": None,
        "rr_weekly_delta": None,
        "rr_weekly_matches": 0,
        "current_rank": None,
        "peak_rank": None,
        "peak_rank_source": None,
        "last_ranked_at": None,
        "rank_confidence": "low",
        "rank_errors": [],
    }
    errors: list[dict[str, Any]] = []

    try:
        current_payload = await mmr.get_mmr(region, name, tag, platform)
        current = current_payload.get("current") if isinstance(current_payload, dict) else None
        if isinstance(current, dict):
            output["rr"] = current.get("rr")
            output["rr_delta"] = current.get("last_change")
            output["elo"] = current.get("elo")
        output["current_rank"] = _current_rank_name(current_payload)
        peak_rank = _peak_rank_name(current_payload)
        if peak_rank:
            output["peak_rank"] = peak_rank
            output["peak_rank_source"] = "henrik_mmr"
    except Exception as exc:
        errors.append({"reason": "current_mmr_error", "error": str(exc)})

    try:
        history_payload = await get_mmr_history_v1(region, name, tag)
        history = _data_list(history_payload)
        weekly_delta = 0
        weekly_matches = 0
        latest_ranked_at: datetime | None = None

        for item in history:
            started_at = _mmr_history_started_at(item)
            if latest_ranked_at is None and started_at is not None:
                latest_ranked_at = started_at
            if started_at is None or started_at < window_start:
                continue

            weekly_delta += _safe_int_value(
                item.get("mmr_change_to_last_game")
                if item.get("mmr_change_to_last_game") is not None
                else item.get("rr_change_to_last_game")
            )
            weekly_matches += 1

        output["rr_weekly_delta"] = weekly_delta if weekly_matches else None
        output["rr_weekly_matches"] = weekly_matches
        output["last_ranked_at"] = latest_ranked_at.isoformat() if latest_ranked_at else None
    except Exception as exc:
        errors.append({"reason": "mmr_history_error", "error": str(exc)})

    output["rank_errors"] = errors
    output["rank_confidence"] = "high" if not errors and output["rr"] is not None else "medium" if output["rr"] is not None else "low"
    return output


async def _collect_player_weekly_playtime_summary(
    *,
    region: Region,
    platform: Platform,
    name: str,
    tag: str,
    mode: str | None,
    page_size: int = 10,
    max_pages: int = 5,
) -> dict[str, Any]:
    now, window_start = _playtime_window(7)
    today_key = now.date().isoformat()
    total_seconds = 0
    matches_counted = 0
    games_today = 0
    skipped = 0
    daily: dict[str, dict[str, Any]] = {}
    seen_match_ids: set[str] = set()
    last_played: datetime | None = None
    stopped_due_to_old_match = False
    page_size = max(1, min(int(page_size or 5), 5))
    max_pages = max(1, min(int(max_pages or 5), 10))

    for page in range(max_pages):
        start = page * page_size
        history = await get_match_history_v4_trimmed(
            region=region,
            name=name,
            tag=tag,
            platform=platform,
            mode=mode,
            size=page_size,
            start=start,
        )
        if history.get("error"):
            skipped += 1
            break

        history_rows = history.get("matches") or []
        if not isinstance(history_rows, list) or not history_rows:
            break

        for item in history_rows:
            if not isinstance(item, dict):
                continue
            match_id = item.get("match_id")
            if match_id and match_id in seen_match_ids:
                continue
            if match_id:
                seen_match_ids.add(match_id)

            started_at = _parse_iso_datetime(item.get("started_at"))
            if started_at and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if started_at:
                started_at = started_at.astimezone(timezone.utc)
            if started_at and started_at < window_start:
                stopped_due_to_old_match = True
                continue

            seconds = item.get("game_length_seconds")
            if seconds is None:
                skipped += 1
                continue

            seconds_int = _safe_int_value(seconds)
            if seconds_int <= 0:
                skipped += 1
                continue

            matches_counted += 1
            total_seconds += seconds_int
            date_key = started_at.date().isoformat() if started_at else "unknown"
            if started_at and (last_played is None or started_at > last_played):
                last_played = started_at
            if date_key == today_key:
                games_today += 1
            day = daily.setdefault(date_key, {"matches": 0, "seconds": 0, "hhmmss": "00:00:00"})
            day["matches"] += 1
            day["seconds"] += seconds_int
            day["hhmmss"] = _format_hhmmss(day["seconds"])

        if stopped_due_to_old_match:
            break

    confidence = "high"
    notes: list[str] = []
    if skipped:
        confidence = "medium"
        notes.append("Some competitive matches were skipped because duration metadata was missing or a page failed.")
    if not stopped_due_to_old_match and matches_counted >= page_size * max_pages:
        confidence = "medium"
        notes.append("Weekly playtime scan reached max_pages before confirming the full 7-day window.")
    if not matches_counted:
        confidence = "low"
        notes.append("No competitive matches with duration metadata were found in the 7-day window.")

    return {
        "weekly_playtime_seconds": total_seconds,
        "weekly_playtime_hours": round(total_seconds / 3600, 2),
        "weekly_playtime_hhmmss": _format_hhmmss(total_seconds),
        "weekly_playtime_matches": matches_counted,
        "weekly_active_days": len([key for key in daily if key != "unknown"]),
        "weekly_playtime_daily": daily,
        "weekly_games_today": games_today,
        "weekly_last_played_at": last_played.isoformat() if last_played else None,
        "weekly_playtime_confidence": confidence,
        "weekly_playtime_notes": notes,
    }


def _first_stat_value(row: dict[str, Any], *keys: str) -> Any:
    stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
    damage = stats.get("damage") if isinstance(stats.get("damage"), dict) else {}
    shots = stats.get("shots") if isinstance(stats.get("shots"), dict) else {}
    sources = (row, stats, damage, shots)
    for key in keys:
        for source in sources:
            if key in source and source[key] is not None:
                return source[key]
    return None


def _shot_counts_from_row(row: dict[str, Any]) -> dict[str, int | None]:
    headshots = _first_stat_value(row, "headshots", "head_shots", "headshot_hits", "head")
    bodyshots = _first_stat_value(row, "bodyshots", "body_shots", "bodyshot_hits", "body")
    legshots = _first_stat_value(row, "legshots", "leg_shots", "legshot_hits", "leg")

    if headshots is None and bodyshots is None and legshots is None:
        return {"headshots": None, "bodyshots": None, "legshots": None}

    return {
        "headshots": _safe_int_value(headshots),
        "bodyshots": _safe_int_value(bodyshots),
        "legshots": _safe_int_value(legshots),
    }


def _iter_round_player_stats(match: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rounds = _safe_get(match, "data", "rounds", default=None) or match.get("rounds") or []
    for round_row in rounds if isinstance(rounds, list) else []:
        if not isinstance(round_row, dict):
            continue
        for stat in round_row.get("stats") or round_row.get("player_stats") or []:
            if isinstance(stat, dict):
                rows.append(stat)
    return rows


def _shot_counts_from_round_stats(match: dict[str, Any], target_puuid: str | None) -> dict[str, int | None]:
    if not target_puuid:
        return {"headshots": None, "bodyshots": None, "legshots": None}

    totals = {"headshots": 0, "bodyshots": 0, "legshots": 0}
    found = False
    for stat in _iter_round_player_stats(match):
        player = stat.get("player") if isinstance(stat.get("player"), dict) else stat
        if player.get("puuid") != target_puuid and stat.get("puuid") != target_puuid:
            continue

        counts = _shot_counts_from_row(stat)
        if all(value is None for value in counts.values()):
            for event in stat.get("damage_events") or []:
                if not isinstance(event, dict):
                    continue
                totals["headshots"] += _safe_int_value(event.get("headshots") or event.get("head_shots"))
                totals["bodyshots"] += _safe_int_value(event.get("bodyshots") or event.get("body_shots"))
                totals["legshots"] += _safe_int_value(event.get("legshots") or event.get("leg_shots"))
            found = found or any(totals.values())
        else:
            found = True
            for key, value in counts.items():
                totals[key] += _safe_int_value(value)

    return totals if found else {"headshots": None, "bodyshots": None, "legshots": None}


def _merge_shot_counts(primary: dict[str, int | None], fallback: dict[str, int | None]) -> dict[str, int | None]:
    if any(value is not None for value in primary.values()):
        return primary
    return fallback


def _headshot_rate(shots: dict[str, int | None]) -> float | None:
    if any(value is None for value in shots.values()):
        return None
    total = sum(_safe_int_value(value) for value in shots.values())
    if not total:
        return None
    return round(_safe_int_value(shots["headshots"]) / total, 3)


def _round_count(match: dict[str, Any]) -> int:
    rounds = _safe_get(match, "data", "rounds", default=None) or match.get("rounds") or []
    if isinstance(rounds, list) and rounds:
        return len(rounds)
    score = _team_score_summary(match) or {}
    total = 0
    for team in score.values():
        if isinstance(team, dict):
            total += _safe_int_value(team.get("rounds_won"))
    return max(total, 1)


def _team_won_any(row: dict[str, Any], match: dict[str, Any]) -> bool | None:
    won = _team_won(row, match)
    if won is not None:
        return won

    team_id = row.get("team") or row.get("team_id") or row.get("teamId")
    if not team_id:
        return None

    teams = _safe_get(match, "data", "teams", default=None) or match.get("teams") or []
    if isinstance(teams, list):
        for team in teams:
            if not isinstance(team, dict):
                continue
            current = team.get("team_id") or team.get("teamId") or team.get("team")
            if str(current).lower() == str(team_id).lower() and team.get("has_won") is not None:
                return bool(team.get("has_won"))
    return None


def _compact_player_match_stats(
    match: dict[str, Any],
    *,
    region: Region,
    puuid: str | None = None,
    name: str | None = None,
    tag: str | None = None,
) -> dict[str, Any] | None:
    row = _find_player_in_match(match, puuid=puuid, name=name, tag=tag)
    if not row:
        return None

    stats = _player_stats(row)
    rounds = _round_count(match)
    score = _safe_int_value(stats.get("score"))
    damage_dealt = _safe_int_value(_first_stat_value(row, "dealt", "damage_dealt", "damage"))
    if damage_dealt == 0:
        damage = row.get("damage") or {}
        if isinstance(damage, dict):
            damage_dealt = _safe_int_value(damage.get("dealt"))

    target_puuid = row.get("puuid") or puuid
    shots = _merge_shot_counts(
        _shot_counts_from_row(row),
        _shot_counts_from_round_stats(match, target_puuid),
    )
    won = _team_won_any(row, match)
    impact = _player_impact_summary(match, region, puuid=target_puuid, name=name, tag=tag)
    impact_player = impact.get("player") if isinstance(impact, dict) else None
    if not isinstance(impact_player, dict):
        impact_player = {}
    impact_kast = impact_player.get("kast")
    impact_rounds = rounds or 0

    return {
        **_compact_match_history_item(match, region=region, target_puuid=target_puuid),
        "player": _player_identity(row),
        "puuid": target_puuid,
        "agent": _agent_name(row),
        "team": row.get("team") or row.get("team_id") or row.get("teamId"),
        "won": won,
        "rounds_count": rounds,
        "kills": stats["kills"],
        "deaths": stats["deaths"],
        "assists": stats["assists"],
        "score": score,
        "acs": round(score / rounds) if rounds else None,
        "damage_dealt": damage_dealt if damage_dealt else None,
        "adr": round(damage_dealt / rounds, 1) if damage_dealt and rounds else None,
        "kast_rounds": round(float(impact_kast) * impact_rounds)
        if isinstance(impact_kast, (int, float)) and impact_rounds
        else None,
        "first_kills": _safe_int_value(impact_player.get("first_kills")),
        "first_deaths": _safe_int_value(impact_player.get("first_deaths")),
        **shots,
        "hs_pct": _headshot_rate(shots),
    }


def _aggregate_compact_player_matches(
    matches_rows: list[dict[str, Any]],
    *,
    player: str,
    region: Region,
    platform: Platform,
    days: int,
    mode: str | None,
    errors: list[dict[str, Any]],
    include_matches: bool,
) -> dict[str, Any]:
    counted = [row for row in matches_rows if isinstance(row, dict)]
    matches_count = len(counted)
    wins = sum(1 for row in counted if row.get("won") is True)
    losses = sum(1 for row in counted if row.get("won") is False)
    kills = sum(_safe_int_value(row.get("kills")) for row in counted)
    deaths = sum(_safe_int_value(row.get("deaths")) for row in counted)
    assists = sum(_safe_int_value(row.get("assists")) for row in counted)
    rounds = sum(_safe_int_value(row.get("rounds_count")) for row in counted)
    score = sum(_safe_int_value(row.get("score")) for row in counted)
    damage = sum(_safe_int_value(row.get("damage_dealt")) for row in counted)
    kast_rounds = sum(_safe_int_value(row.get("kast_rounds")) for row in counted if row.get("kast_rounds") is not None)
    first_kills = sum(_safe_int_value(row.get("first_kills")) for row in counted)
    first_deaths = sum(_safe_int_value(row.get("first_deaths")) for row in counted)
    headshots = sum(_safe_int_value(row.get("headshots")) for row in counted if row.get("headshots") is not None)
    bodyshots = sum(_safe_int_value(row.get("bodyshots")) for row in counted if row.get("bodyshots") is not None)
    legshots = sum(_safe_int_value(row.get("legshots")) for row in counted if row.get("legshots") is not None)
    shot_total = headshots + bodyshots + legshots
    agent_counts: dict[str, int] = {}
    map_counts: dict[str, int] = {}
    daily_matches: dict[str, int] = {}
    today_key = datetime.now(timezone.utc).date().isoformat()
    games_today = 0
    last_played: datetime | None = None

    for row in counted:
        agent = str(row.get("agent") or "").strip()
        if agent and agent.lower() != "unknown":
            agent_counts[agent] = agent_counts.get(agent, 0) + 1

        map_name = str(row.get("map") or "").strip()
        if map_name:
            map_counts[map_name] = map_counts.get(map_name, 0) + 1

        started_at = _parse_iso_datetime(row.get("started_at"))
        if not started_at:
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        started_at = started_at.astimezone(timezone.utc)
        date_key = started_at.date().isoformat()
        daily_matches[date_key] = daily_matches.get(date_key, 0) + 1
        if date_key == today_key:
            games_today += 1
        if last_played is None or started_at > last_played:
            last_played = started_at

    output: dict[str, Any] = {
        "player": player,
        "region": region,
        "platform": platform,
        "window": {"days": days},
        "mode_filter": mode,
        "weekly_matches": matches_count,
        "matches_counted": matches_count,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / matches_count, 3) if matches_count else None,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kd": round(kills / max(deaths, 1), 2) if matches_count else None,
        "acs": round(score / rounds) if rounds else None,
        "adr": round(damage / rounds, 1) if damage and rounds else None,
        "kast_pct": round(kast_rounds / rounds, 3) if kast_rounds and rounds else None,
        "first_kills": first_kills,
        "first_deaths": first_deaths,
        "headshots": headshots if shot_total else None,
        "bodyshots": bodyshots if shot_total else None,
        "legshots": legshots if shot_total else None,
        "hs_pct": round(headshots / shot_total, 3) if shot_total else None,
        "agent_counts": agent_counts,
        "map_counts": map_counts,
        "daily_matches": daily_matches,
        "games_today": games_today,
        "last_played_at": last_played.isoformat() if last_played else None,
        "rr_delta": None,
        "errors": errors,
        "confidence": "high" if matches_count and not errors else "medium" if matches_count else "low",
    }
    if include_matches:
        output["matches"] = counted
    return output


async def _collect_player_window_stats(
    *,
    region: Region,
    platform: Platform,
    days: int,
    mode: str | None,
    page_size: int,
    max_pages: int,
    max_details: int,
    name: str | None = None,
    tag: str | None = None,
    puuid: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now, window_start = _playtime_window(max(1, int(days or 1)))
    del now

    page_size = max(1, min(int(page_size or 5), 10))
    max_pages = max(1, min(int(max_pages or 4), 10))
    max_details = max(1, min(int(max_details or 20), 50))

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_match_ids: set[str] = set()

    for page in range(max_pages):
        start = page * page_size
        if puuid:
            history = await get_match_history_by_puuid_trimmed(
                region=region,
                puuid=puuid,
                platform=platform,
                mode=mode,
                size=page_size,
                start=start,
            )
        elif name and tag:
            history = await get_match_history_v4_trimmed(
                region=region,
                name=name,
                tag=tag,
                platform=platform,
                mode=mode,
                size=page_size,
                start=start,
            )
        else:
            return rows, [{"reason": "missing_identifier", "message": "Provide puuid or name+tag."}]

        if history.get("error"):
            errors.append({"reason": "history_error", "page": page, "payload": history})
            break

        history_rows = history.get("matches") or []
        if not isinstance(history_rows, list) or not history_rows:
            break

        reached_old_match = False
        reached_rate_limit = False
        for item in history_rows:
            if not isinstance(item, dict):
                continue
            started_at = _parse_iso_datetime(item.get("started_at"))
            if started_at and started_at < window_start:
                reached_old_match = True
                continue

            match_id = item.get("match_id")
            if not match_id or match_id in seen_match_ids:
                continue
            seen_match_ids.add(match_id)

            try:
                details = await _get_match_details_v4_cached(region, match_id)
                if isinstance(details, dict) and details.get("error"):
                    errors.append({"reason": "detail_error", "match_id": match_id, "payload": details})
                    if _payload_status_code(details) == 429:
                        reached_rate_limit = True
                        break
                    continue

                compact = _compact_player_match_stats(
                    details,
                    region=region,
                    puuid=puuid,
                    name=name,
                    tag=tag,
                )
                if compact:
                    rows.append(compact)
                else:
                    errors.append({"reason": "player_not_found", "match_id": match_id})
            except Exception as exc:
                errors.append({"reason": "detail_error", "match_id": match_id, "error": str(exc)})
                if "429" in str(exc) or "too many requests" in str(exc).lower():
                    reached_rate_limit = True
                    break

            if len(rows) >= max_details:
                break

        if reached_old_match or reached_rate_limit or len(rows) >= max_details:
            break

    return rows, errors


DEFAULT_ALLOWED_HOSTS = [
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
    "valorant.csoesports.com",
    "valorant.csoesports.com:*",
]

DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:*",
    "http://127.0.0.1",
    "http://127.0.0.1:*",
    "https://valorant.csoesports.com",
    "https://valorant.csoesports.com:*",
]


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Valorant MCP Server",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_csv_env("MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS),
        allowed_origins=_csv_env("MCP_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS),
    ),
)
analytics.register_analytics_tools(mcp)

DEFAULT_DASHBOARD_ROSTER: list[dict[str, Any]] = [
    {
        "id": "bianca-cronje",
        "rosterName": "Bianca Cronje",
        "riotId": "CSO BumbleB#BUZZ",
        "name": "CSO BumbleB",
        "tag": "BUZZ",
        "team": "CSO AllSorts",
        "country": "South Africa",
        "peakRank": "Gold 2",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20BumbleB%23BUZZ/overview?platform=pc&playlist=competitive",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "jackie-koegelenberg",
        "rosterName": "Jackie Koegelenberg",
        "riotId": "CSO BloodRayne#CSO",
        "name": "CSO BloodRayne",
        "tag": "CSO",
        "team": "CSO AllSorts",
        "country": "South Africa",
        "peakRank": "Silver 2",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20BloodRayne%23CSO/overview?platform=pc&playlist=competitive&season=3ea2b318-423b-cf86-25da-7cbb0eefbe2d",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "jordan-torran",
        "rosterName": "Jordan Torran",
        "riotId": "CSO Caelus#donut",
        "name": "CSO Caelus",
        "tag": "donut",
        "team": "CSO AllSorts",
        "country": "South Africa",
        "peakRank": "Gold 2",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20Caelus%23donut/overview?platform=pc&playlist=competitive&season=4c4b8cff-43eb-13d3-8f14-96b783c90cd2",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "matthew-langton",
        "rosterName": "Matthew Langton",
        "riotId": "CSO Krytos#CSO",
        "name": "CSO Krytos",
        "tag": "CSO",
        "team": "CSO AllSorts",
        "country": "South Africa",
        "peakRank": "Bronze 1",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20Krytos%23CSO/overview?platform=pc&playlist=competitive&season=3ea2b318-423b-cf86-25da-7cbb0eefbe2d",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "ryan-botha",
        "rosterName": "Ryan Botha",
        "riotId": "CSO GH0ST3x#404",
        "name": "CSO GH0ST3x",
        "tag": "404",
        "team": "CSO AllSorts",
        "country": "South Africa",
        "peakRank": "Silver 1",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20GH0ST3x%23404/overview?platform=pc&playlist=competitive",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "tony-mpofu",
        "rosterName": "Tony Mpofu",
        "riotId": "CSO Notox#2002",
        "name": "CSO Notox",
        "tag": "2002",
        "team": "CSO AllSorts",
        "country": "South Africa",
        "peakRank": "Gold 3",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20Notox%232002/overview?platform=pc&playlist=competitive&season=4c4b8cff-43eb-13d3-8f14-96b783c90cd2",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "william-mampuru",
        "rosterName": "William Mampuru",
        "riotId": "CSO BrimReaper#MOLLY",
        "name": "CSO BrimReaper",
        "tag": "MOLLY",
        "team": "CSO AllSorts",
        "country": "South Africa",
        "peakRank": "Platinum 2",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20BrimReaper%23MOLLY/overview?platform=pc&playlist=competitive",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "andrew-browski",
        "rosterName": "Andrew Browski",
        "riotId": "CSO Geto#CULT",
        "name": "CSO Geto",
        "tag": "CULT",
        "team": "CSO Pathward",
        "country": "South Africa",
        "peakRank": "Gold 3",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20Geto%23CULT/overview?platform=pc&playlist=competitive",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "asher-james-anderson",
        "rosterName": "Asher James Anderson",
        "riotId": "CSO Arcatron#123",
        "name": "CSO Arcatron",
        "tag": "123",
        "team": "CSO Pathward",
        "country": "South Africa",
        "peakRank": "Plat 1",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20Arcatron%23123/overview?platform=pc&playlist=competitive",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "duncan-whitehorn",
        "rosterName": "Duncan Whitehorn",
        "riotId": "CSO Freaker#999",
        "name": "CSO Freaker",
        "tag": "999",
        "team": "CSO Pathward",
        "country": "South Africa",
        "peakRank": "Gold 2",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20Freaker%23999/overview?platform=pc&playlist=competitive",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "corey-bowden",
        "rosterName": "Corey Bowden",
        "riotId": "CSO EGO#ruzie",
        "name": "CSO EGO",
        "tag": "ruzie",
        "team": "CSO Riftguard",
        "country": "South Africa",
        "peakRank": "Diamond 1",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20EGO%23ruzie/overview?platform=pc&playlist=competitive",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "jayden-peta",
        "rosterName": "Jayden Peta",
        "riotId": "CSO Veilsettsu#KII",
        "name": "CSO Veilsettsu",
        "tag": "KII",
        "team": "CSO Riftguard",
        "country": "South Africa",
        "peakRank": "Diamond 1",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20Veilsettsu%23KII/overview?platform=pc&playlist=competitive",
        "region": "eu",
        "platform": "pc",
    },
    {
        "id": "tshepo-mohlomi",
        "rosterName": "Tshepo Mohlomi",
        "riotId": "CSO Arctic#Ice",
        "name": "CSO Arctic",
        "tag": "Ice",
        "team": "CSO Riftguard",
        "country": "South Africa",
        "peakRank": "Ascendant 1",
        "trackerUrl": "https://tracker.gg/valorant/profile/riot/CSO%20Arctic%23Ice/overview?platform=pc&playlist=competitive&season=4c4b8cff-43eb-13d3-8f14-96b783c90cd2",
        "region": "eu",
        "platform": "pc",
    },
]

_DASHBOARD_CACHE: dict[str, Any] | None = None
_DASHBOARD_CACHE_KEY: str | None = None
_DASHBOARD_CACHE_EXPIRES_AT = 0.0
_DASHBOARD_PLAYER_CACHE: dict[str, dict[str, Any]] = {}
_DASHBOARD_PLAYER_CACHE_LOADED = False
_DASHBOARD_MATCH_DETAIL_CACHE: dict[str, dict[str, Any]] = {}
_DASHBOARD_MATCH_DETAIL_CACHE_LOADED = False
_DASHBOARD_ROLLING_CURSOR_BY_KEY: dict[str, int] = {}


def _dashboard_api_token() -> str | None:
    return os.getenv("VALORANT_DASHBOARD_API_TOKEN") or os.getenv("VALORANT_STATS_API_TOKEN")


def _dashboard_auth_response(request: Request) -> JSONResponse | None:
    expected = _dashboard_api_token()
    if not expected:
        return JSONResponse(
            {
                "error": "dashboard_stats_token_not_configured",
                "message": "Set VALORANT_DASHBOARD_API_TOKEN before exposing /stats/dashboard.",
            },
            status_code=503,
        )

    auth_header = request.headers.get("authorization", "")
    scheme, _, supplied = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return JSONResponse({"error": "missing_bearer_token"}, status_code=401)

    if not hmac.compare_digest(supplied.strip(), expected):
        return JSONResponse({"error": "invalid_bearer_token"}, status_code=403)

    return None


def _dashboard_int(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(min_value, min(parsed, max_value))


def _dashboard_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _payload_status_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    raw_status = payload.get("status_code") or payload.get("status")
    try:
        return int(raw_status)
    except Exception:
        return None


def _dashboard_match_detail_cache_file() -> Path | None:
    configured = os.getenv("VALORANT_DASHBOARD_MATCH_DETAIL_CACHE_FILE")
    if configured and configured.strip().lower() in {"0", "false", "off", "none"}:
        return None

    path = configured or os.path.join(
        tempfile.gettempdir(),
        "cso-valorant-dashboard-match-detail-cache.json",
    )
    return Path(path)


def _dashboard_match_detail_cache_ttl_seconds() -> int:
    return _dashboard_int(
        os.getenv("VALORANT_DASHBOARD_MATCH_DETAIL_CACHE_TTL_SECONDS", "604800"),
        604800,
        min_value=0,
        max_value=2592000,
    )


def _dashboard_match_detail_cache_max_entries() -> int:
    return _dashboard_int(
        os.getenv("VALORANT_DASHBOARD_MATCH_DETAIL_CACHE_MAX_ENTRIES", "500"),
        500,
        min_value=25,
        max_value=5000,
    )


def _dashboard_match_details_mirror_limit() -> int:
    return _dashboard_int(
        os.getenv("VALORANT_DASHBOARD_MATCH_DETAILS_MIRROR_LIMIT", "50"),
        50,
        min_value=0,
        max_value=500,
    )


def _dashboard_load_match_detail_cache() -> None:
    global _DASHBOARD_MATCH_DETAIL_CACHE, _DASHBOARD_MATCH_DETAIL_CACHE_LOADED

    if _DASHBOARD_MATCH_DETAIL_CACHE_LOADED:
        return

    _DASHBOARD_MATCH_DETAIL_CACHE_LOADED = True
    cache_file = _dashboard_match_detail_cache_file()
    if cache_file is None or not cache_file.exists():
        return

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return

    matches_payload = payload.get("matches") if isinstance(payload, dict) else None
    if isinstance(matches_payload, dict):
        _DASHBOARD_MATCH_DETAIL_CACHE = {
            str(key): value
            for key, value in matches_payload.items()
            if isinstance(value, dict)
        }


def _dashboard_prune_match_detail_cache(now_ts: float) -> None:
    ttl_seconds = _dashboard_match_detail_cache_ttl_seconds()
    if ttl_seconds > 0:
        expired_keys = [
            key
            for key, entry in _DASHBOARD_MATCH_DETAIL_CACHE.items()
            if now_ts - float(entry.get("updatedTs") or 0) > ttl_seconds
        ]
        for key in expired_keys:
            _DASHBOARD_MATCH_DETAIL_CACHE.pop(key, None)

    max_entries = _dashboard_match_detail_cache_max_entries()
    if len(_DASHBOARD_MATCH_DETAIL_CACHE) <= max_entries:
        return

    ordered = sorted(
        _DASHBOARD_MATCH_DETAIL_CACHE.items(),
        key=lambda item: float(item[1].get("updatedTs") or 0),
    )
    for key, _ in ordered[: len(_DASHBOARD_MATCH_DETAIL_CACHE) - max_entries]:
        _DASHBOARD_MATCH_DETAIL_CACHE.pop(key, None)


def _dashboard_save_match_detail_cache() -> None:
    cache_file = _dashboard_match_detail_cache_file()
    if cache_file is None:
        return

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = cache_file.with_suffix(f"{cache_file.suffix}.tmp")
        tmp_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "savedAt": datetime.now(timezone.utc).isoformat(),
                    "matches": _DASHBOARD_MATCH_DETAIL_CACHE,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        tmp_file.replace(cache_file)
    except Exception:
        return


async def _get_match_details_v4_cached(region: Region, match_id: str) -> dict[str, Any]:
    now_ts = time.time()
    cache_key = f"{str(region).lower()}:{match_id}"
    ttl_seconds = _dashboard_match_detail_cache_ttl_seconds()

    _dashboard_load_match_detail_cache()
    entry = _DASHBOARD_MATCH_DETAIL_CACHE.get(cache_key)
    if isinstance(entry, dict):
        payload = entry.get("payload")
        updated_ts = float(entry.get("updatedTs") or 0)
        if isinstance(payload, dict) and (ttl_seconds <= 0 or now_ts - updated_ts <= ttl_seconds):
            return payload

    payload = await get_match_details_v4(region, match_id)
    if isinstance(payload, dict) and not payload.get("error"):
        _DASHBOARD_MATCH_DETAIL_CACHE[cache_key] = {
            "updatedAt": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
            "updatedTs": now_ts,
            "payload": payload,
        }
        _dashboard_prune_match_detail_cache(now_ts)
        _dashboard_save_match_detail_cache()
    return payload


def _dashboard_compact_match_detail_for_mirror(
    cache_key: str,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None

    region, _, fallback_match_id = cache_key.partition(":")
    match_id = _extract_match_id(payload) or fallback_match_id
    if not match_id:
        return None

    meta = _match_meta(payload)
    started_at = (
        _extract_match_started_at(payload)
        or _parse_iso_datetime(meta.get("started_at"))
        or _parse_iso_datetime(meta.get("game_start_patched"))
        or _parse_iso_datetime(meta.get("game_start"))
        or _parse_iso_datetime(meta.get("startedAt"))
        or _parse_iso_datetime(meta.get("gameStart"))
    )
    map_name = (
        _display_name(meta.get("map"))
        or meta.get("map_name")
        or meta.get("mapName")
        or _map_name_from_match(payload)
    )
    mode_name = (
        _display_name(meta.get("queue"))
        or _display_name(meta.get("mode"))
        or meta.get("mode_id")
        or _extract_queue_name(payload)
    )
    mode_name = str(mode_name).lower() if mode_name and str(mode_name).lower() != "unknown" else None
    game_length_seconds = _extract_match_length_seconds(payload)
    if game_length_seconds is None:
        for key in ("game_length_in_ms", "gameLengthInMs", "game_length", "gameLength"):
            raw_length = meta.get(key)
            if raw_length is None:
                continue
            try:
                parsed_length = int(raw_length)
            except Exception:
                continue
            game_length_seconds = parsed_length // 1000 if parsed_length > 10000 else parsed_length
            break
    rows = _player_rows_from_match(payload)
    players = [
        {
            "riotId": _player_identity(row),
            "puuid": row.get("puuid"),
            "agent": _agent_name(row),
            "team": row.get("team") or row.get("team_id") or row.get("teamId"),
            **_player_stats(row),
        }
        for row in rows
        if isinstance(row, dict)
    ]

    return {
        "matchKey": f"{region.lower()}:{match_id}",
        "region": region.lower(),
        "matchId": match_id,
        "map": map_name,
        "mode": mode_name,
        "platform": meta.get("platform"),
        "startedAt": started_at.isoformat() if started_at else None,
        "gameLengthSeconds": game_length_seconds,
        "teams": _team_score_summary(payload),
        "playersCount": len(players),
        "players": players,
        "cachedAt": entry.get("updatedAt"),
        "cachedAtMs": int(float(entry.get("updatedTs") or 0) * 1000),
        "source": "valorant_mcp_match_detail_cache",
    }


def _dashboard_match_details_for_mirror() -> list[dict[str, Any]]:
    limit = _dashboard_match_details_mirror_limit()
    if limit <= 0:
        return []

    now_ts = time.time()
    _dashboard_load_match_detail_cache()
    _dashboard_prune_match_detail_cache(now_ts)

    rows: list[dict[str, Any]] = []
    for cache_key, entry in sorted(
        _DASHBOARD_MATCH_DETAIL_CACHE.items(),
        key=lambda item: float(item[1].get("updatedTs") or 0),
        reverse=True,
    ):
        compact = _dashboard_compact_match_detail_for_mirror(cache_key, entry)
        if compact:
            rows.append(compact)
        if len(rows) >= limit:
            break

    return rows


def _dashboard_mode(value: Any, default: str | None = "competitive") -> str | None:
    raw = value if value is not None else default
    if raw is None:
        return None

    normalized = str(raw).strip().lower()
    if normalized in {"", "all", "any", "none", "off", "*"}:
        return None

    return normalized


def _dashboard_player_label(player: dict[str, Any]) -> str:
    return str(player.get("riotId") or f"{player.get('name')}#{player.get('tag')}")


def _dashboard_player_cache_file() -> Path | None:
    configured = os.getenv("VALORANT_DASHBOARD_PLAYER_CACHE_FILE")
    if configured and configured.strip().lower() in {"0", "false", "off", "none"}:
        return None

    path = configured or os.path.join(
        tempfile.gettempdir(),
        "cso-valorant-dashboard-player-cache.json",
    )
    return Path(path)


def _dashboard_load_player_cache() -> None:
    global _DASHBOARD_PLAYER_CACHE, _DASHBOARD_PLAYER_CACHE_LOADED

    if _DASHBOARD_PLAYER_CACHE_LOADED:
        return

    _DASHBOARD_PLAYER_CACHE_LOADED = True
    cache_file = _dashboard_player_cache_file()
    if cache_file is None or not cache_file.exists():
        return

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return

    players = payload.get("players") if isinstance(payload, dict) else None
    if isinstance(players, dict):
        _DASHBOARD_PLAYER_CACHE = {
            str(key): value
            for key, value in players.items()
            if isinstance(value, dict)
        }


def _dashboard_save_player_cache() -> None:
    cache_file = _dashboard_player_cache_file()
    if cache_file is None:
        return

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = cache_file.with_suffix(f"{cache_file.suffix}.tmp")
        tmp_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "savedAt": datetime.now(timezone.utc).isoformat(),
                    "players": _DASHBOARD_PLAYER_CACHE,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        tmp_file.replace(cache_file)
    except Exception:
        return


def _dashboard_player_cache_key(
    player: dict[str, Any],
    *,
    days: int,
    mode: str | None,
    page_size: int,
    max_pages: int,
    max_details: int,
    include_rr: bool = False,
    include_weekly_playtime: bool = False,
) -> str:
    return json.dumps(
        {
            "player": _dashboard_player_label(player).lower(),
            "region": str(player.get("region") or "eu").lower(),
            "platform": str(player.get("platform") or "pc").lower(),
            "days": days,
            "mode": mode or "",
            "page_size": page_size,
            "max_pages": max_pages,
            "max_details": max_details,
            "include_rr": include_rr,
            "include_weekly_playtime": include_weekly_playtime,
        },
        sort_keys=True,
    )


def _dashboard_player_cache_ttl_seconds(request: Request) -> int:
    configured = os.getenv("VALORANT_DASHBOARD_PLAYER_CACHE_TTL_SECONDS", "86400")
    requested = request.query_params.get("playerCacheTtlSeconds", configured)
    return _dashboard_int(requested, 86400, min_value=0, max_value=604800)


def _dashboard_refresh_players_per_request(request: Request, roster_size: int) -> int:
    configured = os.getenv("VALORANT_DASHBOARD_REFRESH_PLAYERS_PER_REQUEST", "4")
    requested = request.query_params.get("refreshPlayers", configured)
    return _dashboard_int(requested, 4, min_value=1, max_value=max(1, roster_size))


def _dashboard_convex_ingest_url() -> str | None:
    value = os.getenv("CONVEX_DASHBOARD_INGEST_URL") or os.getenv("CONVEX_INGEST_URL")
    return value.strip() if value and value.strip() else None


def _dashboard_convex_ingest_token() -> str | None:
    value = os.getenv("CONVEX_DASHBOARD_INGEST_TOKEN") or os.getenv("CONVEX_INGEST_TOKEN")
    return value.strip() if value and value.strip() else None


def _dashboard_convex_timeout_seconds() -> int:
    return _dashboard_int(
        os.getenv("CONVEX_DASHBOARD_INGEST_TIMEOUT_SECONDS", "10"),
        10,
        min_value=1,
        max_value=60,
    )


def _dashboard_post_snapshot_to_convex_sync(snapshot: dict[str, Any]) -> dict[str, Any]:
    ingest_url = _dashboard_convex_ingest_url()
    ingest_token = _dashboard_convex_ingest_token()
    if not ingest_url or not ingest_token:
        return {"status": "disabled"}

    body = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        ingest_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {ingest_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=_dashboard_convex_timeout_seconds(),
        ) as response:
            raw = response.read(65536).decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else None
            except Exception:
                payload = {"raw": raw[:1000]}
            return {
                "status": "ok",
                "statusCode": response.status,
                "response": payload,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        return {
            "status": "error",
            "statusCode": exc.code,
            "message": raw[:1000],
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }


async def _dashboard_mirror_snapshot_to_convex(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    if not _dashboard_convex_ingest_url() or not _dashboard_convex_ingest_token():
        return None
    return await asyncio.to_thread(_dashboard_post_snapshot_to_convex_sync, snapshot)


def _dashboard_is_good_aggregate(aggregate: dict[str, Any] | None) -> bool:
    if not isinstance(aggregate, dict):
        return False

    return (
        int(aggregate.get("matches_counted") or 0) > 0
        or aggregate.get("rr") is not None
        or int(aggregate.get("weekly_playtime_seconds") or 0) > 0
        or int(aggregate.get("weekly_playtime_matches") or 0) > 0
    )


def _dashboard_has_impact_stats(aggregate: dict[str, Any] | None) -> bool:
    if not isinstance(aggregate, dict):
        return False

    return all(
        key in aggregate
        for key in (
            "kast_pct",
            "first_kills",
            "first_deaths",
            "agent_counts",
            "map_counts",
            "daily_matches",
            "games_today",
            "last_played_at",
            "rr",
            "rr_delta",
            "weekly_playtime_seconds",
            "weekly_playtime_hours",
            "weekly_playtime_hhmmss",
            "weekly_games_today",
            "weekly_last_played_at",
        )
    )


def _dashboard_cache_updated_ts(aggregate: dict[str, Any] | None) -> float:
    if not isinstance(aggregate, dict):
        return 0.0

    dashboard_cache = aggregate.get("dashboard_cache")
    if not isinstance(dashboard_cache, dict):
        return 0.0

    raw_ts = dashboard_cache.get("updatedTs")
    if raw_ts is not None:
        try:
            return float(raw_ts)
        except (TypeError, ValueError):
            pass

    updated_at = _parse_iso_datetime(dashboard_cache.get("updatedAt"))
    if updated_at is None:
        return 0.0
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return updated_at.timestamp()


def _dashboard_signal_refresh_cooldown_seconds() -> int:
    return _dashboard_int(
        os.getenv("VALORANT_DASHBOARD_SIGNAL_REFRESH_COOLDOWN_SECONDS", "900"),
        900,
        min_value=0,
        max_value=86400,
    )


def _dashboard_refresh_priority(
    aggregate: dict[str, Any] | None,
    *,
    now_ts: float,
    ttl_seconds: int,
) -> int:
    if not isinstance(aggregate, dict):
        return 10_000

    has_impact_stats = _dashboard_has_impact_stats(aggregate)
    if not has_impact_stats:
        return 9_000

    updated_ts = _dashboard_cache_updated_ts(aggregate)
    if updated_ts <= 0:
        age_seconds = float(ttl_seconds or 0)
    else:
        age_seconds = max(0.0, now_ts - updated_ts)

    cooldown_seconds = _dashboard_signal_refresh_cooldown_seconds()
    confidence = str(aggregate.get("confidence") or "low").lower()
    if confidence == "low":
        base = 7_000 if age_seconds >= cooldown_seconds else 3_500
    elif confidence == "medium":
        base = 5_500 if age_seconds >= cooldown_seconds else 2_800
    else:
        base = 1_500

    age_boost = min(2_400, int(age_seconds // 60) * 10)
    return base + age_boost


def _dashboard_signal_tier(aggregate: dict[str, Any] | None) -> str:
    if not isinstance(aggregate, dict):
        return "missing"

    if not _dashboard_has_impact_stats(aggregate):
        return "incomplete"

    confidence = str(aggregate.get("confidence") or "low").lower()
    if confidence in {"low", "medium", "high"}:
        return confidence
    return "low"


def _dashboard_is_weak_signal(aggregate: dict[str, Any] | None) -> bool:
    return _dashboard_signal_tier(aggregate) != "high"


def _dashboard_signal_refresh_ready(
    aggregate: dict[str, Any] | None,
    *,
    now_ts: float,
) -> bool:
    tier = _dashboard_signal_tier(aggregate)
    if tier in {"missing", "incomplete"}:
        return True
    if tier == "high":
        return False

    updated_ts = _dashboard_cache_updated_ts(aggregate)
    if updated_ts <= 0:
        return True
    return now_ts - updated_ts >= _dashboard_signal_refresh_cooldown_seconds()


def _dashboard_weak_scan_limits(
    *,
    page_size: int,
    max_pages: int,
    max_details: int,
) -> dict[str, int]:
    return {
        "pageSize": max(
            page_size,
            _dashboard_int(
                os.getenv("VALORANT_DASHBOARD_WEAK_PAGE_SIZE", "5"),
                5,
                min_value=1,
                max_value=10,
            ),
        ),
        "maxPages": max(
            max_pages,
            _dashboard_int(
                os.getenv("VALORANT_DASHBOARD_WEAK_MAX_PAGES", "6"),
                6,
                min_value=1,
                max_value=10,
            ),
        ),
        "maxDetailsPerPlayer": max(
            max_details,
            _dashboard_int(
                os.getenv("VALORANT_DASHBOARD_WEAK_MAX_DETAILS_PER_PLAYER", "10"),
                10,
                min_value=1,
                max_value=50,
            ),
        ),
    }


def _dashboard_scan_limits_for_cached_signal(
    aggregate: dict[str, Any] | None,
    *,
    page_size: int,
    max_pages: int,
    max_details: int,
) -> dict[str, Any]:
    normal_limits = {
        "pageSize": page_size,
        "maxPages": max_pages,
        "maxDetailsPerPlayer": max_details,
    }
    if not _dashboard_is_weak_signal(aggregate):
        return {
            **normal_limits,
            "profile": "normal",
            "signalTier": _dashboard_signal_tier(aggregate),
        }

    return {
        **_dashboard_weak_scan_limits(
            page_size=page_size,
            max_pages=max_pages,
            max_details=max_details,
        ),
        "profile": "weak",
        "signalTier": _dashboard_signal_tier(aggregate),
    }


def _dashboard_cached_aggregate(
    cache_key: str,
    *,
    now_ts: float,
    ttl_seconds: int,
) -> dict[str, Any] | None:
    _dashboard_load_player_cache()
    entry = _DASHBOARD_PLAYER_CACHE.get(cache_key)
    cached = _dashboard_cached_aggregate_from_entry(
        entry,
        now_ts=now_ts,
        ttl_seconds=ttl_seconds,
        status="last_good",
    )
    if cached and _dashboard_is_good_aggregate(cached):
        confidence = str(cached.get("confidence") or "low").lower()
        if confidence in {"medium", "high"}:
            return cached
    if cached and _dashboard_signal_tier(cached) not in {"low", "missing", "incomplete"}:
        return cached

    fallback = _dashboard_fallback_cached_aggregate(
        cache_key,
        now_ts=now_ts,
        ttl_seconds=ttl_seconds,
    )
    if fallback:
        return fallback

    return cached


def _dashboard_cached_aggregate_from_entry(
    entry: Any,
    *,
    now_ts: float,
    ttl_seconds: int,
    status: str,
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None

    aggregate = entry.get("aggregate")
    if not isinstance(aggregate, dict):
        return None

    updated_ts = float(entry.get("updatedTs") or 0)
    if ttl_seconds > 0 and updated_ts > 0 and now_ts - updated_ts > ttl_seconds:
        return None

    cached = dict(aggregate)
    cached["dashboard_cache"] = {
        "status": status,
        "updatedAt": entry.get("updatedAt"),
        "updatedTs": updated_ts,
    }
    return cached


def _dashboard_cache_key_payload(cache_key: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(cache_key)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _dashboard_cache_key_matches_player_window(
    candidate_key: str,
    target: dict[str, Any],
) -> bool:
    candidate = _dashboard_cache_key_payload(candidate_key)
    if candidate is None:
        return False

    for field in (
        "player",
        "region",
        "platform",
        "days",
        "mode",
        "include_rr",
        "include_weekly_playtime",
    ):
        if candidate.get(field) != target.get(field):
            return False
    return True


def _dashboard_fallback_cached_aggregate(
    cache_key: str,
    *,
    now_ts: float,
    ttl_seconds: int,
) -> dict[str, Any] | None:
    target = _dashboard_cache_key_payload(cache_key)
    if target is None:
        return None

    best: tuple[int, int, float, dict[str, Any]] | None = None
    signal_rank = {"medium": 2, "high": 3}
    for candidate_key, entry in _DASHBOARD_PLAYER_CACHE.items():
        if candidate_key == cache_key:
            continue
        if not _dashboard_cache_key_matches_player_window(candidate_key, target):
            continue

        aggregate = _dashboard_cached_aggregate_from_entry(
            entry,
            now_ts=now_ts,
            ttl_seconds=ttl_seconds,
            status="last_good_fallback",
        )
        if not aggregate or not _dashboard_is_good_aggregate(aggregate):
            continue

        confidence = str(aggregate.get("confidence") or "low").lower()
        rank = signal_rank.get(confidence)
        if rank is None:
            continue

        matches_counted = int(aggregate.get("matches_counted") or 0)
        updated_ts = _dashboard_cache_updated_ts(aggregate)
        score = (rank, matches_counted, updated_ts, aggregate)
        if best is None or score[:3] > best[:3]:
            best = score

    return best[3] if best else None


def _dashboard_update_player_cache(
    cache_key: str,
    player: dict[str, Any],
    aggregate: dict[str, Any],
    *,
    now_ts: float,
) -> None:
    if not _dashboard_is_good_aggregate(aggregate):
        return

    _dashboard_load_player_cache()
    updated_at = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()
    _DASHBOARD_PLAYER_CACHE[cache_key] = {
        "updatedAt": updated_at,
        "updatedTs": now_ts,
        "player": _dashboard_player_label(player),
        "aggregate": aggregate,
    }


def _dashboard_select_refresh_players(
    roster: list[dict[str, Any]],
    cache_keys: list[str],
    *,
    now_ts: float,
    ttl_seconds: int,
    refresh_count: int,
    rolling_key: str,
) -> list[tuple[int, dict[str, Any]]]:
    if not roster:
        return []

    start = _DASHBOARD_ROLLING_CURSOR_BY_KEY.get(rolling_key, 0) % len(roster)
    ordered_indices = [(start + offset) % len(roster) for offset in range(len(roster))]
    prioritized_indices = []
    for rolling_position, index in enumerate(ordered_indices):
        cached = _dashboard_cached_aggregate(
            cache_keys[index],
            now_ts=now_ts,
            ttl_seconds=ttl_seconds,
        )
        priority = _dashboard_refresh_priority(
            cached,
            now_ts=now_ts,
            ttl_seconds=ttl_seconds,
        )
        prioritized_indices.append(
            (
                priority,
                _dashboard_is_weak_signal(cached),
                _dashboard_signal_refresh_ready(cached, now_ts=now_ts),
                rolling_position,
                index,
            )
        )

    weak_indices = [item for item in prioritized_indices if item[1]]
    if weak_indices:
        eligible_indices = [item for item in weak_indices if item[2]]
    else:
        eligible_indices = prioritized_indices

    eligible_indices.sort(key=lambda item: (-item[0], item[3]))
    selected = eligible_indices[:refresh_count]
    selected_indices = [index for _, _, _, _, index in selected]

    if selected:
        *_, furthest_selected_index = max(selected, key=lambda item: item[3])
        _DASHBOARD_ROLLING_CURSOR_BY_KEY[rolling_key] = (furthest_selected_index + 1) % len(roster)

    return [(index, roster[index]) for index in selected_indices]


def _dashboard_roster() -> list[dict[str, Any]]:
    raw = os.getenv("CSO_VALORANT_DASHBOARD_PLAYERS_JSON")
    source = DEFAULT_DASHBOARD_ROSTER
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                source = [item for item in parsed if isinstance(item, dict)]
        except Exception:
            source = DEFAULT_DASHBOARD_ROSTER

    roster: list[dict[str, Any]] = []
    for item in source:
        player = dict(item)
        riot_id = str(player.get("riotId") or player.get("riot_id") or "")
        name = player.get("name")
        tag = player.get("tag")
        if riot_id and (not name or not tag) and "#" in riot_id:
            name, tag = riot_id.split("#", 1)
            player["name"] = name
            player["tag"] = tag
        if not riot_id and name and tag:
            riot_id = f"{name}#{tag}"
            player["riotId"] = riot_id
        if player.get("name") and player.get("tag"):
            roster.append(player)
    return roster


def _dashboard_player_stats(player: dict[str, Any], aggregate: dict[str, Any] | None) -> dict[str, Any]:
    region = str(player.get("region") or "eu")
    platform = str(player.get("platform") or "pc")
    riot_id = str(player.get("riotId") or f"{player.get('name')}#{player.get('tag')}")
    errors = aggregate.get("errors") if isinstance(aggregate, dict) else [{"reason": "missing_aggregate"}]
    if not isinstance(errors, list):
        errors = []

    return {
        "player": riot_id,
        "region": aggregate.get("region", region) if aggregate else region,
        "platform": aggregate.get("platform", platform) if aggregate else platform,
        "weeklyMatches": aggregate.get("weekly_matches", 0) if aggregate else 0,
        "matchesCounted": aggregate.get("matches_counted", 0) if aggregate else 0,
        "wins": aggregate.get("wins", 0) if aggregate else 0,
        "losses": aggregate.get("losses", 0) if aggregate else 0,
        "winRate": aggregate.get("win_rate") if aggregate else None,
        "kills": aggregate.get("kills", 0) if aggregate else 0,
        "deaths": aggregate.get("deaths", 0) if aggregate else 0,
        "assists": aggregate.get("assists", 0) if aggregate else 0,
        "kd": aggregate.get("kd") if aggregate else None,
        "acs": aggregate.get("acs") if aggregate else None,
        "adr": aggregate.get("adr") if aggregate else None,
        "kastPct": aggregate.get("kast_pct") if aggregate else None,
        "firstKills": aggregate.get("first_kills") if aggregate else None,
        "firstDeaths": aggregate.get("first_deaths") if aggregate else None,
        "headshots": aggregate.get("headshots") if aggregate else None,
        "bodyshots": aggregate.get("bodyshots") if aggregate else None,
        "legshots": aggregate.get("legshots") if aggregate else None,
        "hsPct": aggregate.get("hs_pct") if aggregate else None,
        "agentCounts": aggregate.get("agent_counts", {}) if aggregate else {},
        "mapCounts": aggregate.get("map_counts", {}) if aggregate else {},
        "dailyMatches": aggregate.get("daily_matches", {}) if aggregate else {},
        "gamesToday": aggregate.get("games_today", aggregate.get("weekly_games_today", 0)) if aggregate else 0,
        "lastPlayedAt": aggregate.get("last_played_at") or aggregate.get("weekly_last_played_at") if aggregate else None,
        "rr": aggregate.get("rr") if aggregate else None,
        "rrDelta": aggregate.get("rr_delta") if aggregate else None,
        "rrWeeklyDelta": aggregate.get("rr_weekly_delta") if aggregate else None,
        "rrWeeklyMatches": aggregate.get("rr_weekly_matches", 0) if aggregate else 0,
        "currentRank": aggregate.get("current_rank") if aggregate else None,
        "peakRank": aggregate.get("peak_rank") if aggregate else None,
        "peakRankSource": aggregate.get("peak_rank_source") if aggregate else None,
        "lastRankedAt": aggregate.get("last_ranked_at") if aggregate else None,
        "weeklyPlaytimeSeconds": aggregate.get("weekly_playtime_seconds", 0) if aggregate else 0,
        "weeklyPlaytimeHours": aggregate.get("weekly_playtime_hours", 0) if aggregate else 0,
        "weeklyPlaytimeHhmmss": aggregate.get("weekly_playtime_hhmmss") if aggregate else None,
        "weeklyCompetitiveMatches": aggregate.get("weekly_playtime_matches", 0) if aggregate else 0,
        "weeklyActiveDays": aggregate.get("weekly_active_days", 0) if aggregate else 0,
        "weeklyPlaytimeDaily": aggregate.get("weekly_playtime_daily", {}) if aggregate else {},
        "weeklyPlaytimeConfidence": aggregate.get("weekly_playtime_confidence") if aggregate else "low",
        "confidence": aggregate.get("confidence", "low") if aggregate else "low",
        "errorCount": len(errors),
    }


def _dashboard_player_peak_rank(
    player: dict[str, Any],
    aggregate: dict[str, Any] | None,
) -> tuple[str, str]:
    if aggregate:
        peak_rank = aggregate.get("peak_rank")
        if peak_rank:
            return str(peak_rank), str(aggregate.get("peak_rank_source") or "henrik_mmr")

    roster_peak = player.get("peakRank") or player.get("peak_rank")
    if roster_peak:
        return str(roster_peak), "roster"

    return "Unranked", "unknown"


def _dashboard_player_payload(
    player: dict[str, Any],
    aggregate: dict[str, Any] | None,
) -> dict[str, Any]:
    stats = _dashboard_player_stats(player, aggregate)
    peak_rank, peak_rank_source = _dashboard_player_peak_rank(player, aggregate)

    return {
        "id": player.get("id") or str(player.get("riotId", "")).lower().replace(" ", "-").replace("#", "-"),
        "rosterName": player.get("rosterName") or player.get("roster_name") or player.get("name"),
        "riotId": player.get("riotId") or f"{player.get('name')}#{player.get('tag')}",
        "team": player.get("team") or "CSO Valorant",
        "status": "Active",
        "country": player.get("country") or "South Africa",
        "peakRank": peak_rank,
        "peakRankSource": peak_rank_source,
        "trackerUrl": player.get("trackerUrl") or player.get("tracker_url"),
        "stats": stats,
    }


def _dashboard_cache_seconds(request: Request) -> int:
    configured = os.getenv("VALORANT_DASHBOARD_CACHE_SECONDS", "60")
    requested = request.query_params.get("cacheSeconds", configured)
    return _dashboard_int(requested, 60, min_value=0, max_value=3600)

# ---------------------------------------------------------------------------
# Account tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_account(
    name: str | None = None,
    tag: str | None = None,
    puuid: str | None = None,
    force_update: bool = False,
) -> dict[str, Any]:
    """Retrieve Valorant account details by Riot ID (name + tag) or by PUUID.

    Returns puuid, region, account level, card image URLs, and last update time.
    Supply exactly one identification form: name and tag together, or puuid
    alone. Any other combination returns an error dict.

    Args:
        name: In-game name (e.g. 'TenZ'). Must be paired with tag.
        tag: Tag line without '#' (e.g. 'SEN'). Must be paired with name.
        puuid: Player unique identifier — mutually exclusive with name/tag.
        force_update: Force a data refresh from Riot servers.
    """
    error = _player_identity_error(name, tag, puuid)
    if error:
        return error
    if puuid:
        return await accounts.get_account_by_puuid(puuid, force_update)
    return await accounts.get_account(name, tag, force_update)


# ---------------------------------------------------------------------------
# Match tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_match_history(
    region: Region,
    name: str | None = None,
    tag: str | None = None,
    puuid: str | None = None,
    platform: Platform = "pc",
    mode: GameMode | None = None,
    map_name: str | None = None,
    size: int | None = None,
    start: int | None = None,
    compact: bool = True,
) -> Any:
    """Retrieve recent v4 match history for a player by Riot ID or PUUID.

    Supply exactly one identification form: name and tag together, or puuid
    alone. Any other combination returns an error dict.

    With compact=True (the default) the response is a small trimmed payload:
    at most five match rows containing match_id, map, mode, start time, game
    length, and team scores (plus the target player's stats on the puuid
    path), with the large nested per-player payloads removed. Prefer this for
    LLM/agent workflows and fetch full details only for a selected match_id.

    With compact=False the full upstream payload is returned: the name+tag
    path returns the list of complete v4 match summaries, and the puuid path
    returns the raw Henrik v4 envelope. Expect large responses.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        name: In-game name. Must be paired with tag.
        tag: Tag line without '#'. Must be paired with name.
        puuid: Player unique identifier — mutually exclusive with name/tag.
        platform: 'pc' (default) or 'console'.
        mode: Optional game mode filter (e.g. 'competitive', 'unrated').
        map_name: Optional map name filter (e.g. 'Ascent'); any string is
            passed through so newly released maps work.
        size: Number of matches to return. Compact responses default to 3 and
            are hard-capped at 5; full responses use the Henrik v4 default
            of 10 when unset.
        start: Optional v4 pagination start index.
        compact: Return the trimmed payload (default True).
    """
    error = _player_identity_error(name, tag, puuid)
    if error:
        return error
    if compact:
        if puuid:
            return await get_match_history_by_puuid_trimmed(
                region, puuid, platform, mode, map_name, size, start
            )
        return await get_match_history_v4_trimmed(
            region, name, tag, platform, mode, map_name, size, start
        )
    if puuid:
        return await get_match_history_by_puuid(
            region, puuid, platform, mode, map_name, size, start
        )
    return await matches.get_match_history(
        region, name, tag, platform, mode, map_name, size, start
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_match(
    region: Region,
    match_id: str,
) -> dict[str, Any]:
    """Retrieve full details for a single Valorant match by match ID.

    Returns complete data: metadata, all players (agents, stats, loadouts),
    round-by-round results, kill feed, and economy. The payload is very large
    (often hundreds of KB); prefer the compact derived tools first —
    get_match_player_stats_compact for player stats/scoreboard,
    get_match_rounds_summary / get_match_round for round data,
    get_match_team_economy_summary or get_match_opening_duels for specifics —
    and use this tool only when the full raw payload is genuinely needed.
    Results are served from a short-lived in-process cache.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        match_id: Match UUID (e.g. '696848f3-f16f-45bf-af13-e2192f81a600').
    """
    return await matches.get_match(region, match_id)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_match_rounds_summary(
    region: Region,
    match_id: str,
    include_kills: bool = False,
    include_economy: bool = False,
) -> dict[str, Any]:
    """Return compact, non-truncating summaries for every round in a match.

    Rounds are returned as 1-indexed round_number values. Killfeed arrays,
    player loadouts, and per-player damage events are excluded.
    """
    full = await matches.get_match(region, match_id)
    return _rounds_summary(
        full,
        region,
        include_kills=include_kills,
        include_economy=include_economy,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_match_round(
    region: Region,
    match_id: str,
    round_number: int | None = None,
    round_id: int | None = None,
    include_killfeed: bool = False,
    include_player_stats: bool = False,
) -> dict[str, Any]:
    """Return one compact round by 1-indexed round_number or 0-indexed round_id."""
    full = await matches.get_match(region, match_id)
    return _one_round(
        full,
        region,
        round_number=round_number,
        round_id=round_id,
        include_killfeed=include_killfeed,
        include_player_stats=include_player_stats,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_match_events_compact(region: Region, match_id: str) -> dict[str, Any]:
    """Return plants, defuses, and round-end events only; no kills."""
    full = await matches.get_match(region, match_id)
    return _compact_events(full, region)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_match_player_impact_summary(
    region: Region,
    match_id: str,
    puuid: str | None = None,
    name: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Return a compact coaching impact summary for one player in a match."""
    full = await matches.get_match(region, match_id)
    return _player_impact_summary(full, region, puuid=puuid, name=name, tag=tag)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_match_team_economy_summary(region: Region, match_id: str) -> dict[str, Any]:
    """Return round-by-round team economy, eco wins, and bonus conversions."""
    full = await matches.get_match(region, match_id)
    return _team_economy_summary(full, region)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_match_opening_duels(region: Region, match_id: str) -> dict[str, Any]:
    """Return first kill/death and conversion context for each round."""
    full = await matches.get_match(region, match_id)
    return _opening_duels(full, region)


async def _player_last_n_rows(
    region: Region,
    name: str,
    tag: str,
    platform: Platform,
    match_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    history = await matches.get_match_history(
        region,
        name,
        tag,
        platform=platform,
        mode="competitive",
        size=max(1, min(match_count, 20)),
    )
    if not isinstance(history, list):
        return [], {
            "error": True,
            "message": "Could not retrieve player match history",
            "response": history,
        }

    rows: list[dict[str, Any]] = []
    for item in history[: max(1, min(match_count, 20))]:
        match_id = _extract_match_id(item)
        if not match_id:
            continue
        full = await matches.get_match(region, match_id)
        player_row = _find_player_in_match(full, name=name, tag=tag)
        if not player_row:
            continue
        stats = _player_stats(player_row)
        rows.append(
            {
                "match_id": match_id,
                "map": _map_name_from_match(full),
                "agent": _agent_name(player_row),
                "won": _team_won(player_row, full),
                "kills": stats["kills"],
                "deaths": stats["deaths"],
                "assists": stats["assists"],
                "score": stats["score"],
            }
        )
    return rows, None


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_player_pools(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
    match_count: int = 10,
) -> dict[str, Any]:
    """Summarize player performance by map AND by agent over the last N competitive matches.

    Fetches the player's recent competitive matches once and returns two
    rollups of the same rows: map_pool (per-map matches, winrate, K/D,
    average score) and agent_pool (the same metrics per agent). match_count
    is clamped to 1-20. Heavy: fetches one full match payload per counted match.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        name: In-game name.
        tag: Tag line without '#'.
        platform: 'pc' (default) or 'console'.
        match_count: Number of recent competitive matches to scan (max 20).
    """
    rows, error = await _player_last_n_rows(region, name, tag, platform, match_count)
    return {
        "region": region,
        "player": f"{name}#{tag}",
        "platform": platform,
        "matches_requested": max(1, min(match_count, 20)),
        "matches_counted": len(rows),
        "history_error": error,
        "map_pool": _rollup_history(rows, group_by="map"),
        "agent_pool": _rollup_history(rows, group_by="agent"),
    }


# ---------------------------------------------------------------------------
# Leaderboard tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
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
    """Retrieve the competitive leaderboard for a region and platform.

    Filter by a specific player using name+tag OR puuid (not both).
    Optionally filter by season for historical data.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        platform: 'pc' (default) or 'console'.
        name: Filter to a specific player name (requires tag).
        tag: Filter to a specific player tag (requires name).
        puuid: Filter by PUUID — mutually exclusive with name/tag.
        season_short: Season code — 'e{episode}a{act}' (e.g. 'e9a3') or
            'v{year}a{act}' (e.g. 'v25a1') for historical leaderboards.
        size: Number of entries to return (default 100, max 1000).
        start: 0-indexed entry offset for pagination (v3 start_index).
    """
    return await leaderboard.get_leaderboard(
        region, platform, name, tag, puuid, season_short, size, start
    )


# ---------------------------------------------------------------------------
# Esports Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_esports_schedule(
    region: EsportsRegion | None = None,
    league: list[League] | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Retrieve the current and upcoming schedule for Valorant esports matches.

    Returns a list of scheduled/completed pro matches with league, teams,
    start time, and scores. Can be filtered by a broader region or by an
    explicit list of leagues/tournaments. On failure a structured error dict
    ({'error': True, ...}) is returned instead of a list.

    Args:
        region: Optional region to filter by (e.g., 'international', 'north america', 'emea').
        league: Optional list of specific leagues to filter by (e.g., ['vct_americas', 'vct_emea']).
    """
    return await esports.get_esports_schedule(region, league)




# ---------------------------------------------------------------------------
# Derived Analytics / Scouting Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_player_summary(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
    match_count: int = 10,
    days: int = 30,
) -> dict[str, Any]:
    """Production-safe compact player summary for agents.

    This avoids returning full match payloads and records API/detail errors in
    the response instead of raising on the first failed match.
    """
    errors: list[dict[str, Any]] = []

    account: dict[str, Any] | None = None
    mmr_data: dict[str, Any] | None = None
    try:
        account = await accounts.get_account(name, tag, False)
    except Exception as exc:
        errors.append({"reason": "account_error", "error": str(exc)})
    try:
        mmr_data = await mmr.get_mmr(region, name, tag, platform)
    except Exception as exc:
        errors.append({"reason": "mmr_error", "error": str(exc)})

    compact_matches, match_errors = await _collect_player_window_stats(
        region=region,
        name=name,
        tag=tag,
        platform=platform,
        days=days,
        mode=None,
        page_size=min(max(match_count, 1), 10),
        max_pages=max(1, (max(match_count, 1) + 9) // 10),
        max_details=match_count,
    )
    errors.extend(match_errors)

    aggregate = _aggregate_compact_player_matches(
        compact_matches,
        player=f"{name}#{tag}",
        region=region,
        platform=platform,
        days=days,
        mode=None,
        errors=errors,
        include_matches=False,
    )
    agents: dict[str, int] = {}
    maps_played: dict[str, int] = {}
    for row in compact_matches:
        agent = row.get("agent")
        map_name = row.get("map")
        if agent and agent != "Unknown":
            agents[agent] = agents.get(agent, 0) + 1
        if map_name:
            maps_played[str(map_name)] = maps_played.get(str(map_name), 0) + 1

    return {
        "account": account,
        "mmr": mmr_data,
        "matches_checked": aggregate["matches_counted"],
        "totals": {
            "kills": aggregate["kills"],
            "deaths": aggregate["deaths"],
            "assists": aggregate["assists"],
        },
        "kd": aggregate["kd"],
        "kda": round((aggregate["kills"] + aggregate["assists"]) / max(aggregate["deaths"], 1), 2)
        if aggregate["matches_counted"]
        else None,
        "acs": aggregate["acs"],
        "adr": aggregate["adr"],
        "hs_pct": aggregate["hs_pct"],
        "win_rate": aggregate["win_rate"],
        "weekly_matches": aggregate["weekly_matches"] if days == 7 else None,
        "agents": agents,
        "maps": maps_played,
        "confidence": aggregate["confidence"],
        "errors": errors,
    }


# Internal helper (no longer a registered tool): used by
# screen_candidates and get_trial_readiness_score.
async def get_recent_form(region: Region, name: str, tag: str, platform: Platform = "pc", match_count: int = 10) -> dict[str, Any]:
    """Return recent performance form and simple trend indicators."""
    summary = await get_player_summary(region, name, tag, platform, match_count)
    raw_kd = summary.get("kd")
    kd = float(raw_kd) if isinstance(raw_kd, (int, float)) else 0.0

    if kd >= 1.25:
        form = "hot"
    elif kd >= 1.0:
        form = "stable"
    elif kd >= 0.8:
        form = "struggling"
    else:
        form = "cold"

    return {
        "form": form,
        "summary": summary,
        "notes": [
            "Form is estimated from recent matches returned by the API.",
            "Use VOD review before making roster decisions."
        ],
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_match_player_stats_compact(
    region: Region,
    match_id: str,
    puuid: str | None = None,
    name: str | None = None,
    tag: str | None = None,
    include_all_players: bool = False,
) -> dict[str, Any]:
    """Return compact per-match player stats without the full match payload.

    Includes team win boolean plus head/body/leg shot counts when the Henrik
    payload exposes them. Pass a PUUID or name+tag for one target player, or
    include_all_players=True for a compact scoreboard. Prefer this over
    get_match when you only need player stats. Match details are served from
    the shared match-detail cache.
    """
    full = await matches.get_match(region, match_id)
    if isinstance(full, dict) and full.get("error"):
        return full
    base = _compact_match_history_item(full, region=region, target_puuid=puuid)

    if include_all_players:
        players: list[dict[str, Any]] = []
        for row in _player_rows_from_match(full):
            if not isinstance(row, dict):
                continue
            item = _compact_player_match_stats(full, region=region, puuid=row.get("puuid"))
            if item:
                players.append({key: value for key, value in item.items() if key not in {"team_score"}})
        return {
            **base,
            "players_count": len(players),
            "players": players,
            "notes": ["Compact scoreboard only; full match payload intentionally omitted."],
        }

    target = _compact_player_match_stats(full, region=region, puuid=puuid, name=name, tag=tag)
    if not target:
        return {
            **base,
            "error": True,
            "message": "Player not found. Provide puuid or name+tag, or set include_all_players=True.",
        }
    return {
        **base,
        "player_stats": target,
        "notes": ["Compact player match stats only; full match payload intentionally omitted."],
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_player_backfill_aggregate(
    region: Region,
    platform: Platform = "pc",
    days: int = 7,
    mode: str | None = None,
    name: str | None = None,
    tag: str | None = None,
    puuid: str | None = None,
    page_size: int = 5,
    max_pages: int = 4,
    max_details: int = 20,
    include_matches: bool = False,
    include_rr: bool = False,
    include_weekly_playtime: bool = False,
) -> dict[str, Any]:
    """Return compact per-player stat aggregates for a date window.

    Small-payload response designed for automated reporting/backfill jobs.
    Output includes ACS, ADR, K/D, HS%, win rate, and weekly match count where
    source data is available. Missing metrics are returned as null rather than
    guessed. Set include_matches=True to also return the per-match rows.
    """
    player_label = f"{name}#{tag}" if name and tag else puuid or "unknown"
    rr_summary: dict[str, Any] | None = None
    playtime_summary: dict[str, Any] | None = None

    if name and tag and include_rr:
        rr_summary = await _collect_player_rr_summary(
            region=region,
            platform=platform,
            name=name,
            tag=tag,
            days=7,
        )

    if name and tag and include_weekly_playtime:
        playtime_summary = await _collect_player_weekly_playtime_summary(
            region=region,
            platform=platform,
            name=name,
            tag=tag,
            mode=mode,
            page_size=5,
            max_pages=5,
        )

    compact_matches, errors = await _collect_player_window_stats(
        region=region,
        platform=platform,
        days=days,
        mode=mode,
        page_size=page_size,
        max_pages=max_pages,
        max_details=max_details,
        name=name,
        tag=tag,
        puuid=puuid,
    )
    output = _aggregate_compact_player_matches(
        compact_matches,
        player=player_label,
        region=region,
        platform=platform,
        days=days,
        mode=mode,
        errors=errors,
        include_matches=include_matches,
    )

    if rr_summary:
        output.update(rr_summary)
        rank_errors = rr_summary.get("rank_errors")
        if isinstance(rank_errors, list):
            output["errors"] = [*output.get("errors", []), *rank_errors]

    if playtime_summary:
        output.update(playtime_summary)

    return output


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_bulk_player_backfill_aggregates(
    players: list[dict[str, Any]],
    default_region: Region = "eu",
    default_platform: Platform = "pc",
    days: int = 7,
    mode: str | None = None,
    page_size: int = 5,
    max_pages: int = 4,
    max_details_per_player: int = 20,
    include_rr: bool = False,
    include_weekly_playtime: bool = False,
) -> dict[str, Any]:
    """Return compact stat aggregates for up to 25 players in one call.

    Bulk variant of get_player_backfill_aggregate with per-player scan limits;
    each player entry needs name+tag or puuid. Small payload per player.
    """
    max_players = 25
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, player in enumerate(players[:max_players]):
        if not isinstance(player, dict):
            errors.append({"index": index, "reason": "invalid_player"})
            continue

        name = player.get("name")
        tag = player.get("tag")
        puuid = player.get("puuid")
        if not puuid and not (name and tag):
            errors.append({"index": index, "reason": "missing_identifier", "input": player})
            continue

        player_page_size = _dashboard_int(
            player.get("pageSize") or player.get("page_size"),
            page_size,
            min_value=1,
            max_value=10,
        )
        player_max_pages = _dashboard_int(
            player.get("maxPages") or player.get("max_pages"),
            max_pages,
            min_value=1,
            max_value=10,
        )
        player_max_details = _dashboard_int(
            player.get("maxDetailsPerPlayer")
            or player.get("max_details_per_player")
            or player.get("max_details"),
            max_details_per_player,
            min_value=1,
            max_value=50,
        )

        try:
            aggregate = await get_player_backfill_aggregate(
                region=player.get("region", default_region),
                platform=player.get("platform", default_platform),
                days=days,
                mode=mode,
                name=name,
                tag=tag,
                puuid=puuid,
                page_size=player_page_size,
                max_pages=player_max_pages,
                max_details=player_max_details,
                include_matches=False,
                include_rr=include_rr,
                include_weekly_playtime=include_weekly_playtime,
            )
            aggregate["scan"] = {
                "pageSize": player_page_size,
                "maxPages": player_max_pages,
                "maxDetailsPerPlayer": player_max_details,
                "profile": player.get("scanProfile") or "normal",
                "requestedSignalTier": player.get("signalTier"),
            }
            results.append(aggregate)
        except Exception as exc:
            errors.append({"index": index, "input": player, "reason": "aggregate_error", "error": str(exc)})

    return {
        "players_requested": len(players),
        "players_processed": len(results),
        "players_limit": max_players,
        "window": {"days": days},
        "mode_filter": mode,
        "results": results,
        "errors": errors,
        "notes": [
            "Bulk-safe compact aggregate response.",
            "Null metrics mean the source payload did not expose enough data; values are not invented.",
        ],
    }


@mcp.custom_route("/stats/dashboard", methods=["GET"], include_in_schema=False)
async def get_cso_dashboard_snapshot(request: Request) -> Response:
    """Return the CSO Valorant dashboard snapshot as plain JSON.

    This endpoint is intentionally separate from MCP so the public Sites app can
    poll it with a normal server-side fetch. It requires a bearer token because
    FastMCP custom routes do not inherit MCP transport auth.
    """
    global _DASHBOARD_CACHE, _DASHBOARD_CACHE_EXPIRES_AT, _DASHBOARD_CACHE_KEY

    auth_response = _dashboard_auth_response(request)
    if auth_response is not None:
        return auth_response

    roster = _dashboard_roster()
    days = _dashboard_int(
        request.query_params.get("days", os.getenv("VALORANT_DASHBOARD_WINDOW_DAYS", "30")),
        30,
        min_value=1,
        max_value=90,
    )
    page_size = _dashboard_int(request.query_params.get("pageSize"), 5, min_value=1, max_value=10)
    max_pages = _dashboard_int(request.query_params.get("maxPages"), 4, min_value=1, max_value=10)
    max_details = _dashboard_int(
        request.query_params.get("maxDetailsPerPlayer"),
        20,
        min_value=1,
        max_value=50,
    )
    mode = _dashboard_mode(
        request.query_params.get("mode"),
        os.getenv("VALORANT_DASHBOARD_MODE", "competitive"),
    )
    mode_label = mode or "all"
    include_rr = _dashboard_bool(request.query_params.get("includeRR"), True)
    include_weekly_playtime = _dashboard_bool(
        request.query_params.get("includeWeeklyPlaytime"),
        True,
    )
    cache_seconds = _dashboard_cache_seconds(request)
    player_cache_ttl_seconds = _dashboard_player_cache_ttl_seconds(request)
    refresh_players = _dashboard_refresh_players_per_request(request, len(roster))
    force = request.query_params.get("force", "").lower() in {"1", "true", "yes"}
    bypass_player_cache = _dashboard_bool(request.query_params.get("bypassPlayerCache"), False)
    cache_key = json.dumps(
        {
            "players": [
                [player.get("name"), player.get("tag"), player.get("region", "eu"), player.get("platform", "pc")]
                for player in roster
            ],
            "days": days,
            "page_size": page_size,
            "max_pages": max_pages,
            "max_details": max_details,
            "mode": mode_label,
            "refresh_players": refresh_players,
            "player_cache_ttl_seconds": player_cache_ttl_seconds,
            "include_rr": include_rr,
            "include_weekly_playtime": include_weekly_playtime,
        },
        sort_keys=True,
    )

    now_ts = time.time()
    if (
        not force
        and cache_seconds > 0
        and _DASHBOARD_CACHE is not None
        and _DASHBOARD_CACHE_KEY == cache_key
        and now_ts < _DASHBOARD_CACHE_EXPIRES_AT
    ):
        return JSONResponse(
            {
                **_DASHBOARD_CACHE,
                "servedAt": datetime.now(timezone.utc).isoformat(),
                "cache": {
                    "status": "hit",
                    "ttlSeconds": max(0, int(_DASHBOARD_CACHE_EXPIRES_AT - now_ts)),
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    player_cache_keys = [
        _dashboard_player_cache_key(
            player,
            days=days,
            mode=mode,
            page_size=page_size,
            max_pages=max_pages,
            max_details=max_details,
            include_rr=include_rr,
            include_weekly_playtime=include_weekly_playtime,
        )
        for player in roster
    ]
    selected_players = _dashboard_select_refresh_players(
        roster,
        player_cache_keys,
        now_ts=now_ts,
        ttl_seconds=player_cache_ttl_seconds,
        refresh_count=refresh_players,
        rolling_key=cache_key,
    )
    selected_indices = {index for index, _ in selected_players}
    refresh_errors: list[dict[str, Any]] = []
    refresh_results: dict[str, dict[str, Any]] = {}
    selected_scan_limits: dict[str, dict[str, Any]] = {}

    if selected_players:
        player_requests: list[dict[str, Any]] = []
        for index, player in selected_players:
            cached = None if bypass_player_cache else _dashboard_cached_aggregate(
                player_cache_keys[index],
                now_ts=now_ts,
                ttl_seconds=player_cache_ttl_seconds,
            )
            scan_limits = _dashboard_scan_limits_for_cached_signal(
                cached,
                page_size=page_size,
                max_pages=max_pages,
                max_details=max_details,
            )
            selected_scan_limits[_dashboard_player_label(player)] = scan_limits
            player_requests.append(
                {
                    "name": player.get("name"),
                    "tag": player.get("tag"),
                    "region": player.get("region", "eu"),
                    "platform": player.get("platform", "pc"),
                    "pageSize": scan_limits["pageSize"],
                    "maxPages": scan_limits["maxPages"],
                    "maxDetailsPerPlayer": scan_limits["maxDetailsPerPlayer"],
                    "scanProfile": scan_limits["profile"],
                    "signalTier": scan_limits["signalTier"],
                }
            )

        try:
            aggregate = await get_bulk_player_backfill_aggregates(
                players=player_requests,
                default_region="eu",
                default_platform="pc",
                days=days,
                mode=mode,
                page_size=page_size,
                max_pages=max_pages,
                max_details_per_player=max_details,
                include_rr=include_rr,
                include_weekly_playtime=include_weekly_playtime,
            )
        except Exception as exc:
            aggregate = {"results": [], "errors": [{"reason": "refresh_failed", "error": str(exc)}]}

        refresh_errors = [
            item for item in aggregate.get("errors", []) if isinstance(item, dict)
        ]
        refresh_results = {
            str(item.get("player", "")).lower(): item
            for item in aggregate.get("results", [])
            if isinstance(item, dict)
        }

    aggregates_by_player: dict[str, dict[str, Any]] = {}
    refreshed_count = 0
    last_good_count = 0
    limited_uncached_count = 0
    player_cache_updated = False

    for index, player in enumerate(roster):
        label = _dashboard_player_label(player).lower()
        cache_key_for_player = player_cache_keys[index]
        refreshed = refresh_results.get(label)
        cached = None if bypass_player_cache else _dashboard_cached_aggregate(
            cache_key_for_player,
            now_ts=now_ts,
            ttl_seconds=player_cache_ttl_seconds,
        )

        if refreshed and _dashboard_is_good_aggregate(refreshed):
            aggregates_by_player[label] = refreshed
            refreshed_count += 1
            _dashboard_update_player_cache(cache_key_for_player, player, refreshed, now_ts=now_ts)
            player_cache_updated = True
            continue

        if cached:
            aggregates_by_player[label] = cached
            last_good_count += 1
            continue

        if refreshed:
            aggregates_by_player[label] = refreshed
            if index in selected_indices:
                limited_uncached_count += 1
            continue

        if index in selected_indices:
            refresh_errors.append(
                {
                    "player": _dashboard_player_label(player),
                    "reason": "missing_refresh_result",
                }
            )
        limited_uncached_count += 1

    if player_cache_updated:
        _dashboard_save_player_cache()

    generated_at = datetime.now(timezone.utc).isoformat()
    match_details_for_mirror = _dashboard_match_details_for_mirror()
    snapshot = {
        "generatedAt": generated_at,
        "servedAt": generated_at,
        "windowDays": days,
        "mode": mode_label,
        "refreshMode": "external",
        "externalRefreshConfigured": True,
        "dataSources": {
            "roster": "CSO Valorant dashboard roster",
            "stats": "Valorant MCP live aggregate endpoint",
        },
        "notes": [
            "Live stats generated by the CSO Valorant MCP server /stats/dashboard endpoint.",
            f"Server-side cache window is {cache_seconds}s to protect HenrikDev rate limits.",
            f"Rolling player cache refreshed {len(selected_players)} players and served {last_good_count} from last-good cache.",
            "Null metrics mean the source payload did not expose enough data; values are not invented.",
        ],
        "players": [
            _dashboard_player_payload(
                player,
                aggregates_by_player.get(_dashboard_player_label(player).lower()),
            )
            for player in roster
        ],
        "matchDetails": match_details_for_mirror,
        "errors": refresh_errors,
        "cache": {"status": "miss", "ttlSeconds": cache_seconds},
        "rollingCache": {
            "refreshPlayersPerRequest": refresh_players,
            "selectionPolicy": "weak-signal-first",
            "scanPolicy": "weak-signal-deep-scan",
            "normalScan": {
                "pageSize": page_size,
                "maxPages": max_pages,
                "maxDetailsPerPlayer": max_details,
            },
            "weakSignalScan": _dashboard_weak_scan_limits(
                page_size=page_size,
                max_pages=max_pages,
                max_details=max_details,
            ),
            "signalRefreshCooldownSeconds": _dashboard_signal_refresh_cooldown_seconds(),
            "playersSelectedForRefresh": [
                _dashboard_player_label(player) for _, player in selected_players
            ],
            "playersDeepScanned": [
                player
                for player, limits in selected_scan_limits.items()
                if limits.get("profile") == "weak"
            ],
            "selectedScanLimits": selected_scan_limits,
            "playersRefreshedWithUsableStats": refreshed_count,
            "playersServedFromLastGoodCache": last_good_count,
            "playersWithoutUsableStats": limited_uncached_count,
            "playerCacheTtlSeconds": player_cache_ttl_seconds,
            "playerCacheFileEnabled": _dashboard_player_cache_file() is not None,
            "matchDetailCacheTtlSeconds": _dashboard_match_detail_cache_ttl_seconds(),
            "matchDetailCacheEntries": len(_DASHBOARD_MATCH_DETAIL_CACHE),
            "matchDetailsMirrored": len(match_details_for_mirror),
            "matchDetailsMirrorLimit": _dashboard_match_details_mirror_limit(),
            "matchDetailCacheFileEnabled": _dashboard_match_detail_cache_file() is not None,
        },
    }

    convex_mirror = await _dashboard_mirror_snapshot_to_convex(snapshot)
    if convex_mirror is not None:
        snapshot["convexMirror"] = convex_mirror

    _DASHBOARD_CACHE = snapshot
    _DASHBOARD_CACHE_KEY = cache_key
    _DASHBOARD_CACHE_EXPIRES_AT = now_ts + cache_seconds

    return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def screen_candidates(
    region: Region,
    candidates: list[dict[str, str]],
    platform: Platform = "pc",
    min_kd: float | None = None,
    max_agent_pool: int | None = None,
    match_count: int = 10,
) -> list[dict[str, Any]]:
    """Screen a supplied list of candidate players by recent K/D and agent-pool size.

    For each candidate (a dict with 'name' and 'tag'; entries missing either
    are skipped) this fetches recent form over the last match_count matches
    and keeps the candidate only if every provided filter passes:
      - min_kd: keep candidates whose recent K/D is at least this value
        (candidates with no computable K/D are dropped when this is set).
      - max_agent_pool: keep candidates who used at most this many distinct
        agents in the window (a proxy for role stability).
    Omit a filter to skip that check; with no filters every resolvable
    candidate is returned with its stats for manual triage.

    Returns one entry per passing candidate: name, tag, kd, agents_used,
    form label (hot/stable/struggling/cold), and the full compact summary.
    Heavy: fetches match history plus per-match details for every candidate.
    """
    found: list[dict[str, Any]] = []
    for c in candidates:
        name = c.get("name")
        tag = c.get("tag")
        if not name or not tag:
            continue
        form = await get_recent_form(region, name, tag, platform, match_count)
        summary = form.get("summary", {}) or {}
        kd = summary.get("kd")
        agents_used = len(summary.get("agents", {}) or {})
        if min_kd is not None and (kd is None or kd < min_kd):
            continue
        if max_agent_pool is not None and agents_used > max_agent_pool:
            continue
        found.append(
            {
                "name": name,
                "tag": tag,
                "kd": kd,
                "agents_used": agents_used,
                "form": form.get("form"),
                "summary": summary,
            }
        )
    return found


# ---------------------------------------------------------------------------
# CSO-Aligned Friendly Tool Names
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_rank(
    region: Region,
    name: str | None = None,
    tag: str | None = None,
    puuid: str | None = None,
    platform: Platform = "pc",
) -> dict[str, Any]:
    """Retrieve a player's current Valorant rank / MMR by Riot ID or PUUID.

    Returns current tier, RR, peak rank, and seasonal MMR details from the
    Henrik v3 MMR endpoint. Supply exactly one identification form: name and
    tag together, or puuid alone. Any other combination returns an error dict.

    Note on response shape: the name+tag path returns the raw Henrik envelope
    ({status, data}); the puuid path returns the unwrapped data object.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        name: In-game name. Must be paired with tag.
        tag: Tag line without '#'. Must be paired with name.
        puuid: Player unique identifier — mutually exclusive with name/tag.
        platform: 'pc' (default) or 'console'.
    """
    error = _player_identity_error(name, tag, puuid)
    if error:
        return error
    if puuid:
        return await mmr.get_mmr_by_puuid(region, puuid, platform)
    return await get_mmr_v3(region, name, tag, platform)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_rank_history(
    region: Region,
    puuid: str,
    platform: Platform = "pc",
) -> dict[str, Any]:
    """Retrieve a player's ranked rating (RR) change history by PUUID.

    Returns one entry per recent competitive match with tier, RR change, map,
    and date (raw Henrik v2 mmr-history envelope). For history going further
    back, use get_stored_mmr_history.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        puuid: Player unique identifier (get it via get_account).
        platform: 'pc' (default) or 'console'.
    """
    return await get_mmr_history_by_puuid(region, puuid, platform)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_live_status(region: Region) -> dict[str, Any]:
    """Retrieve current Valorant platform status for a region.

    Returns active maintenances and incidents from Riot's status API (v1
    status endpoint). For matchmaking queue availability use get_queue_status.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
    """
    return await get_server_status(region)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_static_content(content: str = "agents", locale: str | None = None) -> dict[str, Any]:
    """Retrieve one slice of Valorant static content (agents, maps, skins, ...).

    Returns {version, <slice>: [...]} from the Henrik v1 content payload,
    which is cached per locale in-process. Supported content values: agents,
    maps, skins, sprays, buddies, player_cards, player_titles, seasons,
    game_modes. Note that 'skins' is a very large list. 'buddies' maps to the
    upstream 'charms' key and 'seasons' to 'acts' (with a defensive fallback
    if the upstream key name differs).

    Args:
        content: Which slice to return (default 'agents').
        locale: Optional locale code (e.g. 'en-US') for localized names.
    """
    # Candidate upstream keys per content type, in preference order. Riot's
    # content payload calls weapon buddies "charms" and seasons "acts"; the
    # secondary keys are kept as a defensive fallback.
    content_map: dict[str, tuple[str, ...]] = {
        "agents": ("characters",),
        "characters": ("characters",),
        "maps": ("maps",),
        "skins": ("skins",),
        "sprays": ("sprays",),
        "buddies": ("charms", "buddies"),
        "charms": ("charms", "buddies"),
        "player_cards": ("playerCards",),
        "player_titles": ("playerTitles",),
        "seasons": ("acts", "seasons"),
        "acts": ("acts", "seasons"),
        "game_modes": ("gameModes",),
    }
    keys = content_map.get(content)
    if not keys:
        return {
            "error": True,
            "message": f"Unsupported content type: {content}",
            "supported_content": sorted(content_map),
        }
    payload = await get_valorant_content(locale)
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    for key in keys:
        if isinstance(data, dict) and data.get(key):
            return _content_slice(payload, key)
    return _content_slice(payload, keys[0])


# ---------------------------------------------------------------------------
# HenrikDev Full API Wrapper Tools
# ---------------------------------------------------------------------------

# Content

# The v1 content payload only changes with game versions, so successful
# responses are cached in-process per locale.
_CONTENT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CONTENT_CACHE_TTL_SECONDS = max(
    0.0, float(os.getenv("HENRIK_CONTENT_CACHE_TTL_SECONDS", "21600"))
)


def clear_content_cache() -> None:
    _CONTENT_CACHE.clear()


# Internal helper (no longer a registered tool): used by get_static_content
# and the static-content MCP resources below.
async def get_valorant_content(locale: str | None = None) -> dict[str, Any]:
    cache_key = str(locale or "").lower()
    entry = _CONTENT_CACHE.get(cache_key)
    if entry and entry[0] > time.monotonic():
        return entry[1]

    payload = await _henrik_get("/valorant/v1/content", {"locale": locale})
    if (
        _CONTENT_CACHE_TTL_SECONDS > 0
        and isinstance(payload, dict)
        and not payload.get("error")
    ):
        _CONTENT_CACHE[cache_key] = (
            time.monotonic() + _CONTENT_CACHE_TTL_SECONDS,
            payload,
        )
    return payload


# Internal helpers (no longer registered tools): used by the MCP resources below.
async def get_agents(locale: str | None = None) -> dict[str, Any]:
    return _content_slice(await get_valorant_content(locale), "characters")


async def get_maps(locale: str | None = None) -> dict[str, Any]:
    return _content_slice(await get_valorant_content(locale), "maps")


async def get_seasons(locale: str | None = None) -> dict[str, Any]:
    return _content_slice(await get_valorant_content(locale), "acts")


async def get_game_modes(locale: str | None = None) -> dict[str, Any]:
    return _content_slice(await get_valorant_content(locale), "gameModes")


@mcp.resource("valorant://agents", description="Valorant agent static content")
async def valorant_agents_resource() -> str:
    return json.dumps(await get_agents(), indent=2, ensure_ascii=False)


@mcp.resource("valorant://maps", description="Valorant map static content")
async def valorant_maps_resource() -> str:
    return json.dumps(await get_maps(), indent=2, ensure_ascii=False)


@mcp.resource("valorant://seasons", description="Valorant season and act static content")
async def valorant_seasons_resource() -> str:
    return json.dumps(await get_seasons(), indent=2, ensure_ascii=False)


@mcp.resource("valorant://game_modes", description="Valorant game mode static content")
async def valorant_game_modes_resource() -> str:
    return json.dumps(await get_game_modes(), indent=2, ensure_ascii=False)


# Matches

# Internal helper (no longer a registered tool): full-payload name+tag fetch
# used by the playtime/activity collectors.
async def get_match_history_v4(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
    mode: str | None = None,
    map_name: str | None = None,
    size: int | None = None,
    start: int | None = None,
) -> dict[str, Any]:
    """Return the raw Henrik v4 match-history envelope by Riot ID.

    Large payload with full match summaries. The registered get_match_history
    tool adds puuid support and a compact mode on top of this.
    """
    try:
        safe_name, safe_tag = _riot_id_path(name, tag)
    except ValueError as exc:
        return _riot_id_error(exc, name=name, tag=tag)
    return await _henrik_get(
        f"/valorant/v4/matches/{region}/{platform}/{safe_name}/{safe_tag}",
        {"mode": mode, "map": map_name, "size": size, "start": start},
    )


# Internal helper (no longer a registered tool): full-payload puuid path of
# the merged get_match_history tool.
async def get_match_history_by_puuid(
    region: Region,
    puuid: str,
    platform: Platform = "pc",
    mode: str | None = None,
    map_name: str | None = None,
    size: int | None = None,
    start: int | None = None,
) -> dict[str, Any]:
    return await _henrik_get(
        f"/valorant/v4/by-puuid/matches/{region}/{platform}/{puuid}",
        {"mode": mode, "map": map_name, "size": size, "start": start},
    )


# Internal helper (no longer a registered tool): compact name+tag path of the
# merged get_match_history tool; also used by the playtime/backfill collectors.
async def get_match_history_v4_trimmed(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
    mode: str | None = None,
    map_name: str | None = None,
    size: int | None = 3,
    start: int | None = None,
) -> dict[str, Any]:
    """Return a compact, small-payload v4 match-history page by Riot ID.

    The response is hard-capped to five matches and removes the large nested
    match/player payload. Fetch detailed data only after selecting a match_id.
    """
    try:
        safe_name, safe_tag = _riot_id_path(name, tag)
    except ValueError as exc:
        return _riot_id_error(exc, name=name, tag=tag)
    safe_size = _clamped_matchlist_size(size)
    payload = await _henrik_get(
        f"/valorant/v4/matches/{region}/{platform}/{safe_name}/{safe_tag}",
        {"mode": mode, "map": map_name, "size": safe_size, "start": start},
    )
    return _compact_match_history_response(
        payload,
        region=region,
        platform=platform,
        requested_size=safe_size,
        source_tool="get_match_history_v4_trimmed",
    )


# Internal helper (no longer a registered tool): compact puuid path of the
# merged get_match_history tool; also used by the backfill collectors.
async def get_match_history_by_puuid_trimmed(
    region: Region,
    puuid: str,
    platform: Platform = "pc",
    mode: str | None = None,
    map_name: str | None = None,
    size: int | None = 3,
    start: int | None = None,
) -> dict[str, Any]:
    """Return a compact, small-payload v4 match-history page by PUUID.

    Backs the compact puuid path of the merged get_match_history tool.
    The response is hard-capped to five matches.
    """
    safe_size = _clamped_matchlist_size(size)
    payload = await _henrik_get(
        f"/valorant/v4/by-puuid/matches/{region}/{platform}/{puuid}",
        {"mode": mode, "map": map_name, "size": safe_size, "start": start},
    )
    return _compact_match_history_response(
        payload,
        region=region,
        platform=platform,
        requested_size=safe_size,
        target_puuid=puuid,
        source_tool="get_match_history_by_puuid_trimmed",
    )


# Internal helper (no longer a registered tool): raw, uncached v4 match detail
# fetch backing the dashboard's cached match-detail lookup.
async def get_match_details_v4(region: Region, match_id: str) -> dict[str, Any]:
    """Return the raw, uncached Henrik v4 match-detail envelope.

    Very large payload. The registered get_match tool returns the same data
    cached and unwrapped.
    """
    return await _henrik_get(f"/valorant/v4/match/{region}/{match_id}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_stored_matches(
    region: Region,
    name: str | None = None,
    tag: str | None = None,
    puuid: str | None = None,
    mode: str | None = None,
    map_name: str | None = None,
    page: int | None = None,
    size: int | None = None,
    compact: bool = True,
) -> dict[str, Any]:
    """Retrieve a player's long-term stored match list by Riot ID or PUUID.

    Stored matches are Henrik's persisted per-player match archive (v1
    stored-matches) — lightweight rows going further back than the live v4
    match history. Supply exactly one identification form: name and tag
    together, or puuid alone. Any other combination returns an error dict.

    With compact=True (the default) the response is trimmed to at most five
    rows containing match_id, map, mode, start time, game length, and team
    scores; prefer this for LLM/agent workflows. With compact=False the raw
    Henrik envelope is returned with full stored-match rows.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        name: In-game name. Must be paired with tag.
        tag: Tag line without '#'. Must be paired with name.
        puuid: Player unique identifier — mutually exclusive with name/tag.
        mode: Optional game mode filter (e.g. 'competitive').
        map_name: Optional map name filter (e.g. 'Ascent').
        page: Optional 1-indexed page number (requires size upstream).
        size: Rows per page. Compact responses default to 3 and are
            hard-capped at 5; full responses pass the value through unchanged.
        compact: Return the trimmed payload (default True).
    """
    error = _player_identity_error(name, tag, puuid)
    if error:
        return error
    if puuid:
        path = f"/valorant/v1/by-puuid/stored-matches/{region}/{puuid}"
    else:
        try:
            safe_name, safe_tag = _riot_id_path(name, tag)
        except ValueError as exc:
            return _riot_id_error(exc, name=name, tag=tag)
        path = f"/valorant/v1/stored-matches/{region}/{safe_name}/{safe_tag}"
    if not compact:
        return await _henrik_get(
            path, {"mode": mode, "map": map_name, "page": page, "size": size}
        )
    safe_size = _clamped_matchlist_size(size)
    payload = await _henrik_get(
        path, {"mode": mode, "map": map_name, "page": page, "size": safe_size}
    )
    return _compact_match_history_response(
        payload,
        region=region,
        platform=None,
        requested_size=safe_size,
        target_puuid=puuid,
        source_tool="get_stored_matches",
    )


# MMR

# Internal helper (no longer a registered tool): raw v3 MMR fetch backing the
# name+tag path of get_rank and the trial-readiness scorer.
async def get_mmr_v3(region: Region, name: str, tag: str, platform: Platform = "pc") -> dict[str, Any]:
    """Return the raw Henrik v3 MMR envelope (tier, RR, peak, seasonal) by Riot ID."""
    try:
        safe_name, safe_tag = _riot_id_path(name, tag)
    except ValueError as exc:
        return _riot_id_error(exc, name=name, tag=tag)
    return await _henrik_get(f"/valorant/v3/mmr/{region}/{platform}/{safe_name}/{safe_tag}")


# Internal helper (no longer a registered tool): raw v1 RR-change history used
# by the dashboard rank collector.
async def get_mmr_history_v1(region: Region, name: str, tag: str) -> dict[str, Any]:
    """Return per-match RR changes (raw Henrik v1 mmr-history envelope) by Riot ID."""
    try:
        safe_name, safe_tag = _riot_id_path(name, tag)
    except ValueError as exc:
        return _riot_id_error(exc, name=name, tag=tag)
    return await _henrik_get(f"/valorant/v1/mmr-history/{region}/{safe_name}/{safe_tag}")


# Internal helper (no longer a registered tool): raw v2 RR-change history
# backing the registered get_rank_history tool.
async def get_mmr_history_by_puuid(region: Region, puuid: str, platform: Platform = "pc") -> dict[str, Any]:
    """Return the raw Henrik v2 mmr-history envelope by PUUID."""
    return await _henrik_get(f"/valorant/v2/by-puuid/mmr-history/{region}/{platform}/{puuid}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_stored_mmr_history(
    region: Region,
    name: str | None = None,
    tag: str | None = None,
    puuid: str | None = None,
    platform: Platform = "pc",
    page: int | None = None,
    size: int | None = None,
) -> dict[str, Any]:
    """Retrieve a player's long-term stored ranked-rating (RR) history.

    Returns Henrik's persisted v2 stored MMR history — one row per ranked
    match with tier, RR change, and date — going further back than the live
    RR history (get_rank_history). Supply exactly one identification form:
    name and tag together, or puuid alone. Any other combination returns an
    error dict. Response is the raw Henrik envelope; use page/size to
    paginate long histories.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        name: In-game name. Must be paired with tag.
        tag: Tag line without '#'. Must be paired with name.
        puuid: Player unique identifier — mutually exclusive with name/tag.
        platform: 'pc' (default) or 'console'.
        page: Optional 1-indexed page number.
        size: Optional rows per page.
    """
    error = _player_identity_error(name, tag, puuid)
    if error:
        return error
    if puuid:
        path = f"/valorant/v2/by-puuid/stored-mmr-history/{region}/{platform}/{puuid}"
    else:
        try:
            safe_name, safe_tag = _riot_id_path(name, tag)
        except ValueError as exc:
            return _riot_id_error(exc, name=name, tag=tag)
        path = f"/valorant/v2/stored-mmr-history/{region}/{platform}/{safe_name}/{safe_tag}"
    return await _henrik_get(path, {"page": page, "size": size})


# Premier

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_premier_team(
    team_id: str | None = None,
    team_name: str | None = None,
    team_tag: str | None = None,
    history: bool = False,
) -> dict[str, Any]:
    """Retrieve a Valorant Premier team's details or match history.

    Identify the team by exactly one form: team_id alone, or team_name and
    team_tag together. Any other combination returns an error dict.

    With history=False (the default) returns the team detail payload (roster,
    division, ranking, stats). With history=True returns the team's Premier
    league match history instead.

    Args:
        team_id: Premier team UUID — mutually exclusive with team_name/team_tag.
        team_name: Premier team name. Must be paired with team_tag.
        team_tag: Premier team tag. Must be paired with team_name.
        history: Return the team's match history instead of team details.
    """
    if team_id and not team_name and not team_tag:
        path = f"/valorant/v1/premier/{team_id}"
    elif team_name and team_tag and not team_id:
        path = f"/valorant/v1/premier/{team_name}/{team_tag}"
    else:
        return {
            "error": True,
            "message": (
                "Provide exactly one identification form: team_id alone, or "
                "team_name and team_tag together."
            ),
            "received": {
                "team_id": team_id,
                "team_name": team_name,
                "team_tag": team_tag,
            },
        }
    if history:
        path = f"{path}/history"
    return await _henrik_get(path)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def search_premier_teams(name: str | None = None, tag: str | None = None, division: int | None = None) -> dict[str, Any]:
    """Search Valorant Premier teams by name, tag, and/or division.

    Returns matching teams with their ids; use a returned team id with
    get_premier_team for full details or match history.

    Args:
        name: Optional Premier team name to search for.
        tag: Optional Premier team tag to search for.
        division: Optional division number to filter by.
    """
    return await _henrik_get("/valorant/v1/premier/search", {"name": name, "tag": tag, "division": division})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_premier_conferences() -> dict[str, Any]:
    """List all Valorant Premier conferences (ids, names, regions, timezones).

    Use a returned conference id with get_premier_leaderboard to narrow a
    regional board.
    """
    return await _henrik_get("/valorant/v1/premier/conferences")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_premier_seasons(region: Region) -> dict[str, Any]:
    """List Valorant Premier seasons for a region, with event windows and dates.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
    """
    return await _henrik_get(f"/valorant/v1/premier/seasons/{region}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_premier_leaderboard(
    region: Region,
    conference: str | None = None,
    division: int | None = None,
) -> dict[str, Any]:
    """Retrieve the Valorant Premier leaderboard for a region.

    Narrow the board by conference, and further by division within that
    conference. Passing division without conference returns an error dict.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
        conference: Optional Premier conference id (see get_premier_conferences).
        division: Optional division number within the conference; requires
            conference to be set.
    """
    if division is not None and conference is None:
        return {
            "error": True,
            "message": "division requires conference to be set as well.",
            "received": {"conference": conference, "division": division},
        }
    path = f"/valorant/v1/premier/leaderboard/{region}"
    if conference is not None:
        path = f"{path}/{conference}"
        if division is not None:
            path = f"{path}/{division}"
    return await _henrik_get(path)


# Queue / Status / Version / Store / Website

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_queue_status(region: Region) -> dict[str, Any]:
    """List matchmaking queue availability for a region.

    Returns each queue (competitive, unrated, swiftplay, ...) with whether it
    is enabled plus mode metadata such as team size and party requirements.
    For maintenance/incident status use get_live_status instead.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
    """
    return await _henrik_get(f"/valorant/v1/queue-status/{region}")


# Internal helper (no longer a registered tool): raw platform-status fetch
# backing the registered get_live_status tool.
async def get_server_status(region: Region) -> dict[str, Any]:
    """Return the raw Henrik v1 platform-status envelope for a region."""
    return await _henrik_get(f"/valorant/v1/status/{region}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_valorant_version(region: Region) -> dict[str, Any]:
    """Return the current Valorant client version and branch for a region.

    Small payload: version string, client build date, and branch.

    Args:
        region: Server region — eu, na, latam, br, ap, or kr.
    """
    return await _henrik_get(f"/valorant/v1/version/{region}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_store_featured_v2() -> dict[str, Any]:
    """Return the current featured Valorant store bundles (v2 endpoint).

    Includes bundle names, prices, item contents, and time remaining. This is
    the global featured store, not a specific player's storefront.
    """
    return await _henrik_get("/valorant/v2/store-featured")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_valorant_news(countrycode: str = "en-us") -> dict[str, Any]:
    """Return recent news articles from the official Valorant website.

    Each entry has title, URL, banner image, category, and publish date.

    Args:
        countrycode: Site locale in 'xx-xx' form (e.g. 'en-us', 'de-de').
    """
    return await _henrik_get(f"/valorant/v1/website/{countrycode}")





# ---------------------------------------------------------------------------
# Phase 3 – CSO Academy / Scouting Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_academy_weekly_playtime_report(
    players: list[dict[str, str]],
    default_region: Region = "eu",
    default_platform: Platform = "pc",
    mode: str | None = None,
    page_size: int = 10,
    max_pages: int = 10,
) -> dict[str, Any]:
    """Weekly CSO Academy playtime report for multiple players.

    Each player dict should contain name and tag.
    Optional per-player fields: region, platform.
    """
    reports: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for player in players:
        name = player.get("name")
        tag = player.get("tag")
        region = player.get("region", default_region)
        platform = player.get("platform", default_platform)

        if not name or not tag:
            errors.append({"input": player, "error": "missing name/tag"})
            continue

        try:
            report = await get_weekly_activity_report(
                region=region,
                name=name,
                tag=tag,
                platform=platform,
                mode=mode,
                page_size=page_size,
                max_pages=max_pages,
            )
            reports.append(report)
        except Exception as exc:
            errors.append({
                "input": player,
                "error": str(exc),
            })

    ranked = sorted(
        reports,
        key=lambda r: int(r.get("matches_counted") or 0),
        reverse=True,
    )

    inactive = [r for r in ranked if int(r.get("matches_counted") or 0) == 0]
    low_volume = [r for r in ranked if 0 < int(r.get("matches_counted") or 0) < 5]

    return {
        "players_checked": len(players),
        "reports": ranked,
        "inactive_players": inactive,
        "low_volume_players": low_volume,
        "errors": errors,
        "notes": [
            "Use confidence and audit tools before making roster decisions.",
            "This is activity intelligence, not a replacement for VOD or trial review.",
        ],
    }


# Internal helper: derives a role profile from an already-fetched playtime
# report so the scouting tools can share one fetch per player. Its output is
# embedded verbatim in get_trial_readiness_score responses (role_profile key).
def _role_profile_from_report(report: dict[str, Any]) -> dict[str, Any]:
    """Estimate a player role profile from a get_player_playtime report."""
    agent_counts = _cso_agent_counts_from_report(report)
    role = _cso_role_from_agents(agent_counts)
    agent_pool_size = len(agent_counts)

    if agent_pool_size == 0:
        role_stability = "unknown"
    elif agent_pool_size <= 2:
        role_stability = "high"
    elif agent_pool_size <= 4:
        role_stability = "medium"
    else:
        role_stability = "wide_pool"

    return {
        "player": report.get("player"),
        "window": report.get("window"),
        "matches_counted": report.get("matches_counted"),
        "agent_counts": agent_counts,
        "agent_pool_size": agent_pool_size,
        "primary_role": role["primary_role"],
        "role_counts": role["role_counts"],
        "role_stability": role_stability,
        "confidence": report.get("confidence"),
        "notes": (report.get("notes") or [])
        + ([] if agent_counts else ["No agent data could be extracted from the counted matches."]),
    }


# Internal helper: scores consistency from an already-fetched playtime report
# and its derived role profile, so the scouting tools can share one fetch per
# player. Its output is embedded verbatim in get_trial_readiness_score
# responses (consistency key).
def _consistency_from_report(
    activity: dict[str, Any],
    role: dict[str, Any],
    days: int,
) -> dict[str, Any]:
    """Score player consistency from active days, match volume, playtime and role stability."""
    matches = int(activity.get("matches_counted") or 0)
    active_days = int(activity.get("active_days") or len(activity.get("daily_breakdown", {}) or {}))
    total_seconds = int(activity.get("total_playtime_seconds") or 0)
    agent_pool_size = int(role.get("agent_pool_size") or 0)

    match_score = min(matches / 20, 1.0)
    active_day_score = min((active_days / max(days, 1)) / 0.6, 1.0)
    playtime_score = min(total_seconds / (10 * 3600), 1.0)

    if agent_pool_size <= 2:
        role_score = 1.0
    elif agent_pool_size <= 4:
        role_score = 0.75
    else:
        role_score = 0.45

    score = round(
        (
            match_score * 0.30
            + active_day_score * 0.30
            + playtime_score * 0.25
            + role_score * 0.15
        ) * 100
    )

    return {
        "player": activity.get("player"),
        "score": score,
        "rating": "high" if score >= 75 else "medium" if score >= 50 else "low",
        "matches_counted": matches,
        "active_days": active_days,
        "total_playtime_hhmmss": activity.get("total_playtime_hhmmss"),
        "agent_pool_size": agent_pool_size,
        "role_stability": role.get("role_stability"),
        "confidence": activity.get("confidence"),
        "notes": activity.get("notes", []),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_trial_readiness_score(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
    days: int = 14,
    page_size: int = 10,
    max_pages: int = 10,
) -> dict[str, Any]:
    """CSO scouting readiness score.

    Combines activity consistency, role stability, MMR and recent form.
    This is scouting support only. Human coach review is required.

    The activity window is fetched once per player and shared by the
    consistency and role-profile computations, keeping upstream request
    volume bounded.
    """
    activity_report = await get_player_playtime(
        region=region,
        name=name,
        tag=tag,
        platform=platform,
        days=days,
        mode=None,
        page_size=page_size,
        max_pages=max_pages,
    )
    role = _role_profile_from_report(activity_report)
    consistency = _consistency_from_report(activity_report, role, days)

    recent = await get_recent_form(region, name, tag, platform, 10)
    mmr_payload = await get_mmr_v3(region, name, tag, platform)

    kd = recent.get("summary", {}).get("kd", 0) or 0
    try:
        kd_float = float(kd)
    except Exception:
        kd_float = 0.0

    kd_score = min(kd_float / 1.25, 1.0) * 100
    activity_score = float(consistency.get("score") or 0)

    role_stability = role.get("role_stability")
    role_score = 100 if role_stability == "high" else 75 if role_stability == "medium" else 55

    final_score = round(
        activity_score * 0.45
        + kd_score * 0.35
        + role_score * 0.20
    )

    return {
        "player": f"{name}#{tag}",
        "trial_readiness_score": final_score,
        "rating": "trial_ready" if final_score >= 75 else "watchlist" if final_score >= 55 else "not_ready",
        "consistency": consistency,
        "role_profile": role,
        "recent_form": recent,
        "mmr": mmr_payload,
        "human_review_required": True,
        "notes": [
            "Score supports scouting triage only.",
            "Confirm with VOD review, comms review, trial block, and coach judgement.",
        ],
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def compare_players(
    players: list[dict[str, str]],
    default_region: Region = "eu",
    default_platform: Platform = "pc",
    days: int = 14,
    page_size: int = 10,
    max_pages: int = 10,
) -> dict[str, Any]:
    """Compare candidate players side-by-side for CSO Academy scouting."""
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for player in players:
        name = player.get("name")
        tag = player.get("tag")
        region = player.get("region", default_region)
        platform = player.get("platform", default_platform)

        if not name or not tag:
            errors.append({"input": player, "error": "missing name/tag"})
            continue

        try:
            score = await get_trial_readiness_score(
                region=region,
                name=name,
                tag=tag,
                platform=platform,
                days=days,
                page_size=page_size,
                max_pages=max_pages,
            )
            results.append(score)
        except Exception as exc:
            errors.append({
                "input": player,
                "error": str(exc),
            })

    ranked = sorted(
        results,
        key=lambda item: int(item.get("trial_readiness_score") or 0),
        reverse=True,
    )

    return {
        "players_compared": len(players),
        "ranked": ranked,
        "errors": errors,
        "human_review_required": True,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def get_player_playtime(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
    days: int = 7,
    mode: str | None = None,
    page_size: int = 10,
    max_pages: int = 10,
    include_matches: bool = False,
) -> dict[str, Any]:
    """Calculate player playtime over a date window using v4 match metadata.

    Uses v4 match history metadata.started_at and metadata.game_length_in_ms.
    This is designed for weekly reporting and is more accurate than
    match-count estimates. Returns totals, daily/mode breakdowns, and agent
    counts; the per-match row list is omitted by default to keep the payload
    small (a wide scan can cover up to 250 matches).

    Args:
        region: Server region.
        name: Riot name.
        tag: Riot tag without '#'.
        platform: pc or console.
        days: Lookback window in days. Default 7.
        mode: Optional queue filter, e.g. competitive, swiftplay, unrated.
        page_size: Henrik v4 matchlist page size. Docs indicate max 10.
        max_pages: Number of pages to scan.
        include_matches: Also return the per-match rows (default False).
    """
    now, window_start = _playtime_window(days)

    total_seconds = 0
    counted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    agent_lookup_errors: list[dict[str, Any]] = []
    daily: dict[str, dict[str, Any]] = {}
    modes: dict[str, dict[str, Any]] = {}
    agent_counts: dict[str, int] = {}

    seen_match_ids: set[str] = set()
    stopped_due_to_old_match = False

    page_size = max(1, min(int(page_size), 10))
    max_pages = max(1, min(int(max_pages), 25))

    for page in range(max_pages):
        start = page * page_size

        payload = await get_match_history_v4(
            region=region,
            name=name,
            tag=tag,
            platform=platform,
            mode=mode,
            map_name=None,
            size=page_size,
            start=start,
        )

        if payload.get("error"):
            skipped.append({
                "reason": "api_error",
                "page": page,
                "payload": payload,
            })
            break

        matches_list = payload.get("data") or []
        if not isinstance(matches_list, list) or not matches_list:
            break

        for item in matches_list:
            match_id = _extract_match_id(item)
            if match_id and match_id in seen_match_ids:
                continue
            if match_id:
                seen_match_ids.add(match_id)

            started_at = _extract_match_started_at(item)
            length_seconds = _extract_match_length_seconds(item)
            queue_name = _extract_queue_name(item)

            if not started_at:
                skipped.append({
                    "match_id": match_id,
                    "reason": "missing_started_at",
                })
                continue

            if started_at < window_start:
                stopped_due_to_old_match = True
                continue

            if length_seconds is None:
                skipped.append({
                    "match_id": match_id,
                    "started_at": started_at.isoformat(),
                    "reason": "missing_game_length",
                })
                continue

            date_key = started_at.date().isoformat()
            total_seconds += length_seconds

            daily_bucket = daily.setdefault(date_key, {
                "matches": 0,
                "seconds": 0,
                "hhmmss": "00:00:00",
            })
            daily_bucket["matches"] += 1
            daily_bucket["seconds"] += length_seconds
            daily_bucket["hhmmss"] = _format_hhmmss(daily_bucket["seconds"])

            mode_bucket = modes.setdefault(queue_name, {
                "matches": 0,
                "seconds": 0,
                "hhmmss": "00:00:00",
            })
            mode_bucket["matches"] += 1
            mode_bucket["seconds"] += length_seconds
            mode_bucket["hhmmss"] = _format_hhmmss(mode_bucket["seconds"])

            counted_match = {
                "match_id": match_id,
                "started_at": started_at.isoformat(),
                "queue": queue_name,
                "seconds": length_seconds,
                "hhmmss": _format_hhmmss(length_seconds),
            }

            if match_id:
                try:
                    # Served from the shared match-detail cache so repeated
                    # scans (dashboards, scouting) do not refetch details.
                    details = await matches.get_match(region, match_id)
                    player_row = _find_player_in_match(details, name=name, tag=tag)
                    if player_row:
                        agent = _agent_name(player_row)
                        counted_match["agent"] = agent
                        if agent != "Unknown":
                            agent_counts[agent] = agent_counts.get(agent, 0) + 1
                except Exception as exc:
                    agent_lookup_errors.append({
                        "match_id": match_id,
                        "error": str(exc),
                    })

            counted.append(counted_match)

        if stopped_due_to_old_match:
            break

    confidence = "high"
    notes = []

    if skipped:
        confidence = "medium"
        notes.append("Some matches were skipped because metadata was missing or an API page failed.")

    if agent_lookup_errors:
        notes.append("Some match details could not be fetched for agent-role enrichment.")

    if not stopped_due_to_old_match and len(counted) >= page_size * max_pages:
        confidence = "medium"
        notes.append("Scan reached max_pages before confirming the full date window. Increase max_pages for complete coverage.")

    if not counted:
        confidence = "low"
        notes.append("No matches with usable duration metadata were found in the requested window.")

    output: dict[str, Any] = {
        "player": f"{name}#{tag}",
        "region": region,
        "platform": platform,
        "window": {
            "days": days,
            "from": window_start.isoformat(),
            "to": now.isoformat(),
        },
        "mode_filter": mode,
        "total_playtime_seconds": total_seconds,
        "total_playtime_hhmmss": _format_hhmmss(total_seconds),
        "matches_counted": len(counted),
        "matches_skipped": len(skipped),
        "daily_breakdown": daily,
        "mode_breakdown": modes,
        "agent_counts": agent_counts,
        "skipped": skipped[:20],
        "agent_lookup_errors": agent_lookup_errors[:20],
        "confidence": confidence,
        "notes": notes,
    }
    if include_matches:
        output["matches"] = counted
    return output


# Internal helper (no longer a registered tool): used by
# get_academy_weekly_playtime_report.
async def get_weekly_activity_report(
    region: Region,
    name: str,
    tag: str,
    platform: Platform = "pc",
    mode: str | None = None,
    page_size: int = 10,
    max_pages: int = 10,
) -> dict[str, Any]:
    """CSO weekly activity report for one player.

    Reports hours played, match volume, active days, mode split, longest day,
    and confidence. Uses get_player_playtime internally.
    """
    report = await get_player_playtime(
        region=region,
        name=name,
        tag=tag,
        platform=platform,
        days=7,
        mode=mode,
        page_size=page_size,
        max_pages=max_pages,
    )

    daily = report.get("daily_breakdown", {}) or {}
    longest_day = None

    if daily:
        day_key, day_value = max(
            daily.items(),
            key=lambda item: item[1].get("seconds", 0),
        )
        longest_day = {
            "date": day_key,
            **day_value,
        }

    return {
        "player": report.get("player"),
        "region": report.get("region"),
        "platform": report.get("platform"),
        "window": report.get("window"),
        "total_playtime_seconds": report.get("total_playtime_seconds"),
        "total_playtime_hhmmss": report.get("total_playtime_hhmmss"),
        "matches_counted": report.get("matches_counted"),
        "matches_skipped": report.get("matches_skipped"),
        "active_days": len(daily),
        "longest_day": longest_day,
        "daily_breakdown": daily,
        "mode_breakdown": report.get("mode_breakdown", {}),
        "agent_counts": report.get("agent_counts", {}),
        "confidence": report.get("confidence"),
        "notes": report.get("notes", []),
        "audit_available": True,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the Valorant MCP Server using HTTP transport."""
    mcp.settings.host = os.getenv("MCP_HOST", "0.0.0.0")
    mcp.settings.port = int(os.getenv("MCP_PORT", "8000"))

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
