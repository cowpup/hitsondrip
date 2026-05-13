"""Daily Drip Shop Live "Just Pulled" Instagram post pipeline.

Cron-fired entrypoint (12pm PT via .github/workflows/daily.yml):

  1. Query DripShopLive Postgres MCP for the biggest Drip-fulfilled
     instant-pack hit in the last 24 hours.
  2. Apply string transforms (pack name, card name, prices).
  3. Render an Instagram-square PNG locally with Pillow.
  4. Upload the PNG to the orphan branch `daily-output` on GitHub.
  5. Schedule the post on Metricool for 6pm PT same day.
  6. Call Metricool normalize so the image is snapshot to their CDN
     (insulates against the GitHub URL being overwritten tomorrow).
  7. Notify Slack with the image preview + the Metricool post URL.

On any failure (no hit found is NOT a failure — see below), the script
posts the error text to Slack so it surfaces in the channel within
seconds of the cron firing, then exits non-zero so GitHub Actions
flags the run as failed.

"No hit found in 24h" is handled as a non-error outcome — the script
posts a short "no qualifying hit today" note to Slack and exits 0.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import anthropic
from dotenv import load_dotenv

from src import metricool, schedule_time, slack, string_transforms
from src.image_host import publish_to_github, ImageHostError
from src.metricool import MetricoolError
from src.renderer import RenderError, render_just_pulled
from src.slack import SlackError

# --------------------------------------------------------------------------- #
# Config + logging
# --------------------------------------------------------------------------- #

load_dotenv()

# Windows / non-UTF-8 console fix — character data from DripShopLive can
# include non-cp1252 glyphs and we'd rather log them than crash on print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hitsondrip")

PROMPT_PATH = Path("prompt.md")
SQL_PATH = Path("queries") / "biggest_hit_24h.sql"

ANTHROPIC_MODEL = "claude-opus-4-7"
MCP_BETA = "mcp-client-2025-11-20"
DRIPSHOPLIVE_MCP_URL = "https://db-mcp-production.up.railway.app/sse"
DRIPSHOPLIVE_MCP_NAME = "dripshoplive"

FALLBACK_PACK_IMAGE_URL = (
    "https://cdn.dripshop.live/product/_tpbM51S6K806mAwwCV5E.png"
)
FALLBACK_PACK_NAME = "INSTANT PACK"


# --------------------------------------------------------------------------- #
# Step 1 — Query DripShopLive via Claude + MCP
# --------------------------------------------------------------------------- #


def _build_prompt() -> str:
    """Concatenate prompt.md + the SQL file into the single prompt we send."""
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"Missing {PROMPT_PATH}")
    if not SQL_PATH.exists():
        raise FileNotFoundError(f"Missing {SQL_PATH}")
    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    sql_text = SQL_PATH.read_text(encoding="utf-8")
    return f"{prompt_text}\n\n```sql\n{sql_text}\n```\n"


def fetch_biggest_hit() -> Optional[dict[str, Any]]:
    """Ask Claude to run the SQL via DripShopLive MCP and parse the JSON row.

    Returns the row dict, or None if there are no qualifying hits in 24h.
    Raises on any other failure (auth, MCP transport, JSON parse).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing from environment")

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt()
    log.info("Calling %s with DripShopLive MCP attached...", ANTHROPIC_MODEL)

    response = client.beta.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        betas=[MCP_BETA],
        mcp_servers=[{
            "type": "url",
            "url": DRIPSHOPLIVE_MCP_URL,
            "name": DRIPSHOPLIVE_MCP_NAME,
        }],
        tools=[{
            "type": "mcp_toolset",
            "mcp_server_name": DRIPSHOPLIVE_MCP_NAME,
        }],
        messages=[{"role": "user", "content": prompt}],
    )

    text_chunks: list[str] = []
    tool_calls = 0
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "mcp_tool_use":
            tool_calls += 1
            log.info("  tool call %d: %s", tool_calls, getattr(block, "name", "?"))
        elif btype == "mcp_tool_result":
            if getattr(block, "is_error", False):
                content = getattr(block, "content", "")
                log.warning("  MCP tool error: %s", str(content)[:300])
        elif btype == "text":
            text_chunks.append(getattr(block, "text", ""))

    full_text = "".join(text_chunks)
    log.info("Model returned %d chars of text after %d tool calls",
             len(full_text), tool_calls)

    return _parse_hit_json(full_text)


def _parse_hit_json(text: str) -> Optional[dict[str, Any]]:
    """Pull the JSON object (or null) out of the model's fenced response."""
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text.strip()
    raw = raw.strip()
    if raw.lower() == "null":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Model response wasn't valid JSON: {e}\nResponse text:\n{text[:1000]}"
        ) from e
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"Expected a JSON object (or null) from the model, got "
            f"{type(parsed).__name__}: {str(parsed)[:300]}"
        )
    return parsed


# --------------------------------------------------------------------------- #
# Step 2 — Render PNG to bytes
# --------------------------------------------------------------------------- #


