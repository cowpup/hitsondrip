"""Tests for src/hit_backlog.py — pure FIFO backlog queue logic."""

from __future__ import annotations

from datetime import datetime, timezone

from src import hit_backlog as hb

UTC = timezone.utc


def _now():
    return datetime(2026, 6, 11, 18, 0, 0, tzinfo=UTC)


class TestFoundations:
    def test_empty_backlog_shape(self):
        b = hb.empty_backlog()
        assert b == {"queue": [], "recently_posted": []}

    def test_ensure_shape_none_returns_empty(self):
        assert hb.ensure_shape(None) == {"queue": [], "recently_posted": []}

    def test_ensure_shape_fills_missing_keys(self):
        assert hb.ensure_shape({}) == {"queue": [], "recently_posted": []}
        assert hb.ensure_shape({"queue": [{"hit_id": 1}]}) == {
            "queue": [{"hit_id": 1}], "recently_posted": []
        }

    def test_ensure_shape_coerces_non_list_values(self):
        assert hb.ensure_shape({"queue": "bad", "recently_posted": 5}) == {
            "queue": [], "recently_posted": []
        }

    def test_parse_pulled_at_iso_with_z(self):
        dt = hb.parse_pulled_at("2026-06-11T07:32:00Z", _now())
        assert dt == datetime(2026, 6, 11, 7, 32, 0, tzinfo=UTC)

    def test_parse_pulled_at_space_separator_no_tz_assumes_utc(self):
        dt = hb.parse_pulled_at("2026-06-11 07:32:00", _now())
        assert dt == datetime(2026, 6, 11, 7, 32, 0, tzinfo=UTC)

    def test_parse_pulled_at_unparseable_falls_back_to_now(self):
        assert hb.parse_pulled_at("not-a-date", _now()) == _now()
        assert hb.parse_pulled_at(None, _now()) == _now()
