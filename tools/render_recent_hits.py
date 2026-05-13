"""Render every entry in data/recent_hits.json using the locked renderer.

Reads data/recent_hits.json (written by tools/fetch_recent_hits.py) and
calls src.renderer.render_just_pulled for each entry, writing PNGs to
data/recent_renders/hit_NN.png.

This is the variety-test step: we want to see 10 different real
DripShopLive datasets pushed through the renderer to verify the layout
holds across diverse card images, pack thumbnails, name lengths, and
price/value magnitudes.

Pack-name transform here is a temporary stand-in (uppercase only) —
the real cleanup logic lives in src/string_transforms.py once that's
written. For variety testing the raw uppercase is fine; the renderer
handles long strings via auto-sizing.

Usage:
  uv run python -m tools.render_recent_hits
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Windows console fix — same as in tools/fetch_recent_hits.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow `python -m tools.render_recent_hits` to find src.renderer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.renderer import render_just_pulled, RenderError  # noqa: E402

HITS_PATH = Path("data") / "recent_hits.json"
OUTPUT_DIR = Path("data") / "recent_renders"

# Fallbacks for hits where pack_name / pack_image_url come back null.
# The DripShopLive schema doesn't store pack linkage on the product row
# for instant packs — real pack info lives on product_purchases.order_title
# and .preview_image, which the current query doesn't yet pull. Using
# Moltres pack as a placeholder lets us still verify card-image variety,
# auto-sizer behavior on long card_name text, and hit_value formatting.
FALLBACK_PACK_IMAGE_URL = "https://cdn.dripshop.live/product/_tpbM51S6K806mAwwCV5E.png"
FALLBACK_PACK_NAME = "INSTANT PACK"


def main() -> int:
    if not HITS_PATH.exists():
        print(
            f"ERROR: {HITS_PATH} not found. "
            f"Run `uv run python -m tools.fetch_recent_hits` first.",
            file=sys.stderr,
        )
        return 1

    hits = json.loads(HITS_PATH.read_text(encoding="utf-8"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    successes = 0
    skipped = 0
    for i, h in enumerate(hits, 1):
        out = OUTPUT_DIR / f"hit_{i:02d}.png"
        # Skip rows where hit_value is null — production behavior per the
        # briefing's `ORDER BY pgp.value DESC NULLS LAST` clause.
        if h.get("hit_value") is None:
            print(f"  {i:2d}. {out.name}  SKIP (null hit_value)")
            skipped += 1
            continue
        try:
            pack_url = h.get("pack_image_url") or FALLBACK_PACK_IMAGE_URL
            pack_name = _quick_pack_name(h.get("pack_name") or FALLBACK_PACK_NAME)
            render_just_pulled(
                card_image_url=h["card_image_url"],
                pack_image_url=pack_url,
                pack_name=pack_name,
                pack_price=int(round(float(h["pack_price"]))),
                hit_value=float(h["hit_value"]),
                output_path=out,
            )
            print(f"  {i:2d}. {out.name}  OK  (${h.get('hit_value')} hit)")
            successes += 1
        except (RenderError, KeyError, TypeError, ValueError) as e:
            print(f"  {i:2d}. {out.name}  FAIL: {type(e).__name__}: {e}")

    expected = len(hits) - skipped
    print(
        f"\n{successes}/{expected} renders OK "
        f"({skipped} skipped for null hit_value). "
        f"Outputs in {OUTPUT_DIR.resolve()}"
    )
    return 0 if successes == expected else 1


def _quick_pack_name(name: str) -> str:
    """Temporary cleanup until src/string_transforms.py is written."""
    import re
    cleaned = re.sub(r"\bpok[eé]mon\b", "", name, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.upper()


if __name__ == "__main__":
    sys.exit(main())
