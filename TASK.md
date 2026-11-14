# TASK.md — Second-Monitor Tactical Hub (FiveM NUI + Voice Comms)

## 1. Objective
Build a modular, local-first Python (`FastAPI` + `asyncio`) web application that mirrors specific FiveM NUI elements (ATC Radar, ALPR, Speedometer, etc.) into independent borderless popout windows on a secondary monitor via Chrome DevTools Protocol (`127.0.0.1:13172`), alongside live voice-activity overlays for TeamSpeak 3 and Discord.

---

## 2. Strict Agent Rules (MUST FOLLOW)
1. **Incremental, File-by-File Execution:** Complete and verify one phase at a time. Never dump the entire application in a single file or single turn.
2. **No Inline HTML/CSS/JS Strings in Python:** All frontend code MUST reside in `static/*.html`. Python routes only return `FileResponse` or JSON.
3. **Zero Third-Party CDP Wrappers:** Do NOT install `playwright`, `selenium`, or `pychrome`. Use raw `websockets` (`max_size=None`) + `httpx` for `127.0.0.1:13172`.
4. **Isolated Worlds Only:** Never inject scripts into a frame's main execution context. Always create and cache `Page.createIsolatedWorld` contexts per `frameId`.
5. **No Command Spam:** Never auto-send chat commands in a polling loop when an element disappears. Unmounted elements enter `STANDBY`; commands are only dispatched once per explicit user click on the `Launch` button (with a 5s cooldown).
6. **Clean In-Game Restoration:** When the last WebSocket subscriber for a selector disconnects, immediately remove `#nui-hide-override` from the FiveM iframe so the UI reappears in-game.

---

## 3. Target Directory Structure
```text
tactical-hub/
├── bridges/
│   ├── __init__.py
│   ├── fivem_cdp.py      # CDP transport, isolated worlds, DOM/WebP extraction, hide CSS, click/command forwarding
│   ├── teamspeak.py      # Async TCP ClientQuery client (127.0.0.1:25639)
│   └── discord_rpc.py    # Cross-platform Discord IPC pipe client (<II # ## ### & (`radar` * **Deliverables:** **Tasks:** - --- 1. 1: 2. 4. Borderless Built-in Config Create Environment, FastAPI Implementation Initialize Launch Launcher NUI Phase Phases Pydantic REST STANDBY Schemas TeamSpeak/Discord WS `.venv` ``` `config/presets.json` `config/presets.json`, `fastapi`, `httpx`, `models.py`. `pydantic`. `requirements.txt`, `uvicorn[standard]`, `websockets`, adaptive and app, badges, bridge button, click comms.html config/ dashboard.html definitions fastapi, for forwarder framing) httpx, hub, initial install lifespan live loop, main.py models.py overlay overlay, payloads, popout popout.html presets presets, presets.json pydantic renderer, requests requirements.txt routes scanner, schemas spawners speaker static/ status stream struct uvicorn[standard], websockets, widget with │ └── ├──> `#bezel`, `speedo` -> `.spd-root`, `alpr` -> `#alpr-container`), each specifying `id`, `name`, `selector`, `mode` (`fill` | `natural`), `open_command` (`str | null`), `hide_in_game` (`bool`), and `default_size` (`[w, h]`).
  3. Define Pydantic models in `models.py` for `WidgetPreset`, `NuiStateMessage` (`state`: `ACTIVE` | `STANDBY` | `DISCONNECTED`), `CommsStateMessage`, and REST payloads (`LaunchRequest`, `VisibilityRequest`, `CommsConnectRequest`).

