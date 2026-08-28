from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import valorant_mcp_server.server as server


class IdentityValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_when_no_identity_supplied(self) -> None:
        result = await server.get_account()
        self.assertTrue(result.get("error"))

    async def test_error_when_both_identity_forms_supplied(self) -> None:
        result = await server.get_rank("eu", name="TenZ", tag="SEN", puuid="p-1")
        self.assertTrue(result.get("error"))

    async def test_error_when_partial_riot_id_supplied(self) -> None:
        result = await server.get_stored_mmr_history("eu", name="TenZ")
        self.assertTrue(result.get("error"))

    async def test_error_when_tag_combined_with_puuid(self) -> None:
        result = await server.get_match_history("eu", tag="SEN", puuid="p-1")
        self.assertTrue(result.get("error"))


class AccountRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_tag_routes_to_riot_id_lookup(self) -> None:
        request = AsyncMock(return_value={"puuid": "p-1"})
        with patch.object(server.accounts, "get_account", request):
            result = await server.get_account(name="TenZ", tag="SEN")

        request.assert_awaited_once_with("TenZ", "SEN", False)
        self.assertEqual({"puuid": "p-1"}, result)

    async def test_puuid_routes_to_puuid_lookup(self) -> None:
        request = AsyncMock(return_value={"puuid": "p-1"})
        with patch.object(server.accounts, "get_account_by_puuid", request):
            result = await server.get_account(puuid="p-1", force_update=True)

        request.assert_awaited_once_with("p-1", True)
        self.assertEqual({"puuid": "p-1"}, result)


class RankRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_tag_routes_to_v3_mmr_endpoint(self) -> None:
        request = AsyncMock(return_value={"status": 200, "data": {}})
        with patch.object(server, "_henrik_get", request):
            result = await server.get_rank("eu", name="TenZ", tag="SEN")

        self.assertEqual("/valorant/v3/mmr/eu/pc/TenZ/SEN", request.await_args.args[0])
        self.assertEqual({"status": 200, "data": {}}, result)

    async def test_puuid_routes_to_puuid_mmr_lookup(self) -> None:
        request = AsyncMock(return_value={"current": {}})
        with patch.object(server.mmr, "get_mmr_by_puuid", request):
            result = await server.get_rank("eu", puuid="p-1", platform="console")

        request.assert_awaited_once_with("eu", "p-1", "console")
        self.assertEqual({"current": {}}, result)


class MatchHistoryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_name_tag_uses_trimmed_riot_id_path(self) -> None:
        request = AsyncMock(return_value={"data": []})
        with patch.object(server, "_henrik_get", request):
            result = await server.get_match_history("eu", name="TenZ", tag="SEN")

        path, params = request.await_args.args
        self.assertEqual("/valorant/v4/matches/eu/pc/TenZ/SEN", path)
        self.assertEqual(3, params["size"])  # compact default is 3
        self.assertEqual("get_match_history_v4_trimmed", result["source_tool"])
        self.assertEqual([], result["matches"])

    async def test_compact_puuid_uses_trimmed_puuid_path_and_size_cap(self) -> None:
        request = AsyncMock(return_value={"data": []})
        with patch.object(server, "_henrik_get", request):
            result = await server.get_match_history("eu", puuid="p-1", size=50)

        path, params = request.await_args.args
        self.assertEqual("/valorant/v4/by-puuid/matches/eu/pc/p-1", path)
        self.assertEqual(5, params["size"])  # hard cap at 5
        self.assertEqual("get_match_history_by_puuid_trimmed", result["source_tool"])

    async def test_full_name_tag_uses_matches_module(self) -> None:
        request = AsyncMock(return_value=[{"metadata": {"match_id": "m-1"}}])
        with patch.object(server.matches, "get_match_history", request):
            result = await server.get_match_history(
                "eu", name="TenZ", tag="SEN", size=10, compact=False
            )

        request.assert_awaited_once_with(
            "eu", "TenZ", "SEN", "pc", None, None, 10, None
        )
        self.assertEqual([{"metadata": {"match_id": "m-1"}}], result)

    async def test_full_puuid_uses_raw_puuid_endpoint(self) -> None:
        request = AsyncMock(return_value={"status": 200, "data": []})
        with patch.object(server, "_henrik_get", request):
            result = await server.get_match_history(
                "eu", puuid="p-1", size=10, compact=False
            )

        path, params = request.await_args.args
        self.assertEqual("/valorant/v4/by-puuid/matches/eu/pc/p-1", path)
        self.assertEqual(10, params["size"])  # no compact cap on the full path
        self.assertEqual({"status": 200, "data": []}, result)


class StoredMatchesRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_puuid_clamps_size_and_trims(self) -> None:
        request = AsyncMock(return_value={"data": []})
        with patch.object(server, "_henrik_get", request):
            result = await server.get_stored_matches("eu", puuid="p-1", size=50)

        path, params = request.await_args.args
        self.assertEqual("/valorant/v1/by-puuid/stored-matches/eu/p-1", path)
        self.assertEqual(5, params["size"])
        self.assertEqual("get_stored_matches", result["source_tool"])

    async def test_compact_name_tag_uses_riot_id_path(self) -> None:
        request = AsyncMock(return_value={"data": []})
        with patch.object(server, "_henrik_get", request):
            result = await server.get_stored_matches("eu", name="TenZ", tag="SEN")

        path, params = request.await_args.args
        self.assertEqual("/valorant/v1/stored-matches/eu/TenZ/SEN", path)
        self.assertEqual(3, params["size"])
        self.assertEqual("get_stored_matches", result["source_tool"])

    async def test_full_name_tag_passes_size_through(self) -> None:
        request = AsyncMock(return_value={"status": 200, "data": []})
        with patch.object(server, "_henrik_get", request):
            result = await server.get_stored_matches(
                "eu", name="TenZ", tag="SEN", size=25, page=2, compact=False
            )

        path, params = request.await_args.args
        self.assertEqual("/valorant/v1/stored-matches/eu/TenZ/SEN", path)
        self.assertEqual({"mode": None, "map": None, "page": 2, "size": 25}, params)
        self.assertEqual({"status": 200, "data": []}, result)


class StoredMmrHistoryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_tag_routes_to_riot_id_path(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_stored_mmr_history("eu", name="TenZ", tag="SEN", page=1, size=20)

        path, params = request.await_args.args
        self.assertEqual("/valorant/v2/stored-mmr-history/eu/pc/TenZ/SEN", path)
        self.assertEqual({"page": 1, "size": 20}, params)

    async def test_puuid_routes_to_puuid_path(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_stored_mmr_history("eu", puuid="p-1", platform="console")

        path, _params = request.await_args.args
        self.assertEqual("/valorant/v2/by-puuid/stored-mmr-history/eu/console/p-1", path)


class PremierTeamRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_id_detail(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_premier_team(team_id="team-1")

        request.assert_awaited_once_with("/valorant/v1/premier/team-1")

    async def test_team_id_history(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_premier_team(team_id="team-1", history=True)

        request.assert_awaited_once_with("/valorant/v1/premier/team-1/history")

    async def test_team_name_tag_detail(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_premier_team(team_name="CSO", team_tag="ACAD")

        request.assert_awaited_once_with("/valorant/v1/premier/CSO/ACAD")

    async def test_team_name_tag_history(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_premier_team(
                team_name="CSO", team_tag="ACAD", history=True
            )

        request.assert_awaited_once_with("/valorant/v1/premier/CSO/ACAD/history")

    async def test_error_on_ambiguous_or_missing_identity(self) -> None:
        both = await server.get_premier_team(team_id="team-1", team_name="CSO", team_tag="ACAD")
        self.assertTrue(both.get("error"))

        partial = await server.get_premier_team(team_name="CSO")
        self.assertTrue(partial.get("error"))

        none = await server.get_premier_team()
        self.assertTrue(none.get("error"))


class PremierLeaderboardRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_region_only(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_premier_leaderboard("eu")

        request.assert_awaited_once_with("/valorant/v1/premier/leaderboard/eu")

    async def test_conference(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_premier_leaderboard("eu", conference="EU_CENTRAL_EAST")

        request.assert_awaited_once_with(
            "/valorant/v1/premier/leaderboard/eu/EU_CENTRAL_EAST"
        )

    async def test_conference_and_division(self) -> None:
        request = AsyncMock(return_value={"status": 200})
        with patch.object(server, "_henrik_get", request):
            await server.get_premier_leaderboard(
                "eu", conference="EU_CENTRAL_EAST", division=5
            )

        request.assert_awaited_once_with(
            "/valorant/v1/premier/leaderboard/eu/EU_CENTRAL_EAST/5"
        )

    async def test_division_without_conference_is_an_error(self) -> None:
        result = await server.get_premier_leaderboard("eu", division=5)
        self.assertTrue(result.get("error"))


class PlayerPoolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_both_groupings_from_one_fetch(self) -> None:
        rows = [
            {"map": "Ascent", "agent": "Jett", "won": True, "kills": 20, "deaths": 10, "assists": 5, "score": 250},
            {"map": "Ascent", "agent": "Raze", "won": False, "kills": 10, "deaths": 15, "assists": 2, "score": 180},
            {"map": "Bind", "agent": "Jett", "won": True, "kills": 25, "deaths": 12, "assists": 3, "score": 300},
        ]
        fetch = AsyncMock(return_value=(rows, None))
        with patch.object(server, "_player_last_n_rows", fetch):
            result = await server.get_player_pools("eu", "TenZ", "SEN", match_count=3)

        fetch.assert_awaited_once_with("eu", "TenZ", "SEN", "pc", 3)
        self.assertEqual(3, result["matches_counted"])
        map_groups = {row["map"] for row in result["map_pool"]["groups"]}
        agent_groups = {row["agent"] for row in result["agent_pool"]["groups"]}
        self.assertEqual({"Ascent", "Bind"}, map_groups)
        self.assertEqual({"Jett", "Raze"}, agent_groups)


class ScreenCandidatesTests(unittest.IsolatedAsyncioTestCase):
    def _form(self, kd: float | None, agents: dict[str, int], form: str) -> dict:
        return {"form": form, "summary": {"kd": kd, "agents": agents}}

    async def test_min_kd_filter(self) -> None:
        forms = {
            "High": self._form(1.5, {"Jett": 5}, "hot"),
            "Low": self._form(0.9, {"Jett": 5}, "struggling"),
        }
        fetch = AsyncMock(side_effect=lambda region, name, tag, platform, count: forms[name])
        candidates = [{"name": "High", "tag": "A"}, {"name": "Low", "tag": "B"}]
        with patch.object(server, "get_recent_form", fetch):
            result = await server.screen_candidates("eu", candidates, min_kd=1.2)

        self.assertEqual(["High"], [row["name"] for row in result])
        self.assertEqual(1.5, result[0]["kd"])
        self.assertEqual("hot", result[0]["form"])

    async def test_max_agent_pool_filter(self) -> None:
        forms = {
            "Narrow": self._form(1.1, {"Jett": 8, "Raze": 2}, "stable"),
            "Wide": self._form(1.3, {"Jett": 2, "Raze": 2, "Omen": 2, "Sova": 2, "Sage": 2}, "hot"),
        }
        fetch = AsyncMock(side_effect=lambda region, name, tag, platform, count: forms[name])
        candidates = [{"name": "Narrow", "tag": "A"}, {"name": "Wide", "tag": "B"}]
        with patch.object(server, "get_recent_form", fetch):
            result = await server.screen_candidates("eu", candidates, max_agent_pool=4)

        self.assertEqual(["Narrow"], [row["name"] for row in result])
        self.assertEqual(2, result[0]["agents_used"])

    async def test_no_filters_returns_all_resolvable_candidates(self) -> None:
        fetch = AsyncMock(return_value=self._form(None, {}, "cold"))
        candidates = [{"name": "Any", "tag": "A"}, {"name": "Missing"}]
        with patch.object(server, "get_recent_form", fetch):
            result = await server.screen_candidates("eu", candidates)

        self.assertEqual(1, len(result))
        self.assertEqual("Any", result[0]["name"])

    async def test_none_kd_fails_min_kd_filter(self) -> None:
        fetch = AsyncMock(return_value=self._form(None, {}, "cold"))
        with patch.object(server, "get_recent_form", fetch):
            result = await server.screen_candidates(
                "eu", [{"name": "Empty", "tag": "A"}], min_kd=1.0
            )

        self.assertEqual([], result)


if __name__ == "__main__":
    unittest.main()
