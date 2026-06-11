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

from src import image_filter, schedule_time, slack, state_branch, string_transforms
from src.image_host import publish_to_github, ImageHostError
from src.renderer import RenderError, render_just_pulled
from src.slack import SlackError
from src.state_branch import StateBranchError

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


def fetch_top_hits() -> list[dict[str, Any]]:
    """Ask Claude to run the SQL via DripShopLive MCP and parse the JSON array.

    Returns up to 5 rows ordered by value DESC. Empty list = no qualifying
    hits in the last 24h. Raises on any other failure (auth, MCP transport,
    JSON parse).
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

    return _parse_hits_json(full_text)


def _parse_hits_json(text: str) -> list[dict[str, Any]]:
    """Pull the JSON array out of the model's fenced response.

    Accepts an empty array (no hits) or up to 5 hit dicts. Defensive:
    handles a legacy single-dict response by wrapping it in a list,
    and handles a literal `null` as an empty list.
    """
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text.strip()
    raw = raw.strip()
    if raw.lower() == "null":
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Model response wasn't valid JSON: {e}\nResponse text:\n{text[:1000]}"
        ) from e
    if parsed is None:
        return []
    if isinstance(parsed, dict):
        # Tolerate the legacy single-object shape.
        return [parsed]
    if isinstance(parsed, list):
        return [h for h in parsed if isinstance(h, dict)]
    raise RuntimeError(
        f"Expected a JSON array (or empty) from the model, got "
        f"{type(parsed).__name__}: {str(parsed)[:300]}"
    )


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


def build_x_caption(hit: dict[str, Any]) -> str:
    """X (Twitter) caption — Option A: punchy, leads with dollar value.

    Hashtags include a dynamic grade tag derived from the cleaned card name
    (e.g. "#PSA10", "#CGC9"). Total length stays well under 280 chars even
    for the longest pack + card names we've seen (~150 chars at most).

    Format:
        $X HIT 🎯

        <Clean Card Name>
        from <Clean Pack Name>

        Rip yours → dripshop.live

        #PokemonTCG #PokemonHits #<Grade>
    """
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    clean_pack = string_transforms.pack_name_for_caption(
        hit.get("pack_name") or FALLBACK_PACK_NAME
    )
    hit_value = int(round(float(hit["hit_value"])))
    grade_hashtag = _grade_hashtag(clean_card)

    return (
        f"${hit_value} HIT 🎯\n"
        f"\n"
        f"{clean_card}\n"
        f"from {clean_pack}\n"
        f"\n"
        f"Rip yours → dripshop.live\n"
        f"\n"
        f"#PokemonTCG #PokemonHits {grade_hashtag}"
    )


def _grade_hashtag(clean_card_name: str) -> str:
    """Extract '#PSA10', '#CGC9', etc. from a cleaned card name.

    The cleaned name starts with "<GRADER> <GRADE> <Rest>" (per
    card_name_cleanup), so the first two whitespace-separated tokens
    give us the hashtag. Falls back to "#PokemonCards" if the name
    doesn't start with a recognizable grader.
    """
    if not clean_card_name:
        return "#PokemonCards"
    tokens = clean_card_name.split()
    if len(tokens) < 2:
        return "#PokemonCards"
    grader, grade = tokens[0], tokens[1]
    if grader.upper() in {"PSA", "CGC", "BGS", "BCCG", "TAG"}:
        # Strip any decimal point from "9.5" → "95" so the hashtag is clean.
        grade_clean = grade.replace(".", "")
        return f"#{grader.upper()}{grade_clean}"
    return "#PokemonCards"


def build_slack_success_text(
    hit: dict[str, Any],
    image_url: str,
    publish_at_iso: str,
    *,
    x_publish_at_iso: Optional[str] = None,
) -> str:
    """Single mrkdwn block for the Slack success notification.

    Shows the candidate hit, the proposed publish times, and a link to
    the raw PNG. Nothing is actually scheduled on Metricool yet — the
    Worker POSTs to Metricool only after ✅ is clicked.
    """
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    clean_pack = string_transforms.pack_name_for_caption(
        hit.get("pack_name") or FALLBACK_PACK_NAME
    )
    hit_value = int(round(float(hit["hit_value"])))

    lines = [
        f":dollar: *${hit_value} hit on {clean_card}* from _{clean_pack}_",
        f":calendar: IG would publish at `{publish_at_iso}` PT — "
        f"<https://www.instagram.com/dripshoplive_/|@dripshoplive_>",
    ]
    if x_publish_at_iso:
        lines.append(
            f":bird: X would publish at `{x_publish_at_iso}` PT — "
            f"<https://x.com/dripshop_live|@dripshop_live>"
        )
    lines.append(f":frame_with_picture: <{image_url}|raw PNG>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Step 4 — Main orchestration
# --------------------------------------------------------------------------- #


def run() -> int:
    """Returns 0 on success or empty-day, non-zero on any actual failure."""
    try:
        candidates = fetch_top_hits()
    except Exception as e:  # noqa: BLE001 — surface raw exception to Slack
        _emit_failure_to_slack("DripShopLive query failed", e)
        return 1

    if not candidates:
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

    # Iterate candidates highest-value first. Skip any whose card image
    # matches a known placeholder hash; pick the first clean one.
    blacklist = image_filter.load_blacklist()
    log.info(
        "Got %d candidate hit(s); blacklist contains %d known placeholders",
        len(candidates), len(blacklist),
    )
    hit: Optional[dict[str, Any]] = None
    skipped: list[tuple[dict[str, Any], str]] = []
    for candidate in candidates:
        card_url = candidate.get("card_image_url") or ""
        if not card_url:
            skipped.append((candidate, "no card_image_url"))
            continue
        blocked, reason = image_filter.is_placeholder(card_url, blacklist)
        if blocked:
            log.info(
                "Skipping $%s hit on %s — %s",
                candidate.get("hit_value"), candidate.get("card_name"), reason,
            )
            skipped.append((candidate, reason))
            continue
        hit = candidate
        log.info("Using hit: %s (after %d skipped)", card_url, len(skipped))
        break

    if hit is None:
        log.warning(
            "All %d candidate hits had placeholder images; no post today.",
            len(candidates),
        )
        skipped_lines = "\n".join(
            f"  • ${c.get('hit_value')} on {c.get('card_name', '?')[:60]} — {reason}"
            for c, reason in skipped
        )
        try:
            slack.post_message(
                f":no_entry_sign: *No usable hit today* — all "
                f"{len(candidates)} top hits had placeholder card images.\n"
                f"```{skipped_lines}```\n_If any of these should actually "
                f"be postable, the renderer's filter is the issue. "
                f"Otherwise, no action needed._"
            )
        except SlackError as e:
            log.error("Slack notify failed on all-placeholder path: %s", e)
            return 1
        return 0

    log.info(
        "Hit: $%s on %s from %s",
        hit.get("hit_value"), hit.get("card_name"), hit.get("pack_name"),
    )

    # --- De-dup guard ------------------------------------------------------
    # Unlike New Chase (which skips a deduped candidate and shows the NEXT
    # one), Just Pulled features the SINGLE biggest hit of the day. So we
    # select the top usable hit FIRST (loop above), then compare it to the
    # last hit we posted. If it matches, a prior run already sent an
    # approval card for this exact hit — skip the WHOLE run rather than
    # demote to a smaller hit. This stops a manual workflow_dispatch rerun
    # (or an occasional double-fired schedule) from producing a second
    # approval card → duplicate IG/X posts.
    hit_id = hit.get("hit_id")
    try:
        last_hit_id = state_branch.read_last_hit_id()
    except StateBranchError as e:
        # Fail closed: without state we can't de-dup, and posting a
        # possible duplicate is worse than missing one run (mirrors
        # new_chase.py's read-error handling).
        _emit_failure_to_slack("State branch read failed", e)
        return 1
    log.info("Last posted hit_id: %r (this hit: %r)", last_hit_id, hit_id)

    if (
        hit_id is not None
        and last_hit_id is not None
        and int(hit_id) == int(last_hit_id)
    ):
        log.info(
            "Hit id=%s already posted on a prior run; skipping to avoid a "
            "duplicate.", hit_id,
        )
        try:
            slack.post_message(
                f":repeat: *Already posted* — the biggest hit in the last 24h "
                f"(${int(round(float(hit['hit_value'])))} on "
                f"{hit.get('card_name', '?')}, id={hit_id}) already went out "
                f"for approval on an earlier run. No new post created."
            )
        except SlackError as e:
            log.error("Slack notify failed on already-posted path: %s", e)
            return 1
        return 0

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
        raw_image_url = publish_to_github(png_bytes)
        log.info("Uploaded to %s", raw_image_url)
    except ImageHostError as e:
        _emit_failure_to_slack("Image host upload failed", e)
        return 1

    # Cache-bust the image URL before handing it to Slack / Metricool.
    # raw.githubusercontent.com is fronted by Fastly; we overwrite the
    # SAME `latest.png` URL every day, so without a unique query string
    # Slack's image-preview CDN serves the previously-cached render
    # (we hit this 2026-05-14 — a fresh Beedrill render appeared in the
    # Slack preview as the cached Charizard from earlier the same day).
    # Appending a unique `?v=<run-epoch>` makes Slack/Metricool treat
    # each daily run's image as a new URL → fresh fetch. The query
    # string doesn't affect what GitHub serves (same file either way).
    import time as _time
    image_url = f"{raw_image_url}?v={int(_time.time())}"

    # Build the post payload for Slack-button-driven approval. The Slack
    # message carries everything the Cloudflare Worker needs to create
    # the Metricool posts on approve — caption, image URL, publish time
    # for both IG and X — base64-encoded into the button value.
    #
    # NOTHING goes to Metricool here. The Worker creates posts (with
    # autoPublish=true) only after a human clicks ✅ in Slack. This
    # eliminates the "duplicate posts" risk from previous architectures
    # that scheduled drafts up front, and also removes the need for a
    # fail-closed cleanup cron — no Metricool state exists to clean up.
    publish_iso, publish_tz = schedule_time.next_6pm_pt()
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    publish_dt = _dt.fromisoformat(publish_iso)
    x_publish_dt = publish_dt + _td(minutes=15)
    x_publish_iso = x_publish_dt.strftime("%Y-%m-%dT%H:%M:%S")

    ig_caption = build_caption(hit)
    x_caption = build_x_caption(hit)

    payload = {
        "image_url": image_url,
        "timezone": publish_tz,
        "ig": {"caption": ig_caption, "publish": publish_iso},
        "x":  {"caption": x_caption,  "publish": x_publish_iso},
    }

    try:
        slack_text = build_slack_success_text(
            hit, image_url, publish_iso,
            x_publish_at_iso=x_publish_iso,
        )
        slack_text += (
            "\n\n:warning: *Awaiting approval — click ✅ Approve & schedule "
            "to publish both posts at their scheduled times, or ❌ Skip to "
            "discard today's post entirely.*"
        )
        buttons = _build_approval_buttons(payload)
        slack.post_message(slack_text, image_url=image_url, actions=buttons)
        log.info(
            "Slack notified with approval buttons (IG %s, X %s)",
            publish_iso, x_publish_iso,
        )
    except SlackError as e:
        # Render succeeded, image hosted — but we couldn't notify. Fail
        # the run so we see it in GitHub Actions and don't silently miss
        # a day. State is NOT written (below), so a retry re-posts cleanly.
        _emit_failure_to_slack("Slack notify failed", e)
        return 1

    # --- Record state ------------------------------------------------------
    # Written AFTER the approval card posts (not on Worker ✅) so a same-day
    # rerun won't regenerate a card for this hit. Semantic mirrors
    # new_chase: state = "hit already presented to Noah." If Noah ❌ Skips,
    # we still won't re-show it. A failed run above returns non-zero and
    # leaves state untouched so it retries. On write failure we log +
    # Slack-warn but DON'T fail the run — the card is already out; risking
    # a duplicate beats aborting a successful post.
    if hit_id is not None:
        try:
            state_branch.write_last_hit_id(int(hit_id))
            log.info("Wrote state: last_hit_id=%s", hit_id)
        except StateBranchError as e:
            log.error("State branch write FAILED (continuing): %s", e)
            try:
                slack.post_message(
                    f":warning: Just Pulled post succeeded BUT state write "
                    f"failed: `{e}`. A rerun may re-show hit id `{hit_id}` — "
                    f"manually update `state/last_hit_id.txt` if needed."
                )
            except SlackError:
                pass

    return 0


def _build_approval_buttons(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Slack button elements for ✅ Approve / ❌ Skip.

    Approve carries the FULL post payload (caption, image URL, publish
    times for both IG + X) base64-encoded so the Worker can create both
    Metricool posts on click without needing any other state. The Worker
    POSTs both with autoPublish=true so they publish at their original
    scheduled times — no draft, no later update, no duplication risk.

    Skip carries no data — there's nothing to delete since main.py never
    created any Metricool state.

    Slack's button `value` field is capped at 2000 chars; a typical
    payload base64-encodes to ~1000 chars (caption ~280 + URL ~80 +
    timestamps ~50 + JSON syntax ~50 + room for hashtags). Safe margin.
    """
    import base64
    import json as _json

    payload_json = _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii")
    approve_value = f"approve:{encoded}"
    if len(approve_value) > 1900:
        # Defensive: Slack rejects button values > 2000 chars. If we're
        # ever close, the caption is too long and we should fail loudly
        # so we notice before posting a broken button.
        raise ValueError(
            f"Slack button value too long ({len(approve_value)} chars). "
            f"Trim the caption or image URL."
        )

    return [
        {
            "type": "button",
            "action_id": "approve",
            "style": "primary",
            "text": {"type": "plain_text", "text": "✅ Approve & schedule"},
            "value": approve_value,
        },
        {
            "type": "button",
            "action_id": "reject",
            "style": "danger",
            "text": {"type": "plain_text", "text": "❌ Skip today"},
            "value": "reject",
        },
    ]


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
