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
SQL_PACK_IMAGE_PATH = Path("queries") / "pack_image_lookup.sql"
CONFIG_PATH = Path("config") / "featured_pack.json"

ANTHROPIC_MODEL = "claude-opus-4-7"
MCP_BETA = "mcp-client-2025-11-20"
DRIPSHOPLIVE_MCP_URL = "https://db-mcp-production.up.railway.app/sse"
DRIPSHOPLIVE_MCP_NAME = "dripshoplive"

# Image hosted as a sibling of latest.png on the daily-output orphan branch.
CHASE_IMAGE_FILENAME = "latest_chase.png"

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

    Required keys: pack_name (str), pack_price (positive number).
    Optional keys:
        pack_box_break_id (UUID string) — if set, new_chase.py queries
            box_breaks.reveal_animation_data->>'packImage' at runtime
            to get the canonical "custom pack image" set in Drip's
            admin panel. Validated as a UUID at load time to make any
            mistake fail fast (and to make later string substitution
            into SQL injection-safe).
        pack_image_url (str) — explicit fallback URL. Used when
            pack_box_break_id is absent OR the DB lookup returns null
            (legacy animation_type=NULL rows, broken box_break, etc.).
            At least one of pack_box_break_id / pack_image_url MUST be
            set, otherwise we have nothing to render.
        chase_threshold_multiplier (number) — defaults to 10.

    Raises RuntimeError on any validation failure.
    """
    import uuid as _uuid

    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Missing {CONFIG_PATH}. Update with current featured pack info; "
            f"see config/featured_pack.json's _comment for the schema."
        )
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    # Strict required fields.
    required = {"pack_name": str, "pack_price": (int, float)}
    for key, expected_type in required.items():
        if key not in raw:
            raise RuntimeError(f"{CONFIG_PATH} missing key {key!r}")
        if not isinstance(raw[key], expected_type):
            raise RuntimeError(
                f"{CONFIG_PATH} key {key!r} has wrong type: "
                f"expected {expected_type}, got {type(raw[key]).__name__}"
            )
    if raw["pack_price"] <= 0:
        raise RuntimeError(f"{CONFIG_PATH} pack_price must be > 0")

    # Pack image: at least one of pack_box_break_id / pack_image_url
    # must be set. Validate UUID format eagerly so we never substitute
    # an unsafe string into SQL at lookup time.
    box_break_id = raw.get("pack_box_break_id")
    image_url = raw.get("pack_image_url")
    if not box_break_id and not image_url:
        raise RuntimeError(
            f"{CONFIG_PATH} must set at least one of pack_box_break_id "
            f"or pack_image_url"
        )
    if box_break_id is not None:
        if not isinstance(box_break_id, str):
            raise RuntimeError(
                f"{CONFIG_PATH} pack_box_break_id must be a UUID string, "
                f"got {type(box_break_id).__name__}"
            )
        try:
            _uuid.UUID(box_break_id)
        except (ValueError, AttributeError) as e:
            raise RuntimeError(
                f"{CONFIG_PATH} pack_box_break_id is not a valid UUID: "
                f"{box_break_id!r}"
            ) from e
    if image_url is not None and not isinstance(image_url, str):
        raise RuntimeError(
            f"{CONFIG_PATH} pack_image_url must be a string when set, "
            f"got {type(image_url).__name__}"
        )

    # Optional multiplier — default to 10 for backward compatibility.
    mult = raw.get("chase_threshold_multiplier", DEFAULT_CHASE_THRESHOLD_MULTIPLIER)
    if not isinstance(mult, (int, float)) or mult <= 0:
        raise RuntimeError(
            f"{CONFIG_PATH} chase_threshold_multiplier must be a positive "
            f"number, got {mult!r}"
        )
    raw["chase_threshold_multiplier"] = mult
    return raw


def resolve_pack_image(pack: dict[str, Any]) -> str:
    """Resolve the pack image URL to use for rendering.

    If `pack_box_break_id` is set in config, query box_breaks for that
    row's `reveal_animation_data->>'packImage'` — the canonical "custom
    pack image" set in Drip's admin panel. This is buried in JSONB
    and won't appear in any column-level scan (discovered 2026-05-14).

    Fall back to `pack_image_url` from config when:
      - pack_box_break_id is absent
      - the box_break row doesn't exist
      - the box_break row exists but has no packImage key (legacy rows
        with animation_type=NULL)

    Raises RuntimeError only when BOTH lookup and fallback fail — i.e.
    there's nothing to render. Single-resolved-string return keeps the
    render call site clean.
    """
    box_break_id = pack.get("pack_box_break_id")
    fallback_url = pack.get("pack_image_url")

    if not box_break_id:
        if not fallback_url:
            raise RuntimeError(
                "No pack_box_break_id and no pack_image_url in config — "
                "nothing to render."
            )
        log.info("Pack image: using config pack_image_url (no box_break_id set)")
        return fallback_url

    # UUID format already validated at load time (see load_featured_pack).
    sql_template = SQL_PACK_IMAGE_PATH.read_text(encoding="utf-8")
    sql_text = sql_template.replace(":box_break_id", box_break_id)
    if ":box_break_id" in sql_text:
        raise RuntimeError("Failed to substitute :box_break_id in pack_image_lookup.sql")

    try:
        rows = _call_mcp(_build_prompt(sql_text), label="pack-image")
    except Exception as e:  # noqa: BLE001 — fall back rather than fail
        log.warning(
            "Pack image DB lookup failed (%s); falling back to config "
            "pack_image_url.", e,
        )
        if fallback_url:
            return fallback_url
        raise

    if not rows:
        log.warning(
            "box_break %s returned no rows (or no packImage key); "
            "falling back to config pack_image_url.", box_break_id,
        )
        if not fallback_url:
            raise RuntimeError(
                f"box_break {box_break_id} has no packImage AND config has "
                f"no pack_image_url fallback — nothing to render."
            )
        return fallback_url

    resolved = rows[0].get("pack_image_url")
    if not resolved:
        log.warning(
            "box_break %s row has null packImage; falling back to config "
            "pack_image_url.", box_break_id,
        )
        if not fallback_url:
            raise RuntimeError(
                f"box_break {box_break_id} has null packImage AND config "
                f"has no pack_image_url fallback — nothing to render."
            )
        return fallback_url

    log.info(
        "Pack image: resolved from box_break %s (%r): %s",
        box_break_id, rows[0].get("title", "?"), resolved,
    )
    return resolved


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
    """
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    clean_pack = string_transforms.pack_name_for_caption(pack["pack_name"])
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
    clean_pack = string_transforms.pack_name_for_caption(pack["pack_name"])
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
    clean_pack = string_transforms.pack_name_for_caption(pack["pack_name"])
    hit_value = int(round(float(hit["hit_value"])))
    ratio = float(hit["hit_value"]) / float(pack["pack_price"])

    return "\n".join([
        f":dart: *${hit_value} chase on {clean_card}* "
        f"(_{ratio:.1f}×_ the ${pack['pack_price']} pack price)",
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
        f"featured pack price)."
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
        + f"_{top_card}_ ({top_ratio:.1f}× the pack price).\n"
        + "_If this looks postable, lower `pack_price` in "
        + "`config/featured_pack.json` so the threshold drops._"
    )


def _slack_already_shown(card_id: int, hit: dict[str, Any]) -> None:
    """Skip message: candidate card was already presented to Noah on a
    previous run. Could be a quiet weekend repeating the same batch."""
    clean_card = string_transforms.card_name_cleanup(hit.get("card_name") or "")
    slack.post_message(
        f":repeat: *Skipping New Chase post — already shown.*\n"
        f"Top chase is _{clean_card}_ (id `{card_id}`), which matches "
        f"`state/last_chase_card_id.txt` from a previous run.\n"
        f"_Waiting for a new batch with a different top card._"
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
        "Featured pack: %r @ $%s (multiplier=%s×) — %s",
        pack["pack_name"], pack["pack_price"],
        pack["chase_threshold_multiplier"], pack["pack_image_url"],
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

    hit = candidates[0]
    card_product_id = int(hit["card_product_id"])
    log.info(
        "Candidate: $%s on %s (id=%s)",
        hit.get("hit_value"), hit.get("card_name"), card_product_id,
    )

    # --- De-dup gate --------------------------------------------------------
    try:
        last_card_id = state_branch.read_last_card_id()
    except StateBranchError as e:
        _emit_failure_to_slack("State branch read failed", e)
        return 1
    log.info("Last posted card_id: %r", last_card_id)
    if last_card_id is not None and last_card_id == card_product_id:
        try:
            _slack_already_shown(card_product_id, hit)
        except SlackError as e:
            log.error("Slack notify failed on already-shown path: %s", e)
            return 1
        return 0

    # --- Resolve pack image (DB-driven if pack_box_break_id set) -----------
    try:
        pack_image_url = resolve_pack_image(pack)
    except Exception as e:  # noqa: BLE001
        _emit_failure_to_slack("Pack image resolution failed", e)
        return 1

    # --- Render -------------------------------------------------------------
    try:
        pack_name_render = string_transforms.pack_name_for_canva(pack["pack_name"])
        png_bytes = render_post_to_bytes(
            card_image_url=hit["card_image_url"],
            pack_image_url=pack_image_url,
            pack_name=pack_name_render,
            hit_value=float(hit["hit_value"]),
        )
        log.info("Rendered %d bytes", len(png_bytes))
    except (RenderError, KeyError, TypeError, ValueError) as e:
        _emit_failure_to_slack("Render failed", e)
        return 1

    # --- Upload -------------------------------------------------------------
    try:
        raw_image_url = publish_to_github(png_bytes, filename=CHASE_IMAGE_FILENAME)
        log.info("Uploaded to %s", raw_image_url)
    except ImageHostError as e:
        _emit_failure_to_slack("Image host upload failed", e)
        return 1

    # Cache-bust the image URL — same reasoning as main.py: Slack and
    # Metricool both cache raw.githubusercontent.com responses, and we
    # overwrite the same `latest_chase.png` URL every successful run.
    image_url = f"{raw_image_url}?v={int(time.time())}"

    # --- Compose payload + post Slack ---------------------------------------
    publish_iso, publish_tz = schedule_time.next_6pm_pt()
    publish_dt = datetime.fromisoformat(publish_iso)
    x_publish_dt = publish_dt + timedelta(minutes=15)
    x_publish_iso = x_publish_dt.strftime("%Y-%m-%dT%H:%M:%S")

    ig_caption = build_ig_caption(hit, pack)
    x_caption = build_x_caption(hit, pack)

    payload = {
        "image_url": image_url,
        "timezone": publish_tz,
        "ig": {"caption": ig_caption, "publish": publish_iso},
        "x":  {"caption": x_caption,  "publish": x_publish_iso},
    }

    try:
        slack_text = build_slack_success_text(
            hit, pack, image_url, publish_iso, x_publish_iso,
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
