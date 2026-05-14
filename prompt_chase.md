# Workflow prompt for new_chase.py

You have access to the DripShopLive Postgres database via the MCP tool `dripshoplive`. Your job in this run is small and mechanical.

## Task

Run the SQL query provided to you below verbatim against DripShopLive. Different queries in this pipeline return different row counts (some 0–1, some 0–5). Return EVERY row the query produces, in the order the query returns them. Do not filter or truncate.

## Output format

Respond with exactly one fenced ```json``` block containing a JSON ARRAY of the rows (any length from 0 up to whatever the SQL's LIMIT permits), no prose around it. Each element is an object with one key per SELECTed column. Example shape (column names will vary by query):

```json
[
  {
    "card_product_id": 1234567,
    "card_name": "...",
    "hit_value": 474,
    "added_at": "2026-05-14T17:00:00..."
  }
]
```

If the query returns zero rows, respond with an empty array:

```json
[]
```

Do not retry the query, do not explore the schema, do not write commentary. Run the SQL, parse the rows, emit the JSON array, stop.

## SQL

(The harness appends one of the project's `queries/*.sql` files here at runtime, with any `:placeholder` already substituted to a literal value.)
