"""Slack Web API wrapper — one function, one purpose.

Posts a single notification to Slack at the end of each daily run.
Uses chat.postMessage with a bot token. No threading, no retries, no batching.

Required env:
    SLACK_BOT_TOKEN  — bot token (xoxb-...)
    SLACK_CHANNEL_ID — channel to post into (Cxxxxx, not the channel name)
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

SLACK_API_URL = "https://slack.com/api/chat.postMessage"
DEFAULT_TIMEOUT_SECONDS = 15


class SlackError(RuntimeError):
    """Raised when Slack returns ok=false or the HTTP call fails."""


def post_message(
    text: str,
    image_url: Optional[str] = None,
    *,
    channel_id: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Post a message to a Slack channel.

    Args:
        text: Message body, supports Slack mrkdwn (`*bold*`, `_italic_`, `<url|label>`).
            Also used as the fallback for notifications and clients without block support.
        image_url: Optional public image URL. If given, rendered as an image block
            beneath the text. Slack's servers fetch the image, so the URL must be
            reachable from the public internet (a Canva presigned export URL is fine).
        channel_id: Slack channel ID. Defaults to SLACK_CHANNEL_ID env var.
        token: Bot token. Defaults to SLACK_BOT_TOKEN env var.
        timeout: HTTP timeout in seconds.

    Returns:
        The decoded JSON response from Slack.

    Raises:
        SlackError: On missing config, HTTP failure, or `ok: false` response.
    """
    bot_token = token or os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise SlackError("SLACK_BOT_TOKEN is not set in the environment")

    channel = channel_id or os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        raise SlackError("SLACK_CHANNEL_ID is not set in the environment")

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        }
    ]
    if image_url:
        blocks.append(
            {
                "type": "image",
                "image_url": image_url,
                "alt_text": "Just Pulled — daily Drip post",
            }
        )

    payload = {
        "channel": channel,
        "text": text,  # fallback for notifications and old clients
        "blocks": blocks,
    }

    try:
        response = requests.post(
            SLACK_API_URL,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise SlackError(f"Slack HTTP failure: {e}") from e

    if response.status_code >= 400:
        raise SlackError(
            f"Slack HTTP {response.status_code}: {response.text[:500]}"
        )

    data = response.json()
    if not data.get("ok"):
        # Slack-specific errors land here: invalid_auth, channel_not_found,
        # not_in_channel, missing_scope, etc.
        raise SlackError(f"Slack API error: {data.get('error')!r} (full={data})")
    return data
