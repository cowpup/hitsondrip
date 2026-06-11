# Just Pulled — backlog queue design

**Date:** 2026-06-11
**Status:** Approved (design phase)
**Author:** Noah + Claude

## Problem

The Just Pulled automation posts the single biggest Drip-fulfilled graded
hit in the last 24h, once per day (12pm PT cron). After adding the
**$1,000 floor** (commit `97b1a0e`), most days have no qualifying hit —
in a representative 7-day sample only **3** hits cleared $1k, all on a
single day (2026-06-07: $1,500 / $1,080 / $850 spread). The result is
"feast or famine": a burst day wastes its extra big hits (only one posts),
while the following dry days post nothing.

## Goal

Smooth the output: post **one** $1k+ graded hit per day, and when a day
yields multiple, hold the extras in a **backlog** so quiet days draw from
it. Build a "natural backlog" that levels the daily cadence.

## Non-goals

- No change to the posting schedule (still one 12pm PT cron run/day).
- No change to the approval flow (Slack ✅/❌ buttons, Worker → Metricool).
- No caption change — "Just Pulled" framing stays even for backlogged
  hits (a few-days-old hit posting under that brand is acceptable; no one
  tracks pull-to-post latency closely).
- Graded-only is already enforced (`card.cert_number IS NOT NULL`); raw
  hits never enter the pipeline. No change needed.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Queue order | **FIFO** — oldest pull posts first (bounds wait, keeps chronology) |
