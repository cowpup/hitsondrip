-- queries/pack_lookup_by_card.sql
--
-- Auto-resolve the pack (box_break) for a given chase card via the
-- "collection tracking" linkage discovered 2026-05-14:
--
--   products.id
--     → user_product_collection_product_mappings.product_id
--       → user_product_collections.id (via .collection_id)
--         → box_break_spot_mappings.collection_id
--           → box_breaks.id (via .box_break_id)
--
-- Collection membership is set automatically via tags + price-based
-- dynamic conditions on each user_product_collection. A chase listing
-- with the right tags lands in the right collection(s) immediately.
-- A card with NO mapping == "doesn't match any collection's
-- dynamic conditions" (missing tags, no matching collection set up,
-- or product attributes that don't match price ranges).
--
-- A single collection can be referenced by many box_breaks over time
-- (the "Graded Splash Collection" feeds packs like Chomp Slab Pack,
-- Crypto and Comics Pack, Don't Like It Raw, etc.). Tiebreaker:
-- most-recent box_break wins (Noah's call, 2026-05-14).
--
-- Filters:
--   - bb.reveal_animation_data ? 'packImage': the pack must have a
--     custom packImage set (the bare pack render uploaded via the
--     admin panel). Box_breaks without packImage are legacy / not
--     usable for rendering.
--
-- The :card_product_id placeholder is substituted at runtime via
-- plain str.replace. The product_id is an integer from a DripShopLive
-- products row we just queried — no untrusted input, no injection
-- risk.
--
-- Returns 0 or 1 rows:
--   - 0 rows: card has no collection mapping (missing tags) OR none
--     of its collections feed a box_break with packImage. new_chase.py
--     skips this candidate and tries the next.
--   - 1 row: pack_image_url + pack_title are the values to render.

SELECT
    bb.id                                   AS box_break_id,
    bb.title                                AS pack_title,
    bb.reveal_animation_data->>'packImage'  AS pack_image_url,
    bb.created_at                           AS box_break_created_at,
    upcpm.collection_id,
    upc.name                                AS collection_name
FROM user_product_collection_product_mappings upcpm
JOIN user_product_collections    upc ON upc.id            = upcpm.collection_id
JOIN box_break_spot_mappings     bsm ON bsm.collection_id = upcpm.collection_id
JOIN box_breaks                  bb  ON bb.id             = bsm.box_break_id
WHERE upcpm.product_id = :card_product_id
  AND bb.reveal_animation_data ? 'packImage'
ORDER BY bb.created_at DESC
LIMIT 1;
