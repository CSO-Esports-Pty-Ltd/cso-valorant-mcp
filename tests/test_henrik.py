from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from valorant_mcp_server import henrik


class HenrikClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await henrik.close_http_client()

    async def test_retries_rate_limit_and_preserves_request_metadata(self) -> None:
        request = httpx.Request("GET", "https://api.henrikdev.xyz/test")
        limited = httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "0", "X-Request-ID": "request-1"},
            json={"status": 429, "errors": [{"message": "Rate Limited"}]},
        )
        success = httpx.Response(200, request=request, json={"status": 200, "data": {"ok": True}})
        fake_client = AsyncMock()
        fake_client.get.side_effect = [limited, success]

        with (
            patch.object(henrik, "get_api_key", return_value="test-key"),
            patch.object(henrik, "_get_http_client", return_value=fake_client),
        ):
            result = await henrik.henrik_get("/test")

        self.assertEqual({"status": 200, "data": {"ok": True}}, result)
        self.assertEqual(2, fake_client.get.await_count)

    async def test_final_error_includes_rate_limit_metadata(self) -> None:
        request = httpx.Request("GET", "https://api.henrikdev.xyz/test")
        limited = httpx.Response(
            429,
            request=request,
            headers={
                "Retry-After": "12",
                "X-Request-ID": "request-2",
                "X-RateLimit-Remaining": "0",
            },
            json={"status": 429, "errors": [{"message": "Rate Limited"}]},
        )
        fake_client = AsyncMock()
        fake_client.get.return_value = limited

        with (
            patch.object(henrik, "get_api_key", return_value="test-key"),
            patch.object(henrik, "HENRIK_MAX_RETRIES", 0),
            patch.object(henrik, "_get_http_client", return_value=fake_client),
        ):
            result = await henrik.henrik_get("/test")

        self.assertTrue(result["error"])
        self.assertEqual("request-2", result["request_id"])
        self.assertEqual("12", result["retry_after"])
        self.assertEqual("0", result["rate_limit_remaining"])


if __name__ == "__main__":
    unittest.main()
