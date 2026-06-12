# Just Pulled Backlog Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Just Pulled automation post one $1k+ graded hit per day from a FIFO backlog, holding extras so burst days don't waste big hits and dry days still post.

**Architecture:** A new pure-logic module `src/hit_backlog.py` manages a queue (merge new hits, expire stale ones, FIFO-pop the next usable one). The queue persists as JSON on the existing `state` orphan branch via new helpers in `src/state_branch.py`. `main.py`'s `run()` orchestrates: fetch → merge → expire → pop → render/post → save. This supersedes the `last_hit_id.txt` dedup; the queue file is now the single source of truth for what's pending and what's already been shown.

**Tech Stack:** Python 3.13, pytest, `uv` for running, GitHub Contents API (via `requests`) for state persistence.

**Reference spec:** `docs/superpowers/specs/2026-06-11-just-pulled-backlog-queue-design.md`

---

## File Structure

- **Create** `src/hit_backlog.py` — pure queue logic (no network). Functions: `empty_backlog`, `ensure_shape`, `parse_pulled_at`, `merge_new`, `expire`, `pop_next_usable`, `mark_posted`. Module constants `QUEUE_MAX_AGE_DAYS = 7`, `POSTED_RETENTION_DAYS = 14`.
- **Create** `tests/test_hit_backlog.py` — unit tests for the above.
- **Create** `tests/test_state_branch_backlog.py` — round-trip tests for the new JSON state helpers via the local fallback (no network).
- **Modify** `src/state_branch.py` — refactor transport into `_read_raw_state`/`_write_raw_state`; add `read_hit_backlog`/`write_hit_backlog` + `HIT_BACKLOG_FILENAME`/`HIT_BACKLOG_LOCAL_PATH`; **retire** `read_last_hit_id`/`write_last_hit_id` and their constants (keep the New Chase `last_chase_card_id` helpers).
- **Modify** `queries/biggest_hit_24h.sql` — return all 48h hits ≥ $1,000 with `hit_id` + `pulled_at`; drop `LIMIT 5`.
- **Modify** `prompt.md` — add `pulled_at` to the JSON row shape; note many rows may return.
- **Modify** `main.py` — replace the select-top-and-dedup block in `run()` with the queue flow; drop the `last_hit_id` import usage.

---

## Task 1: hit_backlog foundations — `empty_backlog`, `ensure_shape`, `parse_pulled_at`

**Files:**
- Create: `src/hit_backlog.py`
- Test: `tests/test_hit_backlog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hit_backlog.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hit_backlog.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.hit_backlog'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/hit_backlog.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_hit_backlog.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/hit_backlog.py tests/test_hit_backlog.py
git commit -m "feat(backlog): hit_backlog foundations (empty/ensure_shape/parse_pulled_at)"
```

---

## Task 2: `merge_new` — add unseen hits, dedup by hit_id

**Files:**
- Modify: `src/hit_backlog.py`
- Test: `tests/test_hit_backlog.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_hit_backlog.py

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hit_backlog.py::TestMergeNew -v`
Expected: FAIL with `AttributeError: module 'src.hit_backlog' has no attribute 'merge_new'`

- [ ] **Step 3: Write minimal implementation**

```python
# Append to src/hit_backlog.py

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_hit_backlog.py::TestMergeNew -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/hit_backlog.py tests/test_hit_backlog.py
git commit -m "feat(backlog): merge_new dedups by hit_id against queue + recently_posted"
```

---

## Task 3: `expire` — drop stale queue items, prune recently_posted

**Files:**
- Modify: `src/hit_backlog.py`
- Test: `tests/test_hit_backlog.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_hit_backlog.py

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hit_backlog.py::TestExpire -v`
Expected: FAIL with `AttributeError: module 'src.hit_backlog' has no attribute 'expire'`

- [ ] **Step 3: Write minimal implementation**

