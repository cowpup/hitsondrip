"""Hunt for the chase card → collection → pack linkage in DripShopLive.

Noah confirmed the linkage model is "collection tracking":
    product → collection mapping → pack mapping

So a chase card belongs to a collection, and a collection is associated
with one or more packs (box_breaks). We need to find:
  - The product → collection mapping table/column
  - The collection → box_break mapping table/column
Then chain them so we can resolve card_product_id → box_break_id at runtime.

Strategy: enumerate every table/column with "collection" in the name,
then probe each one in the context of a known chase product and a
known pack to figure out which links what.

Procedure (ONE Anthropic+MCP call, sonnet-4-6, ≤ 8 tool calls):

1. Find every public table whose name contains "collection":
     SELECT table_name FROM information_schema.tables
     WHERE table_schema='public' AND table_name ILIKE '%collection%';

2. Find every column whose name contains "collection":
     SELECT table_name, column_name, data_type
     FROM information_schema.columns
     WHERE table_schema='public' AND column_name ILIKE '%collection%'
     ORDER BY table_name, ordinal_position;

3. List columns of any table found in step 1 (information_schema, NOT
   describe_table — describe times out for some tables):
     SELECT column_name, data_type FROM information_schema.columns
     WHERE table_schema='public' AND table_name = '<table>'
     ORDER BY ordinal_position;

4. With the target pack title substring, find candidate box_break(s):
     SELECT id, title FROM box_breaks
     WHERE title ILIKE '%{target_substring}%'
     ORDER BY created_at DESC LIMIT 5;

5. Look for a connection from those box_breaks to collections:
     - Probe any table/column found in steps 1-3 that has both
       box_break_id and collection_id (or similar)
     - Or a JSONB field on box_breaks naming collection ids
     - Sample rows joining target box_break → collection table

6. Verify the product → collection path for a known chase card
   (user_id=65643, cert_number IS NOT NULL):
     - Find tables linking product_id to collection_id
     - Sample rows for a chase product

7. Trace the full chain end-to-end for ONE example:
   product_id → collection_id → box_break_id → bb.title, packImage

OUTPUT: Markdown report at data/card_pack_linkage_findings.md with:
  - The collections-related tables identified
  - The product→collection linking table + columns
  - The collection→box_break linking table + columns
  - End-to-end recommended SQL for resolve_pack_box_break(card_id)
  - Sample rows proving the chain works

Usage:
  uv run python -u -m tools.find_card_pack_linkage [pack_title_substring]

Defaults to "gengars gone wild".
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()
API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-sonnet-4-6"
MCP_BETA = "mcp-client-2025-11-20"
MCP_URL = "https://db-mcp-production.up.railway.app/sse"
MCP_NAME = "dripshoplive"

OUTPUT_PATH = Path("data") / "card_pack_linkage_findings.md"
TRANSCRIPT_PATH = Path("data") / "card_pack_linkage.transcript.txt"

PROMPT_TEMPLATE = """You have DripShopLive Postgres MCP access. STRICT budget: ≤ 8 tool calls.

GOAL: Find how a chase card connects to its pack via the "collection tracking" model:

    product → collection mapping → pack (box_break) mapping

We need the two linking tables + columns so we can resolve, at runtime:

    given card_product_id, return the pack's box_break_id + reveal_animation_data->>'packImage'

CONTEXT (confirmed):
- Chase cards: `products` WHERE user_id=65643 AND type='rip_and_ship' AND cert_number IS NOT NULL.
- Pack metadata: `box_breaks.id` (uuid), `box_breaks.title`, `box_breaks.reveal_animation_data->>'packImage'`.
- Noah confirmed the linkage IS in the DB via collection tracking — chase cards like the $25k Gengar are EXCLUSIVE to "Gengars Gone Wild" pack.
- DO NOT call describe_table on pull_game_pulls or box_breaks (timeouts). Use information_schema.

Target pack title substring (case-insensitive): "{target_substring}"

PROCEDURE:

1. PARALLEL — enumerate the "collection" namespace:
   1a. Tables: SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name ILIKE '%collection%' ORDER BY table_name;
   1b. Columns: SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND column_name ILIKE '%collection%' ORDER BY table_name, ordinal_position;
   1c. Target packs: SELECT id, title, created_at FROM box_breaks WHERE title ILIKE '%{target_substring}%' ORDER BY created_at DESC LIMIT 5;

2. Inspect column lists for the most-promising collection-related tables found in 1a (1-2 tables, use information_schema.columns). Look for tables that have BOTH a product_id and a collection_id (product→collection linking table), AND a separate table with collection_id and box_break_id (collection→pack linking table).

3. Probe product→collection: for a sample chase product (find the highest-value chase via a tiny query if helpful, e.g.
     SELECT id, name FROM products WHERE user_id=65643 AND cert_number IS NOT NULL AND cert_number != '' AND type='rip_and_ship' ORDER BY price DESC LIMIT 1;
   ), check which "product-collection" table has rows for that product_id. Print the collection_id(s).

