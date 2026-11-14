"""Send chat messages or slash commands to the GTAW chat window via FiveM CDP."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridges.fivem_cdp import CDPClient


async def get_root_ws_url(cdp_url: str) -> str:
    async with httpx.AsyncClient(timeout=3.0) as http:
        resp = await http.get(f"{cdp_url.rstrip('/')}/json")
        resp.raise_for_status()
        targets = resp.json()
    root = next((t for t in targets if t.get("url") == "nui://game/ui/root.html"), None)
    if not root or not root.get("webSocketDebuggerUrl"):
        raise RuntimeError("Could not find nui://game/ui/root.html on CDP")
    return root["webSocketDebuggerUrl"]


async def _get_client_ctx(client: CDPClient) -> int:
    """Get or create an isolated world context for cfx-nui-client."""
    tree = (await client.call("Page.getFrameTree")).get("frameTree", {})
    client_frame = None

    def walk(node: dict) -> None:
        nonlocal client_frame
        frame = node.get("frame", {})
        if "cfx-nui-client" in frame.get("url", ""):
            client_frame = frame
            return
        for ch in node.get("childFrames", []):
            walk(ch)

    walk(tree)
    if not client_frame:
        raise RuntimeError("cfx-nui-client frame not found")

    iw = await client.call(
        "Page.createIsolatedWorld",
        {
            "frameId": client_frame["id"],
            "worldName": "tactical-chat",
            "grantUniveralAccess": True,
        },
    )
    return iw["executionContextId"]


async def send_chat(client: CDPClient, text: str, open_delay: float = 0.15) -> None:
    """Trigger ON_OPEN in cfx-nui-client, focus .chat__input, insert text, and press Enter."""
    ctx_id = await _get_client_ctx(client)
    await client.call(
        "Runtime.evaluate",
        {
            "expression": (
                'window.postMessage({type: "ON_OPEN", design: "v2"}, "*");'
                'window.postMessage({type: "ON_CHAT_CURSOR", shown: true}, "*");'
            ),
            "contextId": ctx_id,
            "returnByValue": True,
        },
    )
    await asyncio.sleep(open_delay)
    await client.call(
        "Runtime.evaluate",
        {
            "expression": 'document.querySelector(".chat__input")?.focus()',
            "contextId": ctx_id,
            "returnByValue": True,
        },
    )
    await asyncio.sleep(0.05)
    await client.call("Input.insertText", {"text": text})
    await asyncio.sleep(0.05)
    await client.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
    )
    await client.call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
    )


async def send_nui_post(client: CDPClient, endpoint: str, body_json: str) -> str:
    """Invoke a direct NUI fetch() callback inside the cfx-nui-client isolated world."""
    ctx_id = await _get_client_ctx(client)
    js = f"""(async () => {{
        const resp = await fetch({json.dumps(endpoint)}, {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: {json.dumps(body_json)},
        }});
        return resp.status + ' ' + await resp.text();
    }})()"""
    res = await client.call(
        "Runtime.evaluate",
        {"expression": js, "contextId": ctx_id, "returnByValue": True, "awaitPromise": True},
    )
    return str(res.get("result", {}).get("value"))


async def send_chat_nui(client: CDPClient, text: str) -> str:
    """Submit a chat message or /command directly via GTAW's https://client/chatResult callback."""
    payload = json.dumps(
        {
            "canceled": False,
            "message": text,
            "isCommand": text.startswith("/"),
            "mode": "_global",
        }
    )
    return await send_nui_post(client, "https://client/chatResult", payload)


async def run_chat(
    cdp_url: str,
    message: str | None,
    nui_post: str | None,
    nui_body: str,
    use_ui: bool,
    dry_run: bool,
) -> None:
    ws_url = await get_root_ws_url(cdp_url)
    client = CDPClient(ws_url)
    await client.connect()
    try:
        if dry_run:
            tree = (await client.call("Page.getFrameTree")).get("frameTree", {})
            print("Connected to root UI:", tree.get("frame", {}).get("url"))
            return

        if nui_post:
            out = await send_nui_post(client, nui_post, nui_body)
            print("NUI POST response:", out)
            return

        async def _dispatch(txt: str) -> None:
            if use_ui:
                await send_chat(client, txt)
                print(f"Sent via chat UI: {txt}")
            else:
                out = await send_chat_nui(client, txt)
                print(f"Sent via chatResult ({out}): {txt}")

        if message:
            await _dispatch(message)
            return

        print("Interactive GTAW chat (Ctrl+C or empty line to exit):")
        while True:
            try:
                line = input("chat> ").strip()
            except EOFError:
                break
            if not line:
                break
            await _dispatch(line)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send chat/commands to GTAW via FiveM CDP.")
    parser.add_argument("message", nargs="?", default=None, help="Chat message or /command")
    parser.add_argument("--cdp", default="http://127.0.0.1:13172", help="CDP HTTP base URL")
    parser.add_argument("--nui-post", default=None, help="Optional NUI callback URL to POST directly")
    parser.add_argument("--nui-body", default="{}", help="JSON body for --nui-post")
    parser.add_argument("--ui", action="store_true", help="Send via visual chat UI (ON_OPEN) instead of chatResult")
    parser.add_argument("--dry-run", action="store_true", help="Verify CDP connection without sending")
    args = parser.parse_args()

    try:
        asyncio.run(run_chat(args.cdp, args.message, args.nui_post, args.nui_body, args.ui, args.dry_run))
    except KeyboardInterrupt:
        print("\nExited.")


if __name__ == "__main__":
    main()
