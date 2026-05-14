-- queries/new_chase_near_miss.sql
--
-- Returns the top card in the latest batch IGNORING the chase threshold.
-- Runs ONLY when queries/new_chase.sql returns zero rows — i.e. the
-- batch is fresh but no card cleared 10× pack_price. Powers the
-- Slack skip message's near-miss tuning info ("top was $X at Y×").
--
-- Identical to queries/new_chase.sql EXCEPT no `p.price >= :threshold`
-- filter, so we always get the highest-value card in the batch.

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
ORDER BY p.price DESC NULLS LAST
LIMIT 1;
