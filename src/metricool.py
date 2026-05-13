"""Metricool REST API wrapper — brand lookup + Instagram feed-post scheduling.

Confirmed against the official mcp-metricool v1.1.9 source:
- Base URL: https://app.metricool.com/api (no trailing slash)
- Auth: header  "X-Mc-Auth: <token>"  (NOT Authorization: Bearer)
- Every endpoint takes ?userId=<id>&integrationSource=MCP
- POST scheduler takes ?blogId=<id> as well

Required env:
    METRICOOL_USER_TOKEN  — API token from dashboard → Settings → API
    METRICOOL_USER_ID     — numeric account user ID, also from Settings → API
    METRICOOL_BLOG_ID     — optional explicit blog override (str of int)
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

import requests

METRICOOL_BASE_URL = "https://app.metricool.com/api"
DEFAULT_TIMEOUT_SECONDS = 30
INTEGRATION_SOURCE = "MCP"  # Metricool tags requests for analytics — required


class MetricoolError(RuntimeError):
    """Raised on auth, HTTP, or contract failures with the Metricool API."""


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("METRICOOL_USER_TOKEN")
    if not token:
        raise MetricoolError("METRICOOL_USER_TOKEN is not set in the environment")
    return {
        "X-Mc-Auth": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _user_id() -> str:
    user_id = os.environ.get("METRICOOL_USER_ID")
    if not user_id:
        raise MetricoolError("METRICOOL_USER_ID is not set in the environment")
    return user_id


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """Internal HTTP helper. Surfaces auth/HTTP failures as MetricoolError."""
    url = f"{METRICOOL_BASE_URL}{path}"
    merged_params = {"userId": _user_id(), "integrationSource": INTEGRATION_SOURCE}
    if params:
        merged_params.update(params)

    try:
        response = requests.request(
            method,
            url,
            headers=_auth_headers(),
            params=merged_params,
            json=json_body,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise MetricoolError(f"Metricool HTTP failure: {e}") from e

    if response.status_code >= 400:
        raise MetricoolError(
            f"Metricool {method} {path} → HTTP {response.status_code}: "
            f"{response.text[:600]}"
        )

    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as e:
        raise MetricoolError(
            f"Metricool {method} {path} returned non-JSON: {response.text[:300]}"
        ) from e


# --------------------------------------------------------------------------- #
# Brand discovery
# --------------------------------------------------------------------------- #


def list_brands() -> list[dict[str, Any]]:
    """Fetch every brand connected to the configured Metricool user.

    Returns the raw brand list as returned by the API. The exact field names
    can vary across Metricool plan tiers; verify_metricool.py prints the full
    response so we can confirm shape before relying on specific keys.
    """
    response = _request("GET", "/v2/settings/brands")
    # /v2/settings/brands wraps the list under "data" (observed in production);
    # other Metricool endpoints have been seen using "brands" as the wrapper key;
    # and an early-tier response could be a bare list. Handle all three.
    if isinstance(response, dict):
        for wrapper_key in ("data", "brands"):
            if wrapper_key in response and isinstance(response[wrapper_key], list):
                return response[wrapper_key]
        raise MetricoolError(
            f"/v2/settings/brands returned a dict with no recognized wrapper "
            f"key (expected 'data' or 'brands'). Keys: {sorted(response.keys())!r}"
        )
    if isinstance(response, list):
        return response
    raise MetricoolError(
        f"Unexpected /v2/settings/brands response shape: {type(response).__name__} "
        f"— expected list or wrapped list. payload: {str(response)[:300]}"
    )


# Candidate field names for the brand fields we care about. Probed in order;
# first non-empty wins. Keeps us resilient to minor schema variations.
_BLOG_ID_KEYS = ("blogId", "id")
_TIMEZONE_KEYS = ("timezone", "timeZone")
_NAME_KEYS = ("label", "title", "name", "url", "domain")


def _pick(brand: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = brand.get(k)
        if v not in (None, ""):
            return v
    return None


def _normalize_brand(brand: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields we care about out of a brand dict.

    Raises MetricoolError with the full source dict if blog_id is missing —
    nothing else works without it.
    """
    blog_id = _pick(brand, _BLOG_ID_KEYS)
    if blog_id is None:
        raise MetricoolError(
            f"Brand dict missing blog_id (tried keys {_BLOG_ID_KEYS!r}). "
            f"Raw: {brand!r}"
        )
    return {
        "blog_id": int(blog_id),
        "timezone": _pick(brand, _TIMEZONE_KEYS),
        "name": _pick(brand, _NAME_KEYS),
        "raw": brand,
    }


