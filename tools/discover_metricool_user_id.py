"""Probe Metricool to recover the numeric user ID from just the token.

Why this exists: every Metricool API endpoint requires userId as a query
parameter, but the dashboard UI doesn't expose it. The official mcp-metricool
source hardcodes userId on every URL — it offers no bootstrap call.

This script tries three angles in order, stopping at the first that yields
the user ID:

  1. Local JWT decode. If the token is JWT-shaped (three base64url-encoded
     segments separated by dots), decode the payload and look for any claim
     that resembles a user ID (userId, user_id, uid, sub, id).

  2. Hit candidate "introspection" endpoints with NO userId param. For each:
     print status + body, then scan the response for the user ID field.

  3. Fall back to a guaranteed-4xx call (brands endpoint with no userId) so
     we can see if Metricool's error body echoes the principal's ID.

All raw HTTP responses are printed verbatim so the output is itself useful
if none of the heuristics succeed — you can spot the user ID by eye.

Run: uv run python -m tools.discover_metricool_user_id
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://app.metricool.com/api"
TIMEOUT = 20

# Field names in JSON responses that historically indicate the user ID.
USER_ID_KEYS = ("userId", "user_id", "uid", "id", "sub", "userID")

# Candidate endpoints to probe with NO userId param.
# Ordered: most likely to succeed first.
PROBES: list[tuple[str, str]] = [
    ("GET", "/v2/settings/brands"),       # might succeed and include userId in payload
    ("GET", "/admin/simpleProfiles"),     # surfaced in Metricool's own search snippets
    ("GET", "/v1/users/me"),
    ("GET", "/v2/users/me"),
    ("GET", "/v2/me"),
    ("GET", "/me"),
    ("GET", "/v2/settings/user"),
    ("GET", "/v2/settings/account"),
    ("GET", "/v2/profile"),
    ("GET", "/admin/profile"),
]


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url segment, padding as needed."""
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def try_jwt_decode(token: str) -> Optional[dict[str, Any]]:
    """If token is JWT-shaped, return the decoded payload; else None.

    Does NOT verify the signature — we only care about the readable claims.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_bytes = _b64url_decode(parts[1])
        return json.loads(payload_bytes)
    except Exception:  # noqa: BLE001
        return None


def scan_for_user_id(blob: Any) -> list[tuple[str, Any]]:
    """Walk a nested structure and surface any value under a user-ID-shaped key.

    Returns a list of (json_path, value) pairs.
    """
    found: list[tuple[str, Any]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                child_path = f"{path}.{k}" if path else k
                if k in USER_ID_KEYS and not isinstance(v, (dict, list)):
                    found.append((child_path, v))
                walk(v, child_path)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(blob, "")
    return found


def grep_user_id_from_text(text: str) -> list[str]:
    """Last-resort regex scan for things that look like userId mentions in
    a non-JSON response body or an error message that echoes the param.
    """
    hits = set()
    for pat in (
        r'"userId"\s*:\s*(\d+)',
        r"userId=(\d+)",
        r"user_id[\"=:\s]+(\d+)",
        r"\buser\s+id[:=\s]+(\d+)",
    ):
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            hits.add(m.group(1))
    return sorted(hits)


def probe(method: str, path: str, headers: dict[str, str]) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    print(f"\n--- {method} {path} ---")
    try:
        resp = requests.request(method, url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  HTTP error: {e}")
        return {"ok": False, "error": str(e)}

    print(f"  status: {resp.status_code}")
    ctype = resp.headers.get("content-type", "")
    print(f"  content-type: {ctype}")

    body_text = resp.text
    # Truncate for display; full text still scanned for matches.
    preview = body_text[:1000]
    print(f"  body (first 1000 chars):\n{preview}")
    if len(body_text) > 1000:
        print(f"  ... ({len(body_text) - 1000} more chars truncated)")

    candidates: list[tuple[str, Any]] = []
    if "application/json" in ctype or body_text.lstrip().startswith(("{", "[")):
        try:
            data = resp.json()
            candidates = scan_for_user_id(data)
        except ValueError:
            pass

    text_hits = grep_user_id_from_text(body_text)
    return {
        "ok": resp.status_code < 400,
        "status": resp.status_code,
        "json_candidates": candidates,
        "text_candidates": text_hits,
    }


def main() -> int:
    token = os.environ.get("METRICOOL_USER_TOKEN")
    if not token:
        print("ERROR: METRICOOL_USER_TOKEN not set in .env")
        return 2

    print(f"Token length: {len(token)} chars")
    print(f"Token prefix: {token[:8]}…")

    # --- Angle 1: JWT decode -----------------------------------------------
    print("\n=== Angle 1: JWT payload decode ===")
    payload = try_jwt_decode(token)
    if payload is not None:
        print("Token is JWT-shaped. Decoded payload:")
        print(json.dumps(payload, indent=2, default=str))
        jwt_candidates = scan_for_user_id(payload)
        if jwt_candidates:
            print("\nUSER ID candidates from JWT claims:")
            for path, value in jwt_candidates:
                print(f"  {path} = {value!r}")
            print(
                "\nIf one of these is numeric and stable, set METRICOOL_USER_ID "
                "to that value in .env and you're done."
            )
            return 0
        print("(no user-ID-shaped claim found in JWT)")
    else:
        print("Token is not JWT-shaped (not 3 dot-separated base64url segments).")

    # --- Angles 2 + 3: HTTP probes -----------------------------------------
    headers = {
        "X-Mc-Auth": token,
        "Accept": "application/json",
    }
    print("\n=== Angle 2: probe candidate endpoints with NO userId ===")
    all_candidates: list[tuple[str, str, Any]] = []  # (source, path/regex, value)
    for method, path in PROBES:
        result = probe(method, path, headers)
        for jpath, value in result.get("json_candidates", []):
            all_candidates.append((f"json {path}", jpath, value))
        for value in result.get("text_candidates", []):
            all_candidates.append((f"text {path}", "regex", value))

    print("\n=== Aggregated user-ID candidates across all probes ===")
    if not all_candidates:
        print("No user-ID-shaped value found in any probe.")
        print("\nNext step: inspect the response bodies above by eye, or "
              "open Metricool dashboard browser dev tools and look at any "
              "XHR request — the userId will be a query param on every call.")
        return 1

    # Dedupe values, count occurrences (the right ID typically appears in
    # multiple places — JWT claim + brands payload + error echo).
    counts: dict[str, int] = {}
    sources: dict[str, list[str]] = {}
    for source, where, value in all_candidates:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
        sources.setdefault(key, []).append(f"{source} ({where})")

    print(f"Found {len(counts)} distinct candidate value(s):\n")
    for value, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  value={value!r}  (seen {n}x)")
        for s in sources[value]:
            print(f"    via {s}")

    if len(counts) == 1:
        only = next(iter(counts))
        print(f"\nOnly one candidate ({only}) — set METRICOOL_USER_ID={only} in .env.")
        return 0
    print("\nMultiple candidates — inspect the raw bodies above to pick the "
          "right one. Numeric values appearing in /v2/settings/brands or the "
          "JWT 'sub' claim are usually the user ID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
