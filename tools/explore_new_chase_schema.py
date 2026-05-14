"""Phase C — DripShopLive schema exploration for the New Chase pipeline.

Goal: discover two things via the DripShopLive Postgres MCP server.

1. The "card added to pack inventory" event.
   - There must be a table (or column) that records when a graded card
     slab becomes AVAILABLE in a pack's pool — NOT when the card is
     pulled. The New Chase trigger fires the moment a high-value chase
     is added to inventory, before anyone has won it.
   - Likely candidates: `box_break_items`, `pack_contents`, an
     `inventory_*` table, or `products.parent_id` where child=card and
     parent=pack-product.
   - Whatever the row is, it needs a `created_at` (the "added" stamp)
     and a way to join to (a) the card product and (b) the pack
     product / pack price.

2. The raw pack image.
   - `box_breaks.image` is the marketing graphic with text overlays
     baked in. New Chase needs the *bare* pack render (transparent PNG
     at cdn.dripshop.live/product/<id>.png).
   - Hypothesis: there is a "pack product" row in `products` for each
     pack SKU, and `products.image` on that row is the raw pack image.
     We need to identify the FK that links a box_break (or whatever
     inventory row we found in step 1) to the pack-product in
     `products`.

Tightly scoped, sonnet-4-6, ≤ 5 MCP calls, streaming so we see progress.
Saves transcript + a structured findings JSON for me to read after.

Usage:
  uv run python -u -m tools.explore_new_chase_schema
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

# Windows console is cp1252 by default and crashes on arrows/em-dashes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("ERROR: ANTHROPIC_API_KEY missing from .env", file=sys.stderr)
    sys.exit(2)

MODEL = "claude-sonnet-4-6"
MCP_BETA = "mcp-client-2025-11-20"
MCP_URL = "https://db-mcp-production.up.railway.app/sse"
MCP_NAME = "dripshoplive"

TRANSCRIPT_PATH = Path("data") / "new_chase_schema.transcript.txt"
FINDINGS_PATH = Path("data") / "new_chase_schema_findings.md"

PROMPT = """You have access to the DripShopLive Postgres database via MCP tools.

CONTEXT (already discovered, do NOT re-explore):
- `product_purchases` (pp): id, product_id (= the CARD pulled), unit_price (pack price paid), created_at, fulfilment_partner_id, ...
- `products`: id, name, image, price, cert_number (NON-NULL = PSA/CGC/BGS graded), fulfilment_partner_id, parent_id. Some rows are CARDS, some are PACKS (SKUs).
- `pull_game_pulls` (pgp): id (uuid), user_id, box_break_id (uuid), purchase_id (int → pp.id), value (numeric, dollars). DO NOT describe_table this — it times out.
- `box_breaks` (bb): id (uuid), title (pack name), image (marketing graphic with text baked in), description, ... Columns NOT fully enumerated yet — use information_schema.

GOAL: answer the two questions below. Output a Markdown findings doc at the end.

QUESTION 1 — "Card added to pack inventory" event.
  A graded card slab becomes AVAILABLE in a pack's pool BEFORE anyone pulls it.
  Find the table that records this. It needs:
    - a column joining to the card product (e.g. card_id, product_id pointing at a graded products row)
    - a column joining to the pack (box_break_id OR a pack-product id)
    - a created_at / added_at timestamp
  Candidate names to look for in information_schema.tables:
    box_break_items, box_break_products, pack_contents, pack_items,
    inventory_items, pull_game_items, slab_inventory, anything with
    "_items" or "_contents" or "inventory" in the name.

QUESTION 2 — Raw pack image.
  `box_breaks.image` is a marketing graphic. We need the bare pack
  render (transparent PNG at cdn.dripshop.live/product/<id>.png).
  Hypothesis: there is a pack-product row in `products` for each pack
  SKU, and `products.image` on that row is the raw pack image. Find
  the FK linking a box_break (or whatever inventory row from Q1) to
  that pack-product in `products`.

STRICT BUDGET: ≤ 6 MCP tool calls total.

PROCESS:

1. Discover `box_breaks` columns:
     SELECT column_name, data_type FROM information_schema.columns
     WHERE table_name = 'box_breaks' ORDER BY ordinal_position;
   Look especially for a product_id / pack_product_id / parent_id column.

2. List candidate inventory tables:
     SELECT table_name FROM information_schema.tables
     WHERE table_schema = 'public'
       AND (table_name ILIKE '%item%' OR table_name ILIKE '%content%'
            OR table_name ILIKE '%inventory%' OR table_name ILIKE '%slot%'
            OR table_name ILIKE '%pool%' OR table_name ILIKE '%slab%')
     ORDER BY table_name;

3. For the 1-2 most likely candidates, dump their columns:
     SELECT column_name, data_type FROM information_schema.columns
     WHERE table_name = '<candidate>' ORDER BY ordinal_position;

4. Sample a few rows to confirm semantics (one query, max):
     SELECT * FROM <best_candidate>
     WHERE created_at >= NOW() - INTERVAL '24 hours'
     LIMIT 3;

