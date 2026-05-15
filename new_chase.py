"""Daily Drip Shop Live "New Chase" Instagram + X post pipeline.

Cron-fired entrypoint (10am PT via .github/workflows/new_chase.yml):

  1. Load config/featured_pack.json (pack_name, pack_price, pack_image_url).
  2. Freshness check: query DripShopLive for MAX(created_at) on user
     65643 chase listings. If latest batch is older than
     MAX_BATCH_AGE_HOURS (default 36), skip cleanly — log to Slack
     and exit 0. Prevents Monday cron reposting Friday's chase.
  3. Main query: top chase card in the latest batch with
     card.price >= 10 * pack.price. Threshold substituted into
     queries/new_chase.sql at the :threshold placeholder.
  4. If main query returns zero, run the near-miss query and
     emit a "top card was $X at Y×" skip message so Noah can tune
     pack_price downward if desired.
  5. De-dup: read state branch's last_chase_card_id.txt. If the
     candidate matches, skip — we've already shown Noah this card.
     State is updated when the Slack approval message goes out
     (NOT on Worker ✅), so a ❌ Skip still consumes the card.
  6. Render the New Chase PNG (src/new_chase_renderer.render_new_chase).
  7. Upload to GitHub `daily-output` branch as latest_chase.png
     (sibling of latest.png).
  8. Post Slack approval message with ✅/❌ buttons; the Worker
     handles Metricool publishing on approve, same as Just Pulled.
  9. Write candidate's card_product_id to state/last_chase_card_id.txt.

Three skip paths (all exit 0):
  - "Stale batch"            → freshness > MAX_BATCH_AGE_HOURS
  - "No qualifying chase"    → main query empty + near-miss reported
  - "Already shown"          → state file matches today's candidate

Failures (renderer / upload / Slack) post error text to Slack and exit 1.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import anthropic
from dotenv import load_dotenv

from src import schedule_time, slack, state_branch, string_transforms
from src.image_host import publish_to_github, ImageHostError
from src.new_chase_renderer import RenderError, render_new_chase
from src.slack import SlackError
from src.state_branch import StateBranchError

# --------------------------------------------------------------------------- #
# Config + logging
# --------------------------------------------------------------------------- #

load_dotenv()

# Windows / non-UTF-8 console fix — same as main.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("new_chase")

PROMPT_PATH = Path("prompt_chase.md")
SQL_FRESHNESS_PATH = Path("queries") / "new_chase_freshness.sql"
SQL_MAIN_PATH = Path("queries") / "new_chase.sql"
SQL_NEAR_MISS_PATH = Path("queries") / "new_chase_near_miss.sql"
SQL_PACK_LOOKUP_BY_CARD_PATH = Path("queries") / "pack_lookup_by_card.sql"
CONFIG_PATH = Path("config") / "featured_pack.json"

ANTHROPIC_MODEL = "claude-opus-4-7"
MCP_BETA = "mcp-client-2025-11-20"
DRIPSHOPLIVE_MCP_URL = "https://db-mcp-production.up.railway.app/sse"
DRIPSHOPLIVE_MCP_NAME = "dripshoplive"

# Image hosted on the daily-output orphan branch. Filename includes the
# run epoch so each upload is a NEW path — defeats Slack's image-proxy
# cache, which caches by path and ignores the `?v=<timestamp>` query
# string we used to use with the static `latest_chase.png` URL. Without
# this, Slack shows a previous run's render in the embed even when the
# message text is correctly chain-resolved (observed 2026-05-14:
# message body said "High Roller Pack" but image preview was the prior
# test's Collector's Jam).
def _chase_image_filename() -> str:
    return f"chase_{int(time.time())}.png"

# Freshness guardrail. Default 36 hours; env override for ad-hoc tuning
# during quiet stretches. Just-Pulled doesn't need this because its
# cron consumes the last-24h window directly; New Chase anchors to
# MAX(created_at) and would happily repost an old batch otherwise.
DEFAULT_MAX_BATCH_AGE_HOURS = 36

# Chase threshold multiplier — DEFAULT only. The real value comes from
# config/featured_pack.json's `chase_threshold_multiplier` key (added
# 2026-05-14 after the first GH Actions run revealed the 10× rule was
# too strict for current inventory shape — top of latest batch was a
# $474 chase at only 4.7× the $100 pack price). Tuning via config means
# Noah can drop to 5× or 3× without touching code.
DEFAULT_CHASE_THRESHOLD_MULTIPLIER = 10


# --------------------------------------------------------------------------- #
# Step 0 — Config loading
# --------------------------------------------------------------------------- #


def load_featured_pack() -> dict[str, Any]:
    """Load and validate config/featured_pack.json.

    Required: pack_price (positive number).
    Optional: chase_threshold_multiplier (number; default 10).

    Pack metadata (name + image) is no longer in config — it's
    auto-resolved per-candidate via the collection→box_break chain
    (resolve_pack_for_card). pack_price is now a GLOBAL threshold
    reference, not the actual rendered pack's price.

    Raises RuntimeError on any validation failure.
    """
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Missing {CONFIG_PATH}. See _comment for the schema."
        )
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    if "pack_price" not in raw:
        raise RuntimeError(f"{CONFIG_PATH} missing key 'pack_price'")
    if not isinstance(raw["pack_price"], (int, float)) or raw["pack_price"] <= 0:
        raise RuntimeError(f"{CONFIG_PATH} pack_price must be a positive number")

    # Optional multiplier — default to 10 for backward compatibility.
    mult = raw.get("chase_threshold_multiplier", DEFAULT_CHASE_THRESHOLD_MULTIPLIER)
    if not isinstance(mult, (int, float)) or mult <= 0:
        raise RuntimeError(
            f"{CONFIG_PATH} chase_threshold_multiplier must be a positive "
            f"number, got {mult!r}"
        )
    raw["chase_threshold_multiplier"] = mult
    return raw


def resolve_pack_for_card(card_product_id: int) -> Optional[dict[str, Any]]:
    """Auto-resolve the pack for a chase card via the collection chain:

        products.id
          → user_product_collection_product_mappings.product_id
            → user_product_collections.id
              → box_break_spot_mappings.collection_id
                → box_breaks.id

    Tiebreaker (a collection feeds many packs over time): the most-
    recent box_break with packImage in its JSONB.

    Returns a dict with keys {box_break_id, pack_title, pack_image_url,
    collection_id, collection_name} on success, or None when the card
    has no collection mapping (missing tags / no matching dynamic
    conditions) or none of its collections feed any box_break with a
    packImage. Caller should try the next candidate.

    Never raises for "no mapping" — that's the expected miss case.
    Raises only on infrastructure failures.
    """
    sql_template = SQL_PACK_LOOKUP_BY_CARD_PATH.read_text(encoding="utf-8")
    sql_text = sql_template.replace(":card_product_id", str(int(card_product_id)))
    if ":card_product_id" in sql_text:
        raise RuntimeError(
            "Failed to substitute :card_product_id in pack_lookup_by_card.sql"
        )

    rows = _call_mcp(_build_prompt(sql_text), label=f"pack-resolve[{card_product_id}]")
    if not rows:
        return None
    row = rows[0]
    if not row.get("pack_image_url"):
        # Defensive: SQL has `bb.reveal_animation_data ? 'packImage'`
        # so we shouldn't see a null URL, but a JSONB key with null
        # value would slip past that filter.
        return None
    return row


# --------------------------------------------------------------------------- #
# Step 1 — DripShopLive query helpers
# --------------------------------------------------------------------------- #


def _build_prompt(sql_text: str) -> str:
    """Concatenate prompt_chase.md + the given SQL into one prompt."""
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"Missing {PROMPT_PATH}")
    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    return f"{prompt_text}\n\n```sql\n{sql_text}\n```\n"


def _call_mcp(prompt: str, *, label: str) -> list[dict[str, Any]]:
    """Send one prompt to Claude with DripShopLive MCP attached.

    Parses the model's fenced JSON-array response. Empty array is a
    legitimate "no rows" result — the caller decides what that means.

    `label` is used only for logging.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing from environment")

    client = anthropic.Anthropic(api_key=api_key)
    log.info("[%s] Calling %s with DripShopLive MCP attached...", label, ANTHROPIC_MODEL)
    started = time.time()

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
        elif btype == "mcp_tool_result":
            if getattr(block, "is_error", False):
                content = getattr(block, "content", "")
                log.warning("[%s]   MCP tool error: %s", label, str(content)[:300])
        elif btype == "text":
            text_chunks.append(getattr(block, "text", ""))

    full_text = "".join(text_chunks)
    elapsed = time.time() - started
    log.info(
        "[%s] %d chars returned after %d tool calls (%.1fs)",
        label, len(full_text), tool_calls, elapsed,
    )
    return _parse_rows_json(full_text)


