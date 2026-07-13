from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from valorant_mcp_server.tools import matches


class MatchToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        matches.clear_match_detail_cache()

    async def test_history_uses_current_map_parameter_and_pagination(self) -> None:
        request = AsyncMock(return_value={"data": []})
        with patch.object(matches.client, "get", request):
            await matches.get_match_history(
                "eu", "Player", "TAG", map_name="Ascent", size=10, start=20
            )

        self.assertEqual(
            {"map": "Ascent", "size": 10, "start": 20},
            request.await_args.kwargs["params"],
        )

    async def test_match_details_are_cached(self) -> None:
        request = AsyncMock(return_value={"data": {"metadata": {"match_id": "match-1"}}})
        with patch.object(matches.client, "get", request):
            first = await matches.get_match("eu", "match-1")
            second = await matches.get_match("eu", "match-1")

        self.assertEqual(first, second)
        self.assertEqual(1, request.await_count)


if __name__ == "__main__":
    unittest.main()
