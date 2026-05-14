/**
 * Slack interactive endpoint — creates today's hitsondrip post(s) on
 * Metricool only when a human clicks ✅ in Slack.
 *
 * Flow:
 *   1. main.py runs at 12pm PT, renders the PNG, uploads to GitHub,
 *      and posts a Slack message with two interactive buttons. The
 *      Approve button's `value` is "approve:<base64 of full payload>",
 *      where the payload is a JSON object with image_url + IG/X caption
 *      and publish times. The Skip button's `value` is just "reject".
 *      NOTHING is scheduled on Metricool at this stage.
 *   2. User clicks Approve or Skip in Slack.
 *   3. Slack POSTs a signed payload to this Worker.
 *   4. We verify the Slack signature, then either:
 *        Approve: decode the payload, POST both IG + X to Metricool
 *                 with autoPublish=true so they fire at their scheduled
 *                 times (6pm PT IG, 6:15pm PT X).
 *        Reject:  no-op (nothing to delete since nothing was created).
 *   5. We reply in-thread via response_url so the channel sees what
 *      happened. The Metricool POSTs happen via ctx.waitUntil() so we
 *      respond to Slack within 3 seconds even if Metricool is slow.
 *
 * Why no Metricool state until approval:
 *   Earlier architectures scheduled drafts at 12pm and PUT-updated them
 *   on approval. Metricool's PUT endpoint surprised us in two ways:
 *   (a) its 400 response sometimes persists partial fields anyway, and
 *   (b) PUTs with no `id` in the body create a duplicate post rather
 *   than updating the URL-path-identified one. Skipping the draft entirely
 *   sidesteps both quirks and removes the need for a fail-closed cleanup.
 */

export interface Env {
  SLACK_SIGNING_SECRET: string;
  METRICOOL_USER_TOKEN: string;
  METRICOOL_USER_ID: string;
  METRICOOL_BLOG_ID: string;
  /** GitHub fine-grained PAT with `actions: write` on cowpup/hitsondrip,
   * used for the /justpulled slash command to trigger the daily.yml
   * workflow_dispatch. Stored as a Worker secret. */
  GITHUB_PAT: string;
  /**
   * KV namespace for click deduplication. Worker stores the Slack
   * message_ts as a key with 24h TTL on first click; subsequent clicks
   * on the same message see the entry and no-op. Prevents double-click
   * (or rage-click) from creating duplicate Metricool posts.
   */
  DEDUP: KVNamespace;
}

const GITHUB_REPO = "cowpup/hitsondrip";
const GITHUB_WORKFLOW = "daily.yml";

const METRICOOL_BASE = "https://app.metricool.com/api";
const DEFAULT_TIMEZONE = "America/Los_Angeles";

interface PostPayload {
  image_url: string;
  timezone?: string;
  ig: { caption: string; publish: string };
  x?: { caption: string; publish: string } | null;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    const body = await request.text();
    const timestamp = request.headers.get("x-slack-request-timestamp");
    const signature = request.headers.get("x-slack-signature");

    if (!timestamp || !signature) {
      return new Response("missing slack signature headers", { status: 400 });
    }

    const now = Math.floor(Date.now() / 1000);
    const ts = parseInt(timestamp, 10);
    if (!Number.isFinite(ts) || Math.abs(now - ts) > 60 * 5) {
      return new Response("stale slack request", { status: 401 });
    }

    const sigBase = `v0:${timestamp}:${body}`;
    const expected = await hmacSha256Hex(env.SLACK_SIGNING_SECRET, sigBase);
    const computed = `v0=${expected}`;

    if (!safeEqual(computed, signature)) {
      return new Response("invalid slack signature", { status: 401 });
    }

    const params = new URLSearchParams(body);

    // Route by payload shape:
    //   - Slack INTERACTIONS (button clicks) come with a `payload` form
    //     field containing JSON.
    //   - Slack SLASH COMMANDS (/justpulled etc.) come with `command`,
    //     `text`, `user_name`, etc. as separate form fields (no `payload`).
    const payloadStr = params.get("payload");
    const command = params.get("command");