4. Probe collection→pack: for one of the target box_break ids from 1c, find which collection_ids it's associated with. Compare to the collection_ids from step 3.

5. End-to-end chain query for ONE chase card to its pack:
     SELECT p.id AS card_id, p.name AS card_name,
            <collection_link>.collection_id,
            bb.id AS box_break_id, bb.title AS pack_title,
            bb.reveal_animation_data->>'packImage' AS pack_image_url
     FROM products p
     JOIN <product_collection_table> pc ON pc.product_id = p.id
     JOIN <collection_pack_table> cb ON cb.collection_id = pc.collection_id
     JOIN box_breaks bb ON bb.id = cb.box_break_id
     WHERE p.id = <chase_card_id>;
   This proves the chain works.

OUTPUT — Markdown report (only this, no prose around it):

```markdown
# Chase card → collection → pack linkage findings

## Collection-related tables and columns
| table | column | type | notes |
|---|---|---|---|

## Product → collection linkage
- **Table**: `...`
- **Columns**: `product_id`, `collection_id`, [other relevant]
- **Sample**: chase product X → collection(s) [...]

## Collection → pack linkage
- **Table**: `...`
- **Columns**: `collection_id`, `box_break_id`, [other relevant]
- **Sample**: collection X → pack(s) [...]

## End-to-end chain (sample row)
| card_id | card_name | collection_id | box_break_id | pack_title | pack_image_url |
|---|---|---|---|---|---|

## Recommended SQL for queries/pack_lookup_by_card.sql
```sql
SELECT bb.id AS box_break_id,
       bb.title AS pack_title,
       bb.reveal_animation_data->>'packImage' AS pack_image_url
FROM <product_collection_table> pc
JOIN <collection_pack_table> cb ON cb.collection_id = pc.collection_id
JOIN box_breaks bb ON bb.id = cb.box_break_id
WHERE pc.product_id = :card_product_id
  AND bb.reveal_animation_data ? 'packImage'
ORDER BY <reasonable tiebreaker>
LIMIT 1;
```

## Caveats and notes
- (Multiple collections per product? Multiple packs per collection? Soft-deletes? Surface anything weird.)
```
"""


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "gengars gone wild"
    print(f"Target pack title substring: {target!r}\n")

    client = anthropic.Anthropic(api_key=API_KEY)
    transcript: list[str] = []
    text_chunks: list[str] = []
    current_tool: dict[str, Any] = {}
    tool_call_count = 0

    def log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        transcript.append(line)

    log("Starting collection-linkage exploration...")
    started = time.time()

    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=6144,
            betas=[MCP_BETA],
            mcp_servers=[{"type": "url", "url": MCP_URL, "name": MCP_NAME}],
            tools=[{"type": "mcp_toolset", "mcp_server_name": MCP_NAME}],
            messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(target_substring=target)}],
        ) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_start":
                    cb = event.content_block
                    cb_type = getattr(cb, "type", "?")
                    if cb_type == "mcp_tool_use":
                        tool_call_count += 1
                        current_tool = {"input_partial": ""}
                        log(f"  [{tool_call_count}] tool start")
                    elif cb_type == "mcp_tool_result":
                        is_error = bool(getattr(cb, "is_error", False))
                        content = getattr(cb, "content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                getattr(c, "text", "") for c in content
                                if getattr(c, "type", None) == "text"
                            )
                        preview = str(content).replace("\n", " ")[:1500]
                        prefix = "      TOOL ERROR" if is_error else "      result"
                        log(f"{prefix}: {preview}")
                    elif cb_type == "text":
                        log("  [text]")
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", "?")
                    if dtype == "text_delta":
                        chunk = getattr(delta, "text", "")
                        text_chunks.append(chunk)
                        print(chunk, end="", flush=True)
                    elif dtype == "input_json_delta":
                        current_tool["input_partial"] = current_tool.get("input_partial", "") + getattr(delta, "partial_json", "")
                elif etype == "content_block_stop":
                    if "input_partial" in current_tool and current_tool["input_partial"]:
                        log(f"      input: {current_tool['input_partial'][:600]}")
                        current_tool = {}
                    else:
                        print(flush=True)
    except anthropic.APIError as e:
        log(f"API ERROR: {type(e).__name__}: {e}")

    elapsed = time.time() - started
    log(f"Elapsed: {elapsed:.1f}s, tool calls: {tool_call_count}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("".join(text_chunks), encoding="utf-8")
    TRANSCRIPT_PATH.write_text("\n".join(transcript), encoding="utf-8")
    log(f"Findings:   {OUTPUT_PATH}")
    log(f"Transcript: {TRANSCRIPT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
