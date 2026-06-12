"""Read/write de-dup state files on the `state` orphan branch.

Mirrors the image_host.py pattern: an orphan branch holds small state
files that the daily-cron jobs update after a successful run. Code
lives on `main`; the state branch never has any code committed to it.

Two independent automations each keep their own state file:
  - New Chase  → state/last_chase_card_id.txt  (the card_product_id of
    the last chase we posted; see new_chase.py)
  - Just Pulled → state/hit_backlog.json       (FIFO queue of pending
    $1k+ graded hits; see main.py + src/hit_backlog.py)

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
  New Chase:   read_last_card_id() / write_last_card_id(id)
  Just Pulled: read_hit_backlog() / write_hit_backlog(backlog)

Required env (same as image_host.py):
  GITHUB_TOKEN — auto-injected by Actions with `contents: write`.
  GITHUB_REPO  — "owner/repo" form.

Local-dev fallback:
  If GITHUB_TOKEN is missing, falls back to data/<file> so the
  pipeline can be exercised locally without touching real GitHub.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
STATE_BRANCH = "state"
DEFAULT_TIMEOUT_SECONDS = 30

# New Chase de-dup (card_product_id of the last posted chase).
CHASE_STATE_FILENAME = "state/last_chase_card_id.txt"
CHASE_LOCAL_FALLBACK_PATH = Path("data") / "last_chase_card_id.txt"

# Just Pulled de-dup + backlog (JSON queue of pending $1k+ graded hits).
HIT_BACKLOG_FILENAME = "state/hit_backlog.json"
HIT_BACKLOG_LOCAL_PATH = Path("data") / "hit_backlog.json"


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


def _read_raw_state(remote_filename: str, local_path: Path) -> Optional[str]:
    """Return the decoded UTF-8 contents of a state file, or None if it
    doesn't exist yet (Contents API 404, or no local fallback file).
    Raises StateBranchError on network / HTTP failure."""
    env = _resolve_repo()
    if env is None:
        if local_path.exists():
            try:
                return local_path.read_text(encoding="utf-8")
            except OSError:
                return None
        return None

    owner, repo, token = env
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{remote_filename}"
    try:
        response = requests.get(
            url, headers=_api_headers(token),
            params={"ref": STATE_BRANCH}, timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise StateBranchError(f"GitHub GET {url}: {e}") from e

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise StateBranchError(
            f"GitHub GET {url} → HTTP {response.status_code}: "
            f"{response.text[:400]}"
        )
    encoded = response.json().get("content", "")
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _write_raw_state(
    text: str, remote_filename: str, local_path: Path, commit_message: str
) -> None:
    """Overwrite a state file with `text`. Raises StateBranchError on
    API failure."""
    env = _resolve_repo()
    if env is None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(text, encoding="utf-8")
        return

    owner, repo, token = env
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{remote_filename}"
    try:
        sha_response = requests.get(
            url, headers=_api_headers(token),
            params={"ref": STATE_BRANCH}, timeout=DEFAULT_TIMEOUT_SECONDS,
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

    body: dict[str, object] = {
        "message": commit_message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": STATE_BRANCH,
    }
    if existing_sha:
        body["sha"] = existing_sha

    try:
        put_response = requests.put(
            url, headers=_api_headers(token), json=body,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise StateBranchError(f"GitHub PUT {url}: {e}") from e

    if put_response.status_code not in (200, 201):
        raise StateBranchError(
            f"GitHub PUT {url} → HTTP {put_response.status_code}: "
            f"{put_response.text[:400]}"
        )


def _read_last_id(remote_filename: str, local_path: Path) -> Optional[int]:
    raw = _read_raw_state(remote_filename, local_path)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _write_last_id(
    value: int,
    remote_filename: str,
    local_path: Path,
    commit_prefix: str,
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _write_raw_state(
        f"{value}\n", remote_filename, local_path,
        f"{commit_prefix}={value} @ {ts}",
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


# --- Just Pulled backlog ------------------------------------------------ #

def read_hit_backlog() -> Optional[dict]:
    """Return the parsed hit-backlog dict, or None if it doesn't exist
    yet (first run) or is corrupt. Raises StateBranchError on network
    failure (caller should fail closed)."""
    raw = _read_raw_state(HIT_BACKLOG_FILENAME, HIT_BACKLOG_LOCAL_PATH)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("hit_backlog.json is corrupt (%s); treating as empty", e)
        return None
    if not isinstance(parsed, dict):
        log.warning(
            "hit_backlog.json is not a JSON object (got %s); treating as corrupt",
            type(parsed).__name__,
        )
        return None
    return parsed


def write_hit_backlog(backlog: dict) -> None:
    """Persist the hit-backlog dict to the state branch."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    n = len(backlog.get("queue", []))
    text = json.dumps(backlog, indent=2, ensure_ascii=False) + "\n"
    _write_raw_state(
        text, HIT_BACKLOG_FILENAME, HIT_BACKLOG_LOCAL_PATH,
        f"just_pulled: backlog ({n} queued) @ {ts}",
    )
