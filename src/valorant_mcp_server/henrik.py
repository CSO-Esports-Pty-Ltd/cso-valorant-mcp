"""Shared helpers for Henrik Dev API wrapper tools."""

import asyncio
import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv

_ = load_dotenv()

HENRIK_BASE_URL = "https://api.henrikdev.xyz"
HENRIK_TIMEOUT_SECONDS = float(os.getenv("HENRIK_TIMEOUT_SECONDS", "30"))
HENRIK_MAX_RETRIES = max(0, int(os.getenv("HENRIK_MAX_RETRIES", "2")))
HENRIK_MAX_RETRY_AFTER_SECONDS = max(
    0.0, float(os.getenv("HENRIK_MAX_RETRY_AFTER_SECONDS", "30"))
)
HENRIK_MAX_CONNECTIONS = max(1, int(os.getenv("HENRIK_MAX_CONNECTIONS", "20")))
HENRIK_MAX_KEEPALIVE_CONNECTIONS = max(
    1, int(os.getenv("HENRIK_MAX_KEEPALIVE_CONNECTIONS", "10"))
)

_LOGGER = logging.getLogger(__name__)
_HTTP_CLIENT: httpx.AsyncClient | None = None
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


def build_error(
    message: str,
    *,
    path: str,
    params: dict[str, Any] | None = None,
    status_code: int | None = None,
    response: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": True,
        "message": message,
        "path": path,
        "params": params or {},
    }
    if status_code is not None:
        payload["status_code"] = status_code
    if response is not None:
        payload["response"] = response
    return payload


def get_api_key() -> str | None:
    return os.getenv("HENRIK_API_KEY")


def build_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    api_key = get_api_key()
    if api_key:
        headers["Authorization"] = api_key
    return headers


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=HENRIK_TIMEOUT_SECONDS,
            limits=httpx.Limits(
                max_connections=HENRIK_MAX_CONNECTIONS,
                max_keepalive_connections=HENRIK_MAX_KEEPALIVE_CONNECTIONS,
            ),
        )
    return _HTTP_CLIENT


async def close_http_client() -> None:
    """Close the shared connection pool, primarily for graceful shutdown/tests."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        await _HTTP_CLIENT.aclose()
    _HTTP_CLIENT = None


def _response_metadata(response: httpx.Response) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for output_key, header_name in (
        ("request_id", "X-Request-ID"),
        ("retry_after", "Retry-After"),
        ("rate_limit", "RateLimit"),
        ("rate_limit_policy", "RateLimit-Policy"),
        ("rate_limit_remaining", "X-RateLimit-Remaining"),
        ("rate_limit_reset", "X-RateLimit-Reset"),
        ("cache_status", "X-Cache-Status"),
        ("cache_ttl", "X-Cache-TTL"),
    ):
        value = response.headers.get(header_name)
        if value is not None:
            metadata[output_key] = value
    return metadata


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset")
    try:
        requested = float(raw) if raw is not None else 0.0
    except ValueError:
        requested = 0.0
    fallback = min(0.5 * (2**attempt), 4.0)
    return min(max(requested, fallback), HENRIK_MAX_RETRY_AFTER_SECONDS)


async def henrik_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    if not get_api_key():
        return build_error(
            "HENRIK_API_KEY is not set",
            path=path,
            params=clean_params,
        )

    response: httpx.Response | None = None
    for attempt in range(HENRIK_MAX_RETRIES + 1):
        try:
            client = _get_http_client()
            response = await client.get(
                f"{HENRIK_BASE_URL}{path}",
                headers=build_headers(),
                params=clean_params,
            )
        except httpx.HTTPError as exc:
            if attempt < HENRIK_MAX_RETRIES:
                await asyncio.sleep(min(0.5 * (2**attempt), 4.0))
                continue
            return build_error(str(exc), path=path, params=clean_params)

        if response.status_code not in _RETRYABLE_STATUS_CODES or attempt >= HENRIK_MAX_RETRIES:
            break

        delay = _retry_delay(response, attempt)
        _LOGGER.warning(
            "Henrik retry status=%s attempt=%s delay=%s request_id=%s path=%s",
            response.status_code,
            attempt + 1,
            delay,
            response.headers.get("X-Request-ID"),
            path,
        )
        await asyncio.sleep(delay)

    if response is None:
        return build_error("Henrik request did not return a response", path=path, params=clean_params)

    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text}

    if response.status_code >= 400:
        error = build_error(
            f"Henrik API returned HTTP {response.status_code}",
            path=path,
            params=clean_params,
            status_code=response.status_code,
            response=payload,
        )
        error.update(_response_metadata(response))
        return error

    return payload


def content_slice(payload: dict[str, Any], key: str) -> dict[str, Any]:
    data = payload.get("data", payload)
    return {
        "version": data.get("version"),
        key: data.get(key, []),
    }
