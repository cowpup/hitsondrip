"""Compute the next 6pm Pacific Time slot for the daily Metricool schedule.

The cron fires at 12pm PT every day; the daily IG post is scheduled for
6pm PT the same day. If the cron is manually triggered AFTER 6pm PT
(workflow_dispatch), the post goes out tomorrow at 6pm PT instead — we
never schedule a slot in the past.

Metricool's API expects a NAIVE wall-clock timestamp paired with a
separate IANA timezone string (see src/metricool.py:schedule_instagram_post
for the contract). This module returns exactly that pairing.

DST is handled by zoneinfo — "America/Los_Angeles" auto-shifts between
PST (UTC-8) and PDT (UTC-7); the wall-clock "6pm PT" stays 6pm in both.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

PACIFIC_TZ_NAME = "America/Los_Angeles"
TARGET_HOUR = 18           # 6pm in 24h time
TARGET_MINUTE = 0
TARGET_SECOND = 0


def next_6pm_pt(*, now: Optional[datetime] = None) -> tuple[str, str]:
    """Return ``(naive_iso, tz_name)`` for the next 6pm PT slot.

    Args:
        now: Optional override for "now" (must be tz-aware). Defaults to
            datetime.now(ZoneInfo(PACIFIC_TZ_NAME)). The override is what
            makes this testable across DST boundaries without monkeypatching.

    Returns:
        A two-tuple of:
          - naive_iso: "YYYY-MM-DDTHH:MM:SS" with no Z and no offset. This
            is the wall-clock time in Pacific. Metricool requires the
            string without any timezone suffix.
          - tz_name: "America/Los_Angeles" (always). Metricool pairs the
            naive timestamp with this separately.

    Logic:
      - If `now` is before today's 6pm PT, target = today 6pm PT.
      - If `now` is at or after today's 6pm PT, target = tomorrow 6pm PT.
        (We use >= so that exactly-at-6pm runs schedule for tomorrow,
        avoiding any race with Metricool's "publish in the past" rejection.)
    """
    pacific = ZoneInfo(PACIFIC_TZ_NAME)
    if now is None:
        now = datetime.now(pacific)
    else:
        # Convert into Pacific so all the wall-clock math below stays in
        # the post timezone. If naive, assume Pacific (a friendly default
        # but tests should pass a tz-aware datetime).
        if now.tzinfo is None:
            now = now.replace(tzinfo=pacific)
        else:
            now = now.astimezone(pacific)

    today_6pm = now.replace(
        hour=TARGET_HOUR, minute=TARGET_MINUTE, second=TARGET_SECOND, microsecond=0,
    )
    target = today_6pm if now < today_6pm else today_6pm + timedelta(days=1)

    naive_iso = target.strftime("%Y-%m-%dT%H:%M:%S")
    return naive_iso, PACIFIC_TZ_NAME
