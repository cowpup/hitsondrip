"""Validate the chase → collection → pack chain for one card.

Runs the end-to-end chain query for a single card_product_id and
prints the resolved pack info. Should land in 5-15s if the MCP is
healthy; this is a much smaller query than the schema enumeration
that stalled out.

Usage:
  uv run python -u -m tools.verify_chain 2692316
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


CHAIN_SQL = """
SELECT pcm.product_id,
       p.name                                  AS card_name,
       pcm.collection_id,
       bb.id                                   AS box_break_id,
       bb.title                                AS pack_title,
       bb.reveal_animation_data->>'packImage'  AS pack_image_url
FROM product_collection_mappings pcm
JOIN products                     p  ON p.id  = pcm.product_id
JOIN box_break_spot_mappings      bsm ON bsm.collection_id = pcm.collection_id
JOIN box_breaks                   bb  ON bb.id = bsm.box_break_id
WHERE pcm.product_id = {card_id}
  AND bb.reveal_animation_data ? 'packImage'
ORDER BY bb.created_at DESC
LIMIT 5;
"""


def main() -> int:
    card_id = int(sys.argv[1]) if len(sys.argv) > 1 else 2692316
    print(f"Resolving pack for card_product_id = {card_id}\n")

    sql = CHAIN_SQL.format(card_id=card_id)
    print("SQL:")
    print(sql)
    print()

    client = anthropic.Anthropic(api_key=API_KEY)
    started = time.time()
    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        betas=["mcp-client-2025-11-20"],
        mcp_servers=[{"type": "url", "url": "https://db-mcp-production.up.railway.app/sse", "name": "drip"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "drip"}],
        messages=[{
            "role": "user",
            "content": (
                f"Run this SQL against DripShopLive and return results "
                f"as a JSON array (one object per row). No prose, just "
                f"the fenced JSON block.\n\n```sql\n{sql}\n```"
            ),
        }],
    )

    text_parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", ""))
        elif getattr(block, "type", None) == "mcp_tool_result":
            if getattr(block, "is_error", False):
                print(f"TOOL ERROR: {getattr(block, 'content', '')!s:.500}")

    elapsed = time.time() - started
    print(f"Elapsed: {elapsed:.1f}s\n")
    print("Model response:")
    print("".join(text_parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
