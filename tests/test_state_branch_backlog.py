"""Round-trip tests for the JSON backlog state via the local fallback.

GITHUB_TOKEN is unset here so state_branch uses its data/ file fallback,
exercising the serialize/deserialize path without any network calls."""

from __future__ import annotations

import os

import pytest

from src import state_branch


@pytest.fixture(autouse=True)
def _force_local_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    # Redirect the local fallback file into a temp dir for isolation.
    monkeypatch.setattr(
        state_branch, "HIT_BACKLOG_LOCAL_PATH", tmp_path / "hit_backlog.json"
    )


def test_read_missing_returns_none():
    assert state_branch.read_hit_backlog() is None


def test_write_then_read_roundtrips():
    backlog = {
        "queue": [{"hit_id": 1, "pulled_at": "2026-06-11T07:00:00Z"}],
        "recently_posted": [{"hit_id": 9, "at": "2026-06-10T00:00:00Z"}],
    }
    state_branch.write_hit_backlog(backlog)
    assert state_branch.read_hit_backlog() == backlog


def test_corrupt_json_returns_none():
    state_branch.HIT_BACKLOG_LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    state_branch.HIT_BACKLOG_LOCAL_PATH.write_text("{not json", encoding="utf-8")
    assert state_branch.read_hit_backlog() is None


def test_last_hit_id_helpers_removed():
    # The old single-id dedup is superseded by the backlog.
    assert not hasattr(state_branch, "read_last_hit_id")
    assert not hasattr(state_branch, "write_last_hit_id")
