"""FIFO backlog queue for the Just Pulled automation (pure logic, no I/O).

The Just Pulled post features one $1,000+ graded hit per day. On burst
days multiple hits qualify; this module holds the extras in a queue so
quiet days still post. Persistence lives in src/state_branch.py
(read_hit_backlog / write_hit_backlog); this module only transforms the
backlog dict.

Backlog shape:
  {
    "queue": [ <hit dict>, ... ],            # pending, FIFO by pulled_at
    "recently_posted": [ {"hit_id": int, "at": iso}, ... ]  # consumed
  }

A hit dict carries the full render payload so a backlogged hit can be
rendered days later without re-querying:
  hit_id, pulled_at, hit_value, card_name, card_image_url,
  pack_name, pack_image_url, pack_price
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

QUEUE_MAX_AGE_DAYS = 7
POSTED_RETENTION_DAYS = 14


def empty_backlog() -> dict:
    return {"queue": [], "recently_posted": []}


def ensure_shape(backlog: Optional[dict]) -> dict:
    """Return a backlog dict guaranteed to have list-typed queue +
    recently_posted, tolerating None / missing / wrong-typed keys."""
    if not isinstance(backlog, dict):
        return empty_backlog()
    queue = backlog.get("queue")
    posted = backlog.get("recently_posted")
    return {
        "queue": queue if isinstance(queue, list) else [],
        "recently_posted": posted if isinstance(posted, list) else [],
    }


def parse_pulled_at(value: Any, now: datetime) -> datetime:
    """Parse a DB timestamp into a tz-aware UTC datetime.

    Accepts ISO 8601 with a trailing 'Z', a numeric UTC offset (e.g.
    '+00:00'), or a space separator between date and time; naive timestamps
    are assumed UTC. Anything unparseable falls back to `now` (treats the
    hit as fresh — it won't be wrongly expired, and sorts as newest under
    FIFO)."""
    if not isinstance(value, str):
        return now
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        log.warning("Unparseable pulled_at %r; treating as now", value)
        return now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _known_ids(backlog: dict) -> set[int]:
    ids: set[int] = set()
    for h in backlog["queue"]:
        hid = h.get("hit_id")
        if hid is not None:
            ids.add(int(hid))
    for r in backlog["recently_posted"]:
        hid = r.get("hit_id")
        if hid is not None:
            ids.add(int(hid))
    return ids


def merge_new(backlog: dict, hits: list[dict]) -> int:
    """Append hits whose hit_id isn't already in queue or recently_posted.
    Skips hits missing a hit_id. Returns the number added."""
    known = _known_ids(backlog)
    added = 0
    for h in hits:
        hid = h.get("hit_id")
        if hid is None:
            log.warning("Skipping hit with no hit_id: %r", h.get("card_name"))
            continue
        hid = int(hid)
        if hid in known:
            continue
        backlog["queue"].append(h)
        known.add(hid)
        added += 1
    return added


def expire(
    backlog: dict,
    now: datetime,
    max_age_days: int = QUEUE_MAX_AGE_DAYS,
    posted_retention_days: int = POSTED_RETENTION_DAYS,
) -> tuple[int, int]:
    """Drop queue items pulled more than `max_age_days` ago and prune
    recently_posted entries older than `posted_retention_days`.
    Returns (queue_dropped, posted_pruned).

    Note: expired queue items are NOT added to recently_posted — unlike a
    placeholder discard. An expired hit was never rendered/posted, so it
    needs no dedup protection; if the fetch window ever re-surfaced it,
    re-queuing would be harmless (it'd just expire again). The two paths
    are deliberately asymmetric."""
    queue_cutoff = now - timedelta(days=max_age_days)
    kept_queue = [
        h for h in backlog["queue"]
        if parse_pulled_at(h.get("pulled_at"), now) >= queue_cutoff
    ]
    dropped = len(backlog["queue"]) - len(kept_queue)
    backlog["queue"] = kept_queue

    posted_cutoff = now - timedelta(days=posted_retention_days)
    kept_posted = [
        r for r in backlog["recently_posted"]
        if parse_pulled_at(r.get("at"), now) >= posted_cutoff
    ]
    pruned = len(backlog["recently_posted"]) - len(kept_posted)
    backlog["recently_posted"] = kept_posted

    return dropped, pruned


def _record_consumed(backlog: dict, hit: dict, now: datetime) -> None:
    hid = hit.get("hit_id")
    if hid is not None:
        backlog["recently_posted"].append(
            {"hit_id": int(hid), "at": now.isoformat()}
        )
    else:
        log.warning("Consumed a hit with no hit_id; not recorded: %r",
                    hit.get("card_name"))


def pop_next_usable(
    backlog: dict,
    is_placeholder: Callable[[str], bool],
    now: datetime,
) -> Optional[dict]:
    """Pop the oldest usable hit (FIFO by pulled_at).

    Removes and permanently consumes any head whose card image is missing
    or a placeholder (recording it in recently_posted). The returned hit
    is removed from the queue but NOT marked posted — the caller calls
    mark_posted() after a successful Slack post. Returns None if the queue
    drains without a usable hit."""
    ordered = sorted(
        backlog["queue"],
        key=lambda h: parse_pulled_at(h.get("pulled_at"), now),
    )
    for hit in ordered:
        backlog["queue"] = [h for h in backlog["queue"] if h is not hit]
        url = hit.get("card_image_url") or ""
        if not url:
            log.info("Discarding queued hit %s — no card_image_url",
                     hit.get("hit_id"))
            _record_consumed(backlog, hit, now)
            continue
        if is_placeholder(url):
            log.info("Discarding queued hit %s — placeholder image",
                     hit.get("hit_id"))
            _record_consumed(backlog, hit, now)
            continue
        return hit
    return None


def mark_posted(backlog: dict, hit: dict, now: datetime) -> None:
    """Record a successfully-posted hit in recently_posted."""
    _record_consumed(backlog, hit, now)
