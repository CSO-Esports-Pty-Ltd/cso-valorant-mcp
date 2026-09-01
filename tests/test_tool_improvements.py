from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import valorant_mcp_server.server as server
from valorant_mcp_server.riot_id import parse_riot_id, riot_id_path
from valorant_mcp_server.tools import leaderboard, matches


class RiotIdHelperTests(unittest.TestCase):
    def test_strips_leading_hash_from_tag(self) -> None:
        self.assertEqual(("TenZ", "SEN"), parse_riot_id("TenZ", "#SEN"))

    def test_strips_whitespace(self) -> None:
        self.assertEqual(("TenZ", "SEN"), parse_riot_id(" TenZ ", " SEN "))

    def test_accepts_combined_riot_id_when_tag_empty(self) -> None:
        self.assertEqual(("TenZ", "SEN"), parse_riot_id("TenZ#SEN", None))

    def test_rejects_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            parse_riot_id("", "SEN")

    def test_rejects_empty_tag(self) -> None:
        with self.assertRaises(ValueError):
            parse_riot_id("TenZ", "#")

    def test_rejects_hash_inside_name_when_tag_present(self) -> None:
        with self.assertRaises(ValueError):
            parse_riot_id("TenZ#SEN", "SEN")

    def test_path_encoding_makes_segments_url_safe(self) -> None:
        encoded_name, encoded_tag = riot_id_path("CSO BumbleB", "BU/ZZ")
        self.assertEqual("CSO%20BumbleB", encoded_name)
        self.assertEqual("BU%2FZZ", encoded_tag)


class RiotIdToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mmr_v3_wrapper_encodes_and_strips_hash(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_mmr_v3("eu", "CSO BumbleB", "#BUZZ")

        self.assertEqual(
            "/valorant/v3/mmr/eu/pc/CSO%20BumbleB/BUZZ",
            request.await_args.args[0],
        )

    async def test_mmr_v3_wrapper_rejects_hash_in_name(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            result = await server.get_mmr_v3("eu", "TenZ#SEN", "SEN")

        self.assertTrue(result.get("error"))
        request.assert_not_awaited()

    async def test_match_history_module_rejects_invalid_riot_id(self) -> None:
        request = AsyncMock(return_value={"data": []})
        with patch.object(matches.client, "get", request):
            result = await matches.get_match_history("eu", "TenZ", "")

        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("error"))
        request.assert_not_awaited()


class LeaderboardPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_sent_as_start_index(self) -> None:
        request = AsyncMock(return_value={"data": {"players": []}})
        with patch.object(leaderboard.client, "get", request):
            await leaderboard.get_leaderboard("eu", size=50, start=200)

        params = request.await_args.kwargs["params"]
        self.assertEqual(200, params["start_index"])
        self.assertEqual(50, params["size"])
        self.assertNotIn("page", params)

    async def test_default_size_is_capped_at_100(self) -> None:
        request = AsyncMock(return_value={"data": {}})
        with patch.object(leaderboard.client, "get", request):
            await leaderboard.get_leaderboard("eu")

        params = request.await_args.kwargs["params"]
        self.assertEqual(100, params["size"])
        self.assertNotIn("start_index", params)

    async def test_oversized_size_is_clamped(self) -> None:
        request = AsyncMock(return_value={"data": {}})
        with patch.object(leaderboard.client, "get", request):
            await leaderboard.get_leaderboard("eu", size=999999)

        self.assertEqual(1000, request.await_args.kwargs["params"]["size"])

    async def test_new_season_formats_are_accepted(self) -> None:
        request = AsyncMock(return_value={"data": {}})
        with patch.object(leaderboard.client, "get", request):
            await leaderboard.get_leaderboard("eu", season_short="e10a1")
            await leaderboard.get_leaderboard("eu", season_short="V25A2")

        first, second = request.await_args_list
        self.assertEqual("e10a1", first.kwargs["params"]["season_short"])
        self.assertEqual("v25a2", second.kwargs["params"]["season_short"])

    async def test_invalid_season_returns_error_dict(self) -> None:
        request = AsyncMock(return_value={"data": {}})
        with patch.object(leaderboard.client, "get", request):
            result = await leaderboard.get_leaderboard("eu", season_short="episode9")

        self.assertTrue(result.get("error"))
        request.assert_not_awaited()

    async def test_partial_riot_id_filter_returns_error_dict(self) -> None:
        request = AsyncMock(return_value={"data": {}})
        with patch.object(leaderboard.client, "get", request):
            result = await leaderboard.get_leaderboard("eu", name="TenZ")

        self.assertTrue(result.get("error"))
        request.assert_not_awaited()


class StaticContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_buddies_maps_to_upstream_charms_key(self) -> None:
        payload = {"data": {"version": "1.0", "charms": [{"name": "Buddy"}]}}
        request = AsyncMock(return_value=payload)
        with patch.object(server, "get_valorant_content", request):
            result = await server.get_static_content("buddies")

        self.assertEqual([{"name": "Buddy"}], result["charms"])

    async def test_buddies_falls_back_when_upstream_uses_buddies_key(self) -> None:
        payload = {"data": {"version": "1.0", "buddies": [{"name": "Buddy"}]}}
        request = AsyncMock(return_value=payload)
        with patch.object(server, "get_valorant_content", request):
            result = await server.get_static_content("buddies")

        self.assertEqual([{"name": "Buddy"}], result["buddies"])

    async def test_seasons_maps_to_upstream_acts_key(self) -> None:
        payload = {"data": {"version": "1.0", "acts": [{"name": "EPISODE 1"}]}}
        request = AsyncMock(return_value=payload)
        with patch.object(server, "get_valorant_content", request):
            result = await server.get_static_content("seasons")

        self.assertEqual([{"name": "EPISODE 1"}], result["acts"])

    async def test_unsupported_content_returns_error_dict(self) -> None:
        result = await server.get_static_content("weapons")
        self.assertTrue(result.get("error"))

    async def test_content_payload_is_cached_per_locale(self) -> None:
        server.clear_content_cache()
        request = AsyncMock(return_value={"data": {"version": "1.0"}})
        try:
            with patch.object(server, "_henrik_get", request):
                await server.get_valorant_content("en-US")
                await server.get_valorant_content("en-US")
                await server.get_valorant_content("de-DE")

            self.assertEqual(2, request.await_count)
        finally:
            server.clear_content_cache()


class MatchDetailCacheRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_stats_compact_uses_shared_cached_fetch(self) -> None:
        request = AsyncMock(return_value={"metadata": {"match_id": "m-1"}, "players": []})
        with patch.object(server.matches, "get_match", request):
            await server.get_match_player_stats_compact(
                "eu", "m-1", include_all_players=True
            )

        request.assert_awaited_once_with("eu", "m-1")

    async def test_player_stats_compact_propagates_error_dict(self) -> None:
        error = {"error": True, "message": "boom"}
        request = AsyncMock(return_value=error)
        with patch.object(server.matches, "get_match", request):
            result = await server.get_match_player_stats_compact("eu", "m-1")

        self.assertEqual(error, result)


class PlaytimePayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_matches_omitted_by_default(self) -> None:
        request = AsyncMock(return_value={"data": []})
        with patch.object(server, "get_match_history_v4", request):
            result = await server.get_player_playtime("eu", "TenZ", "SEN")

        self.assertNotIn("matches", result)
        self.assertEqual(0, result["matches_counted"])

    async def test_matches_included_on_request(self) -> None:
        request = AsyncMock(return_value={"data": []})
        with patch.object(server, "get_match_history_v4", request):
            result = await server.get_player_playtime(
                "eu", "TenZ", "SEN", include_matches=True
            )

        self.assertEqual([], result["matches"])


class TrialReadinessFanOutTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_window_is_fetched_once_per_player(self) -> None:
        report = {
            "player": "TenZ#SEN",
            "window": {"days": 14},
            "matches_counted": 10,
            "active_days": 5,
            "daily_breakdown": {"2026-08-01": {"matches": 2, "seconds": 3600}},
            "total_playtime_seconds": 36000,
            "total_playtime_hhmmss": "10:00:00",
            "agent_counts": {"Jett": 8, "Raze": 2},
            "confidence": "high",
            "notes": [],
        }
        playtime = AsyncMock(return_value=report)
        recent = AsyncMock(return_value={"form": "hot", "summary": {"kd": 1.4}})
        mmr_payload = AsyncMock(return_value={"status": 200, "data": {}})
        with (
            patch.object(server, "get_player_playtime", playtime),
            patch.object(server, "get_recent_form", recent),
            patch.object(server, "get_mmr_v3", mmr_payload),
        ):
            result = await server.get_trial_readiness_score("eu", "TenZ", "SEN")

        playtime.assert_awaited_once()
        self.assertIn("trial_readiness_score", result)
        self.assertEqual("TenZ#SEN", result["consistency"]["player"])
        self.assertEqual("duelist", result["role_profile"]["primary_role"])


class RecentFormNoneGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_kd_yields_cold_form_instead_of_crashing(self) -> None:
        summary = AsyncMock(return_value={"kd": None, "agents": {}})
        with patch.object(server, "get_player_summary", summary):
            result = await server.get_recent_form("eu", "TenZ", "SEN")

        self.assertEqual("cold", result["form"])


if __name__ == "__main__":
    unittest.main()
