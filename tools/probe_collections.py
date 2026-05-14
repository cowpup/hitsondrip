"""Probe the 'collection'-named columns we haven't tested yet.

Earlier debug showed the Hitmonchan + Pikachu test cards aren't in
product_collection_mappings or box_break_spot_mappings. But we found
several other suspicious 'collection' columns we haven't probed.
Most suspicious: products.box_break_collection_ref_id (text column).

This script asks the model to run 5 diagnostic queries in parallel
to figure out where chase→pack linkages ACTUALLY live.

Usage: uv run python -u -m tools.probe_collections
"""

from __future__ import annotations
import os, sys, time
import anthropic
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()


PROMPT = """Run these 5 diagnostic queries in PARALLEL (one tool call each) and return one JSON object: {"q1":[...], ..., "q5":[...]}. No prose.

Q1 — Sample any product with box_break_collection_ref_id IS NOT NULL.
  SELECT id, name, user_id, cert_number, type, box_break_collection_ref_id
  FROM products
  WHERE box_break_collection_ref_id IS NOT NULL
  LIMIT 5;

Q2 — Same as Q1 but specifically for chase listings (user 65643).
  SELECT id, name, cert_number, type, box_break_collection_ref_id
  FROM products
  WHERE user_id = 65643
    AND cert_number IS NOT NULL AND cert_number != ''
    AND box_break_collection_ref_id IS NOT NULL
  LIMIT 5;

Q3 — Count chase listings vs how many have the ref_id set.
  SELECT
    COUNT(*) AS total_chases,
    COUNT(*) FILTER (WHERE box_break_collection_ref_id IS NOT NULL) AS with_ref_id,
    COUNT(*) FILTER (WHERE parent_id IS NOT NULL) AS with_parent_id
  FROM products
  WHERE user_id = 65643
    AND cert_number IS NOT NULL AND cert_number != ''
    AND type = 'rip_and_ship';

Q4 — Inspect user_product_collections + user_product_collection_product_mappings columns.
  SELECT table_name, column_name, data_type
  FROM information_schema.columns
  WHERE table_schema='public'
    AND table_name IN ('user_product_collections','user_product_collection_product_mappings')
  ORDER BY table_name, ordinal_position;

Q5 — Find any chase product (user 65643, cert set) that has a row in user_product_collection_product_mappings.
  SELECT p.id AS card_id, p.name, p.cert_number,
         m.collection_id, m.product_id
  FROM products p
  JOIN user_product_collection_product_mappings m ON m.product_id = p.id
  WHERE p.user_id = 65643
    AND p.cert_number IS NOT NULL AND p.cert_number != ''
  LIMIT 10;
"""


def main() -> int:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    started = time.time()
    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        betas=["mcp-client-2025-11-20"],
        mcp_servers=[{"type":"url","url":"https://db-mcp-production.up.railway.app/sse","name":"drip"}],
        tools=[{"type":"mcp_toolset","mcp_server_name":"drip"}],
        messages=[{"role":"user","content":PROMPT}],
    )
    text = "".join(getattr(b,"text","") for b in response.content if getattr(b,"type",None)=="text")
    errs = [str(getattr(b,"content",""))[:400] for b in response.content
            if getattr(b,"type",None)=="mcp_tool_result" and getattr(b,"is_error",False)]
    elapsed = time.time() - started
    print(f"Elapsed: {elapsed:.1f}s")
    if errs:
        print(f"\n{len(errs)} tool error(s):")
        for e in errs: print(f"  - {e}")
    print("\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
