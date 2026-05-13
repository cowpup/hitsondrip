"""Canva access token management — refresh + cache to .env atomically.

Single public function: get_valid_access_token().

Flow:
  1. Read CANVA_ACCESS_TOKEN and CANVA_ACCESS_TOKEN_EXPIRES_AT from env.
  2. If the access token has > REFRESH_SAFETY_MARGIN seconds left, return it.
  3. Otherwise POST refresh_token grant to Canva, write the new access_token
     and the new refresh_token (Canva rotates refresh tokens) back to .env
     under a cross-process file lock, then return the new access token.

Required env (populated by tools/canva_oauth.py):
    CANVA_CLIENT_ID
    CANVA_CLIENT_SECRET
    CANVA_ACCESS_TOKEN
    CANVA_REFRESH_TOKEN
    CANVA_ACCESS_TOKEN_EXPIRES_AT  — ISO 8601 UTC, e.g. "2026-05-12T22:15:00+00:00"

Scope note (future-you will be confused otherwise):
    Canva's documented description of `design:content:write` says "Create
    designs on the user's behalf", but in practice this same scope is also
    required to MODIFY existing designs (update_fill, insert_fill,
    delete_element, format_text, etc.). The daily workflow only modifies
    the rolling archive — it never creates fresh designs — yet
    design:content:write is still required. Don't try to drop it.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import find_dotenv, load_dotenv, set_key
from filelock import FileLock, Timeout

TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"
# Refresh this many seconds before the access token expires. Canva access
# tokens last 4 hours by default; refreshing 5 minutes early gives plenty of
# headroom for clock skew and slow networks without burning refreshes.
REFRESH_SAFETY_MARGIN_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 20
# Lock acquisition timeout. Refresh is fast (~1 sec round trip); anything
# beyond a few seconds means a stuck process — surface it.
LOCK_TIMEOUT_SECONDS = 30


class CanvaAuthError(RuntimeError):
    """Raised on missing config, refresh failure, or .env update failure."""


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    creds = f"{client_id}:{client_secret}".encode("utf-8")
    return f"Basic {base64.b64encode(creds).decode('ascii')}"


def _required_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise CanvaAuthError(
            f"{key} is not set in the environment. "
            f"Run `uv run python -m tools.canva_oauth` to provision credentials."
        )
    return val


def _parse_expires_at(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise CanvaAuthError(
            f"CANVA_ACCESS_TOKEN_EXPIRES_AT is not ISO 8601: {value!r}"
        ) from e
    # Treat naive timestamps as UTC — that's what we wrote.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _dotenv_path() -> Path:
    """Locate the project's .env file. Errors loudly if not found —
    we never want to silently write a fresh .env in the wrong directory."""
    path = find_dotenv(usecwd=True)
    if not path:
        # Fall back to cwd/.env so we always have a target, but only if it exists.
        candidate = Path.cwd() / ".env"
        if not candidate.exists():
            raise CanvaAuthError(
                f"Could not locate .env file (looked from {Path.cwd()}). "
                f"Cannot persist refreshed tokens."
            )
        return candidate
    return Path(path)


def _persist_tokens(
    env_path: Path,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
) -> None:
    """Write the three token-related keys back to .env under a file lock.

    Canva rotates refresh tokens, so the new refresh_token MUST be persisted
    or the next refresh attempt fails with invalid_grant.
    """
    lock_path = env_path.with_name(env_path.name + ".lock")
    lock = FileLock(str(lock_path), timeout=LOCK_TIMEOUT_SECONDS)
    try:
        with lock:
            # set_key uses a temp-file-and-rename strategy for atomicity.
            set_key(str(env_path), "CANVA_ACCESS_TOKEN", access_token)
            set_key(str(env_path), "CANVA_REFRESH_TOKEN", refresh_token)
            set_key(
                str(env_path),
                "CANVA_ACCESS_TOKEN_EXPIRES_AT",
                expires_at.isoformat(),
            )
    except Timeout as e:
        raise CanvaAuthError(
            f"Could not acquire {lock_path} within {LOCK_TIMEOUT_SECONDS}s — "
            f"another process is refreshing. Check for stuck instances."
        ) from e

    # Update in-process env too so the caller sees the new values immediately
    # without needing to reload .env.
    os.environ["CANVA_ACCESS_TOKEN"] = access_token
    os.environ["CANVA_REFRESH_TOKEN"] = refresh_token
    os.environ["CANVA_ACCESS_TOKEN_EXPIRES_AT"] = expires_at.isoformat()


def _refresh(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    """POST the refresh_token grant. Returns the raw JSON response."""
    try:
        response = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(client_id, client_secret),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise CanvaAuthError(f"Canva token endpoint HTTP failure: {e}") from e

    if response.status_code >= 400:
        # Common failure: invalid_grant when refresh token rotation got
        # out of sync (the old refresh token was already consumed).
        raise CanvaAuthError(
            f"Canva refresh failed: HTTP {response.status_code}: "
            f"{response.text[:500]}. "
            f"If this is 'invalid_grant', re-run `uv run python -m tools.canva_oauth` "
            f"to reauthorize from scratch."
        )

    try:
        return response.json()
    except ValueError as e:
        raise CanvaAuthError(
            f"Canva token endpoint returned non-JSON: {response.text[:300]}"
        ) from e


def get_valid_access_token() -> str:
    """Return a non-expired Canva access token, refreshing if needed.

    Side effect on refresh: rewrites CANVA_ACCESS_TOKEN, CANVA_REFRESH_TOKEN,
    and CANVA_ACCESS_TOKEN_EXPIRES_AT in .env atomically (under a file lock).
    """
    # load_dotenv() is idempotent and won't overwrite already-set env vars
    # by default, so this is safe to call even mid-process.
    load_dotenv()

    access_token = _required_env("CANVA_ACCESS_TOKEN")
    expires_at = _parse_expires_at(_required_env("CANVA_ACCESS_TOKEN_EXPIRES_AT"))

    now = datetime.now(timezone.utc)
    if expires_at > now + timedelta(seconds=REFRESH_SAFETY_MARGIN_SECONDS):
        return access_token

    # Need to refresh.
    client_id = _required_env("CANVA_CLIENT_ID")
    client_secret = _required_env("CANVA_CLIENT_SECRET")
    refresh_token = _required_env("CANVA_REFRESH_TOKEN")

    payload = _refresh(client_id, client_secret, refresh_token)

    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not new_access or not new_refresh or not expires_in:
        raise CanvaAuthError(
            f"Canva refresh response missing fields. Got: {payload!r}"
        )

    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    _persist_tokens(_dotenv_path(), new_access, new_refresh, new_expires_at)
    return new_access