def _parse_rows_json(text: str) -> list[dict[str, Any]]:
    """Pull the JSON array out of the model's fenced response.

    Same defensive logic as main.py: accepts empty array, a literal
    `null`, or a single object (legacy). Returns list[dict].
    """
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    raw = (fenced.group(1) if fenced else text).strip()
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
        return [parsed]
    if isinstance(parsed, list):
        return [r for r in parsed if isinstance(r, dict)]
    raise RuntimeError(
        f"Expected a JSON array (or empty) from the model, got "
        f"{type(parsed).__name__}: {str(parsed)[:300]}"
    )


def fetch_freshness() -> Optional[dict[str, Any]]:
    """Run queries/new_chase_freshness.sql, return the single row or None.

    The row has {latest_batch_ts, hours_since_batch}. None means the
    query returned zero rows, i.e. user 65643 has never listed a
    qualifying card — treat as "no data" / stale.
    """
    sql_text = SQL_FRESHNESS_PATH.read_text(encoding="utf-8")
    rows = _call_mcp(_build_prompt(sql_text), label="freshness")
    if not rows:
        return None
    return rows[0]


def fetch_chase_candidate(threshold: float) -> list[dict[str, Any]]:
    """Run queries/new_chase.sql with :threshold substituted.

    Substitution is plain string-replace (Postgres doesn't natively
    support `:name` placeholders without going through a driver, but
    the model+MCP path uses raw SQL). `threshold` comes from
    `featured_pack.chase_threshold_multiplier * featured_pack.pack_price` where
    pack_price is a numeric value from a config file Noah controls,
    so injection risk is nil.

    Returns 0 or 1 rows.
    """
    sql_template = SQL_MAIN_PATH.read_text(encoding="utf-8")
    sql_text = sql_template.replace(":threshold", str(threshold))
    if ":threshold" in sql_text:
        # Belt-and-suspenders — flag if the placeholder wasn't substituted.
        raise RuntimeError("Failed to substitute :threshold in new_chase.sql")
    return _call_mcp(_build_prompt(sql_text), label="chase")


