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

    Accepts ISO 8601 with a trailing 'Z' or a space separator. A naive
    timestamp is assumed UTC. Anything unparseable falls back to `now`
    (treats the hit as fresh — it won't be wrongly expired, and sorts as
    newest under FIFO)."""
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
