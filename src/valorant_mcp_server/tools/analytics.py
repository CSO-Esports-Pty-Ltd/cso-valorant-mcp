"""Computed multi-match analytics for CSO coaching workflows.

The Henrik v4 payload has changed shape over time, so these helpers normalize
the fields we need instead of assuming one exact round or kill schema.
"""

from __future__ import annotations

import os
import time
from collections import Counter, OrderedDict, defaultdict
from typing import Any

from mcp.types import ToolAnnotations

from valorant_mcp_server.cso_utils import extract_match_id
from valorant_mcp_server.literals import GameMode, Platform, Region
from valorant_mcp_server.match_utils import (
    agent_name,
    find_player_in_match,
    player_rows_from_match,
    player_stats,
    safe_get,
)
from valorant_mcp_server.tools import matches


MAX_MATCH_COUNT = 10
TRADE_WINDOW_MS = 5000
ANALYTICS_CACHE_TTL_SECONDS = max(
    0.0, float(os.getenv("HENRIK_ANALYTICS_CACHE_TTL_SECONDS", "60"))
)
ANALYTICS_CACHE_MAX_ENTRIES = max(
    1, int(os.getenv("HENRIK_ANALYTICS_CACHE_MAX_ENTRIES", "128"))
)

_AnalyticsRows = list[tuple[str, dict[str, Any], dict[str, Any]]]
_ANALYTICS_QUERY_CACHE: OrderedDict[
    tuple[str, str, str, str, str | None, int],
    tuple[float, _AnalyticsRows, list[dict[str, Any]], int],
] = OrderedDict()

READ_ONLY_ANALYTICS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _clamp_match_count(value: int | None) -> int:
    return max(1, min(_as_int(value, 10), MAX_MATCH_COUNT))


def _clear_analytics_cache() -> None:
    _ANALYTICS_QUERY_CACHE.clear()


def _clean_mode(mode: GameMode | str | None) -> str | None:
    return str(mode) if mode else None


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _rounds(match: dict[str, Any]) -> list[dict[str, Any]]:
    rounds = safe_get(match, "data", "rounds", default=None) or match.get("rounds") or []
    rows = _rows(rounds)
    score_total = sum(_final_score(match).values())
    if score_total and len(rows) > score_total:
        return rows[:score_total]
    return rows


def _teams(match: dict[str, Any]) -> list[dict[str, Any]]:
    teams = safe_get(match, "data", "teams", default=None) or match.get("teams") or []
    if isinstance(teams, dict):
        return [team for team in teams.values() if isinstance(team, dict)]
    return _rows(teams)


def _team_id(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    value = row.get("team") or row.get("team_id") or row.get("teamId")
    return str(value) if value is not None else None


def _team_won(team: dict[str, Any]) -> bool | None:
    for key in ("won", "has_won", "hasWon"):
        if team.get(key) is not None:
            return bool(team.get(key))
    return None


def _final_score(match: dict[str, Any]) -> dict[str, int]:
    score: dict[str, int] = {}
    for team in _teams(match):
        team_id = _team_id(team)
        if not team_id:
            continue
        rounds = team.get("rounds") or {}
        won = rounds.get("won") if isinstance(rounds, dict) else team.get("rounds_won")
        score[team_id] = _as_int(won)
    return score


def _team_rounds(match: dict[str, Any], team_id: str | None) -> int:
    for team in _teams(match):
        if team_id and _team_id(team) == team_id:
            rounds = team.get("rounds") or {}
            if isinstance(rounds, dict):
                total = _as_int(rounds.get("won")) + _as_int(rounds.get("lost"))
                if total:
                    return total
            total = _as_int(team.get("rounds_played") or team.get("roundsPlayed"))
            if total:
                return total
    return max(len(_rounds(match)), 1)


def _player_team_won(match: dict[str, Any], team_id: str | None) -> bool | None:
    for team in _teams(match):
        if team_id and _team_id(team) == team_id:
            return _team_won(team)
    return None


def _display_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("displayName") or value.get("id") or "Unknown")
    return str(value or "Unknown")


def _map_name(match: dict[str, Any]) -> str:
    meta = safe_get(match, "data", "metadata", default=None) or match.get("metadata") or {}
    return _display_name(meta.get("map") or meta.get("map_name") or meta.get("mapName"))


def _person_puuid(person: Any) -> str | None:
    if not isinstance(person, dict):
        return None
    value = (
        person.get("puuid")
        or person.get("player_puuid")
        or person.get("playerPuuid")
        or person.get("id")
    )
    return str(value) if value else None


def _person_display(person: Any) -> str | None:
    if not isinstance(person, dict):
        return str(person) if person else None

    name = (
        person.get("name")
        or person.get("gameName")
        or person.get("game_name")
        or person.get("player_display_name")
        or person.get("display_name")
        or person.get("displayName")
    )
    tag = (
        person.get("tag")
        or person.get("tagLine")
        or person.get("tag_line")
        or person.get("player_display_tag")
        or person.get("display_tag")
    )
    if name and tag:
        return f"{name}#{tag}"

    display = person.get("display") or person.get("display_name") or person.get("displayName")
    return str(display) if display else None


