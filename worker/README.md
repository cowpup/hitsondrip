# hitsondrip-approver

Cloudflare Worker that handles Slack approval buttons for the daily Drip "Just Pulled" post pipeline.

## What it does

`main.py` at 12pm PT schedules an IG post (and X cross-post) on Metricool as `autoPublish=false` drafts, then posts a Slack message with `Approve` and `Skip` buttons. When a user clicks a button:

- **Approve** → this Worker flips both posts to `autoPublish=true`, so Metricool publishes them at the originally-scheduled time (6pm PT / 6:15pm PT).
- **Skip** → this Worker deletes both drafts.

Either way the Worker replies in-thread with who clicked and what happened.

A separate GitHub Actions cron at 5:55pm PT runs the fail-closed cleanup: if no one has approved by then, the drafts are auto-deleted and Slack gets a "no approval, skipping" notice.

## Deploy

```sh
cd worker
npm install                          # one-time
wrangler secret put SLACK_SIGNING_SECRET
wrangler secret put METRICOOL_USER_TOKEN
wrangler secret put METRICOOL_USER_ID
wrangler secret put METRICOOL_BLOG_ID
wrangler deploy
```

`wrangler deploy` prints the Worker URL (e.g. `https://hitsondrip-approver.<your-subdomain>.workers.dev`). Paste that into the Slack app's **Interactivity & Shortcuts → Request URL**.

## Logs

```sh
wrangler tail                        # live tail of Worker invocations
```

## Local dev

```sh
wrangler dev                         # boots a local Worker on http://127.0.0.1:8787
```

To simulate a Slack interaction locally you'd need to sign a fake payload with the real signing secret — easier to just run a test fire of the production pipeline and click the button.
