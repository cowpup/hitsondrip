-- queries/new_chase_freshness.sql
--
-- Pre-check query. Runs BEFORE queries/new_chase.sql to short-circuit
-- the chase post entirely if the latest batch is stale.
--
-- Why a separate query: the main query (new_chase.sql) joins to the
-- batch anchor and filters by price >= :threshold. If the latest
-- batch is from 5 days ago, the main query would happily return a
-- 5-day-old chase that we DON'T want to post. Instead, we check
-- freshness up front and skip cleanly if the data is stale.
--
-- Threshold for "stale" is MAX_BATCH_AGE_HOURS (default 36, env-
-- overridable). Quiet weekends (no Drip chase drops Sat + Sun) are
-- the case this guards against — without it, the Monday cron would
-- repost Friday's chase.
--
-- Same user_id = 65643 + filter set as the main query, ensuring
-- the batch anchor matches what the main query would compute.

SELECT
  MAX(created_at) AS latest_batch_ts,
  EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) / 3600 AS hours_since_batch
FROM products
WHERE user_id = 65643
  AND cert_number IS NOT NULL
  AND cert_number != ''
  AND type = 'rip_and_ship'
  AND image IS NOT NULL
  AND image NOT LIKE '%video-renders%';