def _person_key(person: Any) -> str | None:
    puuid = _person_puuid(person)
    if puuid:
        return puuid
    display = _person_display(person)
    return display.lower() if display else None


def _target_tokens(player: dict[str, Any], name: str, tag: str) -> tuple[str | None, str]:
    return _person_puuid(player), f"{name}#{tag}".lower()


def _matches_target(person: Any, target_puuid: str | None, target_display: str) -> bool:
    puuid = _person_puuid(person)
    if target_puuid and puuid == target_puuid:
        return True
    display = _person_display(person)
    return bool(display and display.lower() == target_display)


def _same_person(left: Any, right: Any) -> bool:
    left_key = _person_key(left)
    right_key = _person_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _kill_party(kill: dict[str, Any], role: str) -> dict[str, Any]:
    party = kill.get(role)
    if isinstance(party, dict):
        return party

    return {
        "puuid": kill.get(f"{role}_puuid") or kill.get(f"{role}Puuid"),
        "team": kill.get(f"{role}_team") or kill.get(f"{role}Team"),
        "display_name": kill.get(f"{role}_display_name") or kill.get(f"{role}DisplayName"),
    }


def _killer(kill: dict[str, Any]) -> dict[str, Any]:
    return _kill_party(kill, "killer")


def _victim(kill: dict[str, Any]) -> dict[str, Any]:
    return _kill_party(kill, "victim")


def _assistants(kill: dict[str, Any]) -> list[Any]:
    assistants = kill.get("assistants")
    if isinstance(assistants, list):
        rows: list[Any] = list(assistants)
    else:
        rows = []

    for key in ("assistant_puuids", "assistantPuuids"):
        values = kill.get(key)
        if isinstance(values, list):
            rows.extend({"puuid": value} for value in values)

    names = kill.get("assistant_display_names") or kill.get("assistantDisplayNames")
    if isinstance(names, list):
        rows.extend(str(value) for value in names if value)

    return rows


def _time_ms(kill: dict[str, Any]) -> int | None:
    for key in (
        "time_in_round_in_ms",
        "kill_time_in_round",
        "time_in_round_ms",
        "round_time_in_ms",
    ):
        if kill.get(key) is not None:
            return _as_int(kill.get(key))
    return None


def _kill_round_id(kill: dict[str, Any]) -> int | None:
    for key in ("round", "round_id", "roundId", "round_num", "_round_fallback_index"):
        if kill.get(key) is not None:
            return _as_int(kill.get(key))
    return None


def _round_base_id(round_row: dict[str, Any], index: int) -> int:
    for key in ("id", "round", "round_id", "roundId", "round_num", "round_number"):
        if round_row.get(key) is not None:
            return _as_int(round_row.get(key))
    return index


def _round_offset(rounds: list[dict[str, Any]], kills: list[dict[str, Any]]) -> int:
    kill_ids = {_kill_round_id(kill) for kill in kills}
    kill_ids.discard(None)
    if not kill_ids:
        return 0

    round_ids = [_round_base_id(row, index) for index, row in enumerate(rounds)]
    scores = {
        0: len({value for value in round_ids} & kill_ids),
        1: len({value + 1 for value in round_ids} & kill_ids),
        -1: len({value - 1 for value in round_ids} & kill_ids),
    }
    return max(scores, key=lambda offset: (scores[offset], offset == 0))


def _round_key(round_row: dict[str, Any], index: int, offset: int) -> int:
    return _round_base_id(round_row, index) + offset