def fetch_near_miss() -> Optional[dict[str, Any]]:
    """Run queries/new_chase_near_miss.sql.

    Returns the top card in the latest batch IGNORING the threshold,
    powering the "top was $X at Y×" Slack tuning hint. None if the
    near-miss query also has nothing (shouldn't happen if freshness
    just passed, but defensive).
    """
    sql_text = SQL_NEAR_MISS_PATH.read_text(encoding="utf-8")
    rows = _call_mcp(_build_prompt(sql_text), label="near-miss")
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# Step 2 — Render PNG to bytes
# --------------------------------------------------------------------------- #


def render_post_to_bytes(
    card_image_url: str,
    pack_image_url: str,
    pack_name: str,
    hit_value: float,
) -> bytes:
    """Render the New Chase PNG and return its bytes.

    Cast hit_value to int at this boundary — the renderer's signature
    declares int and its caption is `f"${int(hit_value):,}"`. Passing
    a float would format the same way but lies about the type.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "chase.png"
        render_new_chase(
            card_image_url=card_image_url,
            pack_image_url=pack_image_url,
            pack_name=pack_name,
            hit_value=int(round(hit_value)),
            output_path=out_path,
        )
        return out_path.read_bytes()


# --------------------------------------------------------------------------- #
# Step 3 — Compose captions + Slack messages
# --------------------------------------------------------------------------- #


def build_ig_caption(hit: dict[str, Any], pack: dict[str, Any]) -> str:
    """Instagram caption for New Chase.

    Format: "$X chase just dropped — <Card Name> now available in
    <Pack Name>. Rip yours at dripshop.live"

    `pack` is the chain-resolved pack dict (keys include `pack_title`
    from box_breaks). Falls back to config-style `pack_name` for
    backward compat with tests.
    """
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    pack_title = pack.get("pack_title") or pack.get("pack_name") or ""
    clean_pack = string_transforms.pack_name_for_caption(pack_title)
    hit_value = int(round(float(hit["hit_value"])))
    return (
        f"${hit_value} chase just dropped — {clean_card} now available "
        f"in {clean_pack}. Rip yours at dripshop.live"
    )


def build_x_caption(hit: dict[str, Any], pack: dict[str, Any]) -> str:
    """X (Twitter) caption — Option A style, mirrors main.py.

    Format:
        $X CHASE 🎯

        <Clean Card Name>
        Available in <Clean Pack Name>

        Rip yours → dripshop.live

        #PokemonTCG #ChaseCard #<Grade>
    """
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    pack_title = pack.get("pack_title") or pack.get("pack_name") or ""
    clean_pack = string_transforms.pack_name_for_caption(pack_title)
    hit_value = int(round(float(hit["hit_value"])))
    grade_hashtag = _grade_hashtag(clean_card)

    return (
        f"${hit_value} CHASE 🎯\n"
        f"\n"
        f"{clean_card}\n"
        f"Available in {clean_pack}\n"
        f"\n"
        f"Rip yours → dripshop.live\n"
        f"\n"
        f"#PokemonTCG #ChaseCard {grade_hashtag}"
    )


def _grade_hashtag(clean_card_name: str) -> str:
    """Extract '#PSA10' etc. from a cleaned card name. Same logic as main.py."""
    if not clean_card_name:
        return "#PokemonCards"
    tokens = clean_card_name.split()
    if len(tokens) < 2:
        return "#PokemonCards"
    grader, grade = tokens[0], tokens[1]
    if grader.upper() in {"PSA", "CGC", "BGS", "BCCG", "TAG"}:
        grade_clean = grade.replace(".", "")
        return f"#{grader.upper()}{grade_clean}"
    return "#PokemonCards"


def build_slack_success_text(
    hit: dict[str, Any],
    pack: dict[str, Any],
    image_url: str,
    publish_at_iso: str,
    x_publish_at_iso: str,
) -> str:
    """Single mrkdwn block for the Slack approval notification."""
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    pack_title = pack.get("pack_title") or pack.get("pack_name") or ""
    clean_pack = string_transforms.pack_name_for_caption(pack_title)
    hit_value = int(round(float(hit["hit_value"])))
    ratio = float(hit["hit_value"]) / float(pack["pack_price"])

    return "\n".join([
        f":dart: *${hit_value} chase on {clean_card}* "
        f"(_{ratio:.1f}×_ the ${pack['pack_price']} reference pack price)",
        f":pick: Available in _{clean_pack}_",
        f":calendar: IG would publish at `{publish_at_iso}` PT — "
        f"<https://www.instagram.com/dripshoplive_/|@dripshoplive_>",
        f":bird: X would publish at `{x_publish_at_iso}` PT — "
        f"<https://x.com/dripshop_live|@dripshop_live>",
        f":frame_with_picture: <{image_url}|raw PNG>",
    ])


def _build_approval_buttons(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Slack ✅/❌ button elements. Payload format identical to main.py
    so the Cloudflare Worker handles New Chase posts unchanged."""
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii")
    approve_value = f"approve:{encoded}"
    if len(approve_value) > 1900:
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