```python
# Append to src/hit_backlog.py

def expire(
    backlog: dict,
    now: datetime,
    max_age_days: int = QUEUE_MAX_AGE_DAYS,
    posted_retention_days: int = POSTED_RETENTION_DAYS,
) -> tuple[int, int]:
    """Drop queue items pulled more than `max_age_days` ago and prune
    recently_posted entries older than `posted_retention_days`.
    Returns (queue_dropped, posted_pruned)."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_hit_backlog.py::TestExpire -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/hit_backlog.py tests/test_hit_backlog.py
git commit -m "feat(backlog): expire stale queue items + prune recently_posted"
```

---

## Task 4: `pop_next_usable` + `mark_posted` — FIFO selection with placeholder discard

**Files:**
- Modify: `src/hit_backlog.py`
- Test: `tests/test_hit_backlog.py`

**Design notes (read before implementing):**
- `pop_next_usable` sorts the queue oldest-first by `pulled_at`, walks from the oldest, and returns the first hit with a non-placeholder card image. It **removes** the returned hit from the queue but does **not** add it to `recently_posted` — the caller does that via `mark_posted` only after a successful Slack post (so a render/upload/Slack failure leaves the hit queued for retry).
- Discarded heads (missing `card_image_url`, or placeholder) ARE removed from the queue and recorded in `recently_posted` immediately — they can never become usable, so they're permanently consumed.
- `is_placeholder` is injected as `Callable[[str], bool]` so the test doesn't hit the network. In `main.py` it wraps `image_filter.is_placeholder(url, blacklist)`.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_hit_backlog.py

class TestPopNextUsable:
    def _never_placeholder(self, url):
        return False

    def test_returns_oldest_by_pulled_at(self):
        b = {
            "queue": [
                _hit(2, pulled_at="2026-06-11T09:00:00Z"),
                _hit(1, pulled_at="2026-06-11T07:00:00Z"),  # oldest
            ],
            "recently_posted": [],
        }
        chosen = hb.pop_next_usable(b, self._never_placeholder, _now())
        assert chosen["hit_id"] == 1
        # Removed from queue, NOT yet in recently_posted (caller marks it).
        assert [h["hit_id"] for h in b["queue"]] == [2]
        assert b["recently_posted"] == []

    def test_skips_and_discards_placeholder_heads(self):
        def is_ph(url):
            return url.endswith("/1.webp")  # hit 1 is a placeholder
        b = {
            "queue": [
                _hit(1, pulled_at="2026-06-11T07:00:00Z"),  # placeholder
                _hit(2, pulled_at="2026-06-11T08:00:00Z"),  # usable
            ],
            "recently_posted": [],
        }
        chosen = hb.pop_next_usable(b, is_ph, _now())
        assert chosen["hit_id"] == 2
        assert b["queue"] == []  # both removed (1 discarded, 2 returned)
        # placeholder 1 recorded as consumed; 2 left for caller to mark.
        assert [r["hit_id"] for r in b["recently_posted"]] == [1]

    def test_discards_hits_without_card_image_url(self):
        broken = _hit(1)
        broken["card_image_url"] = ""
        b = {"queue": [broken, _hit(2, pulled_at="2026-06-11T08:00:00Z")],
             "recently_posted": []}
        chosen = hb.pop_next_usable(b, self._never_placeholder, _now())
        assert chosen["hit_id"] == 2
        assert [r["hit_id"] for r in b["recently_posted"]] == [1]

    def test_returns_none_when_all_placeholder(self):
        b = {"queue": [_hit(1), _hit(2)], "recently_posted": []}
        chosen = hb.pop_next_usable(b, lambda url: True, _now())
        assert chosen is None
        assert b["queue"] == []
        assert {r["hit_id"] for r in b["recently_posted"]} == {1, 2}

    def test_returns_none_on_empty_queue(self):
        b = hb.empty_backlog()
        assert hb.pop_next_usable(b, self._never_placeholder, _now()) is None