5. If box_breaks has a pack_product fk, sample the join:
     SELECT bb.id, bb.title, bb.image AS marketing_image,
            p.id AS pack_product_id, p.name AS pack_product_name,
            p.image AS raw_pack_image, p.price AS pack_price
     FROM box_breaks bb
     JOIN products p ON p.id = bb.<the_fk_column>
     LIMIT 3;

FINAL OUTPUT (exactly this Markdown doc, no prose around it):

```markdown
# Phase C — Schema findings

## Q1: Card added to pack inventory
- **Table**: `<table_name>`
- **Timestamp column**: `<column>` (type: <data_type>)
- **Card FK**: `<column>` → `products.id`
- **Pack FK**: `<column>` → `<table>.<column>` (`box_breaks.id` or `products.id`)
- **Sample row count in last 24h**: <number>
- **Notes**: <anything notable — null patterns, soft-delete columns, etc.>

## Q2: Raw pack image
- **Pack-product link**: `box_breaks.<fk_column>` → `products.id`  (or "no FK — pack metadata only on box_breaks.image")
- **Raw image column**: `products.image` on the pack-product row  (or whatever the actual answer is)
- **Pack price column**: `products.price` on the pack-product row  (or `product_purchases.unit_price` if no pack-product link exists)
- **Notes**: <anything — null patterns, multiple images, etc.>

## Recommended SQL for queries/new_chase.sql
```sql
<the actual query, with WHERE filter `card.cert_number IS NOT NULL` AND inventory added in last 24h AND card_value >= 10 * pack_price, ORDER BY card.price DESC LIMIT 5>
```
```
"""


def main() -> int:
    client = anthropic.Anthropic(api_key=API_KEY)
    transcript: list[str] = []
    text_chunks: list[str] = []
    tool_call_count = 0
    current_tool: dict[str, Any] = {}

    def log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        transcript.append(line)

    log(f"Model: {MODEL}")
    log(f"MCP: {MCP_URL}")
    log("Starting stream...")

    started = time.time()
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=8192,
            betas=[MCP_BETA],
            mcp_servers=[{"type": "url", "url": MCP_URL, "name": MCP_NAME}],
            tools=[{"type": "mcp_toolset", "mcp_server_name": MCP_NAME}],
            messages=[{"role": "user", "content": PROMPT}],
        ) as stream:
            for event in stream:
                etype = getattr(event, "type", None)

                if etype == "content_block_start":
                    cb = event.content_block
                    cb_type = getattr(cb, "type", "?")
                    if cb_type == "mcp_tool_use":
                        tool_call_count += 1
                        current_tool = {
                            "name": getattr(cb, "name", "?"),
                            "server": getattr(cb, "server_name", "?"),
                            "input_partial": "",
                        }
                        log(f"  [{tool_call_count}] tool start: "
                            f"{current_tool['server']}.{current_tool['name']}")
                    elif cb_type == "mcp_tool_result":
                        is_error = bool(getattr(cb, "is_error", False))
                        content = getattr(cb, "content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                getattr(c, "text", "") for c in content
                                if getattr(c, "type", None) == "text"
                            )
                        preview = str(content).replace("\n", " ")[:500]
                        prefix = "      TOOL ERROR" if is_error else "      result"
                        log(f"{prefix}: {preview}")
                    elif cb_type == "text":
                        log("  [text block started]")

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", "?")
                    if dtype == "text_delta":
                        chunk = getattr(delta, "text", "")
                        text_chunks.append(chunk)
                        print(chunk, end="", flush=True)
                    elif dtype == "input_json_delta":
                        chunk = getattr(delta, "partial_json", "")
                        current_tool["input_partial"] = (
                            current_tool.get("input_partial", "") + chunk
                        )

                elif etype == "content_block_stop":
                    if current_tool.get("name"):
                        log(f"      input: {current_tool.get('input_partial', '')[:500]}")
                        current_tool = {}
                    else:
                        print(flush=True)

                elif etype == "message_stop":
                    log("Stream complete.")

                elif etype == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        log(f"  usage delta: in={getattr(usage, 'input_tokens', '?')} "
                            f"out={getattr(usage, 'output_tokens', '?')}")

            final = stream.get_final_message()
    except anthropic.APIError as e:
        log(f"API ERROR: {type(e).__name__}: {e}")
        TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRANSCRIPT_PATH.write_text("\n".join(transcript), encoding="utf-8")
        return 1

    elapsed = time.time() - started
    log(f"Total elapsed: {elapsed:.1f}s")
    log(f"Total tool calls: {tool_call_count}")
    usage = getattr(final, "usage", None)
    if usage is not None:
        log(f"Final usage: in={usage.input_tokens} out={usage.output_tokens}")

    TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_PATH.write_text("\n".join(transcript), encoding="utf-8")
    log(f"Transcript: {TRANSCRIPT_PATH}")

    full_text = "".join(text_chunks)
    FINDINGS_PATH.write_text(full_text, encoding="utf-8")
    log(f"Findings: {FINDINGS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
