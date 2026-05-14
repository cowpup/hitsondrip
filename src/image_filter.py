"""Detect placeholder card images before they get rendered into a daily post.

Some DripShopLive products don't have a real photo uploaded — their
image URL points to a generic placeholder. The URL pattern itself
isn't diagnostic (legit images share suffixes with placeholders), so
we rely on **content fingerprinting** instead:

  1. Download the candidate image.
  2. SHA-256 the bytes.
  3. Look up the hash in assets/placeholder_hashes.json.
  4. If it matches, skip this hit and try the next one.

A suspiciously-small-file warning logs alongside the hash check so
new placeholder hashes can be spotted in production logs and added
to the blacklist.

To add a new placeholder to the blacklist:
  uv run python -m tools.hash_placeholder <image-url>
Then paste the printed hash into assets/placeholder_hashes.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

BLACKLIST_PATH = Path("assets") / "placeholder_hashes.json"
DEFAULT_TIMEOUT_SECONDS = 15

# Placeholder images tend to be small. Anything below this is logged
# as a warning so we can investigate and add it to the blacklist if
# it turns out to be a placeholder. NOT used as a hard filter — the
# blacklist is the source of truth.
SUSPICIOUS_SIZE_BYTES = 20_000


def load_blacklist(path: Path = BLACKLIST_PATH) -> set[str]:
    """Return the set of SHA-256 hex digests of known placeholder images."""
    if not path.exists():
        log.warning(
            "Placeholder blacklist not found at %s; no images will be filtered",
            path,
        )
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("Placeholder blacklist not valid JSON (%s): %s", path, e)
        return set()
    entries = raw.get("placeholders") or []
    return {entry["sha256"].lower().strip() for entry in entries if entry.get("sha256")}


def fetch_image_bytes(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    """Download an image, return raw bytes. Raises on HTTP / network failure."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def is_placeholder(
    url: str,
    blacklist: Optional[set[str]] = None,
) -> tuple[bool, str]:
    """Check whether the image at `url` should be skipped.

    Returns ``(is_blocked, reason)``.

    Download or hash failures are treated as NOT blocked — let the
    renderer surface the real error. This avoids accidentally skipping
    a good hit because of a transient network blip.
    """
    if blacklist is None:
        blacklist = load_blacklist()
    try:
        content = fetch_image_bytes(url)
    except Exception as e:  # noqa: BLE001 — surface error details
        log.warning("Could not fetch %s for placeholder check: %s", url, e)
        return False, f"fetch failed: {e}"

    digest = hash_bytes(content)
    size = len(content)

    if digest in blacklist:
        return True, f"blacklisted hash {digest} ({size} bytes)"

    if size < SUSPICIOUS_SIZE_BYTES:
        log.warning(
            "Candidate image is suspiciously small (%d bytes, hash=%s). "
            "If this is a placeholder, add the hash to %s. URL: %s",
            size, digest, BLACKLIST_PATH, url,
        )

    return False, f"ok ({size} bytes, hash={digest[:12]}...)"
