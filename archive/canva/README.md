# archive/canva/

Two attempts. Both archived. The Drip Daily Just Pulled workflow
permanently uses the Pillow renderer (`src/renderer.py`) with no
Canva runtime dependency.

## Attempt 1 — Anthropic-managed Canva MCP

Files: `canva_auth.py` (OAuth + refresh), `canva_oauth.py` (one-time
setup).

Outcome: blocked. The Canva MCP at `mcp.canva.com` rejected our Canva
Connect access tokens — the MCP's auth model is undocumented and
unrelated to the Connect API we provisioned credentials for. Verified
empirically by `verify_mcp_auth.py` returning the Plan-B pivot block
on every Canva probe.

## Attempt 2 — Canva Connect REST + Brand Template autofill

Files: `prompt.md` (the orchestration prompt we drafted for this path
before the gate was discovered).

Outcome: blocked. The Brand Template autofill endpoint
(`POST /rest/v1/brand-templates/{id}/autofill`) and the Data Autofill
app are gated behind the Teams / Enterprise plan tier — "contact
sales" gated. Not available on personal or lower-tier accounts.

## Final architecture

`src/renderer.py` (Pillow) composites the daily PNG locally:
- Base canvas: `assets/background.png` (Architecture B: gold frame +
  drip logo + headers + pill outline + static "PACK PRICE" label,
  with the four variable elements removed).
- Card image: downloaded, content-bbox trimmed, fit into a target box
  with consistent 10% inner padding (this permanently fixes the Path A
  cut-off issue).
- Pack name + pack price: rendered as text via Pillow ImageFont
  (DM Sans Bold).

The rendered PNG is uploaded via GitHub Contents API (`src/image_host.py`
— pending) and that URL is handed to Metricool for IG scheduling.

## When to revive (and what's reusable)

If Canva ever opens Brand Template autofill on lower plan tiers,
Attempt 2 is the right pattern — much cleaner than the MCP path and
much less work than maintaining the Pillow renderer.

Reusable artifacts:
- `canva_oauth.py` — full PKCE flow with local one-shot HTTP server.
  Token capture, persistence to `.env`, port autoselect. Works as-is.
- `canva_auth.py` — refresh + atomic `.env` rewrite under filelock.
  Handles Canva's refresh-token rotation correctly. Works as-is.
- `prompt.md` — the orchestration prompt. Would need its
  "rolling archive" approach swapped for "fresh template autofill",
  but the element matching rules, delete+insert quirk explanation,
  and error-handling shape are all directly applicable.
