-- Find the single highest-value Drip-fulfilled card hit in the last 24 hours.
-- Returns one row with everything main.py needs to render the daily post.
--
-- Schema map (confirmed via DripShopLive MCP exploration, 2026-05-13):
--   product_purchases (pp): the purchase row recorded when a customer paid
--     for an instant pack and "won" the card. pp.product_id points to the
--     CARD that was pulled; pp.unit_price is the pack price paid; pp.created_at
--     is the purchase timestamp.
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
--   - pp.created_at >= NOW() - INTERVAL '24 hours': last 24h only
--   - card.cert_number IS NOT NULL: graded cards only ("Drip-fulfilled")
--   - card.image NOT LIKE '%video-renders%': skip watermarked /video-renders/
--     thumbnails — they're tiled with the Drip logo and unusable in a post
--   - pgp.value IS NOT NULL: some pulls don't have a recorded value yet;
--     they can't be the "biggest hit" and would crash the renderer
--
-- Sort + limit:
--   ORDER BY pgp.value DESC NULLS LAST -- belt-and-suspenders with the IS NOT NULL filter
--   LIMIT 5                              -- top 5 hits; main.py picks the
--                                          first one whose card_image_url
--                                          isn't on the placeholder
--                                          blacklist (src/image_filter.py).
--                                          Originally LIMIT 1 — bumped to 5
--                                          to allow skipping placeholder
--                                          images (URL pattern isn't
--                                          diagnostic, must check content
--                                          hash).

SELECT
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
WHERE pp.created_at >= NOW() - INTERVAL '24 hours'
  AND card.cert_number IS NOT NULL
  AND card.image NOT LIKE '%video-renders%'
  AND pgp.value IS NOT NULL
ORDER BY pgp.value DESC NULLS LAST
LIMIT 5;
