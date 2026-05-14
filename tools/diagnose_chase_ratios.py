"""Phase C diagnostic — figure out why 10x chase filter returns zero.

The verification run confirmed:
- 906 graded cards added in 24h, 8067 in 7d
- 0 chase candidates with `card.price >= 10 * pack.price`
- Sample rows show pack_price = card_price (1:1 giveaway pattern)

This script investigates:
1. The actual ratio distribution (histogram of card.price / pack.price).
2. Whether `products` has a `category` / `format` / `type` column
   that distinguishes "single-card giveaway" from "multi-card chase pack".
3. Whether packs ever have multiple child cards (parent_id grouping).
4. Whether `box_breaks` is actually the right pack concept here
   (box-break packs DO have multiple cards by design).

Output: data/chase_ratios_diagnostic.txt
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
OUTPUT_PATH = Path("data") / "chase_ratios_diagnostic.txt"

PROMPT = """DripShopLive Postgres MCP. STRICT budget: ≤ 5 tool calls.

CONTEXT: The "New Chase" filter `card.price >= 10 * pack.price` returns
ZERO rows in last 7 days despite 8067 graded cards being added.
Sample rows show pack.price = card.price (1:1 giveaway-style packs
where the pack IS effectively the card).

INVESTIGATE:

1. Ratio distribution — histogram of card.price / pack.price for
   recent graded cards. Reveals the actual price relationships:
     SELECT
       CASE
         WHEN pack.price IS NULL OR pack.price = 0 THEN 'null/zero'
         WHEN card.price / pack.price < 1 THEN '< 1x'
         WHEN card.price / pack.price = 1 THEN 'exactly 1x'
         WHEN card.price / pack.price BETWEEN 1.01 AND 2 THEN '1-2x'
         WHEN card.price / pack.price BETWEEN 2.01 AND 5 THEN '2-5x'
         WHEN card.price / pack.price BETWEEN 5.01 AND 10 THEN '5-10x'
         ELSE '> 10x'
       END AS ratio_bucket,
       COUNT(*) AS n
     FROM products card
     JOIN products pack ON pack.id = card.parent_id
     WHERE card.cert_number IS NOT NULL AND card.cert_number != ''
       AND card.created_at >= NOW() - INTERVAL '7 days'
     GROUP BY 1 ORDER BY 1;

2. Does `products` have a category/format/type column that
   distinguishes pack-products from card-products? List `products`
   columns via information_schema.columns. If the earlier run timed
   out, retry it.

3. Show the top 10 packs by number of graded children in last 7 days —
   these are the "real" chase packs (multi-card containers):
     SELECT pack.id, pack.name, pack.price AS pack_price,
            COUNT(*) AS n_graded_children_7d,
            MAX(card.price) AS top_card_price,
            MAX(card.price) / NULLIF(pack.price, 0) AS top_ratio
     FROM products pack
     JOIN products card ON card.parent_id = pack.id
     WHERE card.cert_number IS NOT NULL AND card.cert_number != ''
       AND card.created_at >= NOW() - INTERVAL '7 days'
     GROUP BY pack.id, pack.name, pack.price
     HAVING COUNT(*) >= 2
     ORDER BY n_graded_children_7d DESC LIMIT 10;

4. Check box_breaks as alternative pack concept — is there an
   "instant pack" pattern where many graded cards belong to ONE
   box_break? Sample the join via pull_game_pulls:
     SELECT bb.id, bb.title, bb.price_per_pull, bb.image,
            COUNT(DISTINCT pgp.id) AS n_pulls,
            MAX(pgp.value) AS top_hit
     FROM box_breaks bb
     JOIN pull_game_pulls pgp ON pgp.box_break_id = bb.id
     WHERE pgp.value IS NOT NULL
     GROUP BY bb.id, bb.title, bb.price_per_pull, bb.image
     HAVING COUNT(DISTINCT pgp.id) > 5
     ORDER BY top_hit DESC LIMIT 10;

5. Final synthesis question — given the data, what's the right
   definition of "chase card added to pack inventory" for this DB?

OUTPUT: Markdown summary with the histogram, the top-10 chase pack
table, the box_breaks alternative, and a concrete recommendation:
either (A) keep card.parent_id but adjust threshold, or (B) switch
to a different join entirely.
"""


def main() -> int:
    client = anthropic.Anthropic(api_key=API_KEY)
    text_chunks: list[str] = []
    transcript: list[str] = []
    current_tool: dict[str, Any] = {}
    tool_call_count = 0

    def log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        transcript.append(line)

    log("Starting diagnostic stream...")
    started = time.time()

    with client.beta.messages.stream(
        model=MODEL, max_tokens=6144, betas=[MCP_BETA],
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
                    preview = str(content).replace("\n", " ")[:1200]
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

    elapsed = time.time() - started
    log(f"Elapsed: {elapsed:.1f}s, tool calls: {tool_call_count}")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(transcript) + "\n\n--- MODEL TEXT ---\n" + "".join(text_chunks), encoding="utf-8")
    log(f"Saved to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
