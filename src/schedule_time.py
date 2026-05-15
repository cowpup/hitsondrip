"""Compute the next "wall-clock target time" Pacific Time slot.

Each daily automation publishes at a fixed wall-clock time. The cron
fires earlier (e.g. 12pm PT for Just Pulled, 10am PT for New Chase),
and the post is scheduled via Metricool for the target time. If the
cron is manually triggered AFTER the target, the post goes out
tomorrow at the same wall-clock time instead — we never schedule a
slot in the past.

Metricool's API expects a NAIVE wall-clock timestamp paired with a
separate IANA timezone string (see src/metricool.py:schedule_instagram_post
for the contract). This module returns exactly that pairing.

DST is handled by zoneinfo — "America/Los_Angeles" auto-shifts between
PST (UTC-8) and PDT (UTC-7); the wall-clock target stays the same in
both, e.g. "6pm PT" is 6pm year-round.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

PACIFIC_TZ_NAME = "America/Los_Angeles"


def next_target_pt(
    *,
    hour: int,
    minute: int = 0,
    second: int = 0,
    now: Optional[datetime] = None,
) -> tuple[str, str]:
    """Return ``(naive_iso, tz_name)`` for the next ``hour:minute`` PT slot.

    Args:
        hour: Target wall-clock hour in 24h time (0-23). E.g. 18 = 6pm,
            17 = 5pm.
        minute: Target minute (0-59). Defaults to 0.
        second: Target second (0-59). Defaults to 0. Almost always
            left at 0; exposed so the caller can produce e.g. 5:15:00.
        now: Optional override for "now" (must be tz-aware). Defaults
            to ``datetime.now(ZoneInfo(PACIFIC_TZ_NAME))``. The
            override is what makes this testable across DST boundaries
            without monkeypatching.

    Returns:
        A two-tuple of:
          - naive_iso: "YYYY-MM-DDTHH:MM:SS" with no Z and no offset.
            This is the wall-clock time in Pacific. Metricool requires
            the string without any timezone suffix.
          - tz_name: "America/Los_Angeles" (always). Metricool pairs
            the naive timestamp with this separately.

    Logic:
      - If ``now`` is before today's target, target = today at target.
      - If ``now`` is at or after today's target, target = tomorrow at target.
        (We use ``>=`` so that exactly-at-target runs schedule for
        tomorrow, avoiding any race with Metricool's "publish in the
        past" rejection.)
    """
    pacific = ZoneInfo(PACIFIC_TZ_NAME)
    if now is None:
        now = datetime.now(pacific)
    else:
        # Convert into Pacific so all the wall-clock math below stays
        # in the post timezone. If naive, assume Pacific (a friendly
        # default — tests should pass a tz-aware datetime).
        if now.tzinfo is None:
            now = now.replace(tzinfo=pacific)
        else:
            now = now.astimezone(pacific)

    today_target = now.replace(
        hour=hour, minute=minute, second=second, microsecond=0,
    )
    target = today_target if now < today_target else today_target + timedelta(days=1)
    return target.strftime("%Y-%m-%dT%H:%M:%S"), PACIFIC_TZ_NAME


# ----- Backward-compatible wrappers ------------------------------------- #


def next_6pm_pt(*, now: Optional[datetime] = None) -> tuple[str, str]:
    """Return the next 6pm PT slot. Used by main.py (Just Pulled).

    Thin wrapper around ``next_target_pt(hour=18)``. Kept for backward
    compatibility with main.py + its tests.
    """
    return next_target_pt(hour=18, now=now)


def next_5pm_pt(*, now: Optional[datetime] = None) -> tuple[str, str]:
    """Return the next 5pm PT slot. Used by new_chase.py.

    Thin wrapper around ``next_target_pt(hour=17)``. Set 2026-05-14 to
    give a 1-hour separation between New Chase (5pm) and Just Pulled
    (6pm) on days when both automations qualify.
    """
    return next_target_pt(hour=17, now=now)
