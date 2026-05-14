/**
 * Slack interactive endpoint — approves or rejects today's drafted
 * hitsondrip post (IG + X) by calling Metricool.
 *
 * Flow:
 *   1. main.py runs at 12pm PT, schedules IG and X posts as
 *      autoPublish=false drafts on Metricool, then posts to Slack with
 *      two interactive buttons. Each button carries a value of the form
 *      "<approve|reject>:<igPostId>:<xPostId>" (xPostId may be "none"
 *      if the X schedule failed).
 *   2. User clicks Approve or Reject in Slack.
 *   3. Slack POSTs a signed payload to this Worker.
 *   4. We verify the signature, then PUT (approve → autoPublish=true)
 *      or DELETE (reject) each post on Metricool.
 *   5. We reply in-thread via response_url so the channel sees what
 *      happened.
 *
 * Why this lives in a Worker instead of GitHub Actions:
 *   Slack's interactive endpoint requires a sub-3-second HTTPS response.
 *   GitHub Actions cold-starts take 20-60 seconds. A Worker is the
 *   right fit — runs in tens of milliseconds, free for our volume.
 */

export interface Env {
  SLACK_SIGNING_SECRET: string;
  METRICOOL_USER_TOKEN: string;
  METRICOOL_USER_ID: string;
  METRICOOL_BLOG_ID: string;
}

const METRICOOL_BASE = "https://app.metricool.com/api";

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    // Slack signs the raw request body. Read it as text BEFORE parsing
    // so we have the exact bytes Slack signed.
    const body = await request.text();
    const timestamp = request.headers.get("x-slack-request-timestamp");
    const signature = request.headers.get("x-slack-signature");

    if (!timestamp || !signature) {
      return new Response("missing slack signature headers", { status: 400 });
    }

    // Reject replays of >5min-old requests (per Slack's recommendation).
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

    // Slack sends interactive payloads as application/x-www-form-urlencoded
    // with the JSON payload in a `payload` form field. Standard quirk.
    const params = new URLSearchParams(body);
    const payloadStr = params.get("payload");
    if (!payloadStr) {
      return new Response("missing payload", { status: 400 });
    }

    let payload: SlackInteractionPayload;
    try {
      payload = JSON.parse(payloadStr) as SlackInteractionPayload;
    } catch {
      return new Response("payload not valid json", { status: 400 });
    }

    if (payload.type !== "block_actions" || !payload.actions?.length) {
      // URL verification or non-button interaction — acknowledge but
      // don't process.
      return new Response("", { status: 200 });
    }

    const action = payload.actions[0];
    const value = action.value || "";
    const parts = value.split(":");
    if (parts.length < 3) {
      await replyInThread(payload, `:warning: Malformed button value: \`${value}\``);
      return new Response("", { status: 200 });
    }
    const [actionType, igPostId, xPostId] = parts;
    const username = payload.user?.username || payload.user?.name || "someone";

    let result: string;
    try {
      if (actionType === "approve") {
        const ig = await publishPost(igPostId, env);
        const x = xPostId && xPostId !== "none"
          ? await publishPost(xPostId, env).catch((e: Error) => `X publish failed: ${e.message}`)
          : null;
        result =
          `:white_check_mark: *Approved by @${username}* — ` +
          `IG ${ig ? "✓" : "✗"}${x === null ? "" : x === true ? " · X ✓" : ` · ${x}`}`;
      } else if (actionType === "reject") {
        const ig = await deletePost(igPostId, env);
        const x = xPostId && xPostId !== "none"
          ? await deletePost(xPostId, env).catch((e: Error) => `X delete failed: ${e.message}`)
          : null;
        result =
          `:no_entry_sign: *Skipped by @${username}* — drafts deleted from Metricool ` +
          `(IG ${ig ? "✓" : "✗"}${x === null ? "" : x === true ? " · X ✓" : ` · ${x}`}).`;
      } else {
        result = `:warning: Unknown action: \`${actionType}\``;
      }
    } catch (e) {
      const err = e as Error;
      result = `:rotating_light: Metricool API failure: \`${err.message}\``;
    }

    // Send confirmation back via response_url (Slack lets us reply
    // out-of-band for up to 30 min after the interaction).
    await replyInThread(payload, result);
    return new Response("", { status: 200 });
  },
};

