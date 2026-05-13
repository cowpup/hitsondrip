"""Canva OAuth setup helper — one-time PKCE flow to capture access + refresh tokens.

Run this ONCE to provision Canva credentials. Tokens are written to .env and
src/canva_auth.py keeps them fresh from then on.

============================================================================
STEP 1 — Register a Canva integration (do this in your browser first)
============================================================================

1. Go to https://www.canva.com/developers/integrations/
   (Sign in with the same Canva account that owns the templates the daily
   workflow uses — DAHJebHcVRk and DAHJeXoAsqc.)

2. Click "Create an integration", name it "Drip Daily Just Pulled" (only
   you ever see this name later, on the consent screen).

   If the UI asks you to pick "Public" vs "Private", pick Public. ("Public"
   here just means OAuth-based; nothing gets published to a marketplace.)
   At time of writing the Public/Private distinction may not be surfaced
   anymore — if you don't see it, just create the integration and move on.

3. After it's created, you land on the integration's settings page. Look for
   the OAuth / Authentication section and fill in:

   a) Authentication → "This integration uses OAuth"
      Click the toggle on if it isn't already.

   b) Redirect URIs (this is the critical part — exact match required)
      Add ALL THREE of these as separate entries:
          http://127.0.0.1:8765/callback
          http://127.0.0.1:8766/callback
          http://127.0.0.1:8767/callback
      (Three URIs because this helper picks the first available port from
      8765, 8766, 8767. Registering all three means we don't have to come
      back and edit Canva's settings if 8765 is taken locally.)

   c) Scopes → enable EXACTLY these five (uncheck everything else):
          asset:read
          asset:write
          design:content:read
          design:content:write
          design:meta:read

      Why no folder/comment/permission/profile scopes: the daily workflow
      doesn't use them. Requesting writes we don't need is unnecessary attack
      surface. If a future feature needs more, re-run this script — re-auth
      is a 30-second flow.

4. Once saved, on the integration's main page locate:

   a) Client ID — copy this string verbatim. It's visible on the page.

   b) Client secret — click "Generate secret" (button may be under "Auth
      and permissions" or similar). The secret looks like `cnvca…`. It is
      shown ONLY ONCE; if you lose it, you have to generate a new one and
      the old one stops working immediately.

5. Open .env and paste both:
       CANVA_CLIENT_ID=<the client ID>
       CANVA_CLIENT_SECRET=<the client secret>
   Save the file.

============================================================================
STEP 2 — Run this script
============================================================================

    uv run python -m tools.canva_oauth

The script will:
  - Verify CANVA_CLIENT_ID + CANVA_CLIENT_SECRET are set.
  - Pick a free port (8765 → 8766 → 8767).
  - Generate PKCE verifier + challenge and a CSRF state string.
  - Open your browser to Canva's authorization page.
  - You'll see a consent screen showing the five scopes. Click Allow.
  - Canva redirects to http://127.0.0.1:<port>/callback?code=…&state=…
  - The script's one-shot local HTTP server catches the code, exchanges it
    for tokens, validates state, and writes back to .env:
        CANVA_ACCESS_TOKEN
        CANVA_REFRESH_TOKEN
        CANVA_ACCESS_TOKEN_EXPIRES_AT
  - The browser tab will show a small success page; you can close it.

After this, src/canva_auth.py handles all future refresh automatically.
You should never need to run this script again unless:
  - The refresh token gets invalidated (e.g. you revoke the integration's
    access from Canva → Settings → Apps and integrations).
  - You change the scope list (which Canva treats as a fresh consent).
  - You regenerate the client secret.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import socket
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import find_dotenv, load_dotenv, set_key

AUTH_URL = "https://www.canva.com/api/oauth/authorize"
TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"

SCOPES = [
    "asset:read",
    "asset:write",
    "design:content:read",
    "design:content:write",
    "design:meta:read",
]

CANDIDATE_PORTS = [8765, 8766, 8767]
DEFAULT_TIMEOUT_SECONDS = 30


# --------------------------------------------------------------------------- #
# PKCE helpers
# --------------------------------------------------------------------------- #


def _generate_code_verifier() -> str:
    """Generate a 96-char PKCE code verifier (within Canva's 43–128 range)."""
    # token_urlsafe(64) gives ~86 base64url chars — comfortably mid-range.
    return secrets.token_urlsafe(64)


def _generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    # base64url, strip trailing padding ("=") per RFC 7636.
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    creds = f"{client_id}:{client_secret}".encode("utf-8")
    return f"Basic {base64.b64encode(creds).decode('ascii')}"


# --------------------------------------------------------------------------- #
# Local one-shot HTTP server
# --------------------------------------------------------------------------- #


def _pick_port() -> int:
    """Return the first port in CANDIDATE_PORTS that's free for binding.

    Tests by actually binding+closing — the only reliable way on Windows,
    where a TIME_WAIT socket can pass an SO_REUSEADDR check but still reject
    a real bind moments later.
    """
    for port in CANDIDATE_PORTS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
        except OSError:
            continue
        return port
    raise RuntimeError(
        f"None of {CANDIDATE_PORTS} are free. Pick another port range and "
        f"update CANDIDATE_PORTS in this file plus the Redirect URIs in your "
        f"Canva integration."
    )


class _CallbackState:
    """Mutable bucket the HTTP handler writes the captured query params into."""

    def __init__(self) -> None:
        self.code: Optional[str] = None
        self.state: Optional[str] = None
        self.error: Optional[str] = None
        self.error_description: Optional[str] = None


def _make_handler(capture: _CallbackState) -> type[http.server.BaseHTTPRequestHandler]:
    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        # Silence default request logging — keep the script's own output clean.
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not the OAuth callback path.")
                return

            params = urllib.parse.parse_qs(parsed.query)
            capture.code = (params.get("code") or [None])[0]
            capture.state = (params.get("state") or [None])[0]
            capture.error = (params.get("error") or [None])[0]
            capture.error_description = (
                params.get("error_description") or [None]
            )[0]

            if capture.error:
                body = (
                    "<h1>Canva OAuth failed</h1>"
                    f"<p><b>{capture.error}</b>: "
                    f"{capture.error_description or ''}</p>"
                    "<p>You can close this tab. Check the terminal for details.</p>"
                ).encode("utf-8")
                self.send_response(400)
            else:
                body = (
                    "<h1>Canva OAuth complete</h1>"
                    "<p>You can close this tab. Tokens have been written to .env.</p>"
                ).encode("utf-8")
                self.send_response(200)

            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return CallbackHandler


def _wait_for_callback(port: int) -> _CallbackState:
    capture = _CallbackState()
    handler = _make_handler(capture)
    # serve_forever would block indefinitely; handle_request serves exactly
    # one request and returns. Two requests can arrive (e.g. Chrome favicon
    # prefetch), so loop until we have either a code or an error.
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    try:
        while capture.code is None and capture.error is None:
            server.handle_request()
    finally:
        server.server_close()
    return capture


# --------------------------------------------------------------------------- #
# Token exchange
# --------------------------------------------------------------------------- #


def _exchange_code_for_tokens(
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict:
    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Canva token exchange failed: HTTP {response.status_code}\n"
            f"Body: {response.text[:1000]}"
        )
    return response.json()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    load_dotenv()

    client_id = os.environ.get("CANVA_CLIENT_ID")
    client_secret = os.environ.get("CANVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "ERROR: CANVA_CLIENT_ID and/or CANVA_CLIENT_SECRET not set in .env.\n"
            "       Complete STEP 1 in this script's module docstring first:\n"
            "         uv run python -m tools.canva_oauth   (after registering)\n"
            "       See the top of this file for the full registration walkthrough."
        )
        return 2

    port = _pick_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    print(f"Using port {port}, redirect_uri={redirect_uri}")
    print(
        "Make sure this exact redirect URI is in your Canva integration's "
        "Redirect URIs list — Canva enforces exact-match including port."
    )

    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    state = secrets.token_urlsafe(24)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    print("\nOpening your browser to:")
    print(f"  {auth_url}\n")
    print(
        "If the browser doesn't open automatically, paste the URL above into "
        "your browser manually. Then click Allow on the consent screen."
    )
    webbrowser.open(auth_url, new=2)

    print(f"\nListening on http://127.0.0.1:{port}/callback ... ", flush=True)
    capture = _wait_for_callback(port)

    if capture.error:
        print(
            f"\nCanva returned an OAuth error:\n"
            f"  error: {capture.error}\n"
            f"  description: {capture.error_description}"
        )
        return 1

    if not capture.code:
        print("ERROR: callback received but no 'code' parameter present.")
        return 1

    if capture.state != state:
        print(
            f"ERROR: state mismatch (CSRF check failed). "
            f"expected={state!r} got={capture.state!r}"
        )
        return 1

    print("Authorization code received. Exchanging for tokens... ", end="", flush=True)
    try:
        payload = _exchange_code_for_tokens(
            client_id, client_secret, capture.code, code_verifier, redirect_uri
        )
    except RuntimeError as e:
        print(f"\n{e}")
        return 1
    print("OK.")

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    granted_scope = payload.get("scope", "")
    if not access_token or not refresh_token or not expires_in:
        print(f"ERROR: token endpoint returned incomplete payload: {payload!r}")
        return 1

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    # Persist to .env. find_dotenv() locates the existing file; set_key creates
    # one if needed, but the project always has .env at root so this is just
    # a safety net.
    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")
    set_key(env_path, "CANVA_ACCESS_TOKEN", access_token)
    set_key(env_path, "CANVA_REFRESH_TOKEN", refresh_token)
    set_key(env_path, "CANVA_ACCESS_TOKEN_EXPIRES_AT", expires_at.isoformat())

    print("\n=== Success ===")
    print(f"  env file: {env_path}")
    print(f"  access token: {access_token[:8]}… ({len(access_token)} chars)")
    print(f"  refresh token: {refresh_token[:8]}… ({len(refresh_token)} chars)")
    print(f"  access token expires at: {expires_at.isoformat()}")
    print(f"  granted scopes: {granted_scope!r}")
    print(
        "\nNext: run `uv run python verify_mcp_auth.py` to confirm the Canva "
        "MCP now accepts these credentials."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