| Staleness | **Expire**: drop a queued hit if its pull date is older than **7 days** when its turn comes |
| ❌ Skip behavior | **Discard permanently** — skipped hit is consumed, never re-shown |
| Threshold | **$1,000** floor (unchanged) |
| Fetch window | **48h** lookback (wider than 24h so cron drift can't drop a boundary hit; dedup makes re-seeing harmless) |

## Approach

**`main.py` owns the queue.** The moment it presents a hit's approval
card to Slack, that hit is consumed (moved out of the pending queue).
Approve / skip / ignore all leave it consumed — consistent with the
existing "state = presented to Noah" semantic. The Cloudflare Worker is
**not** involved in queue state (no new write access, no skip/ignore
edge cases).

This **supersedes** the simple `last_hit_id.txt` dedup shipped in commit
`ca1234f`: the backlog file becomes the single source of truth for what
is pending and what has already been shown, which still prevents the
same hit from posting twice (a same-day rerun finds the hit already in
`recently_posted` / already removed from `queue`).

Rejected alternative: have the Worker move items out of the queue on ✅
Approve. Requires granting the Worker state-branch write access and
handling skip/ignore re-queue logic — more moving parts for no benefit,
since "skip = discard" means approve and skip consume identically.

## Data model

One JSON file on the existing `state` orphan branch:
`state/hit_backlog.json`

```json
{
  "queue": [
    {
      "hit_id": 12345,
      "pulled_at": "2026-06-11T07:32:00Z",
      "hit_value": 1500.0,
      "card_name": "PSA GEM MT 10 VICTINI, 2025, ...",
      "card_image_url": "https://cdn.dripshop.live/product/...",
      "pack_name": "Charizard Pack",
      "pack_image_url": "https://cdn.dripshop.live/product/...",
      "pack_price": 250.0
    }
  ],
  "recently_posted": [
    { "hit_id": 12000, "at": "2026-06-10T19:00:00Z" }
  ]
}
```

- `queue` — pending $1k+ graded hits, ordered FIFO by `pulled_at`. Holds
  the full render payload so a backlogged hit can be rendered days later
  without re-querying.
- `recently_posted` — consumed `hit_id`s with consumption timestamp,
  pruned to the last **14 days**. Prevents the 48h re-query from
  re-adding a hit that was already shown.

`hit_id` = `product_purchases.id` (stable per-hit int key).

First run (file absent → Contents API 404) is treated as an empty
backlog `{"queue": [], "recently_posted": []}`.

## Daily run flow

Replaces the current select-top-usable-and-post logic in `main.py`'s
`run()`:

1. **Fetch** all $1k+ graded hits in the last **48h** via the query
   (returns `hit_id`, `pulled_at`, + render fields). No `LIMIT`; FIFO
   ordering happens in Python.
2. **Load** `hit_backlog.json` (empty structure on first run).
3. **Merge**: for each fetched hit, add to `queue` iff its `hit_id` is
   not already in `queue` and not in `recently_posted`.
4. **Expire**: remove `queue` items whose `pulled_at` is older than
   **7 days** from now. Prune `recently_posted` entries older than 14 days.
5. **Select**: pop the FIFO head (oldest `pulled_at`). If its
   `card_image_url` is a placeholder (per `src/image_filter.py` hash
   check), discard it (placeholders never become usable) — record its
   `hit_id` in `recently_posted` so the 48h re-query doesn't re-add it —
   and pop the next. Repeat until a usable hit is found or the queue
   empties.
6. **Post**: if a usable hit was found → render PNG, upload, post Slack
   approval card (downstream unchanged). Then move the hit from `queue`
   to `recently_posted` and **save** `hit_backlog.json`.
7. **Empty**: if the queue is empty (or only placeholders) → post a quiet
   `:zzz: No qualifying hit today (queue empty)` Slack note, save any
   merge/expire changes, exit 0.

State-write timing mirrors the existing pattern: the backlog is saved
**after** the Slack approval card posts successfully, so a failed run
leaves state untouched and retries cleanly. A save failure logs + Slack-
warns but does not fail the run (the card is already out).

## Components / code changes

- **`queries/biggest_hit_24h.sql`** — return all 48h hits ≥ $1,000 with
  `pp.id AS hit_id` and `pp.created_at AS pulled_at`; drop `LIMIT 5`
  (Python does FIFO selection). Keep filters: `cert_number IS NOT NULL`,
  no `video-renders`, `pgp.value IS NOT NULL`, `pgp.value >= 1000`,
  `created_at >= NOW() - INTERVAL '48 hours'`. The filename becomes a
  mild misnomer (no longer 24h / "biggest") — kept as-is to avoid
  churning `main.py`'s `SQL_PATH` and the prompt wiring; a header comment
  notes the broadened behavior.
- **`prompt.md`** — add `hit_id` (already added) and `pulled_at` to the
  JSON row shape; note the query may return many rows.
- **`src/hit_backlog.py`** (new) — pure queue logic, independently
  testable: `merge_new(backlog, hits)`, `expire(backlog, now)`,
  `pop_next_usable(backlog, is_placeholder_fn)`, plus
  load/save via the state branch. Keeps `main.py` thin.
- **`src/state_branch.py`** — add JSON file read/write for
  `state/hit_backlog.json`; retire the `last_hit_id.txt` int helpers
  (the New Chase `last_chase_card_id` helpers stay untouched).
- **`main.py`** — replace the top-hit selection + `last_hit_id` dedup
  block in `run()` with the queue flow above.

## Error handling

- **State read failure** (GitHub API): fail closed — emit failure to
  Slack, exit non-zero (mirrors New Chase). Without the backlog we can't
  safely de-dup or know what's pending.
- **State write failure** after a successful Slack post: log + Slack-warn,
  exit 0. The card is out; risking a possible duplicate next run beats
  aborting a successful post.
- **Query failure**: existing behavior — emit failure to Slack, exit 1.
- **Corrupt JSON**: treat as empty backlog, log a warning (don't crash).

## Testing

Unit tests for `src/hit_backlog.py` (no network):

- `merge_new`: adds unseen hits; skips ids already in `queue`; skips ids
  in `recently_posted`; preserves FIFO order by `pulled_at`.
- `expire`: drops queue items older than 7 days; keeps fresh ones; prunes
  `recently_posted` older than 14 days.
- `pop_next_usable`: returns oldest usable hit; skips/discards placeholder
  heads; returns None on empty/all-placeholder queue; mutates backlog
  correctly (popped item removed).
- Round-trip serialize/deserialize; empty/first-run structure.

`main.py` integration is covered by existing render/post tests plus a
queue-path smoke test with the state branch mocked to the local fallback.

## Rollout notes

- The first run after deploy starts with an empty backlog (no file yet),
  so behavior is identical to today until hits accumulate.
- `ca1234f`'s `last_hit_id.txt` becomes vestigial; the new backlog file
  takes over dedup. The old file can be left on the state branch (ignored)
  or deleted manually — no code reads it after this change.
```