// --------------------------------------------------------------------- //
// Metricool helpers — direct fetch calls so we don't bundle an SDK.
// --------------------------------------------------------------------- //

function metricoolUrl(postId: string, env: Env): string {
  const u = new URL(`${METRICOOL_BASE}/v2/scheduler/posts/${postId}`);
  u.searchParams.set("blogId", env.METRICOOL_BLOG_ID);
  u.searchParams.set("userId", env.METRICOOL_USER_ID);
  u.searchParams.set("integrationSource", "MCP");
  return u.toString();
}

/**
 * Flip a Metricool draft post to publish-at-its-scheduled-time.
 *
 * Metricool's PUT /v2/scheduler/posts/{id} validates the FULL post body —
 * partial updates with just `{autoPublish: true}` get rejected with a 400
 * ("update.arg3.text must not be null", etc.). So we GET the current post,
 * mutate the flags we care about, then PUT it back whole.
 *
 * (Discovered the hard way on 2026-05-14 when an approve click came back
 * with a 400 in Slack. The partial-PUT may also have a side effect of
 * persisting partial fields anyway — doing the full GET-then-PUT
 * sidesteps that ambiguity.)
 */
async function publishPost(postId: string, env: Env): Promise<true> {
  const url = metricoolUrl(postId, env);

  // 1) GET current post. Single-post GET wraps in {"data": {...}}.
  const getResp = await fetch(url, {
    method: "GET",
    headers: {
      "X-Mc-Auth": env.METRICOOL_USER_TOKEN,
      "Accept": "application/json",
    },
  });
  if (!getResp.ok) {
    const text = await getResp.text();
    throw new Error(`GET ${postId} -> HTTP ${getResp.status}: ${text.slice(0, 300)}`);
  }
  const raw = (await getResp.json()) as Record<string, unknown>;
  const post: Record<string, unknown> =
    raw && typeof raw === "object" && "data" in raw && typeof raw.data === "object"
      ? (raw.data as Record<string, unknown>)
      : raw;

  if (!post || typeof post !== "object") {
    throw new Error(`GET ${postId} returned unexpected shape: ${JSON.stringify(raw).slice(0, 300)}`);
  }

  // 2) Flip the flags. Keep every other field intact.
  const updated: Record<string, unknown> = {
    ...post,
    autoPublish: true,
    draft: false,
  };

  // 3) PUT the full body back.
  const putResp = await fetch(url, {
    method: "PUT",
    headers: {
      "X-Mc-Auth": env.METRICOOL_USER_TOKEN,
      "Content-Type": "application/json",
      "Accept": "application/json",
    },
    body: JSON.stringify(updated),
  });
  if (!putResp.ok) {
    const text = await putResp.text();
    throw new Error(`PUT ${postId} -> HTTP ${putResp.status}: ${text.slice(0, 300)}`);
  }
  return true;
}

async function deletePost(postId: string, env: Env): Promise<true> {
  const resp = await fetch(metricoolUrl(postId, env), {
    method: "DELETE",
    headers: {
      "X-Mc-Auth": env.METRICOOL_USER_TOKEN,
      "Accept": "application/json",
    },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`DELETE ${postId} → HTTP ${resp.status}: ${text.slice(0, 300)}`);
  }
  return true;
}

// --------------------------------------------------------------------- //
// Slack reply via response_url (no token needed — Slack signs the URL
// itself).
// --------------------------------------------------------------------- //

async function replyInThread(
  payload: SlackInteractionPayload,
  text: string,
): Promise<void> {
  if (!payload.response_url) return;
  await fetch(payload.response_url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      replace_original: false,
      thread_ts: payload.message?.ts,
      text,
    }),
  }).catch(() => {
    /* swallow; we already returned to Slack's main handler */
  });
}

// --------------------------------------------------------------------- //
// HMAC + constant-time string comparison (Cloudflare Workers' Web Crypto)
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
