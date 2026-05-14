"""Probe each hop of the chase → collection → pack chain independently.

When the end-to-end chain returns 0 rows, this isolates which hop is
missing. Runs four diagnostic queries in ONE prompt:

  Q1: Does product_collection_mappings have this product_id at all?
  Q2: What collection_ids is it mapped to?
  Q3: Are any of those collections linked to box_breaks via
      box_break_spot_mappings?
  Q4: Of any resolved box_breaks, which have packImage in JSONB?

Also takes a second card_id for cross-check (defaults to the Hitmonchan
we've been testing, id 2797407).

Usage:
  uv run python -u -m tools.debug_chain [card_id_1] [card_id_2]
"""

from __future__ import annotations

import os
import sys
import time

import anthropic
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()
API_KEY = os.environ["ANTHROPIC_API_KEY"]


def main() -> int:
    card1 = int(sys.argv[1]) if len(sys.argv) > 1 else 2797407   # Hitmonchan
    card2 = int(sys.argv[2]) if len(sys.argv) > 2 else 2692316   # Pikachu test

    prompt = f"""Run these 4 diagnostic queries (in PARALLEL via separate tool calls) and return results as a single JSON object. No prose.

Card IDs under test: {card1}, {card2}

Q1 — Are these card_ids present in product_collection_mappings?
  SELECT product_id, collection_id, created_at
  FROM product_collection_mappings
  WHERE product_id IN ({card1}, {card2})
  ORDER BY product_id;

Q2 — For each card, what's the products row look like?
  SELECT id, name, user_id, cert_number, type, parent_id, box_break_collection_ref_id
  FROM products
  WHERE id IN ({card1}, {card2});

Q3 — Find any collections referenced by these products (via any path)
     and check which box_breaks they're linked to.
  WITH card_collections AS (
    SELECT product_id, collection_id
    FROM product_collection_mappings
    WHERE product_id IN ({card1}, {card2})
  )
  SELECT cc.product_id, cc.collection_id,
         bsm.box_break_id,
         bb.title AS pack_title,
         bb.reveal_animation_data ? 'packImage' AS has_packimage
  FROM card_collections cc
  LEFT JOIN box_break_spot_mappings bsm ON bsm.collection_id = cc.collection_id
  LEFT JOIN box_breaks bb ON bb.id = bsm.box_break_id
  LIMIT 20;

Q4 — Independent: pick the Gengars Gone Wild pack (id 30e1b4b5-7d3e-4cda-a2f4-ce36aa6fcab0)
     and find which chase products are linked to its collection(s).
  WITH gengar_collections AS (
    SELECT DISTINCT collection_id
    FROM box_break_spot_mappings
    WHERE box_break_id = '30e1b4b5-7d3e-4cda-a2f4-ce36aa6fcab0'
      AND collection_id IS NOT NULL
  )
  SELECT pcm.product_id, p.name, p.user_id, p.cert_number, p.price,
         gc.collection_id
  FROM gengar_collections gc
  JOIN product_collection_mappings pcm ON pcm.collection_id = gc.collection_id
  JOIN products p ON p.id = pcm.product_id
  WHERE p.user_id = 65643
    AND p.cert_number IS NOT NULL
    AND p.cert_number != ''
  ORDER BY p.price DESC NULLS LAST
  LIMIT 10;

Output one JSON object: {{"q1": [...], "q2": [...], "q3": [...], "q4": [...]}}.
If any query returns no rows, set its key to an empty array.
"""

    client = anthropic.Anthropic(api_key=API_KEY)
    started = time.time()
    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        betas=["mcp-client-2025-11-20"],
        mcp_servers=[{"type": "url", "url": "https://db-mcp-production.up.railway.app/sse", "name": "drip"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "drip"}],
        messages=[{"role": "user", "content": prompt}],
    )

    text_parts: list[str] = []
    tool_errors: list[str] = []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", ""))
        elif btype == "mcp_tool_result":
            if getattr(block, "is_error", False):
                content = getattr(block, "content", "")
                tool_errors.append(str(content)[:400])

    elapsed = time.time() - started
    print(f"Elapsed: {elapsed:.1f}s")
    if tool_errors:
        print(f"\n{len(tool_errors)} tool error(s):")
        for e in tool_errors:
            print(f"  - {e}")
    print("\nModel response:\n")
    print("".join(text_parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
