"""Read/write the de-dup state file on the `state` orphan branch.

Mirrors the image_host.py pattern: an orphan branch holds a single
file that the daily-cron job updates after a successful run. Code
lives on `main`; the state branch never has any code committed to it.

Why an orphan branch over actions/cache:
  - GitHub Actions caches can be evicted at 7 days of inactivity OR
    when the repo hits the 10GB cache cap. Silent eviction = duplicate
    posts. The state branch is durable forever.
  - Same operational pattern Noah already understands from daily-output.
  - ~1KB/day churn is trivial; the orphan branch's history is itself
    an audit log of which chase was posted on which day.

Contract:
  read_last_card_id() — returns the int card_product_id from the file,
      or None if the file/branch doesn't exist yet (first run).
  write_last_card_id(card_id) — overwrites the file with the new id and
      commits via the Contents API.

Required env (same as image_host.py):
  GITHUB_TOKEN — auto-injected by Actions with `contents: write`.
  GITHUB_REPO  — "owner/repo" form.

Local-dev fallback:
  If GITHUB_TOKEN is missing, falls back to data/last_chase_card_id.txt
  so the pipeline can be exercised locally without touching real GitHub.
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
STATE_FILENAME = "state/last_chase_card_id.txt"
DEFAULT_TIMEOUT_SECONDS = 30

LOCAL_FALLBACK_PATH = Path("data") / "last_chase_card_id.txt"


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


def read_last_card_id() -> Optional[int]:
    """Fetch the previously-posted card_product_id, or None on first run.

    A 404 from the Contents API (file or branch doesn't exist yet) is
    the expected first-run state — return None so the caller treats any
    candidate as new. Any other failure raises StateBranchError.
    """
    env = _resolve_repo()
    if env is None:
        # Local fallback
        if LOCAL_FALLBACK_PATH.exists():
            try:
                return int(LOCAL_FALLBACK_PATH.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return None
        return None

    owner, repo, token = env
    url = (
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{STATE_FILENAME}"
    )
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


def write_last_card_id(card_product_id: int) -> None:
    """Overwrite the state file with the given id.

    Uses the Contents API PUT, which requires the existing file's SHA
    when updating. We fetch that SHA inline (one extra round-trip per
    run) rather than caching it from read_last_card_id, so this
    function works correctly even when called from a different process.

    Args:
        card_product_id: The int id of the card we just posted.

    Raises StateBranchError on API failure.
    """
    env = _resolve_repo()
    if env is None:
        LOCAL_FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_FALLBACK_PATH.write_text(f"{card_product_id}\n", encoding="utf-8")
        return

    owner, repo, token = env
    url = (
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{STATE_FILENAME}"
    )

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
        "message": f"new_chase: card_id={card_product_id} @ {ts}",
        "content": base64.b64encode(
            f"{card_product_id}\n".encode("utf-8")
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
