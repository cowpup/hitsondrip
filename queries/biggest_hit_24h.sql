-- Return ALL Drip-fulfilled card hits >= $1,000 in the last 48 hours.
-- The Python backlog (src/hit_backlog.py) handles FIFO one-per-day selection
-- and placeholder skipping; this query just feeds it raw candidates.
--
-- Schema map (confirmed via DripShopLive MCP exploration, 2026-05-13):
--   product_purchases (pp): the purchase row recorded when a customer paid
--     for an instant pack and "won" the card. pp.product_id points to the
--     CARD that was pulled; pp.unit_price is the pack price paid; pp.created_at
--     is the purchase timestamp ("pulled_at"). pp.id is the stable per-hit key
--     used by the backlog (src/state_branch.py / src/hit_backlog.py) to de-dup
--     re-runs so the same hit can't generate two approval cards / two posts.
--   pull_game_pulls (pgp): one row per card pulled in a box break, links a
--     purchase to a box_break and records the card's estimated market value.
--     pgp.purchase_id (int) → pp.id; pgp.box_break_id (uuid) → bb.id;
--     pgp.value (numeric, IN DOLLARS, not cents) is the hit value.
--   products (used as `card` here): the card product. card.cert_number is
--     non-null for PSA/CGC/BGS graded cards, which is the "Drip-fulfilled"
--     filter — Drip grades the card and ships the slab. The OR fulfilment_
--     partner_id = 417995 (WD Group) clause from the original briefing would
--     also pull sealed-product fulfillment, which is NOT the right category
--     for this Instagram post (we only want graded card hits).
--   box_breaks (bb): the box break event the pull came from. bb.title is
--     the pack name (e.g. "Gold PSA 10 Slab Pack") and bb.image is the
--     pack thumbnail URL. Both go directly into the rendered post.
--
-- Filters:
--   - pp.created_at >= NOW() - INTERVAL '48 hours': last 48h window
--   - card.cert_number IS NOT NULL: graded cards only ("Drip-fulfilled")
--   - card.image NOT LIKE '%video-renders%': skip watermarked /video-renders/
--     thumbnails — they're tiled with the Drip logo and unusable in a post
--   - pgp.value IS NOT NULL: some pulls don't have a recorded value yet;
--     they can't qualify and would crash the renderer
--   - pgp.value >= 1000: only surface hits worth $1,000 or more.
--
-- Sort + limit:
--   ORDER BY pp.created_at ASC -- oldest first; Python backlog does FIFO selection
--   No LIMIT — all qualifying hits are returned so the backlog can hold extras
--   for quiet days. src/hit_backlog.py handles FIFO one-per-day selection and
--   placeholder skipping.

SELECT
    pp.id               AS hit_id,
    pp.created_at       AS pulled_at,
    card.name           AS card_name,
    card.image          AS card_image_url,
    bb.title            AS pack_name,
    bb.image            AS pack_image_url,
    pp.unit_price       AS pack_price,
    pgp.value           AS hit_value
FROM product_purchases pp
JOIN pull_game_pulls   pgp ON pgp.purchase_id  = pp.id
JOIN products          card ON card.id         = pp.product_id
JOIN box_breaks        bb  ON bb.id            = pgp.box_break_id
WHERE pp.created_at >= NOW() - INTERVAL '48 hours'
  AND card.cert_number IS NOT NULL
  AND card.image NOT LIKE '%video-renders%'
  AND pgp.value IS NOT NULL
  AND pgp.value >= 250  -- TEMP TEST 2026-06-19: lowered from 1000 to force one card; REVERT to 1000
ORDER BY pp.created_at DESC  -- TEMP TEST: freshest first (REVERT to ASC)
LIMIT 1;  -- TEMP TEST: only one hit so the backlog isn't polluted (REVERT: remove this line)