def render_post_to_bytes(
    card_image_url: str,
    pack_image_url: str,
    pack_name: str,
    pack_price: int,
    hit_value: float,
) -> bytes:
    """Render the daily PNG and return its bytes (renderer writes to disk
    today; we read it back from a tempfile). Future refactor could
    teach renderer to write to BytesIO directly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "daily.png"
        render_just_pulled(
            card_image_url=card_image_url,
            pack_image_url=pack_image_url,
            pack_name=pack_name,
            pack_price=pack_price,
            hit_value=hit_value,
            output_path=out_path,
        )
        return out_path.read_bytes()


# --------------------------------------------------------------------------- #
# Step 3 — Compose caption + Slack messages
# --------------------------------------------------------------------------- #


def build_caption(hit: dict[str, Any]) -> str:
    """Instagram caption.

    Format: "$X hit on <clean_card_name> from <clean_pack_name> — rip yours at dripshop.live"
    """
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    clean_pack = string_transforms.pack_name_for_caption(
        hit.get("pack_name") or FALLBACK_PACK_NAME
    )
    hit_value = int(round(float(hit["hit_value"])))
    return (
        f"${hit_value} hit on {clean_card} from {clean_pack} "
        f"— rip yours at dripshop.live"
    )


def build_slack_success_text(
    hit: dict[str, Any],
    image_url: str,
    publish_at_iso: str,
    metricool_response: Optional[dict[str, Any]] = None,
) -> str:
    """Single line of mrkdwn for the Slack success notification."""
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    clean_pack = string_transforms.pack_name_for_caption(
        hit.get("pack_name") or FALLBACK_PACK_NAME
    )
    hit_value = int(round(float(hit["hit_value"])))
    post_url = ""
    if metricool_response:
        # Metricool's response shape varies; surface whatever URL/ID we
        # can find without crashing if the field is absent.
        post_url = (
            metricool_response.get("url")
            or metricool_response.get("postUrl")
            or metricool_response.get("permalink")
            or ""
        )
    return (
        f":dollar: *${hit_value} hit on {clean_card}* from _{clean_pack}_\n"
        f":calendar: Scheduled for `{publish_at_iso}` PT on Instagram "
        f"<https://www.instagram.com/dripshoplive_/|@dripshoplive_>\n"
        f":frame_with_picture: <{image_url}|raw PNG>"
        + (f"\n:link: <{post_url}|Metricool post>" if post_url else "")
    )


# --------------------------------------------------------------------------- #
# Step 4 — Main orchestration
# --------------------------------------------------------------------------- #


def run() -> int:
    """Returns 0 on success or empty-day, non-zero on any actual failure."""
    try:
        hit = fetch_biggest_hit()
    except Exception as e:  # noqa: BLE001 — surface raw exception to Slack
        _emit_failure_to_slack("DripShopLive query failed", e)
        return 1

    if hit is None:
        log.info("No Drip-fulfilled hits in the last 24 hours.")
        try:
            slack.post_message(
                ":zzz: No Drip-fulfilled hits in the last 24 hours — "
                "no Instagram post will be scheduled for today."
            )
        except SlackError as e:
            log.error("Slack notify failed on empty-day path: %s", e)
            return 1
        return 0

    log.info("Hit: $%s on %s from %s",
             hit.get("hit_value"), hit.get("card_name"), hit.get("pack_name"))

    # Render
    try:
        pack_url = hit.get("pack_image_url") or FALLBACK_PACK_IMAGE_URL
        pack_name_render = string_transforms.pack_name_for_canva(
            hit.get("pack_name") or FALLBACK_PACK_NAME
        )
        png_bytes = render_post_to_bytes(
            card_image_url=hit["card_image_url"],
            pack_image_url=pack_url,
            pack_name=pack_name_render,
            pack_price=int(round(float(hit["pack_price"]))),
            hit_value=float(hit["hit_value"]),
        )
        log.info("Rendered %d bytes", len(png_bytes))
    except (RenderError, KeyError, TypeError, ValueError) as e:
        _emit_failure_to_slack("Render failed", e)
        return 1

    # Upload
    try:
        image_url = publish_to_github(png_bytes)
        log.info("Uploaded to %s", image_url)
    except ImageHostError as e:
        _emit_failure_to_slack("Image host upload failed", e)
        return 1

    # Schedule on Metricool
    try:
        publish_iso, publish_tz = schedule_time.next_6pm_pt()
        from datetime import datetime as _dt
        publish_dt = _dt.fromisoformat(publish_iso)

        brand = metricool.find_instagram_brand()  # defaults to "drip tcg"
        caption = build_caption(hit)
        mc_response = metricool.schedule_instagram_post(
            blog_id=brand["blog_id"],
            caption=caption,
            media_url=image_url,
            publish_at=publish_dt,
            timezone=publish_tz,
        )
        log.info("Scheduled Metricool post for %s %s", publish_iso, publish_tz)
    except MetricoolError as e:
        _emit_failure_to_slack("Metricool schedule failed", e)
        return 1

    # Normalize — snapshot image to Metricool CDN so the GitHub URL can
    # be overwritten tomorrow without affecting tonight's publish.
    try:
        metricool.normalize_image_url(image_url)
        log.info("Image normalized onto Metricool CDN")
    except MetricoolError as e:
        # Not fatal — the post is already scheduled with the original
        # GitHub URL. Log and continue.
        log.warning("normalize_image_url failed (non-fatal): %s", e)

    # Notify Slack
    try:
        slack_text = build_slack_success_text(
            hit, image_url, publish_iso, mc_response
        )
        slack.post_message(slack_text, image_url=image_url)
        log.info("Slack notified")
    except SlackError as e:
        # Pipeline succeeded — log but don't fail the run.
        log.error("Slack notify failed (non-fatal): %s", e)

    return 0


def _emit_failure_to_slack(stage: str, err: Exception) -> None:
    """Best-effort failure post. Logs the underlying error first so we
    never lose context to a downstream Slack-also-broken case."""
    log.error("%s: %s", stage, err)
    text = (
        f":rotating_light: *Daily Drip post FAILED at {stage}*\n"
        f"```{type(err).__name__}: {err}```\n"
        f"_No Instagram post will be scheduled today. Check the GitHub "
        f"Actions run for the full traceback._"
    )
    try:
        slack.post_message(text)
    except SlackError as e:
        log.error("Slack failure-notify ALSO failed: %s", e)


if __name__ == "__main__":
    sys.exit(run())
