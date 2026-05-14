"""Measure how 'fresh' chase listings get a pack/collection mapping.

If chase cards get their collection mapping only AFTER pack-assignment
(which is a separate step in the admin workflow), then auto-resolving
the pack will fail for the very-latest listings — which is exactly
what new_chase.py is trying to post.

Probes:
  Q1 — Of all chase listings created in the last 7 days, how many have
       a user_product_collection_product_mappings row?
  Q2 — For chase listings that DO have a mapping, what's the median +
       p90 delay between product.created_at and mapping.created_at?
  Q3 — Check the test Hitmonchan (2797407) specifically — does it have
       a mapping now (~1h after listing)?
  Q4 — Find the newest chase listing that DOES have a mapping. How old
       is it? That's the practical "minimum age" for auto-resolution.

Usage: uv run python -u -m tools.probe_chase_mapping_freshness
"""

from __future__ import annotations
import os, sys, time
import anthropic
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()


PROMPT = """Run these 4 queries in PARALLEL and return one JSON object {"q1":..., "q2":..., "q3":..., "q4":...}. No prose.

Q1 — Mapping coverage of recent chase listings (7 days):
  WITH recent_chases AS (
    SELECT id, created_at
    FROM products
    WHERE user_id = 65643
      AND cert_number IS NOT NULL AND cert_number != ''
      AND type = 'rip_and_ship'
      AND created_at >= NOW() - INTERVAL '7 days'
  )
  SELECT
    COUNT(*)                                       AS total_chases_7d,
    COUNT(m.product_id)                            AS with_mapping,
    ROUND(100.0 * COUNT(m.product_id) / NULLIF(COUNT(*), 0), 1) AS pct_mapped
  FROM recent_chases rc
  LEFT JOIN user_product_collection_product_mappings m ON m.product_id = rc.id;

Q2 — Delay between listing and mapping for cards that have both:
  SELECT
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (m.created_at - p.created_at))/3600) AS median_hours,
    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (m.created_at - p.created_at))/3600) AS p90_hours,
    COUNT(*) AS sample_size
  FROM products p
  JOIN user_product_collection_product_mappings m ON m.product_id = p.id
  WHERE p.user_id = 65643
    AND p.cert_number IS NOT NULL AND p.cert_number != ''
    AND p.type = 'rip_and_ship'
    AND p.created_at >= NOW() - INTERVAL '30 days';

Q3 — Does product 2797407 (the recent Hitmonchan we tested) now have a mapping?
  SELECT m.product_id, m.collection_id, m.created_at AS mapping_created_at,
         p.created_at AS product_created_at,
         EXTRACT(EPOCH FROM (m.created_at - p.created_at)) / 3600 AS delay_hours
  FROM user_product_collection_product_mappings m
  JOIN products p ON p.id = m.product_id
  WHERE m.product_id = 2797407;

Q4 — Newest chase listing that has a mapping (gives us the practical "minimum age"):
  SELECT p.id, p.name, p.created_at AS product_created_at,
         m.created_at AS mapping_created_at,
         EXTRACT(EPOCH FROM (NOW() - m.created_at)) / 3600 AS mapped_hours_ago,
         m.collection_id
  FROM products p
  JOIN user_product_collection_product_mappings m ON m.product_id = p.id
  WHERE p.user_id = 65643
    AND p.cert_number IS NOT NULL AND p.cert_number != ''
    AND p.type = 'rip_and_ship'
  ORDER BY m.created_at DESC
  LIMIT 5;
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
