"""Standalone tester for the pack-image lookup automation.

Verifies that new_chase.resolve_pack_image() correctly pulls the
`reveal_animation_data->>'packImage'` URL out of box_breaks for a
given UUID, without running the full new_chase.py pipeline (no
render, no upload, no Slack noise, no state-branch churn).

Usage:
  uv run python -u -m tools.test_pack_lookup <box_break_uuid>

Examples:
  # Collector's Jam Exclusive - Silver Pokemon Slab Pack (probed
  # 2026-05-14 by Noah; expected URL is the .png ending in fsSd9...)
  uv run python -u -m tools.test_pack_lookup 77267e10-bc4d-4873-9f1b-fce82a2be2d5

  # Invalid UUID — expects RuntimeError at config-load validation
  uv run python -u -m tools.test_pack_lookup not-a-uuid

  # Real UUID that doesn't exist in box_breaks — expects fallback
  # to pack_image_url in config (warning logged)
  uv run python -u -m tools.test_pack_lookup 00000000-0000-0000-0000-000000000000

Exit codes:
  0  lookup succeeded (DB lookup OR fallback to config URL)
  1  config validation failed OR resolution raised (both fallbacks empty)
  2  no UUID argument given
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    uuid_arg = sys.argv[1].strip()

    # Import after the argv check so --help-style usage doesn't pay
    # the import cost (anthropic + dotenv).
    from new_chase import load_featured_pack, resolve_pack_image

    # Load real config so the fallback URL is whatever Noah actually
    # has set. Then OVERRIDE the box_break_id to the test UUID — this
    # lets us probe arbitrary UUIDs without committing them.
    try:
        pack = load_featured_pack()
    except Exception as e:  # noqa: BLE001
        print(f"\nFAIL: config validation: {e}", file=sys.stderr)
        return 1

    # Re-validate the test UUID through the same code path the harness
    # uses (so passing "not-a-uuid" surfaces the right error message).
    import uuid as _uuid
    try:
        _uuid.UUID(uuid_arg)
    except (ValueError, AttributeError) as e:
        print(f"\nFAIL: '{uuid_arg}' is not a valid UUID: {e}", file=sys.stderr)
        return 1

    pack["pack_box_break_id"] = uuid_arg

    print(f"\n→ Probing box_break_id = {uuid_arg}")
    print(f"  Config fallback URL    = {pack.get('pack_image_url')!r}")

    try:
        url = resolve_pack_image(pack)
    except Exception as e:  # noqa: BLE001
        print(f"\nFAIL: resolve_pack_image raised: {e}", file=sys.stderr)
        return 1

    print(f"\n✓ Resolved pack image URL: {url}")
    if url == pack.get("pack_image_url"):
        print("  (NOTE: matched the config fallback URL — see the log "
              "above; lookup may have returned no rows or null packImage)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