### Phase 2: FiveM CDP Bridge (`bridges/fivem_cdp.py`)
* **Deliverables:** `bridges/__init__.py`, `bridges/fivem_cdp.py`.
* **Tasks:**
  1. Implement `CDPClient` using `websockets.connect(..., max_size=None)` with incrementing request IDs, `pending: dict[int, asyncio.Future]`, and a background `_read_loop()`.
  2. Implement `FiveMBridge`:
     * Discover `nui://game/ui/root.html` via `GET http://127.0.0.1:13172/json`.
     * Walk `Page.getFrameTree`, report loaded `cfx-nui-*` resource names for server verification, and cache `Page.createIsolatedWorld` `executionContextId`s.
     * `scan_active_elements()`: return visible candidate selectors (`#id`, `.class`) across active iframes.
     * `extract_selector(selector, hide_in_game)`: inject/remove `<style id="nui-hide-override">{selector} { opacity: 0 !important; pointer-events: none !important; }</style>`, cache fetched CSS (rewriting relative `url(...)` paths), clone DOM, serialize `<canvas>` nodes to WebP data URLs, and return `ACTIVE` or `STANDBY` state.
     * `clear_hide_override(selector)`: remove `#nui-hide-override` when popouts close.
     * `forward_click(selector, child_path)` and debounced `trigger_command(command)`.

### Phase 3: Voice Comms Bridges (`bridges/teamspeak.py` & `bridges/discord_rpc.py`)
* **Deliverables:** `bridges/teamspeak.py`, `bridges/discord_rpc.py`.
* **Tasks:**
  1. `TeamSpeakBridge`: `asyncio.open_connection('127.0.0.1', 25639)`, send `auth apikey=...`, subscribe to `notifytalkstatuschange` & `notifyclientmoved`, parse `clientlist`, and emit normalized `CommsStateMessage` updates on change.
  2. `DiscordBridge`: Connect to `\\.\pipe\discord-ipc-0` (Windows) or `$XDG_RUNTIME_DIR/discord-ipc-0` (Linux) using `<II` little-endian header framing (`handshake`, `SUBSCRIBE` to `VOICE_STATE_UPDATE`, `SPEAKING_START`, `SPEAKING_STOP`) and emit normalized speaker updates.

### Phase 4: FastAPI Server & Adaptive Stream Loop (`main.py`)
* **Deliverables:** `main.py`.
* **Tasks:**
  1. Manage active WebSocket subscribers per selector (`dict[str, set[WebSocket]]`) and comms subscribers (`set[WebSocket]`).
  2. Run a single background `asyncio` task in `lifespan`:
     * Sleep `0.5s` when no popout subscribers are connected.
     * Poll `STANDBY` selectors at `300ms` (~3 Hz).
     * Extract and broadcast `ACTIVE` selectors at `33ms` (~30 FPS).
  3. Implement routes:
     * **Pages:** `GET /` (`dashboard.html`), `GET /popout` (`popout.html`), `GET /comms` (`comms.html`).
     * **REST:** `GET /api/status`, `GET /api/scan`, `POST /api/launch`, `POST /api/visibility`, `POST /api/comms/connect`.
     * **WebSockets:** `WS /ws/nui` (with `finally:` cleanup calling `clear_hide_override` when subscriber count hits `0`) and `WS /ws/comms`.

### Phase 5: Frontend Views (`static/*.html`)
* **Deliverables:** `static/dashboard.html`, `static/popout.html`, `static/comms.html`.
* **Tasks:**
  1. `dashboard.html`: Render preset cards from `/api/status`, live bridge/server-match indicators, custom selector launcher, `/api/scan` active UI list, and `window.open('/popout?...', ..., 'popup=yes,...')` buttons.
  2. `popout.html`: Read `selector`, `mode`, `hide`, and `can_launch` from URL query params; connect to `/ws/nui`; diff `styles` and `html` before mutating DOM; draw WebP canvases via `createImageBitmap`; forward clicks; display a clean `STANDBY` overlay with a manual **Launch in Game** button when unmounted.
  3. `comms.html`: Connect to `/ws/comms` and render a compact, borderless speaker list highlighting active talkers.

---

## 5. Verification Checklist
- [ ] `uvicorn main:app --host 0.0.0.0 --port 8080` starts cleanly with 0% idle CPU when no popouts are open.
- [ ] Opening `/popout?selector=%23bezel&mode=fill&hide=true` hides `#bezel` in FiveM (`opacity: 0; pointer-events: none;`) and streams live DOM + `<canvas>` frames at ~30 FPS.
- [ ] Unmounting the NUI element in-game transitions the popout to `STANDBY` without spamming commands; closing the popout window immediately restores visibility in FiveM.
