"""Listen to live FiveM NUI incoming messages and outgoing NUI fetch callbacks."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridges.fivem_cdp import CDPClient


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def get_root_ws_url(cdp_url: str) -> str:
    async with httpx.AsyncClient(timeout=3.0) as http:
        resp = await http.get(f"{cdp_url.rstrip('/')}/json")
        resp.raise_for_status()
        targets = resp.json()
    root = next((t for t in targets if t.get("url") == "nui://game/ui/root.html"), None)
    if not root or not root.get("webSocketDebuggerUrl"):
        raise RuntimeError("Could not find nui://game/ui/root.html on CDP")
    return root["webSocketDebuggerUrl"]


async def run_listener(cdp_url: str, filter_text: str | None) -> None:
    ws_url = await get_root_ws_url(cdp_url)
    needle = filter_text.lower() if filter_text else None

    def matches(text: str) -> bool:
        return True if needle is None else needle in text.lower()

    async def on_event(method: str, params: dict[str, Any]) -> None:
        if method == "Network.requestWillBeSent":
            req = params.get("request", {})
            url = req.get("url", "")
            req_method = req.get("method", "")
            post_data = req.get("postData", "")
            if req_method == "POST" or "cfx-nui-" in url or "https://client/" in url:
                line = f"{req_method} {url} {post_data}".strip()
                if matches(line):
                    print(f"[{_ts()}] [NUI_FETCH] {req_method} {url}")
                    if post_data:
                        print(f"    body: {post_data}")

    client = CDPClient(ws_url, on_event=on_event)
    await client.connect()
    try:
        await client.call("Page.enable")
        await client.call("Network.enable")

        tree = (await client.call("Page.getFrameTree")).get("frameTree", {})
        contexts: list[tuple[str, str, int]] = []

        def walk(node: dict[str, Any]) -> None:
            frame = node.get("frame", {})
            fid = frame.get("id")
            url = frame.get("url", "")
            if fid and "cfx-nui-" in url:
                contexts.append((fid, url, 0))
            for ch in node.get("childFrames", []):
                walk(ch)

        walk(tree)

        hook_js = """(() => {
            if (!window.__tacticalEventHooked) {
                window.__tacticalEventHooked = true;
                window.__tacticalEventQueue = [];
                window.addEventListener('message', (ev) => {
                    try {
                        window.__tacticalEventQueue.push(JSON.stringify(ev.data));
                        if (window.__tacticalEventQueue.length > 200) {
                            window.__tacticalEventQueue.shift();
                        }
                    } catch (_) {}
                });
            }
            return true;
        })()"""

        active_contexts: list[tuple[str, str, int]] = []
        for fid, url, _ in contexts:
            iw = await client.call(
                "Page.createIsolatedWorld",
                {"frameId": fid, "worldName": "tactical-events", "grantUniveralAccess": True},
            )
            ctx_id = iw["executionContextId"]
            await client.call(
                "Runtime.evaluate",
                {"expression": hook_js, "contextId": ctx_id, "returnByValue": True},
            )
            active_contexts.append((fid, url, ctx_id))
            print(f"[{_ts()}] Listening on frame: {url}")

        print(f"[{_ts()}] Event listener active (Ctrl+C to stop)...")
        poll_js = "(() => (window.__tacticalEventQueue || []).splice(0))()"
        while True:
            for fid, url, ctx_id in active_contexts:
                try:
                    res = await client.call(
                        "Runtime.evaluate",
                        {"expression": poll_js, "contextId": ctx_id, "returnByValue": True},
                    )
                    items = res.get("result", {}).get("value") or []
                    for raw in items:
                        if matches(raw):
                            print(f"[{_ts()}] [NUI_MSG] ({url}) {raw}")
                except Exception:
                    pass
            await asyncio.sleep(0.1)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Listen to FiveM NUI messages and fetch callbacks.")
    parser.add_argument("filter_pos", nargs="?", default=None, help="Case-insensitive substring filter")
    parser.add_argument("--cdp", default="http://127.0.0.1:13172", help="CDP HTTP base URL")
    parser.add_argument("--filter", default=None, help="Case-insensitive substring filter")
    args = parser.parse_args()

    filter_text = args.filter_pos if args.filter_pos is not None else args.filter
    try:
        asyncio.run(run_listener(args.cdp, filter_text))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