def _all_kills(match: dict[str, Any], rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = _data(match)
    top_level = _rows(data.get("kills"))
    if top_level:
        return top_level

    output: list[dict[str, Any]] = []
    for index, round_row in enumerate(rounds):
        for kill in _rows(round_row.get("kills")):
            item = dict(kill)
            if _kill_round_id(item) is None:
                item["_round_fallback_index"] = index
            output.append(item)
    return output


def _killfeed_available(match: dict[str, Any], rounds: list[dict[str, Any]]) -> bool:
    data = _data(match)
    if isinstance(data.get("kills"), list):
        return True
    return any(isinstance(round_row.get("kills"), list) for round_row in rounds)


def _kills_by_round(match: dict[str, Any], rounds: list[dict[str, Any]]) -> tuple[dict[int, list[dict[str, Any]]], list[dict[str, Any]], int]:
    kills = _all_kills(match, rounds)
    offset = _round_offset(rounds, kills)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for kill in kills:
        round_id = _kill_round_id(kill)
        if round_id is not None:
            grouped[round_id].append(kill)

    for rows in grouped.values():
        rows.sort(key=lambda kill: (_time_ms(kill) is None, _time_ms(kill) or 0))
    return grouped, kills, offset


def _is_suicide(kill: dict[str, Any]) -> bool:
    if kill.get("is_suicide") is not None:
        return bool(kill.get("is_suicide"))
    return _same_person(_killer(kill), _victim(kill))


def _round_stats(round_row: dict[str, Any]) -> list[dict[str, Any]]:
    return _rows(round_row.get("stats") or round_row.get("player_stats"))


def _round_stat_player(stat: dict[str, Any]) -> dict[str, Any]:
    player = stat.get("player")
    if isinstance(player, dict):
        return player
    return {
        "puuid": stat.get("puuid") or stat.get("player_puuid"),
        "name": stat.get("name") or stat.get("player_display_name"),
        "tag": stat.get("tag") or stat.get("player_display_tag"),
        "display_name": stat.get("player_display_name"),
    }


def _find_round_stat(
    round_row: dict[str, Any],
    target_puuid: str | None,
    target_display: str,
) -> dict[str, Any] | None:
    for stat in _round_stats(round_row):
        if _matches_target(_round_stat_player(stat), target_puuid, target_display):
            return stat
    return None


def _stat_block(row: dict[str, Any]) -> dict[str, Any]:
    stats = row.get("stats")
    return stats if isinstance(stats, dict) else {}


def _stat_value(row: dict[str, Any], *keys: str) -> int:
    stats = _stat_block(row)
    for key in keys:
        if row.get(key) is not None:
            return _as_int(row.get(key))
        if stats.get(key) is not None:
            return _as_int(stats.get(key))
    return 0


def _round_stat_kills(stat: dict[str, Any] | None) -> int:
    if not stat:
        return 0
    kills = stat.get("kills")
    if isinstance(kills, list):
        return len(kills)
    return _stat_value(stat, "kills")


def _round_stat_assists(stat: dict[str, Any] | None) -> int:
    if not stat:
        return 0
    assists = stat.get("assists")
    if isinstance(assists, list):
        return len(assists)
    return _stat_value(stat, "assists")


def _round_stat_score(stat: dict[str, Any] | None) -> int:
    return _stat_value(stat or {}, "score")


def _round_stat_damage(stat: dict[str, Any] | None) -> int:
    if not stat:
        return 0

    stats = _stat_block(stat)
    for value in (stats.get("damage"), stat.get("damage")):
        if isinstance(value, (int, float, str)):
            return _as_int(value)
        if isinstance(value, dict):
            return _as_int(value.get("damage") or value.get("dealt"))
        if isinstance(value, list):
            return sum(_as_int(item.get("damage")) for item in _rows(value))

    damage_events = stat.get("damage_events") or stat.get("damageEvents") or []
    return sum(_as_int(event.get("damage")) for event in _rows(damage_events))


def _player_damage_dealt(player: dict[str, Any]) -> int | None:
    stats = _stat_block(player)
    damage = stats.get("damage")
    if isinstance(damage, dict) and damage.get("dealt") is not None:
        return _as_int(damage.get("dealt"))

    for source in (player, stats, damage if isinstance(damage, dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("dealt", "damage_dealt", "damageMade", "damage_made"):
            if source.get(key) is not None:
                return _as_int(source.get(key))
    return None


def _round_winner(round_row: dict[str, Any]) -> str | None:
    value = round_row.get("winning_team") or round_row.get("winningTeam")
    return str(value) if value is not None else None


def _round_result(round_row: dict[str, Any]) -> str:
    return str(round_row.get("result") or round_row.get("end_type") or round_row.get("endType") or "").lower()


def _team_rosters(match: dict[str, Any]) -> dict[str, set[str]]:
    rosters: dict[str, set[str]] = defaultdict(set)
    for player in player_rows_from_match(match):
        team_id = _team_id(player)
        key = _person_key(player)
        if team_id and key:
            rosters[team_id].add(key)
    return rosters


async def _recent_match_ids(
    *,
    region: Region,
    name: str,
    tag: str,
    platform: Platform,
    mode: GameMode | str | None,
    match_count: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    safe_count = _clamp_match_count(match_count)
    history = await matches.get_match_history(
        region,
        name,
        tag,
        platform=platform,
        mode=_clean_mode(mode),
        map_name=None,
        size=safe_count,
    )
    if not isinstance(history, list):
        return [], [{"reason": "history_error", "payload": history}]

    ids: list[str] = []
    skipped: list[dict[str, Any]] = []
    for index, item in enumerate(history[:safe_count]):
        if not isinstance(item, dict):
            skipped.append({"index": index, "reason": "invalid_history_item"})
            continue
        match_id = extract_match_id(item)
        if match_id:
            ids.append(match_id)
        else:
            skipped.append({"index": index, "reason": "missing_match_id"})
    return ids, skipped


async def _recent_full_matches(
    *,
    region: Region,
    name: str,
    tag: str,
    platform: Platform,
    mode: GameMode | str | None,
    match_count: int,
) -> tuple[list[tuple[str, dict[str, Any], dict[str, Any]]], list[dict[str, Any]], int]:
    safe_count = _clamp_match_count(match_count)
    cache_key = (
        str(region).lower(),
        name.lower(),
        tag.lower(),
        str(platform).lower(),
        _clean_mode(mode),
        safe_count,
    )
    cached = _ANALYTICS_QUERY_CACHE.get(cache_key)
    if cached:
        expires_at, cached_rows, cached_errors, cached_requested = cached
        if expires_at > time.monotonic():
            _ANALYTICS_QUERY_CACHE.move_to_end(cache_key)
            return list(cached_rows), [dict(error) for error in cached_errors], cached_requested
        _ANALYTICS_QUERY_CACHE.pop(cache_key, None)

    ids, errors = await _recent_match_ids(
        region=region,
        name=name,
        tag=tag,
        platform=platform,
        mode=mode,
        match_count=safe_count,
    )

    rows: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for match_id in ids:
        try:
            match = await matches.get_match(region, match_id)
        except Exception as exc:
            errors.append({"match_id": match_id, "reason": "detail_exception", "error": str(exc)})
            continue

        if isinstance(match, dict) and match.get("error"):
            errors.append({"match_id": match_id, "reason": "detail_error", "payload": match})
            continue

        player = find_player_in_match(match, name=name, tag=tag)
        if not player:
            errors.append({"match_id": match_id, "reason": "player_not_found"})
            continue

        rows.append((match_id, match, player))

    if rows and ANALYTICS_CACHE_TTL_SECONDS > 0:
        _ANALYTICS_QUERY_CACHE[cache_key] = (
            time.monotonic() + ANALYTICS_CACHE_TTL_SECONDS,
            list(rows),
            [dict(error) for error in errors],
            safe_count,
        )
        _ANALYTICS_QUERY_CACHE.move_to_end(cache_key)
        while len(_ANALYTICS_QUERY_CACHE) > ANALYTICS_CACHE_MAX_ENTRIES:
            _ANALYTICS_QUERY_CACHE.popitem(last=False)

    return rows, errors, safe_count


def _benchmark_kast(value: float) -> str:
    if value < 65:
        return "low (<65%)"
    if value < 72:
        return "average (65-72%)"
    if value < 80:
        return "strong (72-80%)"
    return "elite (>80%)"


def _benchmark_impact(value: float) -> str:
    if value < 40:
        return "struggling (<40)"
    if value < 55:
        return "average (40-55)"
    if value < 70:
        return "solid (55-70)"
    return "elite (>70)"


def _confidence(matches_counted: int, errors: list[dict[str, Any]]) -> str:
    if matches_counted < 3:
        return "low"
    if matches_counted < 5 or errors:
        return "medium"
    return "high"


def _plant_team(round_row: dict[str, Any]) -> str | None:
    plant = round_row.get("plant")
    if isinstance(plant, dict):
        team = _team_id(plant.get("player")) or plant.get("team") or plant.get("team_id")
        if team:
            return str(team)

    events = round_row.get("plant_events") or round_row.get("plantEvents")
    if isinstance(events, dict):
        planted_by = events.get("planted_by") or events.get("player") or {}
        team = _team_id(planted_by) or events.get("team") or events.get("team_id")
        if team:
            return str(team)
    return None


def _has_defuse(round_row: dict[str, Any]) -> bool:
    return bool(round_row.get("defuse") or round_row.get("defuse_events") or round_row.get("defuseEvents"))


def _half_bucket(index: int) -> str | None:
    if index < 12:
        return "first_half"
    if index < 24:
        return "second_half"
    return None


def _opposite_team(team_id: str | None, teams: set[str]) -> str | None:
    if not team_id:
        return None
    others = [team for team in teams if team != team_id]
    return others[0] if len(others) == 1 else None


def _half_attack_teams(rounds: list[dict[str, Any]], teams: set[str]) -> dict[str, str]:
    counters: dict[str, Counter[str]] = {"first_half": Counter(), "second_half": Counter()}
    for index, round_row in enumerate(rounds):
        bucket = _half_bucket(index)
        plant_team = _plant_team(round_row)
        if bucket and plant_team:
            counters[bucket][plant_team] += 1

    attack: dict[str, str] = {}
    for bucket, counter in counters.items():
        if counter:
            attack[bucket] = counter.most_common(1)[0][0]

    if len(teams) == 2:
        if "first_half" in attack and "second_half" not in attack:
            attack["second_half"] = _opposite_team(attack["first_half"], teams) or attack["first_half"]
        elif "second_half" in attack and "first_half" not in attack:
            attack["first_half"] = _opposite_team(attack["second_half"], teams) or attack["second_half"]
    return attack


def _side_for_round(
    round_row: dict[str, Any],
    *,
    index: int,
    player_team: str,
    half_attack: dict[str, str],
) -> tuple[str | None, str]:
    plant_team = _plant_team(round_row)
    if plant_team:
        return ("attack" if plant_team == player_team else "defense"), "plant"

    winner = _round_winner(round_row)
    result = _round_result(round_row)
    if _has_defuse(round_row) or "defus" in result:
        return ("defense" if winner == player_team else "attack"), "defuse_result"
    if "time" in result and "out" in result:
        return ("defense" if winner == player_team else "attack"), "time_result"

    bucket = _half_bucket(index)
    if bucket and bucket in half_attack:
        return ("attack" if half_attack[bucket] == player_team else "defense"), "half_inference"

    return None, "unknown"


def _empty_side_bucket() -> dict[str, int]:
    return {
        "score": 0,
        "damage": 0,
        "kills": 0,
        "deaths": 0,
        "rounds_won": 0,
        "rounds": 0,
        "plant_tracked": 0,
    }


def _side_summary(bucket: dict[str, int]) -> dict[str, Any]:
    rounds = bucket["rounds"]
    divisor = max(rounds, 1)
    return {
        "rounds": rounds,
        "acs": round(bucket["score"] / divisor, 1),
        "adr": round(bucket["damage"] / divisor, 1),
        "kd": round(bucket["kills"] / max(bucket["deaths"], 1), 2),
        "round_win_rate_pct": round(bucket["rounds_won"] / divisor * 100, 1),
        "plant_tracked_pct": round(bucket["plant_tracked"] / divisor * 100, 1),
    }


def register_analytics_tools(mcp: Any) -> None:
    """Attach computed analytics tools to a FastMCP instance."""

    @mcp.tool(annotations=READ_ONLY_ANALYTICS)
    async def get_kast_aggregate(
        region: Region,
        name: str,
        tag: str,
        platform: Platform = "pc",
        match_count: int = 10,
        mode: GameMode | None = "competitive",
    ) -> dict[str, Any]:
        """Aggregate KAST percentage across recent Henrik v4 match details.

        Heavy: fetches one full match payload per match. match_count is capped
        at 10 as a CSO rate-limit safety measure.
        """
        full_matches, errors, requested = await _recent_full_matches(
            region=region,
            name=name,
            tag=tag,
            platform=platform,
            mode=mode,
            match_count=match_count,
        )

        total_rounds = 0
        kast_rounds = 0
        per_match: list[dict[str, Any]] = []

        for match_id, match, player in full_matches:
            player_team = _team_id(player)
            target_puuid, target_display = _target_tokens(player, name, tag)
            rounds = _rounds(match)
            if not _killfeed_available(match, rounds):
                errors.append({"match_id": match_id, "reason": "missing_killfeed"})
                continue
            grouped_kills, _, offset = _kills_by_round(match, rounds)

            match_total = 0
            match_kast = 0
            traded_rounds = 0

            for index, round_row in enumerate(rounds):
                round_key = _round_key(round_row, index, offset)
                round_kills = grouped_kills.get(round_key, [])
                stat = _find_round_stat(round_row, target_puuid, target_display)
                match_total += 1

                got_kill = _round_stat_kills(stat) > 0 or any(
                    _matches_target(_killer(kill), target_puuid, target_display)
                    for kill in round_kills
                )
                got_assist = _round_stat_assists(stat) > 0 or any(
                    any(_matches_target(assistant, target_puuid, target_display) for assistant in _assistants(kill))
                    for kill in round_kills
                )
                death = next(
                    (
                        kill
                        for kill in round_kills
                        if _matches_target(_victim(kill), target_puuid, target_display)
                    ),
                    None,
                )
                survived = death is None
                traded = False

                if death and player_team:
                    death_time = _time_ms(death)
                    killer_person = _killer(death)
                    if death_time is not None:
                        traded = any(
                            _same_person(_victim(kill), killer_person)
                            and _team_id(_killer(kill)) == player_team
                            and (_time_ms(kill) is not None)
                            and 0 < (_time_ms(kill) or 0) - death_time <= TRADE_WINDOW_MS
                            for kill in round_kills
                        )

                if got_kill or got_assist or survived or traded:
                    match_kast += 1
                if traded:
                    traded_rounds += 1

            total_rounds += match_total
            kast_rounds += match_kast
            per_match.append(
                {
                    "match_id": match_id,
                    "map": _map_name(match),
                    "rounds": match_total,
                    "kast_rounds": match_kast,
                    "traded_rounds": traded_rounds,
                    "kast_pct": round(match_kast / match_total * 100, 1) if match_total else 0,
                }
            )

        kast_pct = round(kast_rounds / total_rounds * 100, 1) if total_rounds else 0
        return {
            "player": f"{name}#{tag}",
            "region": region,
            "platform": platform,
            "mode_filter": mode,
            "matches_requested": requested,
            "matches_analysed": len(per_match),
            "total_rounds": total_rounds,
            "kast_rounds": kast_rounds,
            "kast_pct": kast_pct,
            "benchmark": _benchmark_kast(kast_pct),
            "confidence": _confidence(len(per_match), errors),
            "errors": errors,
            "per_match": per_match,
        }

    @mcp.tool(annotations=READ_ONLY_ANALYTICS)
    async def get_fb_rate_aggregate(
        region: Region,
        name: str,
        tag: str,
        platform: Platform = "pc",
        match_count: int = 10,
        mode: GameMode | None = "competitive",
    ) -> dict[str, Any]:
        """Aggregate first-blood kills, deaths, and conversion across matches.

        Heavy: fetches one full match payload per match. match_count is capped
        at 10 as a CSO rate-limit safety measure.
        """
        full_matches, errors, requested = await _recent_full_matches(
            region=region,
            name=name,
            tag=tag,
            platform=platform,
            mode=mode,
            match_count=match_count,
        )

        fb_kills = 0
        fb_deaths = 0
        fb_kill_wins = 0
        fb_death_wins = 0
        per_match: list[dict[str, Any]] = []

        for match_id, match, player in full_matches:
            player_team = _team_id(player)
            target_puuid, target_display = _target_tokens(player, name, tag)
            rounds = _rounds(match)
            if not _killfeed_available(match, rounds):
                errors.append({"match_id": match_id, "reason": "missing_killfeed"})
                continue
            grouped_kills, _, offset = _kills_by_round(match, rounds)

            match_fbk = match_fbd = match_fbk_w = match_fbd_w = 0

            for index, round_row in enumerate(rounds):
                round_key = _round_key(round_row, index, offset)
                round_kills = [kill for kill in grouped_kills.get(round_key, []) if not _is_suicide(kill)]
                if not round_kills:
                    continue

                first = round_kills[0]
                won = bool(player_team and _round_winner(round_row) == player_team)
                if _matches_target(_killer(first), target_puuid, target_display):
                    match_fbk += 1
                    match_fbk_w += 1 if won else 0
                elif _matches_target(_victim(first), target_puuid, target_display):
                    match_fbd += 1
                    match_fbd_w += 1 if won else 0

            fb_kills += match_fbk
            fb_deaths += match_fbd
            fb_kill_wins += match_fbk_w
            fb_death_wins += match_fbd_w
            per_match.append(
                {
                    "match_id": match_id,
                    "map": _map_name(match),
                    "fb_kills": match_fbk,
                    "fb_deaths": match_fbd,
                    "fb_diff": match_fbk - match_fbd,
                    "fb_kill_round_win_rate": round(match_fbk_w / match_fbk * 100, 1) if match_fbk else None,
                    "fb_death_round_win_rate": round(match_fbd_w / match_fbd * 100, 1) if match_fbd else None,
                }
            )

        return {
            "player": f"{name}#{tag}",
            "region": region,
            "platform": platform,
            "mode_filter": mode,
            "matches_requested": requested,
            "matches_analysed": len(per_match),
            "fb_kills": fb_kills,
            "fb_deaths": fb_deaths,
            "fb_diff": fb_kills - fb_deaths,
            "fb_kill_round_win_rate": round(fb_kill_wins / fb_kills * 100, 1) if fb_kills else None,
            "fb_death_round_win_rate": round(fb_death_wins / fb_deaths * 100, 1) if fb_deaths else None,
            "confidence": _confidence(len(per_match), errors),
            "errors": errors,
            "per_match": per_match,
            "notes": ["First blood excludes suicide events when the payload identifies them."],
        }

    @mcp.tool(annotations=READ_ONLY_ANALYTICS)
    async def get_clutch_rate(
        region: Region,
        name: str,
        tag: str,
        platform: Platform = "pc",
        match_count: int = 10,
        mode: GameMode | None = "competitive",
    ) -> dict[str, Any]:
        """Track 1vX clutch attempts and wins across recent matches.

        Heavy: fetches one full match payload per match. match_count is capped
        at 10 as a CSO rate-limit safety measure.
        """
        full_matches, errors, requested = await _recent_full_matches(
            region=region,
            name=name,
            tag=tag,
            platform=platform,
            mode=mode,
            match_count=match_count,
        )

        attempts: Counter[int] = Counter()
        wins: Counter[int] = Counter()
        per_match: list[dict[str, Any]] = []

        for match_id, match, player in full_matches:
            player_team = _team_id(player)
            target_key = _person_key(player)
            rosters = _team_rosters(match)
            opponent_team = next((team for team in rosters if team != player_team), None)
            if not player_team or not target_key or not opponent_team:
                errors.append({"match_id": match_id, "reason": "missing_team_roster"})
                continue

            rounds = _rounds(match)
            if not _killfeed_available(match, rounds):
                errors.append({"match_id": match_id, "reason": "missing_killfeed"})
                continue
            grouped_kills, _, offset = _kills_by_round(match, rounds)
            match_attempts: Counter[int] = Counter()
            match_wins: Counter[int] = Counter()

            for index, round_row in enumerate(rounds):
                round_key = _round_key(round_row, index, offset)
                alive_friendly = set(rosters[player_team])
                alive_enemy = set(rosters[opponent_team])
                clutch_size: int | None = None

                for kill in grouped_kills.get(round_key, []):
                    victim_key = _person_key(_victim(kill))
                    if victim_key:
                        alive_friendly.discard(victim_key)
                        alive_enemy.discard(victim_key)

                    if (
                        clutch_size is None
                        and target_key in alive_friendly
                        and len(alive_friendly) == 1
                        and len(alive_enemy) >= 2
                    ):
                        clutch_size = len(alive_enemy)

                if clutch_size is None:
                    continue

                match_attempts[clutch_size] += 1
                attempts[clutch_size] += 1
                if _round_winner(round_row) == player_team:
                    match_wins[clutch_size] += 1
                    wins[clutch_size] += 1

            match_total = sum(match_attempts.values())
            match_won = sum(match_wins.values())
            per_match.append(
                {
                    "match_id": match_id,
                    "map": _map_name(match),
                    "clutch_attempts": match_total,
                    "clutch_wins": match_won,
                    "conversion_rate": round(match_won / match_total * 100, 1) if match_total else 0,
                    "breakdown": {
                        f"1v{size}": {
                            "attempts": match_attempts[size],
                            "wins": match_wins[size],
                        }
                        for size in sorted(match_attempts)
                    },
                }
            )

        total_attempts = sum(attempts.values())
        total_wins = sum(wins.values())
        return {
            "player": f"{name}#{tag}",
            "region": region,
            "platform": platform,
            "mode_filter": mode,
            "matches_requested": requested,
            "matches_analysed": len(per_match),
            "total_clutch_attempts": total_attempts,
            "total_clutch_wins": total_wins,
            "overall_conversion_rate": round(total_wins / total_attempts * 100, 1) if total_attempts else 0,
            "by_situation": {
                f"1v{size}": {
                    "attempts": attempts[size],
                    "wins": wins[size],
                    "conversion_rate": round(wins[size] / attempts[size] * 100, 1) if attempts[size] else 0,
                }
                for size in sorted(attempts)
            },
            "confidence": _confidence(len(per_match), errors),
            "errors": errors,
            "per_match": per_match,
            "notes": ["Clutch detection replays death order and does not model revive edge cases."],
        }

    @mcp.tool(annotations=READ_ONLY_ANALYTICS)
    async def get_multi_match_impact(
        region: Region,
        name: str,
        tag: str,
        platform: Platform = "pc",
        match_count: int = 10,
        mode: GameMode | None = "competitive",
    ) -> dict[str, Any]:
        """Compute a rolling 0-100 impact score across recent matches.

        Heavy: fetches one full match payload per match. match_count is capped
        at 10 as a CSO rate-limit safety measure.
        """
        full_matches, errors, requested = await _recent_full_matches(
            region=region,
            name=name,
            tag=tag,
            platform=platform,
            mode=mode,
            match_count=match_count,
        )

        scores: list[float] = []
        per_match: list[dict[str, Any]] = []

        for match_id, match, player in full_matches:
            stats = player_stats(player)
            player_team = _team_id(player)
            rounds = _team_rounds(match, player_team)
            damage_dealt = _player_damage_dealt(player)
            kills = stats["kills"]
            deaths = max(stats["deaths"], 1)
            score = stats["score"]
            acs = score / rounds
            kd = kills / deaths
            won = _player_team_won(match, player_team)

            adr = damage_dealt / rounds if damage_dealt is not None else None
            impact: float | None = None
            score_status = "missing_damage"
            if adr is not None:
                impact = round(
                    min(
                        (acs / 300 * 35)
                        + (adr / 150 * 30)
                        + (min(kd / 1.5, 1.0) * 25)
                        + (10 if won else 0),
                        100.0,
                    ),
                    1,
                )
                scores.append(impact)
                score_status = "scored"
            else:
                errors.append({"match_id": match_id, "reason": "missing_damage"})

            per_match.append(
                {
                    "match_id": match_id,
                    "map": _map_name(match),
                    "agent": agent_name(player),
                    "rounds": rounds,
                    "acs": round(acs, 1),
                    "adr": round(adr, 1) if adr is not None else None,
                    "kd": round(kd, 2),
                    "won": won,
                    "impact_score": impact,
                    "damage_available": damage_dealt is not None,
                    "score_status": score_status,
                }
            )

        avg = round(sum(scores) / len(scores), 1) if scores else None
        trend: str | None = None
        if len(scores) >= 4:
            half = len(scores) // 2
            recent = sum(scores[:half]) / half
            older = sum(scores[half:]) / (len(scores) - half)
            diff = recent - older
            trend = "improving" if diff > 3 else "declining" if diff < -3 else "stable"

        return {
            "player": f"{name}#{tag}",
            "region": region,
            "platform": platform,
            "mode_filter": mode,
            "matches_requested": requested,
            "matches_analysed": len(per_match),
            "matches_scored": len(scores),
            "avg_impact_score": avg,
            "trend": trend,
            "benchmark": _benchmark_impact(avg) if avg is not None else None,
            "confidence": _confidence(len(scores), errors),
            "errors": errors,
            "formula": "Impact = ACS/300*35 + ADR/150*30 + capped KD/1.5*25 + win*10.",
            "per_match": per_match,
        }

    @mcp.tool(annotations=READ_ONLY_ANALYTICS)
    async def get_side_split_stats(
        region: Region,
        name: str,
        tag: str,
        platform: Platform = "pc",
        match_count: int = 10,
        mode: GameMode | None = "competitive",
    ) -> dict[str, Any]:
        """Break down ACS, ADR, K/D, and round win rate by attack/defense.

        Heavy: fetches one full match payload per match. match_count is capped
        at 10 as a CSO rate-limit safety measure.
        """
        full_matches, errors, requested = await _recent_full_matches(
            region=region,
            name=name,
            tag=tag,
            platform=platform,
            mode=mode,
            match_count=match_count,
        )

        aggregate = {"attack": _empty_side_bucket(), "defense": _empty_side_bucket()}
        side_sources: Counter[str] = Counter()
        unknown_rounds = 0
        per_match: list[dict[str, Any]] = []

        for match_id, match, player in full_matches:
            player_team = _team_id(player)
            target_puuid, target_display = _target_tokens(player, name, tag)
            rounds = _rounds(match)
            grouped_kills, _, offset = _kills_by_round(match, rounds)
            teams = {team for team in _team_rosters(match) if team}
            half_attack = _half_attack_teams(rounds, teams)
            match_bucket = {"attack": _empty_side_bucket(), "defense": _empty_side_bucket()}
            match_sources: Counter[str] = Counter()
            match_unknown = 0

            if not player_team:
                errors.append({"match_id": match_id, "reason": "missing_player_team"})
                continue

            for index, round_row in enumerate(rounds):
                side, source = _side_for_round(
                    round_row,
                    index=index,
                    player_team=player_team,
                    half_attack=half_attack,
                )
                match_sources[source] += 1
                side_sources[source] += 1

                if side not in ("attack", "defense"):
                    match_unknown += 1
                    unknown_rounds += 1
                    continue

                stat = _find_round_stat(round_row, target_puuid, target_display)
                if not stat:
                    continue

                round_key = _round_key(round_row, index, offset)
                deaths = sum(
                    1
                    for kill in grouped_kills.get(round_key, [])
                    if _matches_target(_victim(kill), target_puuid, target_display)
                )
                won = _round_winner(round_row) == player_team

                for bucket in (match_bucket[side], aggregate[side]):
                    bucket["rounds"] += 1
                    bucket["score"] += _round_stat_score(stat)
                    bucket["damage"] += _round_stat_damage(stat)
                    bucket["kills"] += _round_stat_kills(stat)
                    bucket["deaths"] += deaths
                    bucket["rounds_won"] += 1 if won else 0
                    bucket["plant_tracked"] += 1 if source == "plant" else 0

            per_match.append(
                {
                    "match_id": match_id,
                    "map": _map_name(match),
                    "attack": _side_summary(match_bucket["attack"]),
                    "defense": _side_summary(match_bucket["defense"]),
                    "unknown_side_rounds": match_unknown,
                    "side_source_breakdown": dict(match_sources),
                }
            )

        attack = _side_summary(aggregate["attack"])
        defense = _side_summary(aggregate["defense"])
        has_both_sides = bool(attack["rounds"] and defense["rounds"])
        acs_diff = round(attack["acs"] - defense["acs"], 1) if has_both_sides else None
        side_preference = (
            "attack"
            if acs_diff is not None and acs_diff > 0
            else "defense"
            if acs_diff is not None and acs_diff < 0
            else "even"
            if acs_diff == 0
            else "unknown"
        )

        return {
            "player": f"{name}#{tag}",
            "region": region,
            "platform": platform,
            "mode_filter": mode,
            "matches_requested": requested,
            "matches_analysed": len(per_match),
            "attack": attack,
            "defense": defense,
            "acs_differential": acs_diff,
            "side_preference": side_preference,
            "unknown_side_rounds": unknown_rounds,
            "side_source_breakdown": dict(side_sources),
            "confidence": _confidence(len(per_match), errors),
            "errors": errors,
            "per_match": per_match,
            "notes": [
                "Side is confirmed from plant/defuse/time-out data when possible.",
                "Half inference uses observed plant teams; unknown rounds are excluded from attack/defense stats.",
            ],
        }

    @mcp.tool(annotations=READ_ONLY_ANALYTICS)
    async def get_player_analytics_bundle(
        region: Region,
        name: str,
        tag: str,
        platform: Platform = "pc",
        match_count: int = 10,
        mode: GameMode | None = "competitive",
    ) -> dict[str, Any]:
        """Return all CSO analytics using one shared history/detail fetch window.

        Match payloads are reused across each calculation through the bounded
        analytics cache, avoiding five independent Henrik request batches.
        """
        common = {
            "region": region,
            "name": name,
            "tag": tag,
            "platform": platform,
            "match_count": match_count,
            "mode": mode,
        }
        kast = await get_kast_aggregate(**common)
        first_blood = await get_fb_rate_aggregate(**common)
        clutch = await get_clutch_rate(**common)
        impact = await get_multi_match_impact(**common)
        side_split = await get_side_split_stats(**common)

        return {
            "player": f"{name}#{tag}",
            "region": region,
            "platform": platform,
            "mode_filter": mode,
            "matches_requested": _clamp_match_count(match_count),
            "cache_window_seconds": ANALYTICS_CACHE_TTL_SECONDS,
            "analytics": {
                "kast": kast,
                "first_blood": first_blood,
                "clutch": clutch,
                "impact": impact,
                "side_split": side_split,
            },
        }
