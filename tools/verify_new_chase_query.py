"""Phase C verification — confirm columns + sanity-check the New Chase query.

After tools/explore_new_chase_schema.py identified `products.parent_id`
as the card→pack link and `products.created_at` as the inventory-added
timestamp, this tool:

1. Confirms `products` has `created_at` (and lists all columns) — the
   schema explorer assumed this without explicit verification.
2. Runs the draft New Chase query against the live DB and prints sample
   rows so we can eyeball card + pack name + image URLs + 10× ratio.
3. Also runs a wider 7-day window in case 24h is empty.

Saves the output to data/new_chase_verify.txt for later inspection.

Usage:
  uv run python -u -m tools.verify_new_chase_query
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

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("ERROR: ANTHROPIC_API_KEY missing from .env", file=sys.stderr)
    sys.exit(2)

MODEL = "claude-sonnet-4-6"
MCP_BETA = "mcp-client-2025-11-20"
MCP_URL = "https://db-mcp-production.up.railway.app/sse"
MCP_NAME = "dripshoplive"

OUTPUT_PATH = Path("data") / "new_chase_verify.txt"

PROMPT = """You have access to the DripShopLive Postgres database via MCP tools.

Verify a candidate query for the "New Chase" pipeline. STRICT budget: ≤ 4 tool calls.

1. Confirm `products` table has `created_at`:
     SELECT column_name, data_type FROM information_schema.columns
     WHERE table_name = 'products' ORDER BY ordinal_position;

2. Run the candidate New Chase query (24h window):
     SELECT
         card.id            AS card_product_id,
         card.name          AS card_name,
         card.cert_number,
         card.price         AS card_value,
         card.image         AS card_image,
         card.created_at    AS added_at,
         pack.id            AS pack_product_id,
         pack.name          AS pack_name,
         pack.price         AS pack_price,
         pack.image         AS raw_pack_image,
         ROUND((card.price / NULLIF(pack.price, 0))::numeric, 1) AS ratio
     FROM products card
     JOIN products pack ON pack.id = card.parent_id
     WHERE card.cert_number IS NOT NULL
       AND card.cert_number != ''
       AND card.image NOT LIKE '%video-renders%'
       AND card.created_at >= NOW() - INTERVAL '24 hours'
       AND card.price >= 10 * pack.price
     ORDER BY card.price DESC
     LIMIT 5;

3. If query #2 returned zero rows, widen to 7 days (same query, INTERVAL '7 days').
   If 24h had rows, skip this step.

4. Run a count diagnostic:
     SELECT
       (SELECT COUNT(*) FROM products WHERE cert_number IS NOT NULL AND cert_number != '' AND created_at >= NOW() - INTERVAL '24 hours') AS graded_added_24h,
       (SELECT COUNT(*) FROM products WHERE cert_number IS NOT NULL AND cert_number != '' AND created_at >= NOW() - INTERVAL '7 days') AS graded_added_7d,
       (SELECT COUNT(*) FROM products card JOIN products pack ON pack.id = card.parent_id
         WHERE card.cert_number IS NOT NULL AND card.cert_number != ''
           AND card.created_at >= NOW() - INTERVAL '24 hours'
           AND card.price >= 10 * pack.price) AS chase_candidates_24h;

After all tool calls, output a short Markdown summary with:
- Whether `created_at` exists on `products` (yes/no)
- The 24h chase candidate count
- The 7d chase candidate count
- Sample rows (whichever window had data)
- Any concerns about the SQL.
"""


def main() -> int:
    client = anthropic.Anthropic(api_key=API_KEY)
    text_chunks: list[str] = []
    current_tool: dict[str, Any] = {}
    tool_call_count = 0
    transcript: list[str] = []

    def log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        transcript.append(line)

    log("Starting verification stream...")
    started = time.time()

    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=4096,
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
                    current_tool = {"name": getattr(cb, "name", "?"), "input_partial": ""}
                    log(f"  [{tool_call_count}] tool start")
                elif cb_type == "mcp_tool_result":
                    is_error = bool(getattr(cb, "is_error", False))
                    content = getattr(cb, "content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            getattr(c, "text", "") for c in content
                            if getattr(c, "type", None) == "text"
                        )
                    preview = str(content).replace("\n", " ")[:800]
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
                    current_tool["input_partial"] = (
                        current_tool.get("input_partial", "") + getattr(delta, "partial_json", "")
                    )
            elif etype == "content_block_stop":
                if current_tool.get("name"):
                    log(f"      input: {current_tool.get('input_partial', '')[:500]}")
                    current_tool = {}
                else:
                    print(flush=True)

    elapsed = time.time() - started
    log(f"Elapsed: {elapsed:.1f}s, tool calls: {tool_call_count}")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(transcript) + "\n\n--- MODEL TEXT ---\n" + "".join(text_chunks), encoding="utf-8")
    log(f"Saved to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
