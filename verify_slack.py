"""Smoke-test Slack posting: post one test message to the configured channel.

Run: uv run python verify_slack.py
Exit 0 on success, 1 on any failure.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from src.slack import SlackError, post_message

load_dotenv()


def main() -> int:
    try:
        result = post_message("drip-daily-just-pulled test message")
    except SlackError as e:
        print(f"FAIL: {e}")
        return 1

    ts = result.get("ts")
    channel = result.get("channel")
    print(f"PASS — posted to channel {channel} at ts={ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
