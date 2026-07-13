from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from valorant_mcp_server.tools import analytics


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, **_kwargs):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


def _current_v4_match(*, damage: int | None = 300) -> tuple[dict, dict]:
    player = {
        "puuid": "player-1",
        "name": "Player",
        "tag": "TAG",
        "team_id": "Blue",
        "agent": {"name": "Sova"},
        "stats": {
            "kills": 2,
            "deaths": 1,
            "assists": 1,
            "score": 500,
            **({"damage": {"dealt": damage}} if damage is not None else {}),
        },
    }
    enemy = {
        "puuid": "enemy-1",
        "name": "Enemy",
        "tag": "TAG",
        "team_id": "Red",
        "stats": {"kills": 1, "deaths": 2, "assists": 0, "score": 200},
    }
    match = {
        "metadata": {"match_id": "match-1", "map": {"name": "Ascent"}},
        "players": [player, enemy],
        "teams": [
            {"team_id": "Blue", "won": True, "rounds": {"won": 1, "lost": 0}},
            {"team_id": "Red", "won": False, "rounds": {"won": 0, "lost": 1}},
        ],
        "rounds": [
            {
                "id": 0,
                "winning_team": "Blue",
                "result": "Elimination",
                "stats": [
                    {
                        "player": {"puuid": "player-1", "name": "Player", "tag": "TAG", "team": "Blue"},
                        "stats": {"kills": 2, "score": 500},
                        "damage_events": [{"damage": 300}],
                    }
                ],
            }
        ],
        "kills": [
            {
                "round": 0,
                "time_in_round_in_ms": 1000,
                "killer": {"puuid": "player-1", "name": "Player", "tag": "TAG", "team": "Blue"},
                "victim": {"puuid": "enemy-1", "name": "Enemy", "tag": "TAG", "team": "Red"},
                "assistants": [],
            }
        ],
    }
    return match, player


class AnalyticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        analytics._clear_analytics_cache()
        self.registry = ToolRegistry()
        analytics.register_analytics_tools(self.registry)

    def test_small_sample_confidence_is_low(self) -> None:
        self.assertEqual("low", analytics._confidence(1, []))
        self.assertEqual("medium", analytics._confidence(4, []))
        self.assertEqual("high", analytics._confidence(5, []))

    async def test_missing_damage_is_not_scored_as_zero_adr(self) -> None:
        match, player = _current_v4_match(damage=None)
        fetch = AsyncMock(return_value=([("match-1", match, player)], [], 1))

        with patch.object(analytics, "_recent_full_matches", fetch):
            result = await self.registry.tools["get_multi_match_impact"](
                region="eu", name="Player", tag="TAG"
            )

        self.assertEqual(0, result["matches_scored"])
        self.assertIsNone(result["avg_impact_score"])
        self.assertIsNone(result["per_match"][0]["impact_score"])
        self.assertEqual("missing_damage", result["per_match"][0]["score_status"])

    async def test_missing_killfeed_is_excluded_from_kast(self) -> None:
        match, player = _current_v4_match()
        del match["kills"]
        fetch = AsyncMock(return_value=([("match-1", match, player)], [], 1))

        with patch.object(analytics, "_recent_full_matches", fetch):
            result = await self.registry.tools["get_kast_aggregate"](
                region="eu", name="Player", tag="TAG"
            )

        self.assertEqual(0, result["matches_analysed"])
        self.assertTrue(any(error["reason"] == "missing_killfeed" for error in result["errors"]))

    async def test_bundle_reuses_one_history_and_detail_fetch(self) -> None:
        match, _ = _current_v4_match()
        history = AsyncMock(return_value=[{"metadata": {"match_id": "match-1"}}])
        detail = AsyncMock(return_value=match)

        with (
            patch.object(analytics.matches, "get_match_history", history),
            patch.object(analytics.matches, "get_match", detail),
        ):
            result = await self.registry.tools["get_player_analytics_bundle"](
                region="eu", name="Player", tag="TAG", match_count=1
            )

        self.assertEqual(1, history.await_count)
        self.assertEqual(1, detail.await_count)
        self.assertEqual(
            {"kast", "first_blood", "clutch", "impact", "side_split"},
            set(result["analytics"]),
        )


if __name__ == "__main__":
    unittest.main()
