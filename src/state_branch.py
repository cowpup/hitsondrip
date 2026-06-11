"""Read/write de-dup state files on the `state` orphan branch.

Mirrors the image_host.py pattern: an orphan branch holds small state
files that the daily-cron jobs update after a successful run. Code
lives on `main`; the state branch never has any code committed to it.

Two independent automations each keep their own state file:
  - New Chase  → state/last_chase_card_id.txt  (the card_product_id of
    the last chase we posted; see new_chase.py)
  - Just Pulled → state/last_hit_id.txt        (the product_purchases.id
    of the last hit we posted an approval card for; see main.py)

Keeping them in separate files means the two crons can never clobber
each other's de-dup state.

Why an orphan branch over actions/cache:
  - GitHub Actions caches can be evicted at 7 days of inactivity OR
    when the repo hits the 10GB cache cap. Silent eviction = duplicate
    posts. The state branch is durable forever.
  - Same operational pattern Noah already understands from daily-output.
  - ~1KB/day churn is trivial; the orphan branch's history is itself
    an audit log of which post went out on which day.

Contract (per automation):
  read_last_card_id() / read_last_hit_id() — return the stored int id,
      or None if the file/branch doesn't exist yet (first run).
  write_last_card_id(id) / write_last_hit_id(id) — overwrite the file
      with the new id and commit via the Contents API.

Required env (same as image_host.py):
  GITHUB_TOKEN — auto-injected by Actions with `contents: write`.
  GITHUB_REPO  — "owner/repo" form.

Local-dev fallback:
  If GITHUB_TOKEN is missing, falls back to data/<file>.txt so the
  pipeline can be exercised locally without touching real GitHub.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

GITHUB_API_BASE = "https://api.github.com"
STATE_BRANCH = "state"
DEFAULT_TIMEOUT_SECONDS = 30

# New Chase de-dup (card_product_id of the last posted chase).
CHASE_STATE_FILENAME = "state/last_chase_card_id.txt"
CHASE_LOCAL_FALLBACK_PATH = Path("data") / "last_chase_card_id.txt"

# Just Pulled de-dup (product_purchases.id of the last posted hit).
HIT_STATE_FILENAME = "state/last_hit_id.txt"
HIT_LOCAL_FALLBACK_PATH = Path("data") / "last_hit_id.txt"


class StateBranchError(RuntimeError):
    """Raised on Contents API failures."""


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _resolve_repo() -> Optional[tuple[str, str, str]]:
    """Return (owner, repo, token) if env is configured, else None."""
    token = os.environ.get("GITHUB_TOKEN")
    repo_spec = os.environ.get("GITHUB_REPO")
    if not token or not repo_spec or "/" not in repo_spec:
        return None
    owner, _, repo = repo_spec.partition("/")
    return owner, repo, token


def _read_last_id(remote_filename: str, local_path: Path) -> Optional[int]:
    """Fetch the previously-stored id, or None on first run.

    A 404 from the Contents API (file or branch doesn't exist yet) is
    the expected first-run state — return None so the caller treats any
    candidate as new. Any other failure raises StateBranchError.
    """
    env = _resolve_repo()
    if env is None:
        # Local fallback
        if local_path.exists():
            try:
                return int(local_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return None
        return None

    owner, repo, token = env
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{remote_filename}"
    try:
        response = requests.get(
            url,
            headers=_api_headers(token),
            params={"ref": STATE_BRANCH},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise StateBranchError(f"GitHub GET {url}: {e}") from e

    if response.status_code == 404:
        # First run, or branch doesn't exist yet.
        return None
    if response.status_code >= 400:
        raise StateBranchError(
            f"GitHub GET {url} → HTTP {response.status_code}: "
            f"{response.text[:400]}"
        )

    encoded = response.json().get("content", "")
    try:
        raw = base64.b64decode(encoded).decode("utf-8").strip()
        return int(raw)
    except (ValueError, UnicodeDecodeError):
        # Corrupt state — treat as no prior state.
        return None


def _write_last_id(
    value: int,
    remote_filename: str,
    local_path: Path,
    commit_prefix: str,
) -> None:
    """Overwrite the given state file with `value`.

    Uses the Contents API PUT, which requires the existing file's SHA
    when updating. We fetch that SHA inline (one extra round-trip per
    run) rather than caching it from the read, so this function works
    correctly even when called from a different process.

    Raises StateBranchError on API failure.
    """
    env = _resolve_repo()
    if env is None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(f"{value}\n", encoding="utf-8")
        return

    owner, repo, token = env
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{remote_filename}"

    # Get current SHA (None on first-ever write).
    try:
        sha_response = requests.get(
            url,
            headers=_api_headers(token),
            params={"ref": STATE_BRANCH},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise StateBranchError(f"GitHub GET {url}: {e}") from e

    existing_sha: Optional[str] = None
    if sha_response.status_code == 200:
        existing_sha = sha_response.json().get("sha")
    elif sha_response.status_code != 404:
        raise StateBranchError(
            f"GitHub GET {url} → HTTP {sha_response.status_code}: "
            f"{sha_response.text[:400]}"
        )

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body: dict[str, object] = {
        "message": f"{commit_prefix}={value} @ {ts}",
        "content": base64.b64encode(
            f"{value}\n".encode("utf-8")
        ).decode("ascii"),
        "branch": STATE_BRANCH,
    }
    if existing_sha:
        body["sha"] = existing_sha

    try:
        put_response = requests.put(
            url,
            headers=_api_headers(token),
            json=body,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise StateBranchError(f"GitHub PUT {url}: {e}") from e

    if put_response.status_code not in (200, 201):
        raise StateBranchError(
            f"GitHub PUT {url} → HTTP {put_response.status_code}: "
            f"{put_response.text[:400]}"
        )


# --- New Chase de-dup --------------------------------------------------- #

def read_last_card_id() -> Optional[int]:
    """Fetch the previously-posted chase card_product_id, or None."""
    return _read_last_id(CHASE_STATE_FILENAME, CHASE_LOCAL_FALLBACK_PATH)


def write_last_card_id(card_product_id: int) -> None:
    """Record the card_product_id of the chase we just posted."""
    _write_last_id(
        card_product_id,
        CHASE_STATE_FILENAME,
        CHASE_LOCAL_FALLBACK_PATH,
        "new_chase: card_id",
    )


# --- Just Pulled de-dup ------------------------------------------------- #

def read_last_hit_id() -> Optional[int]:
    """Fetch the previously-posted hit's product_purchases.id, or None."""
    return _read_last_id(HIT_STATE_FILENAME, HIT_LOCAL_FALLBACK_PATH)


def write_last_hit_id(hit_id: int) -> None:
    """Record the product_purchases.id of the hit we just posted."""
    _write_last_id(
        hit_id,
        HIT_STATE_FILENAME,
        HIT_LOCAL_FALLBACK_PATH,
        "just_pulled: hit_id",
    )
