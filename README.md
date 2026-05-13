# hitsondrip

Daily "Just Pulled" Instagram post for [Drip Shop Live](https://dripshop.live). A GitHub Actions cron at 12pm PT pulls the biggest Drip-fulfilled instant-pack hit from the last 24 hours, renders an Instagram-square PNG locally with Pillow, uploads it to this repo's `daily-output` branch, schedules a 6pm PT IG post via Metricool, and notifies Slack so the human in the loop sees the preview ~6 hours before it publishes.

## How it works

```
┌──────────────────────────┐
│ GitHub Actions cron (UTC)│  0 19 * * *  → 12pm PDT / 11am PST
└────────────┬─────────────┘
             ▼
       main.py
             ▼
┌──────────────────────────┐
│ DripShopLive MCP         │  ← queries/biggest_hit_24h.sql
│ via Anthropic API        │     (top 1 by pgp.value, cert_number IS NOT NULL)
└────────────┬─────────────┘
             ▼
       string_transforms
             ▼
       render_just_pulled (Pillow)
             ▼
┌──────────────────────────┐
│ image_host.py            │  → cowpup/hitsondrip @ daily-output : latest.png
└────────────┬─────────────┘
             ▼
┌──────────────────────────┐
│ metricool.schedule_post  │  → 6pm PT today, IG @dripshoplive_
│ metricool.normalize_url  │     (snapshots image to Metricool CDN)
└────────────┬─────────────┘
             ▼
       slack.post_message  → preview in #noah-testing
```

If there are no Drip-fulfilled hits in the last 24 hours, the script posts a "no qualifying hit today" message to Slack and exits cleanly — no Instagram post is scheduled.

## Local setup

```sh
# Clone, then:
uv sync                              # installs deps + tzdata (Windows-friendly)
cp .env.example .env                 # fill in real tokens
uv run pytest tests/                 # all 46 tests should pass

# Render a single test post locally (no API spend, no upload):
uv run python -m src.renderer \
  --card-url https://cdn.dripshop.live/product/<id>.webp \
  --pack-url https://cdn.dripshop.live/product/<id>.png \
  --pack-name "Gold PSA 10 Slab Pack" \
  --pack-price 100 \
  --hit-value 650 \
  --out test_output.png

# Verify MCP / Metricool / Slack auth (one-shot smoke tests):
uv run python verify_mcp_auth.py
uv run python verify_metricool.py
uv run python verify_slack.py

# Dry-run main.py locally (no GITHUB_TOKEN = file:// upload fallback,
# but a REAL Metricool post will be scheduled — be careful):
uv run python -u main.py
```

## Required environment variables

| Variable | Where it lives | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `.env` + GitHub Actions secret | Claude API for the MCP-driven DripShopLive query |
| `METRICOOL_USER_TOKEN` | `.env` + GitHub Actions secret | Metricool API token (Settings → API) |
| `METRICOOL_USER_ID` | `.env` + GitHub Actions secret | Metricool numeric user ID |
| `METRICOOL_BLOG_ID` | `.env` + GitHub Actions secret | Metricool brand/blog ID (Drip TCG) |
| `SLACK_BOT_TOKEN` | `.env` + GitHub Actions secret | `xoxb-...` bot token |
| `SLACK_CHANNEL_ID` | `.env` + GitHub Actions secret | Channel ID (`C...`), not the channel name |
| `GITHUB_TOKEN` | Auto-injected by Actions | Auth for the Contents API write to `daily-output` |
| `GITHUB_REPO` | Auto-injected by Actions | `owner/repo` string, e.g. `cowpup/hitsondrip` |

`.env.example` documents the local-dev form. **Never commit `.env`** — it's in `.gitignore`.

## One-time setup: the `daily-output` orphan branch

`image_host.py` writes `latest.png` to a dedicated `daily-output` branch so the daily PNGs don't pollute `main`'s history. Create it once:

```sh
# From a fresh clone, with main checked out:
git checkout --orphan daily-output
git rm -rf .
echo "PNG drops written by main.py via image_host.publish_to_github." > README.md
git add README.md
git commit -m "init daily-output orphan branch"
git push -u origin daily-output
git checkout main
```

After this, the workflow can read/write `latest.png` on `daily-output` indefinitely.

## GitHub Actions secrets

In the repo's Settings → Secrets and variables → Actions, add each of the 6 env vars listed above (excluding `GITHUB_TOKEN` and `GITHUB_REPO`, which Actions auto-provides). Names must match exactly.

## Manual trigger

For testing or ad-hoc reruns:

```sh
gh workflow run daily.yml
gh run watch                  # follow the run
```

This fires the same logic as the cron. Each run still hits the live DripShopLive query, renders, uploads, schedules, and notifies — so manual runs are not free of side effects.

## Key rotation notes

- **`ANTHROPIC_API_KEY`**: rotate via console.anthropic.com → Settings → API Keys. Update GitHub Actions secret + `.env`.
- **`METRICOOL_USER_TOKEN`**: rotate in Metricool dashboard → Settings → API. Re-run `verify_metricool.py` after rotation.
- **`SLACK_BOT_TOKEN`**: rotate in api.slack.com → Your Apps → OAuth & Permissions. Bot user must remain a member of the channel.

## Repository layout

```
src/
  renderer.py             — local Pillow renderer (locked, 46-test variety-proven)
  string_transforms.py    — pack/card name cleanup + price formatting
  schedule_time.py        — next_6pm_pt(), DST-aware via zoneinfo
  image_host.py           — GitHub Contents API uploader → daily-output branch
  metricool.py            — list_brands / find_instagram_brand / schedule_instagram_post / normalize_image_url
  slack.py                — chat.postMessage with optional image block
queries/
  biggest_hit_24h.sql     — top-1 Drip-fulfilled hit query for DripShopLive
tools/
  fetch_recent_hits.py    — variety-test fetcher (streaming MCP for visibility)
  render_recent_hits.py   — bulk-renders data/recent_hits.json into data/recent_renders/
  discover_metricool_user_id.py   — one-time Metricool onboarding helper
  extract_colors.py       — Pillow color sampling utility
assets/
  background.png          — Instagram-square frame + drip logo + pill + "JUST PULLED" header
  reference_sample.png    — v3 Canva export, the visual target for renderer iteration
  fonts/DMSans-Bold.ttf
tests/
  test_string_transforms.py
  test_schedule_time.py
prompt.md                 — Claude workflow instructions for main.py
main.py                   — daily cron entrypoint
verify_*.py               — smoke tests for each external dependency
archive/                  — preserved historical attempts (Canva, measure_landmarks, pillow_renderer)
.github/workflows/daily.yml — the cron itself
```

## Troubleshooting

- **No Slack message at 12pm PT**: check `gh run list -w daily.yml` for a failed run. The script's failure path posts to Slack, but if Slack itself is broken or the run failed before any code ran (e.g., dependency install), only the GitHub Actions log will have details.
- **Metricool says "publishing date is in the past"**: `schedule_time.next_6pm_pt()` includes a strict `>=` check so this shouldn't happen, but if the cron ever fires after 5:59pm PT (very late run, queue backlog), the post is correctly rescheduled to tomorrow.
- **Image is yesterday's**: Metricool likely couldn't reach the GitHub URL at publish time. The `normalize_image_url` call after scheduling should snapshot the image immediately so this is robust against branch overwrites. If it still happens, check Metricool dashboard's "media" tab for the actual snapshotted URL.
- **"No Drip-fulfilled hits"**: not an error — confirm via DripShopLive directly. The filter is `cert_number IS NOT NULL`; some 24h windows genuinely have zero graded hits.
