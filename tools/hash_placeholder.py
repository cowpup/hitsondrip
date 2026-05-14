"""Compute a SHA-256 hash for a placeholder image URL.

Use this whenever a new placeholder is spotted in production (the
daily run's Slack message preview will tip you off, or main.py's logs
will flag suspiciously-small images). Paste the printed hash into
assets/placeholder_hashes.json under "placeholders".

Usage:
  uv run python -m tools.hash_placeholder <image-url> [<another-url> ...]

Example:
  uv run python -m tools.hash_placeholder \\
    https://cdn.dripshop.live/product/F4d4rBWtHHjajqiguuW76_thumbnail.webp
"""

from __future__ import annotations

import json
import sys
from datetime import date

from src.image_filter import fetch_image_bytes, hash_bytes


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    today = date.today().isoformat()
    for url in sys.argv[1:]:
        try:
            content = fetch_image_bytes(url)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {url}: {e}", file=sys.stderr)
            continue
        digest = hash_bytes(content)
        entry = {
            "sha256": digest,
            "added_at": today,
            "example_url": url,
            "size_bytes": len(content),
        }
        # Print the URL header and the JSON entry — easy to copy-paste
        # into assets/placeholder_hashes.json's "placeholders" array.
        print(f"\n# {url}  ({len(content):,} bytes)")
        print(json.dumps(entry, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