    if (command) {
      return await handleSlashCommand(params, ctx, env);
    }

    if (!payloadStr) {
      return new Response("missing payload or command", { status: 400 });
    }

    let interaction: SlackInteractionPayload;
    try {
      interaction = JSON.parse(payloadStr) as SlackInteractionPayload;
    } catch {
      return new Response("payload not valid json", { status: 400 });
    }

    if (interaction.type !== "block_actions" || !interaction.actions?.length) {
      return new Response("", { status: 200 });
    }

    const action = interaction.actions[0];
    const value = action.value || "";
    const username = interaction.user?.username || interaction.user?.name || "someone";
    const messageTs = interaction.message?.ts;

    // Click deduplication via KV, SCOPED BY ACTION TYPE so an approve-dedup
    // doesn't block a subsequent delete on the same message (and vice
    // versa). The "approve" -> "delete" sequence is intentional after a
    // user approves and changes their mind.
    const actionType = actionTypeOf(value);
    if (messageTs && actionType) {
      const dedupKey = `processed:${actionType}:${messageTs}`;
      const existing = await env.DEDUP.get(dedupKey);
      if (existing) {
        return new Response("", { status: 200 });
      }
      await env.DEDUP.put(dedupKey, "1", { expirationTtl: 60 * 60 * 24 });
    }

    if (value === "reject") {
      ctx.waitUntil(replaceOriginal(
        interaction,
        `:no_entry_sign: *Skipped by @${username}* — nothing posted to Metricool.`,
      ));
      return new Response("", { status: 200 });
    }

    if (value.startsWith("approve:")) {
      const encoded = value.slice("approve:".length);
      let post: PostPayload;
      try {
        post = decodePayload(encoded);
      } catch (e) {
        const err = e as Error;
        ctx.waitUntil(replyInThread(
          interaction,
          `:warning: Couldn't decode the post payload: \`${err.message}\``,
        ));
        return new Response("", { status: 200 });
      }
      ctx.waitUntil(handleApprove(interaction, username, post, env));
      return new Response("", { status: 200 });
    }

    if (value.startsWith("delete:")) {
      // value = "delete:<igPostId>:<xPostId or 'none'>"
      const parts = value.split(":");
      const igId = parts[1] || "none";
      const xId = parts[2] || "none";
      ctx.waitUntil(handleDelete(interaction, username, igId, xId, env));
      return new Response("", { status: 200 });
    }

    ctx.waitUntil(replyInThread(
      interaction,
      `:warning: Unknown button value: \`${value.slice(0, 80)}\``,
    ));
    return new Response("", { status: 200 });
  },
};

/** "approve" | "reject" | "delete" | null based on the button's value prefix. */
function actionTypeOf(value: string): string | null {
  if (value === "reject") return "reject";
  if (value.startsWith("approve:")) return "approve";
  if (value.startsWith("delete:")) return "delete";
  return null;
}

// --------------------------------------------------------------------- //
// Approve handler — POSTs to Metricool, posts a reply summarizing.
// --------------------------------------------------------------------- //

async function handleApprove(
  interaction: SlackInteractionPayload,
  username: string,
  post: PostPayload,
  env: Env,
): Promise<void> {
  await replaceOriginal(
    interaction,
    `:hourglass_flowing_sand: *Processing approval by @${username}...*`,
  );

  const tz = post.timezone || DEFAULT_TIMEZONE;
  const results: string[] = [];
  let igPostId: string | null = null;
  let xPostId: string | null = null;

  try {
    igPostId = await createInstagramPost(post.image_url, post.ig.caption, post.ig.publish, tz, env);
    results.push("IG ✓");
  } catch (e) {
    const err = e as Error;
    results.push(`IG ✗ (${err.message.slice(0, 200)})`);
  }

  if (post.x) {
    try {
      xPostId = await createXPost(post.image_url, post.x.caption, post.x.publish, tz, env);
      results.push("X ✓");
    } catch (e) {
      const err = e as Error;
      results.push(`X ✗ (${err.message.slice(0, 200)})`);
    }
  }

  const summary = results.join(" · ");
  const statusText = `:white_check_mark: *Approved by @${username}* — ${summary}`;

  // If we got at least one post ID back, attach a 🗑 Delete button so
  // the user can change their mind. Encodes both IDs (or "none" for X)
  // into the button value, same pattern as the approve button.
  if (igPostId || xPostId) {
    await replaceOriginalWithButton(
      interaction,
      statusText,
      {
        action_id: "delete",
        style: "danger",
        text: "🗑 Delete post",
        value: `delete:${igPostId || "none"}:${xPostId || "none"}`,
      },
    );
  } else {
    // Both failed; no Metricool state to delete. Plain status, no button.
    await replaceOriginal(interaction, statusText);
  }
}

