-- queries/pack_image_lookup.sql
--
-- Resolve the "custom pack image" URL for a specific box_break.
--
-- Background: the admin-panel "custom pack image" field writes into
-- box_breaks.reveal_animation_data (JSONB) under the `packImage` key,
-- NOT into any column-level products.image / box_breaks.image you'd
-- expect from a column scan. The render-only image lives buried in
-- the JSONB alongside the reveal video URL and animation_type.
--
-- Discovery (2026-05-14): all rows with reveal_animation_data->>'animationType'
-- = 'custom' have packImage set; the preset animation types (tsunami,
-- splash, wave) also store packImage. Only the 57 legacy rows with
-- animation_type = NULL lack the key.
--
-- The :box_break_id placeholder is substituted at runtime by
-- new_chase.py via plain str.replace. The harness validates the
-- substituted value is a valid UUID before invocation (see
-- new_chase.resolve_pack_image), so the literal-string interpolation
-- carries no injection risk. The `::uuid` cast enforces the type at
-- query time as belt-and-suspenders.
--
-- Returns 0 or 1 rows:
--   - 0 rows: the box_break_id doesn't exist OR has no packImage key
--     (legacy animation_type=NULL rows). new_chase.py falls back to
--     the explicit pack_image_url in config/featured_pack.json.
--   - 1 row: pack_image_url is the URL to render. May be NULL if the
--     row exists but its JSONB lacks the key; new_chase.py treats
--     null/missing the same way (fallback to config URL).

SELECT
  id,
  title,
  reveal_animation_data->>'packImage'      AS pack_image_url,
  reveal_animation_data->>'animationType'  AS animation_type
FROM box_breaks
WHERE id = ':box_break_id'::uuid
  AND reveal_animation_data ? 'packImage';
