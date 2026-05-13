"""Smoke-test MCP auth for the MCP server the runtime uses.

After the Canva path was permanently archived, DripShopLive is the only
MCP in the workflow. The PNG is rendered locally with Pillow
(src/renderer.py); Slack and Metricool are driven via REST in
src/slack.py and src/metricool.py and have their own verify scripts
(verify_slack.py, verify_metricool.py).

Run: uv run python verify_mcp_auth.py
Exit 0 on all PASS, 1 on any FAIL, 2 on config errors.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("ERROR: ANTHROPIC_API_KEY missing from .env")
    sys.exit(2)

MODEL = "claude-opus-4-7"
MCP_BETA = "mcp-client-2025-11-20"


@dataclass
class McpProbe:
    label: str
    url: str
    name: str  # unique slug Anthropic uses to namespace tools
    probe: str  # one-sentence task that forces exactly one tool call
    # Returns the bearer token to send as authorization_token on the
    # mcp_servers entry. None = unauthenticated MCP.
    auth_provider: Optional[Callable[[], str]] = field(default=None)


PROBES: list[McpProbe] = [
    McpProbe(
        label="DripShopLive",
        url="https://db-mcp-production.up.railway.app/sse",
        name="dripshoplive",
        probe=(
            "Use the DripShopLive MCP to list the database schemas available. "
            "Call exactly one tool, then briefly report the schema names. "
            "Do not query any tables."
        ),
    ),
]


def _text_preview(value: Any, limit: int = 400) -> str:
    """Pull a short text preview out of an mcp_tool_result content field."""
    if isinstance(value, list):
        for item in value:
            if getattr(item, "type", None) == "text":
                return getattr(item, "text", "")[:limit]
    if isinstance(value, str):
        return value[:limit]
    return str(value)[:limit]


def verify_one(client: anthropic.Anthropic, probe: McpProbe) -> bool:
    print(f"\n=== {probe.label} ===")
    print(f"  url: {probe.url}")

    server_entry: dict[str, Any] = {
        "type": "url",
        "url": probe.url,
        "name": probe.name,
    }
    if probe.auth_provider is not None:
        token = probe.auth_provider()
        if not token:
            print("  FAIL (token retrieval): provider returned empty token")
            return False
        server_entry["authorization_token"] = token
        print(f"  auth: bearer token attached ({len(token)} chars)")
    else:
        print("  auth: none (anonymous MCP)")

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=2048,
            betas=[MCP_BETA],
            mcp_servers=[server_entry],
            tools=[{"type": "mcp_toolset", "mcp_server_name": probe.name}],
            messages=[{"role": "user", "content": probe.probe}],
        )
    except anthropic.APIStatusError as e:
        print(f"  FAIL (API error): {type(e).__name__} status={e.status_code}")
        print(f"    message: {e.message}")
        body = getattr(e, "body", None)
        if body:
            print(f"    body: {body}")
        return False
    except anthropic.APIConnectionError as e:
        print(f"  FAIL (connection): {e}")
        return False
    except Exception as e:  # noqa: BLE001 — surface the raw failure
        print(f"  FAIL (unexpected): {type(e).__name__}: {e}")
        return False

    saw_tool_call = False
    saw_tool_error = False
    final_text_chunks: list[str] = []

    for block in response.content:
        btype = getattr(block, "type", None)

        if btype == "mcp_tool_use":
            saw_tool_call = True
            name = getattr(block, "name", "<unknown>")
            server = getattr(block, "server_name", "<unknown>")
            inp = getattr(block, "input", {})
            print(f"  tool call: {server}.{name}({inp!r})")

        elif btype == "mcp_tool_result":
            is_error = bool(getattr(block, "is_error", False))
            preview = _text_preview(getattr(block, "content", None))
            if is_error:
                saw_tool_error = True
                print(f"  TOOL ERROR: {preview}")
            else:
                print(f"  tool result OK ({len(preview)} chars): {preview[:200]}")

        elif btype == "text":
            final_text_chunks.append(getattr(block, "text", ""))

        else:
            print(f"  (block type={btype})")

    if final_text_chunks:
        summary = " ".join(final_text_chunks).strip()
        print(f"  model summary: {summary[:400]}")

    if not saw_tool_call:
        print("  FAIL: model did not call any MCP tool")
        print("    likely cause: tool discovery failed (auth, transport, or unreachable)")
        return False
    if saw_tool_error:
        print("  FAIL: at least one MCP tool call returned an error (see TOOL ERROR above)")
        return False

    print("  PASS")
    return True


def main() -> int:
    client = anthropic.Anthropic(api_key=API_KEY)

    results: dict[str, bool] = {}
    for probe in PROBES:
        try:
            results[probe.label] = verify_one(client, probe)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130

    print("\n=== Summary ===")
    width = max(len(label) for label in results)
    for label, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  {label.ljust(width)}  {status}")

    failed = [label for label, ok in results.items() if not ok]
    if failed:
        print(f"\n{len(failed)} of {len(results)} MCPs failed: {', '.join(failed)}")
        print("Surface the exact error text above to decide per-MCP fix.")
        return 1

    print(f"\nAll {len(results)} MCPs reachable and authenticated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
