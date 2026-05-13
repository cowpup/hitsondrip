"""Tests for src/schedule_time.py — DST + before/after 6pm cases."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.schedule_time import PACIFIC_TZ_NAME, next_6pm_pt

PT = ZoneInfo(PACIFIC_TZ_NAME)


def _pt(year, month, day, hour, minute=0, second=0):
    """Build a tz-aware datetime in Pacific."""
    return datetime(year, month, day, hour, minute, second, tzinfo=PT)


class TestNextSixPmPT:
    def test_morning_returns_today_6pm(self):
        # 10am PT on a regular day → today 6pm PT.
        now = _pt(2026, 5, 13, 10, 0)
        iso, tz = next_6pm_pt(now=now)
        assert iso == "2026-05-13T18:00:00"
        assert tz == PACIFIC_TZ_NAME

    def test_just_before_6pm_returns_today(self):
        # 5:59pm PT → today 6pm PT (still 1 minute before target).
        now = _pt(2026, 5, 13, 17, 59)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-05-13T18:00:00"

    def test_just_after_6pm_returns_tomorrow(self):
        # 6:01pm PT → tomorrow 6pm PT.
        now = _pt(2026, 5, 13, 18, 1)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-05-14T18:00:00"

    def test_exactly_6pm_returns_tomorrow(self):
        # At-6pm uses strict ">=" so we schedule tomorrow, avoiding any
        # Metricool "publish in the past" race.
        now = _pt(2026, 5, 13, 18, 0, 0)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-05-14T18:00:00"

    def test_just_before_midnight_returns_tomorrow(self):
        # 11:59pm PT → tomorrow 6pm PT.
        now = _pt(2026, 5, 13, 23, 59)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-05-14T18:00:00"

    def test_just_after_midnight_returns_today(self):
        # 12:01am PT on the 14th → still today (14th) 6pm PT.
        now = _pt(2026, 5, 14, 0, 1)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-05-14T18:00:00"

    # ----- DST transitions ----- #

    def test_spring_forward_morning(self):
        # 2026 DST spring-forward: Sunday March 8, 2am PT jumps to 3am PT.
        # At 1am PT on March 8 (before the jump), 6pm PT today still exists.
        now = _pt(2026, 3, 8, 1, 0)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-03-08T18:00:00"

    def test_spring_forward_afternoon(self):
        # 10am PT on the spring-forward day → today 6pm PT (which is PDT).
        now = _pt(2026, 3, 8, 10, 0)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-03-08T18:00:00"

    def test_fall_back_morning(self):
        # 2026 DST fall-back: Sunday November 1, 2am PT repeats. We're
        # talking wall-clock — at 10am PT (which exists unambiguously
        # post-jump), the target is today 6pm PT (now PST).
        now = _pt(2026, 11, 1, 10, 0)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-11-01T18:00:00"

    def test_fall_back_evening_rolls_to_tomorrow(self):
        # 7pm PT on fall-back day → tomorrow (Nov 2) 6pm PT.
        now = _pt(2026, 11, 1, 19, 0)
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-11-02T18:00:00"

    # ----- Input variations ----- #

    def test_utc_input_converted(self):
        # 7pm UTC on May 13 = 12pm PT (UTC-7 in May/PDT). Before 6pm PT,
        # so target is today 6pm PT.
        now = datetime(2026, 5, 13, 19, 0, tzinfo=ZoneInfo("UTC"))
        iso, _ = next_6pm_pt(now=now)
        assert iso == "2026-05-13T18:00:00"

    def test_naive_input_assumed_pacific(self):
        # Naive datetime — we assume Pacific rather than reject.
        naive = datetime(2026, 5, 13, 10, 0)
        iso, _ = next_6pm_pt(now=naive)
        assert iso == "2026-05-13T18:00:00"

    def test_returns_naive_iso_no_offset(self):
        # Metricool requires no offset / no Z suffix in dateTime.
        iso, _ = next_6pm_pt(now=_pt(2026, 5, 13, 10, 0))
        assert "+" not in iso
        assert "Z" not in iso
        assert "-08" not in iso
        assert "-07" not in iso

    def test_timezone_is_always_pacific(self):
        _, tz = next_6pm_pt(now=_pt(2026, 5, 13, 10, 0))
        assert tz == "America/Los_Angeles"