class TestMarkPosted:
    def test_appends_to_recently_posted(self):
        b = hb.empty_backlog()
        hb.mark_posted(b, _hit(7), _now())
        assert b["recently_posted"] == [
            {"hit_id": 7, "at": "2026-06-11T18:00:00+00:00"}
        ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hit_backlog.py::TestPopNextUsable tests/test_hit_backlog.py::TestMarkPosted -v`
Expected: FAIL with `AttributeError: module 'src.hit_backlog' has no attribute 'pop_next_usable'`

- [ ] **Step 3: Write minimal implementation**

```python
# Append to src/hit_backlog.py

def _record_consumed(backlog: dict, hit: dict, now: datetime) -> None:
    hid = hit.get("hit_id")
    if hid is not None:
        backlog["recently_posted"].append(
            {"hit_id": int(hid), "at": now.isoformat()}
        )


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
        backlog["queue"].remove(hit)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_hit_backlog.py -v`
Expected: PASS (all tests across Tasks 1–4)

- [ ] **Step 5: Commit**

```bash
git add src/hit_backlog.py tests/test_hit_backlog.py
git commit -m "feat(backlog): pop_next_usable (FIFO + placeholder discard) + mark_posted"
```

---

## Task 5: state_branch — JSON backlog persistence, retire last_hit_id

**Files:**
- Modify: `src/state_branch.py`
- Test: `tests/test_state_branch_backlog.py`

**Design notes:**
- Refactor the GET/PUT transport out of `_read_last_id`/`_write_last_id` into `_read_raw_state(remote_filename, local_path) -> Optional[str]` and `_write_raw_state(text, remote_filename, local_path, commit_message)`. The int helpers (used by New Chase `last_chase_card_id`) keep working through them.
- Add `read_hit_backlog() -> Optional[dict]` (None on 404 or corrupt JSON; raises `StateBranchError` on network/HTTP failure) and `write_hit_backlog(backlog)`.
- Remove `read_last_hit_id`, `write_last_hit_id`, `HIT_STATE_FILENAME`, `HIT_LOCAL_FALLBACK_PATH`.
- Add `import json` and a module logger.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state_branch_backlog.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_state_branch_backlog.py -v`
Expected: FAIL — `AttributeError: module 'src.state_branch' has no attribute 'HIT_BACKLOG_LOCAL_PATH'` (and `read_hit_backlog`).

- [ ] **Step 3: Write the implementation**

Add near the top imports of `src/state_branch.py` (after `import os`):

```python
import json
import logging
```

Add after the existing logger setup / before `class StateBranchError` (if no logger exists, add it):

```python
log = logging.getLogger(__name__)
```

Replace the `HIT_STATE_FILENAME` / `HIT_LOCAL_FALLBACK_PATH` constant block with:

```python
# Just Pulled de-dup + backlog (JSON queue of pending $1k+ graded hits).
HIT_BACKLOG_FILENAME = "state/hit_backlog.json"
HIT_BACKLOG_LOCAL_PATH = Path("data") / "hit_backlog.json"
```

Add the raw transport helpers (place them above `_read_last_id`):

```python
def _read_raw_state(remote_filename: str, local_path: Path) -> Optional[str]:
    """Return the decoded UTF-8 contents of a state file, or None if it
    doesn't exist yet (Contents API 404, or no local fallback file).
    Raises StateBranchError on network / HTTP failure."""
    env = _resolve_repo()
    if env is None:
        if local_path.exists():
            try:
                return local_path.read_text(encoding="utf-8")
            except OSError:
                return None
        return None

    owner, repo, token = env
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{remote_filename}"
    try:
        response = requests.get(
            url, headers=_api_headers(token),
            params={"ref": STATE_BRANCH}, timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise StateBranchError(f"GitHub GET {url}: {e}") from e

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise StateBranchError(
            f"GitHub GET {url} → HTTP {response.status_code}: "
            f"{response.text[:400]}"
        )
    encoded = response.json().get("content", "")
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _write_raw_state(
    text: str, remote_filename: str, local_path: Path, commit_message: str
) -> None:
    """Overwrite a state file with `text`. Raises StateBranchError on
    API failure."""
    env = _resolve_repo()
    if env is None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(text, encoding="utf-8")
        return

    owner, repo, token = env
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{remote_filename}"
    try:
        sha_response = requests.get(
            url, headers=_api_headers(token),
            params={"ref": STATE_BRANCH}, timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise StateBranchError(f"GitHub GET {url}: {e}") from e

    existing_sha: Optional[str] = None
    if sha_response.status_code == 200:
        existing_sha = sha_response.json().get("sha")
    elif sha_response.status_code != 404:
        raise StateBranchError(
            f"GitHub GET {url} → HTTP {sha_response.status_code}: "
            f"{sha_response.text[:400]}"
        )

    body: dict[str, object] = {
        "message": commit_message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": STATE_BRANCH,
    }
    if existing_sha:
        body["sha"] = existing_sha

    try:
        put_response = requests.put(
            url, headers=_api_headers(token), json=body,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise StateBranchError(f"GitHub PUT {url}: {e}") from e

    if put_response.status_code not in (200, 201):
        raise StateBranchError(
            f"GitHub PUT {url} → HTTP {put_response.status_code}: "
            f"{put_response.text[:400]}"
        )
```

Replace the bodies of `_read_last_id` and `_write_last_id` to delegate to the raw helpers:

```python
def _read_last_id(remote_filename: str, local_path: Path) -> Optional[int]:
    raw = _read_raw_state(remote_filename, local_path)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _write_last_id(
    value: int, remote_filename: str, local_path: Path, commit_prefix: str,
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _write_raw_state(
        f"{value}\n", remote_filename, local_path,
        f"{commit_prefix}={value} @ {ts}",
    )
```

Delete the entire `# --- Just Pulled de-dup ---` section (`read_last_hit_id` + `write_last_hit_id`) and add in its place:

```python
# --- Just Pulled backlog ------------------------------------------------ #

def read_hit_backlog() -> Optional[dict]:
    """Return the parsed hit-backlog dict, or None if it doesn't exist
    yet (first run) or is corrupt. Raises StateBranchError on network
    failure (caller should fail closed)."""
    raw = _read_raw_state(HIT_BACKLOG_FILENAME, HIT_BACKLOG_LOCAL_PATH)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("hit_backlog.json is corrupt (%s); treating as empty", e)
        return None


def write_hit_backlog(backlog: dict) -> None:
    """Persist the hit-backlog dict to the state branch."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    n = len(backlog.get("queue", []))
    text = json.dumps(backlog, indent=2, ensure_ascii=False) + "\n"
    _write_raw_state(
        text, HIT_BACKLOG_FILENAME, HIT_BACKLOG_LOCAL_PATH,
        f"just_pulled: backlog ({n} queued) @ {ts}",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_state_branch_backlog.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Verify the New Chase int helpers still work**

Run: `uv run python -c "from src import state_branch as s; import inspect; print('chase ok' if hasattr(s,'read_last_card_id') and hasattr(s,'write_last_card_id') else 'MISSING')"`
Expected: `chase ok`

- [ ] **Step 6: Commit**

```bash
git add src/state_branch.py tests/test_state_branch_backlog.py
git commit -m "feat(backlog): JSON backlog persistence on state branch; retire last_hit_id"
```

---

## Task 6: Query + prompt — return all 48h $1k+ hits with hit_id + pulled_at

**Files:**
- Modify: `queries/biggest_hit_24h.sql`
- Modify: `prompt.md`

- [ ] **Step 1: Update the SQL header + window + columns + remove LIMIT**

In `queries/biggest_hit_24h.sql`, change the `WHERE` window from 24h to 48h and remove the `LIMIT`. Replace this block:

```sql
WHERE pp.created_at >= NOW() - INTERVAL '24 hours'
  AND card.cert_number IS NOT NULL
  AND card.image NOT LIKE '%video-renders%'
  AND pgp.value IS NOT NULL
  AND pgp.value >= 1000
ORDER BY pgp.value DESC NULLS LAST
LIMIT 5;
```

with:

```sql
WHERE pp.created_at >= NOW() - INTERVAL '48 hours'
  AND card.cert_number IS NOT NULL
  AND card.image NOT LIKE '%video-renders%'
  AND pgp.value IS NOT NULL
  AND pgp.value >= 1000
ORDER BY pp.created_at ASC;  -- oldest first; Python backlog does FIFO selection
```

Confirm the `SELECT` already includes `pp.id AS hit_id` (added earlier). Add `pp.created_at AS pulled_at`. The SELECT list should read:

```sql
SELECT
    pp.id               AS hit_id,
    pp.created_at       AS pulled_at,
    card.name           AS card_name,
    card.image          AS card_image_url,
    bb.title            AS pack_name,
    bb.image            AS pack_image_url,
    pp.unit_price       AS pack_price,
    pgp.value           AS hit_value
```

Update the header comment's "Sort + limit" section to note: returns ALL 48h hits ≥ $1,000 (no LIMIT); the Python backlog (`src/hit_backlog.py`) handles FIFO one-per-day selection and placeholder skipping.

- [ ] **Step 2: Update prompt.md row shape**

In `prompt.md`, the JSON row shape currently starts with `hit_id`. Add `pulled_at` right after it so the block reads:

```json
[
  {
    "hit_id": <number>,
    "pulled_at": "2026-06-11T07:32:00Z",
    "card_name": "...",
    "card_image_url": "https://cdn.dripshop.live/product/...",
    "pack_name": "...",
    "pack_price": <number>,
    "pack_image_url": "https://cdn.dripshop.live/...",
    "hit_value": <number>
  },
  ...
]
```

Also change the Task line "The query returns up to 5 rows" to "The query returns zero or more rows (all $1,000+ graded hits in the last 48h)".

- [ ] **Step 3: Verify the SQL still has the threshold + placeholder substitution sanity**

Run: `uv run python -c "from pathlib import Path; t=Path('queries/biggest_hit_24h.sql').read_text(encoding='utf-8'); assert 'pgp.value >= 1000' in t and 'pulled_at' in t and '48 hours' in t and 'LIMIT 5' not in t and 'created_at ASC' in t; print('sql ok')"`
Expected: `sql ok` (checks: threshold kept, pulled_at added, 48h window, no `LIMIT 5`, FIFO ordering)

- [ ] **Step 4: Commit**

```bash
git add queries/biggest_hit_24h.sql prompt.md
git commit -m "feat(backlog): query returns all 48h \$1k+ hits with hit_id + pulled_at"
```

---

## Task 7: main.py — wire the queue into run()

**Files:**
- Modify: `main.py` (imports near line 39–43; `run()` body lines 320–522)

**Design notes:**
- Replace the candidate-loop + `last_hit_id` dedup with: fetch → ensure_shape(read) → merge → expire → pop_next_usable → (empty? save + notify) → render/upload/slack (unchanged) → mark_posted + save.
- `read_hit_backlog` raising `StateBranchError` → fail closed (`_emit_failure_to_slack`, return 1), same as before.
- Save the backlog on the empty path (persists merges/expiry/placeholder discards) AND on the success path (after the Slack card posts). Do NOT save on render/upload/slack failure paths (return 1) so the hit stays queued for retry.

- [ ] **Step 1: Update imports**

In `main.py`, change:

```python
from src import image_filter, schedule_time, slack, state_branch, string_transforms
from src.image_host import publish_to_github, ImageHostError
from src.renderer import RenderError, render_just_pulled
from src.slack import SlackError
from src.state_branch import StateBranchError
```

to add `hit_backlog`:

```python
from src import (
    hit_backlog, image_filter, schedule_time, slack, state_branch,
    string_transforms,
)
from src.image_host import publish_to_github, ImageHostError
from src.renderer import RenderError, render_just_pulled
from src.slack import SlackError
from src.state_branch import StateBranchError
```

- [ ] **Step 2: Replace the top of run() through the dedup block**

Replace lines from `def run() -> int:` down to (and including) the entire de-dup guard block that ends with the `return 0` just before `# Render` (the block that starts `def run() -> int:` and ends at the line `        return 0` immediately preceding `    # Render`) with:

```python
def run() -> int:
    """Returns 0 on success or empty-day, non-zero on any actual failure."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    try:
        fresh_hits = fetch_top_hits()
    except Exception as e:  # noqa: BLE001 — surface raw exception to Slack
        _emit_failure_to_slack("DripShopLive query failed", e)
        return 1

    # Load the backlog (fail closed on a state read error — without it we
    # can't safely de-dup or know what's pending; mirrors new_chase.py).
    try:
        backlog = hit_backlog.ensure_shape(state_branch.read_hit_backlog())
    except StateBranchError as e:
        _emit_failure_to_slack("State branch read failed", e)
        return 1

    added = hit_backlog.merge_new(backlog, fresh_hits)
    dropped, pruned = hit_backlog.expire(backlog, now)
    log.info(
        "Backlog: %d fetched, %d new added, %d expired, %d pending after merge",
        len(fresh_hits), added, dropped, len(backlog["queue"]),
    )

    # FIFO-select the oldest usable hit, discarding placeholder/broken ones.
    blacklist = image_filter.load_blacklist()

    def _is_placeholder(url: str) -> bool:
        blocked, reason = image_filter.is_placeholder(url, blacklist)
        if blocked:
            log.info("Placeholder card image skipped: %s", reason)
        return blocked

    hit = hit_backlog.pop_next_usable(backlog, _is_placeholder, now)

    if hit is None:
        log.info("Backlog empty / no usable hit; nothing to post today.")
        # Persist merges/expiry/placeholder discards before exiting.
        _save_backlog_best_effort(backlog)
        try:
            slack.post_message(
                ":zzz: No qualifying hit today — the $1,000+ backlog is "
                "empty. No Instagram post will be scheduled."
            )
        except SlackError as e:
            log.error("Slack notify failed on empty-backlog path: %s", e)
            return 1
        return 0

    log.info(
        "Selected hit: $%s on %s from %s (id=%s, pulled %s)",
        hit.get("hit_value"), hit.get("card_name"), hit.get("pack_name"),
        hit.get("hit_id"), hit.get("pulled_at"),
    )
```

**Important:** end the replacement at the `log.info("Selected hit...")` call above. The existing `# Render` block (current line 434 onward) stays unchanged and follows directly after — do **not** re-type it, and make sure you don't leave a duplicate `# Render` comment.

- [ ] **Step 3: Replace the tail state-write block**

Replace the final `# --- Record state ---` block (the `if hit_id is not None:` write-`last_hit_id` block, ending at `    return 0`) with:

```python
    # --- Record state ------------------------------------------------------
    # Mark this hit consumed and persist the backlog AFTER the approval card
    # posted (not on Worker ✅) so a same-day rerun won't re-present it. A
    # failed run above returns non-zero WITHOUT saving, leaving the hit
    # queued for retry. A save failure here logs + Slack-warns but does not
    # fail the run — the card is already out.
    hit_backlog.mark_posted(backlog, hit, now)
    if not _save_backlog_best_effort(backlog):
        try:
            slack.post_message(
                f":warning: Just Pulled post succeeded BUT backlog save "
                f"failed. A rerun may re-present hit id `{hit.get('hit_id')}` "
                f"— check `state/hit_backlog.json`."
            )
        except SlackError:
            pass

    return 0


def _save_backlog_best_effort(backlog: dict) -> bool:
    """Write the backlog to the state branch. Returns True on success,
    False on StateBranchError (logged, not raised)."""
    try:
        state_branch.write_hit_backlog(backlog)
        log.info("Saved backlog: %d queued, %d recently_posted",
                 len(backlog["queue"]), len(backlog["recently_posted"]))
        return True
    except StateBranchError as e:
        log.error("Backlog save FAILED (continuing): %s", e)
        return False
```

- [ ] **Step 4: Verify main.py imports and compiles**

Run: `uv run python -c "import main; print('main imports ok')"`
Expected: `main imports ok`

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -v`
Expected: all tests pass (schedule_time, string_transforms, hit_backlog, state_branch_backlog).

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat(backlog): wire FIFO backlog queue into Just Pulled run()"
```

---

## Task 8: End-to-end dry-run validation (local fallback)

**Files:** none (validation only)

- [ ] **Step 1: Simulate a multi-hit burst, confirm one-per-day FIFO drain**

Run this script (uses the local `data/hit_backlog.json` fallback, no network, no model call):

```bash
uv run python -c "
from datetime import datetime, timezone, timedelta
from src import hit_backlog as hb, state_branch as sb
from pathlib import Path
sb.HIT_BACKLOG_LOCAL_PATH = Path('data')/'hit_backlog_dryrun.json'
if sb.HIT_BACKLOG_LOCAL_PATH.exists(): sb.HIT_BACKLOG_LOCAL_PATH.unlink()
now = datetime(2026,6,11,18,0,tzinfo=timezone.utc)
def mkhit(i,h): return {'hit_id':i,'pulled_at':(now-timedelta(hours=h)).isoformat(),
  'hit_value':1000+i,'card_name':f'Card{i}','card_image_url':f'https://cdn/{i}.webp',
  'pack_name':'Pack','pack_image_url':'https://cdn/p.png','pack_price':100}
b = hb.ensure_shape(sb.read_hit_backlog())
hb.merge_new(b,[mkhit(1,5),mkhit(2,3),mkhit(3,1)])  # 3 hits in one burst
order=[]
for day in range(4):
    h = hb.pop_next_usable(b, lambda u: False, now)
    order.append(h['hit_id'] if h else None)
    if h: hb.mark_posted(b,h,now)
print('post order over 4 days:', order)
assert order==[1,2,3,None], order  # oldest-first FIFO, then empty
print('DRY RUN OK — FIFO one-per-day drain confirmed')
sb.HIT_BACKLOG_LOCAL_PATH.unlink()
"
```
Expected: `post order over 4 days: [1, 2, 3, None]` then `DRY RUN OK`.

- [ ] **Step 2: Confirm no stray dry-run artifacts**

Run: `git status --short`
Expected: clean (the script deletes its temp file).

- [ ] **Step 3: Final commit (if any doc/cleanup pending)**

No code change expected here. If `git status` shows anything, investigate before committing.

---

## Self-Review Notes (completed by plan author)

- **Spec coverage:** FIFO order (Task 4), 7-day expiry (Task 3), skip=discard/consume semantics (Tasks 4+7), $1k floor + 48h window (Task 6), state-branch JSON queue (Task 5), main.py orchestration + fail-closed/best-effort save (Task 7), supersedes last_hit_id (Task 5 retires it). All covered.
- **Type consistency:** `backlog` is always `{"queue": list, "recently_posted": list}`. `pop_next_usable`/`mark_posted`/`merge_new`/`expire` all take and mutate that dict. `now` is tz-aware UTC throughout. `read_hit_backlog` returns `Optional[dict]`; `ensure_shape` normalizes.
- **Known limitation (accepted):** if the selected hit's *render* fails repeatedly (not a known placeholder), it stays at the FIFO head and blocks the queue until fixed — but the failure pages via Slack each run, so it's visible. Out of scope to auto-skip render failures.

---

## Post-implementation (not part of TDD tasks)

- Update the `hitsondrip-deployment` memory: Just Pulled now uses a FIFO `state/hit_backlog.json` queue (supersedes `last_hit_id.txt`); 48h window; 7-day expiry.
- Push to `main` (production deploy) — confirm with Noah first, since this changes daily posting behavior.
