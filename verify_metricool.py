"""Smoke-test Metricool REST: list brands, run find_instagram_brand().

Does NOT schedule anything. Read-only — validates that the token, user ID,
and base URL are correct, and surfaces the brand-list shape so we can confirm
the "drip" name match works (or decide to set METRICOOL_BLOG_ID).

Run: uv run python verify_metricool.py
Exit 0 on success, 1 on any failure.
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

from src.metricool import (
    MetricoolError,
    _normalize_brand,
    find_instagram_brand,
    list_brands,
)

load_dotenv()


def main() -> int:
    print("=== Metricool REST verify ===\n")

    # Step 1: list brands
    try:
        brands = list_brands()
    except MetricoolError as e:
        print(f"FAIL (list_brands): {e}")
        return 1

    if not brands:
        print("FAIL: Metricool returned zero brands. Check token/user ID.")
        return 1

    print(f"Found {len(brands)} brand(s):\n")
    for idx, brand in enumerate(brands):
        try:
            nb = _normalize_brand(brand)
            print(f"  [{idx}] blog_id={nb['blog_id']}  "
                  f"timezone={nb['timezone']!r}  name={nb['name']!r}")
        except MetricoolError as e:
            print(f"  [{idx}] NORMALIZATION FAILED: {e}")
            print(f"        raw keys: {sorted(brand.keys())}")

    # Show raw first-brand shape so we can confirm field names if needed
    print("\nFirst brand raw payload (top-level keys only):")
    first = brands[0]
    if isinstance(first, dict):
        for k in sorted(first.keys()):
            v = first[k]
            preview = v if not isinstance(v, (dict, list)) else f"<{type(v).__name__}>"
            print(f"  {k}: {preview!r}")
    else:
        print(f"  (unexpected type: {type(first).__name__})")
        print(f"  full: {json.dumps(first, default=str)[:500]}")

    # Step 2: run find_instagram_brand with its default name match
    import inspect
    default_match = inspect.signature(find_instagram_brand).parameters["name_contains"].default
    print(f"\n--- find_instagram_brand(name_contains={default_match!r}) ---")
    override = os.environ.get("METRICOOL_BLOG_ID")
    if override:
        print(f"  (METRICOOL_BLOG_ID={override} is set — will take precedence)")
    try:
        picked = find_instagram_brand(brands)
    except MetricoolError as e:
        print(f"  FAIL: {e}")
        print("\nNext step: set METRICOOL_BLOG_ID in .env to the correct blog_id "
              "from the list above.")
        return 1

    print(f"  PASS — picked:")
    print(f"    blog_id:  {picked['blog_id']}")
    print(f"    timezone: {picked['timezone']!r}")
    print(f"    name:     {picked['name']!r}")
    print("\nAll Metricool REST checks passed. Token, user ID, and brand "
          "resolution all work. No post was scheduled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
