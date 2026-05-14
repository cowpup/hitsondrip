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
}

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
    const payloadStr = params.get("payload");
    if (!payloadStr) {
      return new Response("missing payload", { status: 400 });
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

    // Slack requires a 200 within 3s. Heavy work goes into waitUntil so
    // we respond fast and the Metricool POSTs run in the background.
    if (value === "reject") {
      ctx.waitUntil(replyInThread(
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

    ctx.waitUntil(replyInThread(
      interaction,
      `:warning: Unknown button value: \`${value.slice(0, 80)}\``,
    ));
    return new Response("", { status: 200 });
  },
};

// --------------------------------------------------------------------- //
// Approve handler — POSTs to Metricool, posts a reply summarizing.
// --------------------------------------------------------------------- //

async function handleApprove(
  interaction: SlackInteractionPayload,
  username: string,
  post: PostPayload,
  env: Env,
): Promise<void> {
  const tz = post.timezone || DEFAULT_TIMEZONE;
  const results: string[] = [];

  // IG first. Fatal if it fails — IG is the headline.
  try {
    await createInstagramPost(post.image_url, post.ig.caption, post.ig.publish, tz, env);
    results.push("IG ✓");
  } catch (e) {
    const err = e as Error;
    results.push(`IG ✗ (${err.message.slice(0, 200)})`);
  }

  // X cross-post — non-fatal.
  if (post.x) {
    try {
      await createXPost(post.image_url, post.x.caption, post.x.publish, tz, env);
      results.push("X ✓");
    } catch (e) {
      const err = e as Error;
      results.push(`X ✗ (${err.message.slice(0, 200)})`);
    }
  }

  const summary = results.join(" · ");
  await replyInThread(
    interaction,
    `:white_check_mark: *Approved by @${username}* — ${summary}`,
  );
}

// --------------------------------------------------------------------- //
// Metricool POST helpers — direct fetches, no SDK.
// --------------------------------------------------------------------- //

function metricoolPostUrl(env: Env): string {
  const u = new URL(`${METRICOOL_BASE}/v2/scheduler/posts`);
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
): Promise<void> {
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
  await metricoolPost(body, env);
}

async function createXPost(
  imageUrl: string,
  caption: string,
  publishIso: string,
  timezone: string,
  env: Env,
): Promise<void> {
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
  await metricoolPost(body, env);
}

async function metricoolPost(body: Record<string, unknown>, env: Env): Promise<void> {
  const resp = await fetch(metricoolPostUrl(env), {
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
// Slack reply via response_url.
// --------------------------------------------------------------------- //

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
    /* swallow — we already returned 200 to Slack's main handler */
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