def find_instagram_brand(
    brands: list[dict[str, Any]],
    name_contains: str = "drip tcg",
) -> dict[str, Any]:
    """Pick the Drip TCG brand from the list_brands() output.

    Default name_contains is "drip tcg" (case-insensitive) because the account
    has five "Drip *" brands and we want to disambiguate to the TCG/Pokemon
    one — the only one with the dripshoplive_ IG handle and Pokemon focus.

    Resolution order:
        1. If METRICOOL_BLOG_ID is set in env, return the brand with that ID.
           This is the rescue path — if the brand is ever renamed in Metricool,
           setting the env var keeps the harness working without a code change.
        2. Otherwise, case-insensitive substring match on `name_contains`
           against any of the candidate name fields.

    Returns: {"blog_id": int, "timezone": str|None, "name": str|None, "raw": dict}

    Raises MetricoolError on zero matches, multiple matches, or override miss.
    """
    if not brands:
        raise MetricoolError("Metricool returned zero brands for this user")

    normalized = [_normalize_brand(b) for b in brands]

    override = os.environ.get("METRICOOL_BLOG_ID")
    if override:
        try:
            target = int(override)
        except ValueError as e:
            raise MetricoolError(
                f"METRICOOL_BLOG_ID must be an integer, got {override!r}"
            ) from e
        for nb in normalized:
            if nb["blog_id"] == target:
                return nb
        all_ids = [nb["blog_id"] for nb in normalized]
        raise MetricoolError(
            f"METRICOOL_BLOG_ID={target} not found among brands {all_ids!r}"
        )

    needle = name_contains.lower()
    matches = [
        nb for nb in normalized if nb["name"] and needle in nb["name"].lower()
    ]
    if not matches:
        names = [nb["name"] for nb in normalized]
        raise MetricoolError(
            f"No brand name contains {name_contains!r} (case-insensitive). "
            f"Available: {names!r}. Set METRICOOL_BLOG_ID to override."
        )
    if len(matches) > 1:
        names = [nb["name"] for nb in matches]
        raise MetricoolError(
            f"Multiple brands match {name_contains!r}: {names!r}. "
            f"Set METRICOOL_BLOG_ID to disambiguate."
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


def schedule_instagram_post(
    blog_id: int,
    caption: str,
    media_url: str,
    publish_at: datetime,
    timezone: str = "America/Los_Angeles",
    *,
    draft: bool = False,
    auto_publish: bool = True,
) -> dict[str, Any]:
    """Schedule a single Instagram feed post.

    Args:
        blog_id: Metricool blog ID (from find_instagram_brand()["blog_id"]).
        caption: Post caption text.
        media_url: Public URL of the image (Metricool fetches server-side).
            Canva presigned export URLs are valid for ~24h, which is fine.
        publish_at: When to schedule the post. Should be a NAIVE datetime
            representing the wall-clock time in `timezone`. The Metricool API
            does NOT take an offset in the dateTime string — it pairs a
            naive timestamp with a separate IANA timezone field.
        timezone: IANA timezone name. Default "America/Los_Angeles" (PT).
        draft: True keeps the post as a draft (won't publish even at the
            scheduled time). Useful for verification runs.
        auto_publish: True publishes automatically at the scheduled time.
            Set False to leave the post in the queue for manual approval.

    Returns the full response payload from Metricool, which includes the
    created post's ID, uuid, and current status. Raises MetricoolError on
    auth, transport, or contract failure.
    """
    if publish_at.tzinfo is not None:
        raise MetricoolError(
            "publish_at must be a naive datetime (no tzinfo); the timezone "
            "is passed separately. Strip with .replace(tzinfo=None) after "
            "converting to wall-clock time in the target timezone."
        )

    body: dict[str, Any] = {
        "text": caption,
        "media": [media_url],
        "mediaAltText": [],
        "providers": [{"network": "instagram"}],
        "publicationDate": {
            "dateTime": publish_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": timezone,
        },
        "autoPublish": auto_publish,
        "draft": draft,
        "firstCommentText": "",
        "hasNotReadNotes": False,
        "shortener": False,
        "smartLinkData": {"ids": []},
        "descendants": [],
        "instagramData": {"type": "POST"},  # POST = feed; not REEL or STORY
    }

    return _request(
        "POST",
        "/v2/scheduler/posts",
        params={"blogId": blog_id},
        json_body=body,
    )


# --------------------------------------------------------------------------- #
# Image URL snapshot
# --------------------------------------------------------------------------- #


def normalize_image_url(image_url: str) -> str:
    """Tell Metricool to snapshot `image_url` onto its own CDN.

    Why this matters: schedule_instagram_post(media_url=...) accepts any
    public URL, but Metricool fetches the URL at PUBLISH time (6pm PT
    same day), not at schedule time. If our daily image_host strategy
    overwrites the same `latest.png` path each day, today's 12pm-PT
    upload could be overwritten by tomorrow's 12pm-PT upload BEFORE
    today's 6pm-PT publish window. Calling normalize_image_url
    immediately after schedule_instagram_post tells Metricool to fetch
    and cache the image on their side right now, so the scheduled post
    becomes immune to any future change at the source URL.

    Args:
        image_url: Public URL of the image to snapshot. The same URL that
            was passed as media_url to schedule_instagram_post.

    Returns the Metricool-hosted snapshot URL (the normalized CDN URL).
    Raises MetricoolError on transport or contract failure.
    """
    response = _request(
        "POST",
        "/v2/actions/normalize/image/url",
        params={"url": image_url},
    )
    # Response shape (per the mcp-metricool reference): a JSON object
    # with a "url" key containing the normalized snapshot URL. Defensive
    # against schema drift: also accept "data.url" and bare string.
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        if "url" in response and isinstance(response["url"], str):
            return response["url"]
        nested = response.get("data")
        if isinstance(nested, dict) and isinstance(nested.get("url"), str):
            return nested["url"]
    raise MetricoolError(
        f"Unexpected normalize/image/url response shape: "
        f"{type(response).__name__} — {str(response)[:300]}"
    )
