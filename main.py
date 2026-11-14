"""FastAPI server and adaptive NUI/comms streaming loop."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from bridges.radio import RadioBridge
from models import (
    CommsConnectRequest,
    CommsStateMessage,
    LaunchRequest,
    VisibilityRequest,
    WidgetPreset,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "presets.json"
STATIC_DIR = BASE_DIR / "static"


def load_presets() -> list[WidgetPreset]:
    """Load widget presets from config/presets.json."""
    if not CONFIG_PATH.exists():
        return []
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return [WidgetPreset.model_validate(item) for item in raw]


presets: list[WidgetPreset] = load_presets()
bridge = RadioBridge()

nui_subscribers: dict[str, set[WebSocket]] = {}
comms_subscribers: set[WebSocket] = set()
els_subscribers: set[WebSocket] = set()
radio_subscribers: set[WebSocket] = set()
selector_hide_in_game: dict[str, bool] = {p.selector: p.hide_in_game for p in presets}
selector_states: dict[str, str] = {}
selector_last_poll: dict[str, float] = {}
els_last_payload: str = ""
radio_last_payload: str = ""


async def _remove_nui_subscriber(selector: str, ws: WebSocket) -> None:
    """Remove a WebSocket subscriber and restore in-game UI when count hits 0."""
    subs = nui_subscribers.get(selector)
    if subs is not None:
        subs.discard(ws)
        if not subs:
            nui_subscribers.pop(selector, None)
            selector_states.pop(selector, None)
            selector_last_poll.pop(selector, None)
            await bridge.clear_hide_override(selector)


async def _nui_stream_loop() -> None:
    """Adaptive background loop: 0.5s idle, 300ms STANDBY, 33ms ACTIVE."""
    while True:
        if not nui_subscribers:
            if bridge._hidden_selectors:
                await bridge.clear_all_hide_overrides()
            await asyncio.sleep(0.5)
            continue

        now = time.monotonic()
        for selector in list(nui_subscribers.keys()):
            subs = nui_subscribers.get(selector)
            if not subs:
                continue

            state = selector_states.get(selector, "STANDBY")
            interval = 0.033 if state == "ACTIVE" else 0.300
            if now - selector_last_poll.get(selector, 0.0) < interval:
                continue

            selector_last_poll[selector] = now
            hide = selector_hide_in_game.get(selector, True)
            msg = await bridge.extract_selector(selector, hide_in_game=hide)
            if not nui_subscribers.get(selector):
                await bridge.clear_hide_override(selector)
                continue
            selector_states[selector] = msg.state
            payload = msg.model_dump_json()

            dead: list[WebSocket] = []
            for ws in list(subs):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                await _remove_nui_subscriber(selector, ws)

        await asyncio.sleep(0.015)


async def _els_stream_loop() -> None:
    """Poll NUI::Speedometer::State at 100ms when ELS panel subscribers are connected."""
    global els_last_payload
    while True:
        if not els_subscribers:
            await asyncio.sleep(0.5)
            continue

        state = await bridge.poll_els_state()
        key_state = {
            "connected": state["connected"],
            "elsVisible": state["elsVisible"],
            "elsCode": state["elsCode"],
            "elsTone": state["elsTone"],
        }
        payload = json.dumps(key_state)
        if payload != els_last_payload:
            els_last_payload = payload
            dead: list[WebSocket] = []
            for ws in list(els_subscribers):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                els_subscribers.discard(ws)

        await asyncio.sleep(0.1)


def _radio_payload(state: dict) -> str:
    return json.dumps(
        {
            "connected": state["connected"],
            "radioOn": state["radioOn"],
            "primarySlot": state["primarySlot"],
            "atcOn": state["atcOn"],
            "vtsOn": state["vtsOn"],
            "departmentalOn": state["departmentalOn"],
            "activeDep": state.get("activeDep", ""),
            "slots": state["slots"],
        }
    )


async def _radio_stream_loop() -> None:
    """Poll NUI::MinimapBar::ChatRadio at 200ms when subscribed (or 2s idle to keep listener armed)."""
    global radio_last_payload
    while True:
        state = await bridge.poll_radio_state()
        if not radio_subscribers:
            await asyncio.sleep(2.0)
            continue

        payload = _radio_payload(state)
        if payload != radio_last_payload:
            radio_last_payload = payload
            dead: list[WebSocket] = []
            for ws in list(radio_subscribers):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                radio_subscribers.discard(ws)

        await asyncio.sleep(0.2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage background NUI/ELS/Radio stream loops and CDP bridge lifecycle."""
    task = asyncio.create_task(_nui_stream_loop(), name="nui-stream-loop")
    els_task = asyncio.create_task(_els_stream_loop(), name="els-stream-loop")
    radio_task = asyncio.create_task(_radio_stream_loop(), name="radio-stream-loop")
    try:
        yield
    finally:
        for t in (task, els_task, radio_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        await bridge.disconnect()


app = FastAPI(title="Second-Monitor Tactical Hub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def get_dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/popout")
async def get_popout() -> FileResponse:
    return FileResponse(STATIC_DIR / "popout.html")


@app.get("/comms")
async def get_comms() -> FileResponse:
    return FileResponse(STATIC_DIR / "comms.html")


@app.get("/els")
async def get_els() -> FileResponse:
    return FileResponse(STATIC_DIR / "els.html")


@app.get("/radio")
async def get_radio() -> FileResponse:
    return FileResponse(STATIC_DIR / "radio.html")


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    live_presets = [
        {
            **p.model_dump(),
            "hide_in_game": selector_hide_in_game.get(p.selector, p.hide_in_game),
        }
        for p in presets
    ]
    return {
        "bridge_connected": bridge.connected,
        "resources": bridge.loaded_resources,
        "presets": live_presets,
        "active_subscribers": {sel: len(subs) for sel, subs in nui_subscribers.items()},
        "selector_states": {
            sel: st for sel, st in selector_states.items() if sel in nui_subscribers
        },
    }


@app.get("/api/scan")
async def api_scan() -> dict[str, Any]:
    elements = await bridge.scan_active_elements()
    return {"elements": elements}


@app.get("/api/asset")
async def api_asset(url: str = Query(...)) -> Response:
    asset = await bridge.fetch_asset(url)
    if asset is None:
        return Response(status_code=404)
    content_type, raw = asset
    return Response(
        content=raw,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/api/launch")
async def api_launch(req: LaunchRequest) -> dict[str, Any]:
    command = req.command
    if not command:
        preset = next((p for p in presets if p.selector == req.selector), None)
        command = preset.open_command if preset else None
    if not command:
        return {"ok": False, "reason": "no_command_configured"}
    return await bridge.trigger_command(command)


@app.post("/api/visibility")
async def api_visibility(req: VisibilityRequest) -> dict[str, Any]:
    selector_hide_in_game[req.selector] = req.hide_in_game
    if not req.hide_in_game:
        await bridge.clear_hide_override(req.selector)
    return {"ok": True, "selector": req.selector, "hide_in_game": req.hide_in_game}


@app.post("/api/comms/connect")
async def api_comms_connect(req: CommsConnectRequest) -> dict[str, Any]:
    return {
        "ok": False,
        "source": req.source,
        "reason": "comms_bridges_not_enabled",
    }


@app.websocket("/ws/nui")
async def ws_nui(
    websocket: WebSocket,
    selector: str = Query(...),
    hide: bool = Query(True),
) -> None:
    await websocket.accept()
    nui_subscribers.setdefault(selector, set()).add(websocket)
    selector_hide_in_game[selector] = hide
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "click":
                await bridge.forward_click(selector, data.get("child_path"))
    except WebSocketDisconnect:
        pass
    finally:
        await _remove_nui_subscriber(selector, websocket)


@app.websocket("/ws/comms")
async def ws_comms(websocket: WebSocket) -> None:
    await websocket.accept()
    comms_subscribers.add(websocket)
    try:
        initial = CommsStateMessage(
            source="teamspeak",
            connected=False,
            error="Comms bridges not enabled",
        )
        await websocket.send_text(initial.model_dump_json())
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        comms_subscribers.discard(websocket)


@app.websocket("/ws/els")
async def ws_els(websocket: WebSocket) -> None:
    await websocket.accept()
    els_subscribers.add(websocket)
    try:
        state = await bridge.poll_els_state()
        await websocket.send_text(
            json.dumps(
                {
                    "connected": state["connected"],
                    "elsVisible": state["elsVisible"],
                    "elsCode": state["elsCode"],
                    "elsTone": state["elsTone"],
                }
            )
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        els_subscribers.discard(websocket)


@app.websocket("/ws/radio")
async def ws_radio(websocket: WebSocket) -> None:
    global radio_last_payload
    await websocket.accept()
    radio_subscribers.add(websocket)
    try:
        state = await bridge.sync_radio_state(force=False)
        await websocket.send_text(_radio_payload(state))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "sync":
                synced = await bridge.sync_radio_state(force=True)
                radio_last_payload = ""
                await websocket.send_text(_radio_payload(synced))
            elif mtype == "setSlot" and msg.get("slot") is not None:
                slot = int(msg["slot"])
                res = await bridge.set_radio_slot(slot)
                await websocket.send_text(json.dumps({"type": "setSlotResult", "slot": slot, **res}))
                # Force refresh and broadcast updated state on next poll
                radio_last_payload = ""
    except WebSocketDisconnect:
        pass
    finally:
        radio_subscribers.discard(websocket)
