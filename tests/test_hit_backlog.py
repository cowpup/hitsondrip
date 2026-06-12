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


def _hit(hit_id, pulled_at="2026-06-11T07:00:00Z", value=1500.0):
    return {
        "hit_id": hit_id,
        "pulled_at": pulled_at,
        "hit_value": value,
        "card_name": f"Card {hit_id}",
        "card_image_url": f"https://cdn/{hit_id}.webp",
        "pack_name": "Charizard Pack",
        "pack_image_url": "https://cdn/pack.png",
        "pack_price": 250.0,
    }


class TestMergeNew:
    def test_adds_new_hits_to_empty_queue(self):
        b = hb.empty_backlog()
        added = hb.merge_new(b, [_hit(1), _hit(2)])
        assert added == 2
        assert [h["hit_id"] for h in b["queue"]] == [1, 2]

    def test_skips_hits_already_in_queue(self):
        b = {"queue": [_hit(1)], "recently_posted": []}
        added = hb.merge_new(b, [_hit(1), _hit(2)])
        assert added == 1
        assert [h["hit_id"] for h in b["queue"]] == [1, 2]

    def test_skips_hits_in_recently_posted(self):
        b = {"queue": [], "recently_posted": [{"hit_id": 9, "at": "x"}]}
        added = hb.merge_new(b, [_hit(9), _hit(10)])
        assert added == 1
        assert [h["hit_id"] for h in b["queue"]] == [10]

    def test_skips_hits_without_hit_id(self):
        b = hb.empty_backlog()
        bad = {"card_name": "no id"}
        added = hb.merge_new(b, [bad, _hit(3)])
        assert added == 1
        assert [h["hit_id"] for h in b["queue"]] == [3]

    def test_dedups_within_same_batch(self):
        b = hb.empty_backlog()
        added = hb.merge_new(b, [_hit(5), _hit(5)])
        assert added == 1
        assert [h["hit_id"] for h in b["queue"]] == [5]


class TestExpire:
    def test_drops_queue_items_older_than_max_age(self):
        b = {
            "queue": [
                _hit(1, pulled_at="2026-06-01T00:00:00Z"),  # 10 days old
                _hit(2, pulled_at="2026-06-10T00:00:00Z"),  # 1 day old
            ],
            "recently_posted": [],
        }
        dropped, pruned = hb.expire(b, _now())
        assert dropped == 1
        assert [h["hit_id"] for h in b["queue"]] == [2]

    def test_keeps_items_exactly_at_cutoff(self):
        # 7 days old exactly — not older than 7 days, so kept.
        b = {"queue": [_hit(1, pulled_at="2026-06-04T18:00:00Z")],
             "recently_posted": []}
        dropped, _ = hb.expire(b, _now())
        assert dropped == 0
        assert len(b["queue"]) == 1

    def test_prunes_recently_posted_older_than_retention(self):
        b = {
            "queue": [],
            "recently_posted": [
                {"hit_id": 1, "at": "2026-05-20T00:00:00Z"},  # 22 days
                {"hit_id": 2, "at": "2026-06-10T00:00:00Z"},  # 1 day
            ],
        }
        _, pruned = hb.expire(b, _now())
        assert pruned == 1
        assert [r["hit_id"] for r in b["recently_posted"]] == [2]
