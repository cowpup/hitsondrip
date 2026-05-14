"""Fail-closed cleanup — runs at 5:55pm PT (00:55 UTC).

If `main.py` scheduled an IG (and possibly X) post earlier today and
nobody clicked Approve in Slack, this script deletes the unapproved
drafts so they don't sit forever in Metricool's queue.

Implementation:
  1. Query Metricool for posts in the Drip TCG brand's window covering today (PT).
  2. Filter to drafts where autoPublish is false AND scheduled for today.
     A user's Approve click flips autoPublish to true (via the Cloudflare
     Worker), so any post still at false here was never approved.
  3. Delete each unapproved post.
  4. Post a Slack notice — if at least one was deleted — so the channel
     sees that today's auto-post got skipped. Silent on a 0-delete day
     so we don't spam channels for normal "post got approved" cases.

Edge cases:
  - The cron fires daily even when main.py wasn't run that day (no hit
    in 24h, or manual workflow_dispatch testing). In those cases there's
    nothing to delete — list returns 0 drafts, we exit silently.
  - If a draft was already manually deleted in Metricool's dashboard,
    our delete will 404. We swallow that specific case to keep cleanup
    idempotent.
  - We only act on Drip TCG brand (METRICOOL_BLOG_ID). Other brands on
    the same Metricool account are untouched.

Usage:
  uv run python -u cleanup.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src import metricool, slack
from src.metricool import MetricoolError
from src.slack import SlackError

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hitsondrip-cleanup")

PACIFIC = ZoneInfo("America/Los_Angeles")


def _is_today_pt(post: dict[str, Any], now_pt: datetime) -> bool:
    """Return True if `post`'s publicationDate falls on today's PT date.

    Metricool's publicationDate is a dict like:
      {"dateTime": "2026-05-14T18:00:00", "timezone": "America/Los_Angeles"}
    """
    pub = post.get("publicationDate") or {}
    dt_str = pub.get("dateTime")
    if not dt_str:
        return False
    try:
        # The dateTime is naive; pair with the post's timezone if given.
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        return False
    tz_name = pub.get("timezone") or "America/Los_Angeles"
    try:
        post_tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        post_tz = PACIFIC
    dt_aware = dt.replace(tzinfo=post_tz)
    dt_pt = dt_aware.astimezone(PACIFIC)
    return dt_pt.date() == now_pt.date()


def _is_unapproved(post: dict[str, Any]) -> bool:
    """True if the post is still awaiting approval (autoPublish=false)."""
    # Metricool occasionally returns boolean strings or omits the field;
    # treat anything that isn't explicitly truthy-True as still-pending.
    val = post.get("autoPublish")
    if val is True:
        return False
    if isinstance(val, str) and val.lower() == "true":
        return False
    return True


def main() -> int:
    blog_id_raw = os.environ.get("METRICOOL_BLOG_ID")
    if not blog_id_raw:
        log.error("METRICOOL_BLOG_ID missing — cannot clean up.")
        return 1
    blog_id = int(blog_id_raw)

    now_pt = datetime.now(PACIFIC)
    log.info("Cleanup running at %s PT", now_pt.isoformat())

    # Query for posts in a small window around today — a 36h band centered
    # on now covers today's morning-scheduled posts plus any drift.
    start_naive = (now_pt - timedelta(hours=24)).replace(tzinfo=None)
    end_naive = (now_pt + timedelta(hours=12)).replace(tzinfo=None)
    try:
        posts = metricool.list_scheduled_posts(
            blog_id, start=start_naive, end=end_naive,
        )
    except MetricoolError as e:
        log.error("Failed to list scheduled posts: %s", e)
        try:
            slack.post_message(
                f":rotating_light: *Cleanup failed at list_scheduled_posts*\n"
                f"```{e}```\n_Any unapproved drafts will sit until manually cleared._"
            )
        except SlackError:
            pass
        return 1

    log.info("Metricool returned %d posts in window", len(posts))

    # Filter to today's unapproved drafts.
    targets = [
        p for p in posts
        if _is_today_pt(p, now_pt) and _is_unapproved(p)
    ]
    log.info("Found %d unapproved drafts scheduled for today", len(targets))

    if not targets:
        # Normal happy-path: user approved, so the drafts became
        # auto_publish=True and we filtered them out.
        log.info("Nothing to clean up. Exiting silent.")
        return 0

    # Delete each. We tolerate 404s (idempotent — someone may have already
    # manually deleted via Metricool dashboard).
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []
    for post in targets:
        post_id = post.get("id") or post.get("postId") or post.get("uuid")
        if not post_id:
            log.warning("Skipping post with no ID: %s", post)
            continue
        try:
            metricool.delete_scheduled_post(str(post_id), blog_id)
            deleted.append(str(post_id))
            log.info("Deleted draft post %s", post_id)
        except MetricoolError as e:
            msg = str(e)
            if "404" in msg or "not found" in msg.lower():
                log.info("Post %s already gone (404). Treating as deleted.", post_id)
                deleted.append(str(post_id))
            else:
                log.error("Failed to delete %s: %s", post_id, e)
                failed.append((str(post_id), msg))

    # Notify Slack so we know skipped days got cleaned up.
    try:
        if failed:
            err_summary = "\n".join(f"  • `{pid}`: {err[:200]}" for pid, err in failed)
            slack.post_message(
                f":zzz: *Auto-skipped {len(deleted)} draft(s)* — no approval by 5:55pm PT.\n"
                f":warning: Cleanup also hit {len(failed)} error(s):\n{err_summary}"
            )
        else:
            slack.post_message(
                f":zzz: *Auto-skipped {len(deleted)} draft(s)* — no approval received "
                f"by 5:55pm PT. Drafts deleted from Metricool."
            )
    except SlackError as e:
        log.error("Slack notify failed (non-fatal): %s", e)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
