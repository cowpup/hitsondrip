# Workflow prompt for new_chase.py

You have access to the DripShopLive Postgres database via the MCP tool `dripshoplive`. Your job in this run is small and mechanical.

## Task

Run the SQL query provided to you below verbatim against DripShopLive. The query returns AT MOST 1 row — the highest-value chase card added to user 65643's chase-pool inventory in the most recent batch, filtered to `price >= <threshold>` where the threshold is `10 * pack_price` and pack_price is read from a config file (already substituted into the SQL).

## Output format

Respond with exactly one fenced ```json``` block containing a JSON ARRAY of the rows (length 0 or 1), no prose around it. Each element must match this shape:

```json
[
  {
    "card_product_id": <int>,
    "card_name": "...",
    "cert_number": "...",
    "card_image_url": "https://cdn.dripshop.live/product/...",
    "hit_value": <number>,
    "added_at": "YYYY-MM-DDTHH:MM:SS..."
  }
]
```

If the query returns zero rows (no chase ≥ threshold in the latest batch), respond with an empty array:

```json
[]
```

Do not retry the query, do not explore the schema, do not write commentary. Run the SQL, parse the rows, emit the JSON array, stop.

## SQL

(The harness will append the contents of `queries/new_chase.sql` here at runtime, with the `:threshold` placeholder already substituted.)
