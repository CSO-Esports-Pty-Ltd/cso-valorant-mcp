"""Shared Riot ID parsing, validation, and URL-encoding helpers.

Every tool that takes a Riot ID (name + tag) should route the values through
these helpers before interpolating them into a Henrik API URL path. A '#'
inside a path segment would otherwise be treated as a URL fragment delimiter
and silently corrupt the request.
"""

from typing import Any
from urllib.parse import quote


def parse_riot_id(name: str | None, tag: str | None) -> tuple[str, str]:
    """Normalize and validate a Riot ID name + tag pair.

    - Strips surrounding whitespace from both parts.
    - Strips a leading '#' from the tag (users often type '#SEN').
    - Accepts a combined 'Name#Tag' passed as name when tag is empty.
    - Rejects empty values and any remaining '#' in either part.

    Returns the cleaned (name, tag) pair, or raises ValueError with a
    human-readable message describing what was wrong.
    """
    raw_name = str(name).strip() if name is not None else ""
    raw_tag = str(tag).strip() if tag is not None else ""

    # Accept "Name#Tag" typed into the name field when no tag was supplied.
    if raw_name and not raw_tag and "#" in raw_name:
        raw_name, _, raw_tag = raw_name.partition("#")
        raw_name = raw_name.strip()
        raw_tag = raw_tag.strip()

    raw_tag = raw_tag.lstrip("#").strip()

    if not raw_name:
        raise ValueError("Riot ID name must be a non-empty string.")
    if not raw_tag:
        raise ValueError("Riot ID tag must be a non-empty string (without '#').")
    if "#" in raw_name or "#" in raw_tag:
        raise ValueError(
            "Riot ID name and tag must not contain '#'. "
            "Pass name and tag as separate values (e.g. name='TenZ', tag='SEN')."
        )
    return raw_name, raw_tag


def riot_id_error(exc: Exception, *, name: Any = None, tag: Any = None) -> dict[str, Any]:
    """Build the structured error dict returned for an invalid Riot ID."""
    return {
        "error": True,
        "message": str(exc),
        "received": {"name": name, "tag": tag},
    }


def encode_path_component(value: str) -> str:
    """Percent-encode one URL path segment ('#', '/', '?' become safe)."""
    return quote(str(value), safe="")


def riot_id_path(name: str | None, tag: str | None) -> tuple[str, str]:
    """Validate a Riot ID and return URL-path-safe (encoded_name, encoded_tag).

    Raises ValueError for invalid input; see parse_riot_id.
    """
    clean_name, clean_tag = parse_riot_id(name, tag)
    return encode_path_component(clean_name), encode_path_component(clean_tag)
