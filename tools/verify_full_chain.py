"""End-to-end chain test using the corrected linkage tables.

Chain:
  products (card)
    → user_product_collection_product_mappings (.product_id, .collection_id)
    → user_product_collections (collection metadata)
    → box_break_spot_mappings (.collection_id, .box_break_id)
    → box_breaks (.id, .title, .reveal_animation_data->>'packImage')

Card under test: 2334007 (2002 The Town on No Map Hitmonchan CGC 8.5),
collection 375 (per the Q5 probe). If the chain works for this card,
it'll work for any chase with a collection mapping.

Usage: uv run python -u -m tools.verify_full_chain [card_id]
"""

from __future__ import annotations
import os, sys, time
import anthropic
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()


def main() -> int:
    card_id = int(sys.argv[1]) if len(sys.argv) > 1 else 2334007

    sql = f"""
SELECT
    p.id                                    AS card_id,
    p.name                                  AS card_name,
    upcpm.collection_id,
    upc.name                                AS collection_name,
    bsm.box_break_id,
    bb.title                                AS pack_title,
    bb.reveal_animation_data->>'packImage'  AS pack_image_url,
    bb.created_at                           AS box_break_created_at
FROM products p
JOIN user_product_collection_product_mappings upcpm ON upcpm.product_id = p.id
JOIN user_product_collections                 upc   ON upc.id           = upcpm.collection_id
JOIN box_break_spot_mappings                  bsm   ON bsm.collection_id = upcpm.collection_id
JOIN box_breaks                               bb    ON bb.id            = bsm.box_break_id
WHERE p.id = {card_id}
  AND bb.reveal_animation_data ? 'packImage'
ORDER BY bb.created_at DESC
LIMIT 5;
""".strip()

    print(f"Card under test: {card_id}\n")
    print("SQL:\n" + sql + "\n")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    started = time.time()
    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        betas=["mcp-client-2025-11-20"],
        mcp_servers=[{"type":"url","url":"https://db-mcp-production.up.railway.app/sse","name":"drip"}],
        tools=[{"type":"mcp_toolset","mcp_server_name":"drip"}],
        messages=[{"role":"user","content":
            f"Run this SQL and return the rows as a JSON array. No prose.\n\n```sql\n{sql}\n```"
        }],
    )
    text = "".join(getattr(b,"text","") for b in response.content if getattr(b,"type",None)=="text")
    errs = [str(getattr(b,"content",""))[:400] for b in response.content
            if getattr(b,"type",None)=="mcp_tool_result" and getattr(b,"is_error",False)]
    elapsed = time.time() - started
    print(f"Elapsed: {elapsed:.1f}s")
    if errs:
        print(f"\n{len(errs)} tool error(s):")
        for e in errs: print(f"  - {e}")
    print("\nModel response:\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
