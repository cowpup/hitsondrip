"""Fetch N recent Drip-fulfilled hits via DripShopLive MCP (streaming).

Streaming variant: uses client.beta.messages.stream so we see each MCP
tool call as it happens, NOT after the whole response returns. Also
scoped tightly to keep costs low:
  - Prompt hints the likely table names so the model skips list_tables.
  - Prompt hard-caps tool calls at 6.
  - Defaults to claude-sonnet-4-6 (5x cheaper than Opus) — main.py
    can still use Opus per spec since the schema exploration is the
    expensive part and we only need to do it once.

Output: data/recent_hits.json (same shape as before)
Raw transcript: data/recent_hits.transcript.txt (every event, for debug)

Usage:
  uv run python -u -m tools.fetch_recent_hits          # default N=10
  uv run python -u -m tools.fetch_recent_hits 5        # custom N
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

# Windows console is cp1252 by default and can't encode arrows, em-dashes,
# or any other character outside the basic Latin-1 set. Force UTF-8 so
# streamed model text doesn't crash the script mid-render.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("ERROR: ANTHROPIC_API_KEY missing from .env", file=sys.stderr)
    sys.exit(2)

# Cheaper model for the test fetcher. main.py uses Opus per spec; this
# tool only needs schema reasoning + one SQL query, well within Sonnet's
# range. Switch to Opus if Sonnet struggles by setting MODEL below.
MODEL = "claude-sonnet-4-6"
MCP_BETA = "mcp-client-2025-11-20"
MCP_URL = "https://db-mcp-production.up.railway.app/sse"
MCP_NAME = "dripshoplive"

OUTPUT_PATH = Path("data") / "recent_hits.json"
TRANSCRIPT_PATH = Path("data") / "recent_hits.transcript.txt"

PROMPT_TEMPLATE = """You have access to the DripShopLive Postgres database via MCP tools.

TASK: Return JSON for the {n} most recent Drip-fulfilled instant-pack hits, with REAL pack name + image.

KNOWN SCHEMA (from previous exploration):
- `product_purchases` (pp): id (int), product_id, created_at, unit_price, fulfilment_partner_id, amount, order_title, preview_image
- `products` (p): id, name, image, price, cert_number, fulfilment_partner_id, parent_id
- `pull_game_pulls` (pgp): id (uuid), user_id (int), box_break_id (uuid), transaction_id (uuid), purchase_id (int → pp.id), value (numeric, dollars, NOT cents), + other columns
- `box_breaks` (bb): UNKNOWN — must be discovered via information_schema. The pack metadata (name + image) for each instant pack lives here, linked to a "pack product" row in `products`.

DO NOT call `describe_table` on `pull_game_pulls` or `box_breaks` — they time out. Use information_schema instead.

STRICT BUDGET: ≤ 3 MCP tool calls.

PROCESS:
1. Discover `box_breaks` columns:
     SELECT column_name, data_type FROM information_schema.columns
     WHERE table_name = 'box_breaks' ORDER BY ordinal_position;
   Identify the column linking each box_break row to a pack product in `products` (likely a foreign key named something like `product_id`, `pack_product_id`, or `parent_product_id`).
2. Run ONE join query (pack metadata is on box_breaks directly per prior exploration — bb.title and bb.image are the right columns, no separate pack-product join needed):
     SELECT
       card.name           AS card_name,
       card.image          AS card_image_url,
       bb.title            AS pack_name,
       bb.image            AS pack_image_url,
       pp.unit_price       AS pack_price,
       pgp.value           AS hit_value
     FROM product_purchases pp
     JOIN pull_game_pulls pgp ON pgp.purchase_id = pp.id
     JOIN products card        ON card.id = pp.product_id
     JOIN box_breaks bb        ON bb.id = pgp.box_break_id
     WHERE card.cert_number IS NOT NULL
       AND card.image NOT LIKE '%video-renders%'
     ORDER BY pp.created_at DESC
     LIMIT {n};