async function handleDelete(
  interaction: SlackInteractionPayload,
  username: string,
  igId: string,
  xId: string,
  env: Env,
): Promise<void> {
  await replaceOriginal(
    interaction,
    `:hourglass_flowing_sand: *Deleting posts by @${username}...*`,
  );

  const results: string[] = [];

  if (igId && igId !== "none") {
    try {
      await metricoolDelete(igId, env);
      results.push("IG ✓");
    } catch (e) {
      const err = e as Error;
      results.push(`IG ✗ (${err.message.slice(0, 200)})`);
    }
  }
  if (xId && xId !== "none") {
    try {
      await metricoolDelete(xId, env);
      results.push("X ✓");
    } catch (e) {
      const err = e as Error;
      results.push(`X ✗ (${err.message.slice(0, 200)})`);
    }
  }

  const summary = results.length ? results.join(" · ") : "nothing to delete";
  await replaceOriginal(
    interaction,
    `:wastebasket: *Deleted by @${username}* — ${summary}`,
  );
}

// --------------------------------------------------------------------- //
// /justpulled slash command — dispatches the daily.yml workflow.
// --------------------------------------------------------------------- //

async function handleSlashCommand(
  params: URLSearchParams,
  ctx: ExecutionContext,
  env: Env,
): Promise<Response> {
  const command = (params.get("command") || "").trim();
  const userName = params.get("user_name") || "someone";
  const responseUrl = params.get("response_url") || "";

  if (command !== "/justpulled") {
    return jsonResponse({
      response_type: "ephemeral",
      text: `:warning: Unknown command: \`${command}\``,
    });
  }

  // Slack wants a response within 3s. We dispatch the workflow inline
  // (GitHub's dispatch API typically returns in 200-500ms) and reply
  // immediately. If dispatch fails, fall back to a delayed response
  // via response_url so the user sees the error.
  try {
    await dispatchWorkflow(env);
    return jsonResponse({
      response_type: "in_channel",
      text:
        `:rocket: *@${userName} triggered \`/justpulled\`* — ` +
        `daily.yml is running now. Approval message will appear here in ~30 seconds.`,
    });
  } catch (e) {
    const err = e as Error;
    // If we exceeded the 3s window or anything else, push to response_url
    // so the user still sees the error.
    if (responseUrl) {
      ctx.waitUntil(
        fetch(responseUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            response_type: "ephemeral",
            text: `:rotating_light: Workflow dispatch failed: \`${err.message.slice(0, 300)}\``,
          }),
        }).catch(() => undefined),
      );
    }
    return jsonResponse({
      response_type: "ephemeral",
      text: `:rotating_light: Workflow dispatch failed: \`${err.message.slice(0, 300)}\``,
    });
  }
}