# --------------------------------------------------------------------------- #
# Skip-path Slack messages
# --------------------------------------------------------------------------- #


def _slack_stale_batch(hours_since: float, max_hours: int) -> None:
    """Skip message: latest batch is stale, no new chases recently."""
    slack.post_message(
        f":hourglass_flowing_sand: *Skipping New Chase post — stale batch.*\n"
        f"Latest qualifying chase listing is *{hours_since:.1f}h old* "
        f"(threshold: {max_hours}h).\n"
        f"_Will retry tomorrow. If batches are routinely > {max_hours}h apart, "
        f"raise MAX_BATCH_AGE_HOURS in the workflow env._"
    )


def _slack_no_qualifying(
    threshold: float,
    pack: dict[str, Any],
    near_miss: Optional[dict[str, Any]],
) -> None:
    """Skip message: batch is fresh but nothing cleared the threshold.
    Includes near-miss info so Noah can tune pack_price or
    chase_threshold_multiplier downward."""
    base = (
        f":no_entry_sign: *Skipping New Chase post — no qualifying chase.*\n"
        f"Threshold: card.price ≥ *${int(threshold)}* "
        f"({pack['chase_threshold_multiplier']}× the ${pack['pack_price']} "
        f"reference pack price)."
    )
    if near_miss is None:
        slack.post_message(base + "\n_Latest batch was empty._")
        return
    top_value = float(near_miss["hit_value"])
    top_ratio = top_value / float(pack["pack_price"])
    top_card = string_transforms.card_name_cleanup(near_miss.get("card_name") or "")
    slack.post_message(
        base
        + f"\nTop card in latest batch was *${int(top_value)}* on "
        + f"_{top_card}_ ({top_ratio:.1f}× the reference pack price).\n"
        + "_If this looks postable, lower `pack_price` in "
        + "`config/featured_pack.json` so the threshold drops._"
    )