KEY FILTER: card.cert_number IS NOT NULL — this is "Drip-fulfilled" meaning Drip grades the card. DO NOT also OR on fulfilment_partner_id; that pulls sealed-product fulfillment which is the wrong category for this post.

If pack.name / pack.image come back null on a row, leave them null in the JSON (we'll fall back at render time).

FINAL OUTPUT (only this fenced block, no prose around it):

```json
[
  {{
    "card_name": "...",
    "card_image_url": "https://cdn.dripshop.live/product/...",
    "pack_name": "...",
    "pack_price": <number>,
    "pack_image_url": "https://cdn.dripshop.live/...",
    "hit_value": <number>
  }},
  ...
]
```
"""


def fetch_recent_hits(n: int = 10) -> list[dict[str, Any]]:
    client = anthropic.Anthropic(api_key=API_KEY)
    transcript: list[str] = []

    def log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        transcript.append(line)

    log(f"Model: {MODEL}")
    log(f"MCP: {MCP_URL}")
    log("Starting stream...")

    # Accumulators for text + tool inputs as deltas arrive.
    text_chunks: list[str] = []
    tool_call_count = 0
    current_tool: dict[str, Any] = {}

    started = time.time()

    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=8192,
            betas=[MCP_BETA],
            mcp_servers=[{"type": "url", "url": MCP_URL, "name": MCP_NAME}],
            tools=[{"type": "mcp_toolset", "mcp_server_name": MCP_NAME}],
            messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(n=n)}],
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
                        preview = str(content).replace("\n", " ")[:240]
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
                        # Tool input fully assembled — log it.
                        log(f"      input: {current_tool.get('input_partial', '')[:240]}")
                        current_tool = {}
                    else:
                        # Text block ended.
                        print(flush=True)

                elif etype == "message_stop":
                    log("Stream complete.")

                elif etype == "message_delta":
                    # Usage info often arrives here at the end.
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        log(f"  usage delta: in={getattr(usage, 'input_tokens', '?')} "
                            f"out={getattr(usage, 'output_tokens', '?')}")

            final = stream.get_final_message()
    except anthropic.APIError as e:
        log(f"API ERROR: {type(e).__name__}: {e}")
        TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRANSCRIPT_PATH.write_text("\n".join(transcript), encoding="utf-8")
        raise

    elapsed = time.time() - started
    log(f"Total elapsed: {elapsed:.1f}s")
    log(f"Total tool calls: {tool_call_count}")
    usage = getattr(final, "usage", None)
    if usage is not None:
        log(f"Final usage: in={usage.input_tokens} out={usage.output_tokens}")

    TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_PATH.write_text("\n".join(transcript), encoding="utf-8")
    log(f"Transcript saved: {TRANSCRIPT_PATH}")

    full_text = "".join(text_chunks)
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", full_text, re.DOTALL)
    if not match:
        match = re.search(r"(\[\s*\{.*?\}\s*\])", full_text, re.DOTALL)
    if not match:
        raw_path = OUTPUT_PATH.with_suffix(".raw.txt")
        raw_path.write_text(full_text, encoding="utf-8")
        raise RuntimeError(
            f"No JSON array found in model response. "
            f"Raw text saved to {raw_path}"
        )
    return json.loads(match.group(1))


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    try:
        hits = fetch_recent_hits(n)
    except RuntimeError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except anthropic.APIError as e:
        print(f"\nANTHROPIC API ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(hits, indent=2), encoding="utf-8")
    print(f"\nWrote {len(hits)} hits to {OUTPUT_PATH.resolve()}")
    for i, h in enumerate(hits, 1):
        name = (h.get("pack_name") or "?")[:32]
        card = (h.get("card_name") or "?")[:40]
        # hit_value can be None on rows where pgp.value is null; format
        # both numerics through str() so None doesn't blow up the f-string.
        hv = h.get("hit_value")
        pp = h.get("pack_price")
        hv_str = f"${hv}" if hv is not None else "$?"
        pp_str = f"${pp}" if pp is not None else "$?"
        print(f"  {i:2d}. {hv_str:<8} | pack {pp_str:<7} | {name:<32} | {card}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
