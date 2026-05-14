-- queries/new_chase.sql
--
-- Returns the highest-value chase card added to Drip's chase-pool inventory
-- in the most recent batch upload.
--
-- Anchor: user 65643 (Drip's chase-listing account).
-- Batch detection: MAX(created_at) for qualifying listings, with a 1-hour
-- back-window to capture any same-batch listings that trickled in seconds
-- apart. Tested: most batches share an exact second-level timestamp.
-- Threshold: :threshold placeholder is substituted at runtime by the harness
-- with str(10 * featured_pack.pack_price) read from config/featured_pack.json.
-- Highest-multiplier semantics: since pack_price is a single constant per run,
-- ORDER BY price DESC === ORDER BY multiplier DESC. No special math required.
--
-- Filters:
--   - user_id = 65643          (chase-pool listings; not other sellers)
--   - cert_number IS NOT NULL  (graded slabs only; TBD is accepted)
--   - type = 'rip_and_ship'    (active inventory; excludes inactive)
--   - image IS NOT NULL        (need image_url for the renderer)
--   - image NOT LIKE 'video-renders' (excludes placeholder render URLs)

WITH latest_batch_anchor AS (
  SELECT MAX(created_at) AS batch_ts
  FROM products
  WHERE user_id = 65643
    AND cert_number IS NOT NULL
    AND cert_number != ''
    AND type = 'rip_and_ship'
    AND image IS NOT NULL
    AND image NOT LIKE '%video-renders%'
)
SELECT
  p.id          AS card_product_id,
  p.name        AS card_name,
  p.cert_number,
  p.image       AS card_image_url,
  p.price       AS hit_value,
  p.created_at  AS added_at
FROM products p, latest_batch_anchor a
WHERE p.user_id = 65643
  AND p.cert_number IS NOT NULL
  AND p.cert_number != ''
  AND p.type = 'rip_and_ship'
  AND p.image IS NOT NULL
  AND p.image NOT LIKE '%video-renders%'
  AND p.created_at >= a.batch_ts - INTERVAL '1 hour'
  AND p.created_at <= a.batch_ts
  AND p.price >= :threshold
ORDER BY p.price DESC NULLS LAST
LIMIT 5;