def _slack_no_usable_candidate(
    candidates: list[dict[str, Any]],
    skip_reasons: list[tuple[dict[str, Any], str]],
) -> None:
    """Skip message: every qualifying candidate either lacked a pack
    mapping (no collection match / no matching dynamic conditions) OR
    matched the previously-shown card. Surfaces enough info for Noah
    to diagnose (missing tags, broken collection, etc.)."""
    lines = ["    " + f"${c.get('hit_value')} on {(c.get('card_name') or '?')[:60]} — {r}"
             for c, r in skip_reasons]
    detail = "\n".join(lines) if lines else "    (no detail)"
    slack.post_message(
        f":mag: *Skipping New Chase post — no usable candidate.*\n"
        f"All {len(candidates)} top candidates failed pack resolution "
        f"or de-dup:\n```{detail}```\n"
        f"_Common cause: candidate cards are missing the tags that "
        f"feed `user_product_collections.dynamic_conditions`. Add the "
        f"right tags via admin panel and the next run will pick them up._"
    )


# --------------------------------------------------------------------------- #
# Step 4 — Main orchestration
# --------------------------------------------------------------------------- #


def run() -> int:
    """Returns 0 on success or any clean skip, non-zero on real failure."""
    try:
        pack = load_featured_pack()
    except Exception as e:  # noqa: BLE001
        _emit_failure_to_slack("Load featured-pack config failed", e)
        return 1
    log.info(
        "Reference pack price: $%s, multiplier=%s× → threshold $%s",
        pack["pack_price"], pack["chase_threshold_multiplier"],
        float(pack["chase_threshold_multiplier"]) * float(pack["pack_price"]),
    )

    threshold = float(pack["chase_threshold_multiplier"]) * float(pack["pack_price"])
    max_batch_age_hours = int(
        os.environ.get("MAX_BATCH_AGE_HOURS", DEFAULT_MAX_BATCH_AGE_HOURS)
    )

    # --- Freshness gate -----------------------------------------------------
    try:
        freshness = fetch_freshness()
    except Exception as e:  # noqa: BLE001
        _emit_failure_to_slack("Freshness query failed", e)
        return 1
    if freshness is None:
        try:
            _slack_stale_batch(float("inf"), max_batch_age_hours)
        except SlackError as e:
            log.error("Slack notify failed on no-data path: %s", e)
            return 1
        return 0
    hours_since = float(freshness.get("hours_since_batch") or 0)
    if hours_since > max_batch_age_hours:
        log.info("Batch is %.1fh old (> %dh) — skipping.",
                 hours_since, max_batch_age_hours)
        try:
            _slack_stale_batch(hours_since, max_batch_age_hours)
        except SlackError as e:
            log.error("Slack notify failed on stale path: %s", e)
            return 1
        return 0

    # --- Main chase query ---------------------------------------------------
    try:
        candidates = fetch_chase_candidate(threshold)
    except Exception as e:  # noqa: BLE001
        _emit_failure_to_slack("Chase query failed", e)
        return 1

    if not candidates:
        log.info(
            "No card cleared $%.0f threshold (%sx the $%s pack price); "
            "fetching near-miss for the skip message.",
            threshold, pack["chase_threshold_multiplier"], pack["pack_price"],
        )
        try:
            near_miss = fetch_near_miss()
        except Exception as e:  # noqa: BLE001
            # Near-miss is best-effort tuning info; if it fails, fall
            # back to a less-detailed skip message rather than failing
            # the whole run.
            log.warning("Near-miss query failed: %s", e)
            near_miss = None
        try:
            _slack_no_qualifying(threshold, pack, near_miss)
        except SlackError as e:
            log.error("Slack notify failed on no-qualifying path: %s", e)
            return 1
        return 0

    # --- Read state (used by the candidate dedup check below) --------------
    try:
        last_card_id = state_branch.read_last_card_id()
    except StateBranchError as e:
        _emit_failure_to_slack("State branch read failed", e)
        return 1
    log.info("Last posted card_id: %r", last_card_id)

    # --- Candidate selection loop ------------------------------------------
    # Iterate top-N candidates in price-DESC order. For each:
    #   1. Skip if dedup matches state (same card we showed last run)
    #   2. Resolve pack via collection chain. If no mapping, skip.
    #   3. First candidate that clears both checks wins.
    # If ALL candidates fail, Slack-skip with the per-candidate reasons.
    hit: Optional[dict[str, Any]] = None
    resolved_pack: Optional[dict[str, Any]] = None
    skip_reasons: list[tuple[dict[str, Any], str]] = []

    for candidate in candidates:
        cand_id = int(candidate["card_product_id"])
        log.info(
            "Trying candidate: $%s on %s (id=%s)",
            candidate.get("hit_value"), candidate.get("card_name"), cand_id,
        )

        if last_card_id is not None and last_card_id == cand_id:
            log.info("  → matches state's last_card_id, skipping")
            skip_reasons.append((candidate, "already shown last run"))
            continue

        try:
            pack_info = resolve_pack_for_card(cand_id)
        except Exception as e:  # noqa: BLE001
            # Infrastructure failure (MCP unreachable etc.) — log but
            # don't abort; try the next candidate. If ALL fail with
            # infrastructure errors we'll fall out of the loop and
            # surface the issue via the skip message.
            log.warning("  → pack resolution raised: %s", e)
            skip_reasons.append((candidate, f"resolve failed: {e}"))
            continue

        if pack_info is None:
            log.info("  → no pack mapping (missing tags / no matching collection)")
            skip_reasons.append((candidate, "no pack mapping"))
            continue

        log.info(
            "  → resolved pack: %r (box_break_id=%s, image=%s)",
            pack_info.get("pack_title"), pack_info.get("box_break_id"),
            pack_info.get("pack_image_url"),
        )
        hit = candidate
        resolved_pack = pack_info
        break

    if hit is None or resolved_pack is None:
        log.warning(
            "All %d candidates failed pack resolution or dedup.", len(candidates),
        )
        try:
            _slack_no_usable_candidate(candidates, skip_reasons)
        except SlackError as e:
            log.error("Slack notify failed on no-usable path: %s", e)
            return 1
        return 0

    card_product_id = int(hit["card_product_id"])

    # Build the merged pack dict that captions + render consume:
    #   pack_title + pack_image_url come from the chain (per-card)
    #   pack_price + chase_threshold_multiplier from config (global)
    pack_for_render = {
        "pack_title":               resolved_pack["pack_title"],
        "pack_image_url":           resolved_pack["pack_image_url"],
        "box_break_id":             resolved_pack["box_break_id"],
        "pack_price":               pack["pack_price"],
        "chase_threshold_multiplier": pack["chase_threshold_multiplier"],
    }

    # --- Render -------------------------------------------------------------
    try:
        pack_name_render = string_transforms.pack_name_for_canva(
            pack_for_render["pack_title"]
        )
        png_bytes = render_post_to_bytes(
            card_image_url=hit["card_image_url"],
            pack_image_url=pack_for_render["pack_image_url"],
            pack_name=pack_name_render,
            hit_value=float(hit["hit_value"]),
        )
        log.info("Rendered %d bytes", len(png_bytes))
    except (RenderError, KeyError, TypeError, ValueError) as e:
        _emit_failure_to_slack("Render failed", e)
        return 1

    # --- Upload -------------------------------------------------------------
    # Unique filename per run (see _chase_image_filename). No cache-bust
    # query string needed because the path itself is already unique.
    try:
        chase_filename = _chase_image_filename()
        raw_image_url = publish_to_github(png_bytes, filename=chase_filename)
        log.info("Uploaded to %s", raw_image_url)
    except ImageHostError as e:
        _emit_failure_to_slack("Image host upload failed", e)
        return 1

    image_url = raw_image_url

    # --- Compose payload + post Slack ---------------------------------------
    # New Chase publishes at 5pm PT (IG) + 5:15pm PT (X) — gives a
    # 1-hour gap vs Just Pulled's 6pm/6:15pm on dual-qualifying days.
    publish_iso, publish_tz = schedule_time.next_5pm_pt()
    publish_dt = datetime.fromisoformat(publish_iso)
    x_publish_dt = publish_dt + timedelta(minutes=15)
    x_publish_iso = x_publish_dt.strftime("%Y-%m-%dT%H:%M:%S")

    ig_caption = build_ig_caption(hit, pack_for_render)
    x_caption = build_x_caption(hit, pack_for_render)

    payload = {
        "image_url": image_url,
        "timezone": publish_tz,
        "ig": {"caption": ig_caption, "publish": publish_iso},
        "x":  {"caption": x_caption,  "publish": x_publish_iso},
    }

    try:
        slack_text = build_slack_success_text(
            hit, pack_for_render, image_url, publish_iso, x_publish_iso,
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
        # Render + upload succeeded but Slack failed. Fail the run so
        # GitHub Actions surfaces the issue, AND don't write state —
        # next run should retry this candidate.
        _emit_failure_to_slack("Slack notify failed", e)
        return 1

    # --- Update state branch ------------------------------------------------
    # We write state AFTER the Slack post lands (not on Worker ✅).
    # Semantic: state = "candidate already presented to Noah." If Noah
    # ❌ Skips, we still won't re-show it tomorrow — he had his chance.
    # If state-write fails, we log loudly but DON'T fail the run; the
    # Slack approval message is already out and we'd rather risk a
    # duplicate-presentation tomorrow than abort a successful post.
    try:
        state_branch.write_last_card_id(card_product_id)
        log.info("Wrote state: last_chase_card_id=%s", card_product_id)
    except StateBranchError as e:
        log.error("State branch write FAILED (continuing): %s", e)
        try:
            slack.post_message(
                f":warning: New Chase post succeeded BUT state write "
                f"failed: `{e}`. Next run may re-show card id "
                f"`{card_product_id}` — manually update "
                f"`state/last_chase_card_id.txt` if needed."
            )
        except SlackError:
            pass  # already-logged failure; don't shadow it

    return 0


def _emit_failure_to_slack(stage: str, err: Exception) -> None:
    """Best-effort failure post. Same pattern as main.py."""
    log.error("%s: %s", stage, err)
    text = (
        f":rotating_light: *New Chase post FAILED at {stage}*\n"
        f"```{type(err).__name__}: {err}```\n"
        f"_No post will be scheduled today. Check the GitHub Actions "
        f"run for the full traceback._"
    )
    try:
        slack.post_message(text)
    except SlackError as e:
        log.error("Slack failure-notify ALSO failed: %s", e)


if __name__ == "__main__":
    sys.exit(run())