async function dispatchWorkflow(env: Env): Promise<void> {
  if (!env.GITHUB_PAT) {
    throw new Error("GITHUB_PAT not configured on the Worker.");
  }
  const url = `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_PAT}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      // GitHub requires a User-Agent on every API call. Workers don't
      // send a default one, so set it explicitly.
      "User-Agent": "hitsondrip-approver/1.0",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`GitHub dispatches API -> HTTP ${resp.status}: ${text.slice(0, 300)}`);
  }
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

// --------------------------------------------------------------------- //
// Metricool POST helpers — direct fetches, no SDK.
// --------------------------------------------------------------------- //

function metricoolPostsUrl(env: Env, postId?: string): string {
  const path = postId
    ? `/v2/scheduler/posts/${postId}`
    : `/v2/scheduler/posts`;
  const u = new URL(`${METRICOOL_BASE}${path}`);
  u.searchParams.set("blogId", env.METRICOOL_BLOG_ID);
  u.searchParams.set("userId", env.METRICOOL_USER_ID);
  u.searchParams.set("integrationSource", "MCP");
  return u.toString();
}

async function createInstagramPost(
  imageUrl: string,
  caption: string,
  publishIso: string,
  timezone: string,
  env: Env,
): Promise<string | null> {
  const body = {
    text: caption,
    media: [imageUrl],
    mediaAltText: [],
    providers: [{ network: "instagram" }],
    publicationDate: { dateTime: publishIso, timezone },
    autoPublish: true,
    draft: false,
    firstCommentText: "",
    hasNotReadNotes: false,
    shortener: false,
    smartLinkData: { ids: [] },
    descendants: [],
    instagramData: { type: "POST" },
  };
  return await metricoolPost(body, env);
}

async function createXPost(
  imageUrl: string,
  caption: string,
  publishIso: string,
  timezone: string,
  env: Env,
): Promise<string | null> {
  const body = {
    text: caption,
    media: [imageUrl],
    mediaAltText: [],
    providers: [{ network: "twitter" }],
    publicationDate: { dateTime: publishIso, timezone },
    autoPublish: true,
    draft: false,
    firstCommentText: "",
    hasNotReadNotes: false,
    shortener: false,
    smartLinkData: { ids: [] },
    descendants: [],
    twitterData: { type: "POST" },
  };
  return await metricoolPost(body, env);
}

async function metricoolPost(body: Record<string, unknown>, env: Env): Promise<string | null> {
  const resp = await fetch(metricoolPostsUrl(env), {
    method: "POST",
    headers: {
      "X-Mc-Auth": env.METRICOOL_USER_TOKEN,
      "Content-Type": "application/json",
      "Accept": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`POST -> HTTP ${resp.status}: ${text.slice(0, 300)}`);
  }
  // Metricool wraps POST responses as `{"data": {"id": <number>, ...}}`.
  // Pull the ID so the caller can attach a delete button referencing it.
  try {
    const json = (await resp.json()) as Record<string, unknown>;
    const data = (json && typeof json === "object" && "data" in json
      ? json.data
      : json) as Record<string, unknown> | undefined;
    if (data && typeof data === "object" && "id" in data && data.id != null) {
      return String(data.id);
    }
  } catch {
    /* response wasn't JSON; that's fine — post was created, we just
       can't add a delete button. */
  }
  return null;
}

async function metricoolDelete(postId: string, env: Env): Promise<void> {
  const resp = await fetch(metricoolPostsUrl(env, postId), {
    method: "DELETE",
    headers: {
      "X-Mc-Auth": env.METRICOOL_USER_TOKEN,
      "Accept": "application/json",
    },
  });
  if (!resp.ok) {
    // Treat 404 as success — if someone manually deleted in Metricool
    // dashboard between approve and our delete click, the desired state
    // (post gone) is the same.
    if (resp.status === 404) return;
    const text = await resp.text();
    throw new Error(`DELETE ${postId} -> HTTP ${resp.status}: ${text.slice(0, 300)}`);
  }
}

// --------------------------------------------------------------------- //
// Payload encoding — main.py base64-encodes a JSON object into the
// Approve button's value. We base64-decode and JSON.parse on this side.
// --------------------------------------------------------------------- //

function decodePayload(encoded: string): PostPayload {
  // base64.urlsafe_b64encode uses "-_" instead of "+/". Convert back so
  // atob() (which expects standard base64) accepts it.
  const standard = encoded.replace(/-/g, "+").replace(/_/g, "/");
  const padded = standard + "=".repeat((4 - (standard.length % 4)) % 4);
  const jsonStr = decodeBase64Utf8(padded);
  const parsed = JSON.parse(jsonStr) as PostPayload;
  if (!parsed || typeof parsed !== "object") {
    throw new Error("payload is not an object");
  }
  if (!parsed.image_url || !parsed.ig?.caption || !parsed.ig?.publish) {
    throw new Error("payload missing required fields");
  }
  return parsed;
}

/**
 * atob returns Latin-1 bytes — we need UTF-8 for emoji-bearing captions.
 * Decode atob bytes through TextDecoder to get proper UTF-8 strings.
 */
function decodeBase64Utf8(b64: string): string {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder("utf-8").decode(bytes);
}

// --------------------------------------------------------------------- //
// Slack message updates via response_url.
// --------------------------------------------------------------------- //

/**
 * Replace the original Slack message (the one with the buttons) with a
 * new version: same image preview, but the action buttons block is
 * removed and a status section is appended. Visible to everyone who
 * could see the original (the channel members). Used for both approve
 * and reject so the buttons are gone after one click and the outcome
 * is durably visible.
 */
async function replaceOriginal(
  payload: SlackInteractionPayload,
  statusText: string,
): Promise<void> {
  if (!payload.response_url) return;

  // Keep all original blocks EXCEPT the action buttons; append a new
  // section block with the status. This preserves the image preview
  // and the original details lines.
  const originalBlocks = Array.isArray(payload.message?.blocks)
    ? (payload.message?.blocks as Array<{ type?: string }>)
    : [];
  const blocksKept = originalBlocks.filter((b) => b?.type !== "actions");
  const newBlocks = [
    ...blocksKept,
    {
      type: "section",
      text: { type: "mrkdwn", text: statusText },
    },
  ];

  try {
    await fetch(payload.response_url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        replace_original: true,
        text: statusText,
        blocks: newBlocks,
      }),
    });
  } catch {
    /* swallow — we already returned 200 to Slack's main handler */
  }
}

/**
 * Like replaceOriginal but appends a single new action button
 * (e.g. "🗑 Delete post") below the status section.
 */
async function replaceOriginalWithButton(
  payload: SlackInteractionPayload,
  statusText: string,
  button: { action_id: string; style?: "primary" | "danger"; text: string; value: string },
): Promise<void> {
  if (!payload.response_url) return;

  const originalBlocks = Array.isArray(payload.message?.blocks)
    ? (payload.message?.blocks as Array<{ type?: string }>)
    : [];
  const blocksKept = originalBlocks.filter((b) => b?.type !== "actions");
  const newBlocks = [
    ...blocksKept,
    {
      type: "section",
      text: { type: "mrkdwn", text: statusText },
    },
    {
      type: "actions",
      elements: [
        {
          type: "button",
          action_id: button.action_id,
          ...(button.style ? { style: button.style } : {}),
          text: { type: "plain_text", text: button.text, emoji: true },
          value: button.value,
        },
      ],
    },
  ];

  try {
    await fetch(payload.response_url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        replace_original: true,
        text: statusText,
        blocks: newBlocks,
      }),
    });
  } catch {
    /* swallow */
  }
}

/**
 * Post an ephemeral-style reply in-thread without modifying the
 * original message. Used for non-action error states (e.g. malformed
 * payload) so the buttons remain clickable for retry.
 */
async function replyInThread(
  payload: SlackInteractionPayload,
  text: string,
): Promise<void> {
  if (!payload.response_url) return;
  try {
    await fetch(payload.response_url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        replace_original: false,
        thread_ts: payload.message?.ts,
        text,
      }),
    });
  } catch {
    /* swallow */
  }
}

// --------------------------------------------------------------------- //
// HMAC + constant-time compare.
// --------------------------------------------------------------------- //

async function hmacSha256Hex(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(message),
  );
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

// --------------------------------------------------------------------- //
// Slack interaction payload typing — partial, only the fields we read.
// --------------------------------------------------------------------- //

interface SlackInteractionPayload {
  type: string;
  user?: { id?: string; username?: string; name?: string };
  actions?: Array<{ action_id?: string; value?: string }>;
  message?: { ts?: string };
  response_url?: string;
}
