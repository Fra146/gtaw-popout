"""FiveM Chrome DevTools Protocol (CDP) bridge using raw websockets and httpx."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any, Callable, Coroutine
from urllib.parse import quote, urlparse

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from models import NuiStateMessage

_CFX_RESOURCE_RE = re.compile(r"^https?://cfx-nui-([^/]+)")
_HIDE_STYLE_ID = "nui-hide-override"


class CDPError(RuntimeError):
    """Raised when a CDP command returns a protocol error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"CDP error {code}: {message}")
        self.code = code
        self.message = message


class CDPClient:
    """Minimal async Chrome DevTools Protocol client over raw websockets."""

    def __init__(
        self,
        ws_url: str,
        on_event: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.ws_url = ws_url
        self._on_event = on_event
        self._ws: ClientConnection | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._reader_task is not None and not self._reader_task.done()

    async def connect(self) -> None:
        """Open the WebSocket connection and start the background read loop."""
        self._ws = await websockets.connect(self.ws_url, max_size=None)
        self._reader_task = asyncio.create_task(self._read_loop(), name="cdp-read-loop")

    async def close(self) -> None:
        """Close the WebSocket connection and cancel pending requests."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self._reader_task = None

        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        self._fail_pending(ConnectionError("CDP client closed"))

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Send a CDP command and await its response."""
        if not self.is_connected or self._ws is None:
            raise ConnectionError("CDP client is not connected")

        self._next_id += 1
        req_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future

        payload = {"id": req_id, "method": method, "params": params or {}}
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout=timeout)
        except Exception:
            self._pending.pop(req_id, None)
            raise

    async def _read_loop(self) -> None:
        """Continuously read CDP responses and events from the WebSocket."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    req_id = msg["id"]
                    fut = self._pending.pop(req_id, None)
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"]
                            fut.set_exception(
                                CDPError(err.get("code", -1), err.get("message", "Unknown CDP error"))
                            )
                        else:
                            fut.set_result(msg.get("result", {}))
                elif "method" in msg and self._on_event is not None:
                    asyncio.create_task(self._on_event(msg["method"], msg.get("params", {})))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(ConnectionError(f"CDP read loop terminated: {exc}"))
        finally:
            self._fail_pending(ConnectionError("CDP WebSocket disconnected"))

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


class FiveMBridge:
    """Manages FiveM NUI inspection, DOM/canvas extraction, and input forwarding."""

    def __init__(
        self,
        cdp_url: str = "http://127.0.0.1:13172",
        command_cooldown: float = 5.0,
    ) -> None:
        self.cdp_url = cdp_url.rstrip("/")
        self.command_cooldown = command_cooldown
        self._client: CDPClient | None = None
        self._frames: dict[str, dict[str, Any]] = {}
        self._isolated_worlds: dict[str, int] = {}
        self._css_cache: dict[str, str] = {}
        self._asset_cache: dict[str, tuple[str, bytes]] = {}
        self._hidden_selectors: set[str] = set()
        self._last_command_at: float = 0.0
        self.loaded_resources: list[str] = []

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> bool:
        """Discover nui://game/ui/root.html and establish a CDP connection."""
        if self.connected:
            return True

        await self.disconnect()
        try:
            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.get(f"{self.cdp_url}/json")
                resp.raise_for_status()
                targets = resp.json()
        except Exception:
            return False

        root_target = next(
            (t for t in targets if t.get("url") == "nui://game/ui/root.html"),
            None,
        )
        if root_target is None or not root_target.get("webSocketDebuggerUrl"):
            return False

        client = CDPClient(root_target["webSocketDebuggerUrl"], on_event=self._handle_cdp_event)
        try:
            await client.connect()
            self._client = client
            await client.call("Page.enable")
            await self._refresh_frames()
            return True
        except Exception:
            await client.close()
            self._client = None
            return False

    async def disconnect(self) -> None:
        """Restore any hidden NUI elements, close CDP, and clear cached frame state."""
        if self.connected and self._hidden_selectors:
            try:
                await self.clear_all_hide_overrides()
            except Exception:
                pass
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._frames.clear()
        self._isolated_worlds.clear()
        self._hidden_selectors.clear()
        self.loaded_resources.clear()

    async def _handle_cdp_event(self, method: str, params: dict[str, Any]) -> None:
        """Invalidate cached isolated worlds on frame navigation, attachment, or detachment."""
        if method in ("Page.frameAttached", "Page.frameNavigated", "Page.frameDetached"):
            frame_id = params.get("frame", {}).get("id") or params.get("frameId")
            if frame_id:
                self._isolated_worlds.pop(frame_id, None)
                if method == "Page.frameDetached":
                    self._frames.pop(frame_id, None)
            await self._refresh_frames()

    async def _refresh_frames(self) -> None:
        """Walk Page.getFrameTree and index active cfx-nui-* frames."""
        if not self.connected or self._client is None:
            return

        result = await self._client.call("Page.getFrameTree")
        tree = result.get("frameTree", {})
        frames: dict[str, dict[str, Any]] = {}
        resources: list[str] = []

        def _walk(node: dict[str, Any]) -> None:
            frame = node.get("frame", {})
            fid = frame.get("id")
            url = frame.get("url", "")
            if fid and url.startswith(("https://cfx-nui-", "http://cfx-nui-")):
                match = _CFX_RESOURCE_RE.match(url)
                res_name = match.group(1) if match else urlparse(url).netloc
                frames[fid] = {"id": fid, "url": url, "name": frame.get("name", ""), "resource": res_name}
                if res_name and res_name not in resources:
                    resources.append(res_name)
            for child in node.get("childFrames", []):
                _walk(child)

        _walk(tree)
        self._frames = frames
        self.loaded_resources = resources
        stale = [fid for fid in self._isolated_worlds if fid not in frames]
        for fid in stale:
            self._isolated_worlds.pop(fid, None)

    async def _get_isolated_world(self, frame_id: str) -> int:
        """Return a cached isolated-world executionContextId for frame_id."""
        if frame_id in self._isolated_worlds:
            return self._isolated_worlds[frame_id]
        if not self.connected or self._client is None:
            raise ConnectionError("CDP client is not connected")

        res = await self._client.call(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": "tactical-hub",
                "grantUniveralAccess": True,
            },
        )
        ctx_id = res["executionContextId"]
        self._isolated_worlds[frame_id] = ctx_id
        return ctx_id

    async def _eval_in_frame(
        self,
        frame_id: str,
        expression: str,
        await_promise: bool = False,
    ) -> Any:
        """Evaluate JS inside a frame's isolated world, recreating stale contexts once."""
        for attempt in range(2):
            ctx_id = await self._get_isolated_world(frame_id)
            assert self._client is not None
            try:
                res = await self._client.call(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "contextId": ctx_id,
                        "returnByValue": True,
                        "awaitPromise": await_promise,
                    },
                )
            except CDPError as exc:
                if attempt == 0 and exc.code == -32000:
                    self._isolated_worlds.pop(frame_id, None)
                    continue
                raise

            if "exceptionDetails" in res:
                desc = res["exceptionDetails"].get("exception", {}).get("description", "JS exception")
                raise RuntimeError(desc)
            return res.get("result", {}).get("value")
        return None

    async def scan_active_elements(self) -> list[dict[str, Any]]:
        """Return visible candidate selectors (#id, .class) across active NUI iframes."""
        if not await self.connect():
            return []

        await self._refresh_frames()
        js = """(() => {
            const seen = new Set();
            const out = [];
            const els = document.querySelectorAll('[id], [class]');
            for (const el of els) {
                if (el.id === 'nui-hide-override') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 20 || rect.height < 20) continue;
                const style = getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                let selector = '';
                if (el.id) {
                    selector = '#' + el.id;
                } else if (typeof el.className === 'string' && el.className.trim()) {
                    const first = el.className.trim().split(/\\s+/)[0];
                    if (first) selector = '.' + first;
                }
                if (!selector || seen.has(selector)) continue;
                seen.add(selector);
                out.push({
                    selector,
                    tag: el.tagName.toLowerCase(),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                });
                if (out.length >= 60) break;
            }
            return out;
        })()"""

        results: list[dict[str, Any]] = []
        seen_global: set[str] = set()
        for fid, info in list(self._frames.items()):
            try:
                items = await self._eval_in_frame(fid, js) or []
            except Exception:
                continue
            for item in items:
                sel = item["selector"]
                if sel not in seen_global:
                    seen_global.add(sel)
                    item["frame_id"] = fid
                    item["resource"] = info["resource"]
                    results.append(item)
        return results

    async def extract_selector(self, selector: str, hide_in_game: bool = True) -> NuiStateMessage:
        """Extract cloned DOM, cached CSS, and WebP canvas snapshots for selector."""
        if not await self.connect():
            return NuiStateMessage(selector=selector, state="DISCONNECTED")

        if hide_in_game:
            self._hidden_selectors.add(selector)
        else:
            self._hidden_selectors.discard(selector)

        hide_css = "\n".join(
            f"{sel} {{ opacity: 0 !important; pointer-events: none !important; }}"
            for sel in sorted(self._hidden_selectors)
        )
        sel_json = json.dumps(selector)
        hide_css_json = json.dumps(hide_css)

        js = f"""(() => {{
            const selector = {sel_json};
            const hideCss = {hide_css_json};
            let styleEl = document.getElementById({_HIDE_STYLE_ID!r});
            if (hideCss) {{
                if (!styleEl) {{
                    styleEl = document.createElement('style');
                    styleEl.id = {_HIDE_STYLE_ID!r};
                    (document.head || document.documentElement).appendChild(styleEl);
                }}
                if (styleEl.textContent !== hideCss) {{
                    styleEl.textContent = hideCss;
                }}
            }} else if (styleEl) {{
                styleEl.remove();
            }}

            const el = document.querySelector(selector);
            if (!el) return {{ found: false }};

            const rect = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            if (
                rect.width <= 0
                || rect.height <= 0
                || cs.display === 'none'
                || cs.visibility === 'hidden'
                || el.classList.contains('hud-fade-hidden')
            ) {{
                return {{ found: false }};
            }}

            const clone = el.cloneNode(true);
            clone.setAttribute('data-tactical-target', '1');

            const rewriteAttr = (node, attr) => {{
                const raw = node.getAttribute(attr);
                if (!raw || /^(data:|blob:|#|\\/api\\/asset)/i.test(raw)) return;
                try {{
                    const abs = new URL(raw, document.baseURI).href;
                    if (/^https?:\\/\\/cfx-nui-/i.test(abs)) {{
                        node.setAttribute(attr, '/api/asset?url=' + encodeURIComponent(abs));
                    }}
                }} catch (_) {{}}
            }};
            for (const img of clone.querySelectorAll('[src]')) rewriteAttr(img, 'src');
            if (clone.hasAttribute('src')) rewriteAttr(clone, 'src');

            const origCanvases = el.querySelectorAll('canvas');
            const cloneCanvases = clone.querySelectorAll('canvas');
            const canvases = {{}};
            const count = Math.min(origCanvases.length, cloneCanvases.length);
            for (let i = 0; i < count; i++) {{
                const cid = 'c' + i;
                cloneCanvases[i].setAttribute('data-tactical-canvas-id', cid);
                try {{
                    canvases[cid] = origCanvases[i].toDataURL('image/png');
                }} catch (_) {{}}
            }}
            if (el.tagName === 'CANVAS') {{
                clone.setAttribute('data-tactical-canvas-id', 'root');
                try {{
                    canvases['root'] = el.toDataURL('image/png');
                }} catch (_) {{}}
            }}

            let html = clone.outerHTML;
            let anc = el.parentElement;
            while (anc && anc !== document.documentElement) {{
                const tag = anc === document.body ? 'div' : anc.tagName.toLowerCase();
                const idAttr = anc.id ? ' id="' + anc.id.replace(/"/g, '&quot;') + '"' : '';
                const cls = typeof anc.className === 'string' && anc.className.trim()
                    ? ' class="' + anc.className.trim().replace(/"/g, '&quot;') + '"'
                    : '';
                if (idAttr || cls) {{
                    html = '<' + tag + idAttr + cls + ' data-tactical-wrapper="1">' + html + '</' + tag + '>';
                }}
                anc = anc.parentElement;
            }}

            const sheetUrls = [];
            for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {{
                if (link.href) sheetUrls.push(link.href);
            }}
            const inlineStyles = [];
            for (const st of document.querySelectorAll('style')) {{
                if (st.id === {_HIDE_STYLE_ID!r}) continue;
                if (st.textContent) inlineStyles.push(st.textContent);
            }}

            return {{
                found: true,
                html,
                sheetUrls,
                inlineStyles,
                canvases,
            }};
        }})()"""

        for fid in list(self._frames.keys()):
            try:
                data = await self._eval_in_frame(fid, js)
            except Exception:
                continue
            if not data or not data.get("found"):
                continue

            styles = await self._resolve_styles(fid, data.get("sheetUrls", []), data.get("inlineStyles", []))
            return NuiStateMessage(
                selector=selector,
                state="ACTIVE",
                html=data.get("html"),
                styles=styles,
                canvases=data.get("canvases", {}),
                timestamp=time.time(),
            )

        return NuiStateMessage(selector=selector, state="STANDBY", timestamp=time.time())

    async def _resolve_styles(
        self,
        frame_id: str,
        sheet_urls: list[str],
        inline_styles: list[str],
    ) -> list[str]:
        """Fetch and cache external stylesheets (rewriting relative url() paths)."""
        resolved: list[str] = []
        for url in sheet_urls:
            if url not in self._css_cache:
                url_json = json.dumps(url)
                fetch_js = f"""(async () => {{
                    const baseUrl = {url_json};
                    const resp = await fetch(baseUrl);
                    const css = await resp.text();
                    return css.replace(/url\\(\\s*(['"]?)([^)'"]+)\\1\\s*\\)/gi, (m, q, raw) => {{
                        if (/^(data:|blob:|#)/i.test(raw)) return m;
                        try {{
                            const abs = new URL(raw, baseUrl).href;
                            if (/^https?:\\/\\/cfx-nui-/i.test(abs)) {{
                                return 'url("/api/asset?url=' + encodeURIComponent(abs) + '")';
                            }}
                            return 'url("' + abs + '")';
                        }} catch (_) {{
                            return m;
                        }}
                    }});
                }})()"""
                try:
                    css_text = await self._eval_in_frame(frame_id, fetch_js, await_promise=True)
                    if isinstance(css_text, str):
                        self._css_cache[url] = css_text
                except Exception:
                    continue
            if url in self._css_cache:
                resolved.append(self._css_cache[url])

        resolved.extend(inline_styles)
        return resolved

    async def fetch_asset(self, url: str) -> tuple[str, bytes] | None:
        """Fetch and cache an internal cfx-nui-* asset via CDP or isolated world."""
        if not url.startswith(("https://cfx-nui-", "http://cfx-nui-")):
            return None
        if url in self._asset_cache:
            return self._asset_cache[url]
        if not await self.connect():
            return None

        match = _CFX_RESOURCE_RE.match(url)
        target_res = match.group(1) if match else None
        ordered_fids = sorted(
            self._frames.keys(),
            key=lambda fid: 0 if self._frames[fid].get("resource") == target_res else 1,
        )

        # 1. Try CDP Page.getResourceContent (works for cached images/fonts in the frame)
        if self._client is not None:
            for fid in ordered_fids:
                try:
                    res = await self._client.call(
                        "Page.getResourceContent",
                        {"frameId": fid, "url": url},
                    )
                    content = res.get("content")
                    if content is not None:
                        raw = (
                            base64.b64decode(content)
                            if res.get("base64Encoded")
                            else content.encode("utf-8")
                        )
                        ct = self._guess_mime(url)
                        entry = (ct, raw)
                        self._asset_cache[url] = entry
                        return entry
                except Exception:
                    continue

        # 2. Fallback to fetch() + Image()->canvas in isolated world
        url_json = json.dumps(url)
        js = f"""(async () => {{
            const url = {url_json};
            try {{
                const resp = await fetch(url);
                if (resp.ok) {{
                    const blob = await resp.blob();
                    const dataUrl = await new Promise((resolve, reject) => {{
                        const fr = new FileReader();
                        fr.onload = () => resolve(fr.result);
                        fr.onerror = reject;
                        fr.readAsDataURL(blob);
                    }});
                    const comma = dataUrl.indexOf(',');
                    return {{
                        ct: blob.type || resp.headers.get('content-type') || 'application/octet-stream',
                        b64: comma !== -1 ? dataUrl.slice(comma + 1) : '',
                    }};
                }}
            }} catch (_) {{}}

            if (/\\.(png|jpe?g|webp|gif|bmp|svg)(\\?.*)?$/i.test(url)) {{
                try {{
                    const img = new Image();
                    img.crossOrigin = 'anonymous';
                    img.src = url;
                    await new Promise((resolve, reject) => {{
                        img.onload = resolve;
                        img.onerror = reject;
                    }});
                    const c = document.createElement('canvas');
                    c.width = img.naturalWidth;
                    c.height = img.naturalHeight;
                    c.getContext('2d').drawImage(img, 0, 0);
                    const dataUrl = c.toDataURL('image/png');
                    return {{ ct: 'image/png', b64: dataUrl.split(',')[1] }};
                }} catch (_) {{}}
            }}
            return null;
        }})()"""

        for fid in ordered_fids:
            try:
                res = await self._eval_in_frame(fid, js, await_promise=True)
                if res and res.get("b64"):
                    raw = base64.b64decode(res["b64"])
                    entry = (res["ct"], raw)
                    self._asset_cache[url] = entry
                    return entry
            except Exception:
                continue
        return None

    @staticmethod
    def _guess_mime(url: str) -> str:
        lower = url.split("?", 1)[0].lower()
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
        if lower.endswith(".webp"):
            return "image/webp"
        if lower.endswith(".svg"):
            return "image/svg+xml"
        if lower.endswith(".woff2"):
            return "font/woff2"
        if lower.endswith(".woff"):
            return "font/woff"
        if lower.endswith(".ttf"):
            return "font/ttf"
        if lower.endswith(".otf"):
            return "font/otf"
        if lower.endswith(".css"):
            return "text/css"
        return "application/octet-stream"

    async def clear_hide_override(self, selector: str) -> None:
        """Remove selector from #nui-hide-override and delete the tag if empty."""
        self._hidden_selectors.discard(selector)
        if not self.connected:
            return

        hide_css = "\n".join(
            f"{sel} {{ opacity: 0 !important; pointer-events: none !important; }}"
            for sel in sorted(self._hidden_selectors)
        )
        hide_css_json = json.dumps(hide_css)
        js = f"""(() => {{
            const styleEl = document.getElementById({_HIDE_STYLE_ID!r});
            if (!styleEl) return;
            const css = {hide_css_json};
            if (!css) {{
                styleEl.remove();
            }} else {{
                styleEl.textContent = css;
            }}
        }})()"""

        for fid in list(self._frames.keys()):
            try:
                await self._eval_in_frame(fid, js)
            except Exception:
                continue

    async def clear_all_hide_overrides(self) -> None:
        """Remove #nui-hide-override from every frame and reset hidden selectors."""
        self._hidden_selectors.clear()
        if not self.connected:
            return

        js = f"document.getElementById({_HIDE_STYLE_ID!r})?.remove()"
        for fid in list(self._frames.keys()):
            try:
                await self._eval_in_frame(fid, js)
            except Exception:
                continue

    async def forward_click(self, selector: str, child_path: str | None = None) -> bool:
        """Forward a click event to selector (or a descendant child_path) in FiveM."""
        if not self.connected:
            return False

        sel_json = json.dumps(selector)
        child_json = json.dumps(child_path)
        js = f"""(() => {{
            const root = document.querySelector({sel_json});
            if (!root) return false;
            const childPath = {child_json};
            const target = childPath ? root.querySelector(childPath) : root;
            if (!target) return false;
            for (const type of ['mousedown', 'mouseup']) {{
                target.dispatchEvent(new MouseEvent(type, {{
                    bubbles: true,
                    cancelable: true,
                    view: window,
                }}));
            }}
            if (typeof target.click === 'function') {{
                target.click();
            }} else {{
                target.dispatchEvent(new MouseEvent('click', {{
                    bubbles: true,
                    cancelable: true,
                    view: window,
                }}));
            }}
            return true;
        }})()"""

        for fid in list(self._frames.keys()):
            try:
                if await self._eval_in_frame(fid, js):
                    return True
            except Exception:
                continue
        return False

    async def trigger_command(self, command: str) -> dict[str, Any]:
        """Dispatch a FiveM chat command once per explicit user click (5s cooldown)."""
        cmd = command.strip()
        if not cmd:
            return {"ok": False, "reason": "empty_command"}

        now = time.monotonic()
        elapsed = now - self._last_command_at
        if self._last_command_at > 0 and elapsed < self.command_cooldown:
            return {
                "ok": False,
                "reason": "cooldown",
                "retry_after": round(self.command_cooldown - elapsed, 2),
            }

        if not await self.connect() or self._client is None:
            return {"ok": False, "reason": "disconnected"}

        self._last_command_at = now
        try:
            client_fid = next(
                (fid for fid, info in self._frames.items() if info.get("resource") == "client"),
                None,
            )
            if not client_fid:
                return {"ok": False, "reason": "client_frame_not_found"}

            payload = json.dumps(
                {
                    "canceled": False,
                    "message": cmd,
                    "isCommand": cmd.startswith("/"),
                    "mode": "_global",
                }
            )
            js = f"""(async () => {{
                const resp = await fetch('https://client/chatResult', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: {json.dumps(payload)},
                }});
                return resp.status;
            }})()"""
            status = await self._eval_in_frame(client_fid, js, await_promise=True)
            return {"ok": status == 200, "command": cmd, "status": status}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
