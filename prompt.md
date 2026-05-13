# Workflow prompt for main.py

You have access to the DripShopLive Postgres database via the MCP tool `dripshoplive`. Your job in this run is small and mechanical.

## Task

Run the SQL query provided to you below verbatim against DripShopLive. The query returns at most ONE row representing the biggest Drip-fulfilled instant-pack hit in the last 24 hours.

## Output format

Respond with exactly one fenced ```json``` block containing the row data, no prose around it. The JSON must match this shape:

```json
{
  "card_name": "...",
  "card_image_url": "https://cdn.dripshop.live/product/...",
  "pack_name": "...",
  "pack_price": <number>,
  "pack_image_url": "https://cdn.dripshop.live/...",
  "hit_value": <number>
}
```

If the query returns zero rows (no Drip-fulfilled hits in the last 24h), respond with a single fenced block containing `null`:

```json
null
```

Do not retry the query, do not explore the schema, do not write commentary. Run the SQL, parse the row, emit the JSON, stop.

## SQL

(The harness will append the contents of `queries/biggest_hit_24h.sql` here at runtime.)
