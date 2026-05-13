"""Publish the daily rendered PNG to a GitHub orphan branch as the public host.

Strategy:
  - The repo's `daily-output` branch is an ORPHAN branch that holds only
    the daily PNGs (and their commit history). Code lives on `main`; the
    image branch never has any code committed to it. Keeps the diff
    history of main clean.
  - Each daily run overwrites `latest.png` on `daily-output`. The previous
    day's PNG is still in git history (via the SHA) but `latest.png`
    always points at today's image.
  - The returned URL is `https://raw.githubusercontent.com/<owner>/<repo>/daily-output/latest.png`.
    This is publicly fetchable because the repo is public; Metricool can
    download it server-side at publish time (6pm PT).
  - `metricool.normalize_image_url()` should be called immediately after
    scheduling so Metricool snapshots the image onto its own CDN. After
    that, the GitHub URL becoming overwritten the next day is harmless.

Required env:
    GITHUB_TOKEN  — provided automatically by GitHub Actions (Workflow has
                    `permissions: contents: write`). Locally, a PAT with
                    `repo` scope works for testing.
    GITHUB_REPO   — "<owner>/<repo>" form, e.g. "cowpup/hitsondrip"

Local-dev fallback:
    If GITHUB_TOKEN is missing, `publish_to_github` writes the PNG to
    `data/last_host_output.png` and returns a `file://` URL. Lets the
    rest of the pipeline run locally without touching real GitHub.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_BRANCH = "daily-output"
DEFAULT_FILENAME = "latest.png"
DEFAULT_TIMEOUT_SECONDS = 30


class ImageHostError(RuntimeError):
    """Raised on upload / auth / contract failures with the GitHub Contents API."""


def _raw_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _existing_file_sha(
    owner: str, repo: str, branch: str, path: str, token: str
) -> Optional[str]:
    """Return the file's current SHA on `branch`, or None if it doesn't exist.

    The Contents API requires the existing SHA when overwriting a file.
    A 404 means the file is new, which is fine — we omit `sha` in that case.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    try:
        response = requests.get(
            url,
            headers=_api_headers(token),
            params={"ref": branch},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise ImageHostError(f"GitHub GET {url}: {e}") from e

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise ImageHostError(
            f"GitHub GET {url} → HTTP {response.status_code}: "
            f"{response.text[:400]}"
        )
    return response.json().get("sha")


def publish_to_github(
    png_bytes: bytes,
    filename: str = DEFAULT_FILENAME,
    *,
    branch: str = DEFAULT_BRANCH,
    commit_message: Optional[str] = None,
) -> str:
    """Upload `png_bytes` as `filename` to `branch` and return its public URL.

    The PUT /contents endpoint creates a new file if `filename` doesn't
    exist on the branch, or updates the existing file (needs its current
    SHA). Either way the file ends up at the same path and the same
    raw URL.

    Args:
        png_bytes: The full PNG bytes to upload.
        filename: The path on the orphan branch. Defaults to "latest.png".
        branch: Target branch. Defaults to "daily-output". The branch must
            exist on the remote — see README for one-time setup.
        commit_message: Override the default commit message.

    Returns the public raw.githubusercontent.com URL.

    Raises ImageHostError on any HTTP / contract failure.

    Local-dev fallback: if GITHUB_TOKEN is missing, writes the PNG to
    data/last_host_output.png and returns a file:// URL.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo_spec = os.environ.get("GITHUB_REPO")

    if not token:
        # Local fallback — no real upload. Write to disk and return a
        # file:// URL so the rest of the pipeline can be run locally.
        out_path = Path("data") / "last_host_output.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(png_bytes)
        return out_path.resolve().as_uri()

    if not repo_spec or "/" not in repo_spec:
        raise ImageHostError(
            "GITHUB_REPO is not set or malformed (expected 'owner/repo')"
        )
    owner, _, repo = repo_spec.partition("/")

    if commit_message is None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        commit_message = f"daily: {filename} @ {ts}"

    existing_sha = _existing_file_sha(owner, repo, branch, filename, token)

    body: dict[str, object] = {
        "message": commit_message,
        "content": base64.b64encode(png_bytes).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        body["sha"] = existing_sha

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{filename}"
    try:
        response = requests.put(
            url,
            headers=_api_headers(token),
            json=body,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise ImageHostError(f"GitHub PUT {url}: {e}") from e

    if response.status_code not in (200, 201):
        raise ImageHostError(
            f"GitHub PUT {url} → HTTP {response.status_code}: "
            f"{response.text[:400]}"
        )

    return _raw_url(owner, repo, branch, filename)
