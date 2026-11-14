import json
import time
from typing import Any

from bridges.els import ELSBridge


class RadioBridge(ELSBridge):
    """Bridge for passive NUI::MinimapBar::ChatRadio capture and silent slot switching."""

    def _get_client_frame_id(self) -> str | None:
        for fid, info in self._frames.items():
            if info.get("resource") == "client":
                return fid
        return None

    async def poll_radio_state(self) -> dict[str, Any]:
        """Install/refresh the passive radio NUI listener and return the latest radio state."""
        default_state: dict[str, Any] = {
            "connected": False,
            "radioOn": False,
            "primarySlot": 0,
            "atcOn": False,
            "vtsOn": False,
            "departmentalOn": False,
            "activeDep": "",
            "slots": [],
            "timestamp": time.time(),
        }
        if not await self.connect():
            return default_state

        client_fid = self._get_client_frame_id()
        if not client_fid:
            return default_state

        script = """
        (() => {
            window.__tacticalRadioState = window.__tacticalRadioState || {
                radioOn: false,
                primarySlot: 0,
                atcOn: false,
                vtsOn: false,
                departmentalOn: false,
                activeDep: '',
                slots: []
            };
            if (typeof window.__tacticalRadioState.activeDep !== 'string') {
                window.__tacticalRadioState.activeDep = '';
            }

            window.__tacticalRadioHandler = function(event) {
                let d = event && event.data;
                if (typeof d === 'string') {
                    try { d = JSON.parse(d); } catch { return; }
                }
                if (!d) return;

                if (d.action === 'NUI::MinimapBar::ChatRadio' && d.data) {
                    const data = d.data;
                    const prev = window.__tacticalRadioState;
                    const rawSlots = Array.isArray(data.slots) ? data.slots : prev.slots;
                    const slots = rawSlots.map(s => ({
                        slot: Number(s.slot ?? 0),
                        frequency: Number(s.frequency ?? 0),
                        name: String(s.name ?? ''),
                        localName: String(s.localName ?? ''),
                        color: String(s.color ?? 'FFEC8B'),
                        muted: Boolean(s.muted),
                        isPrimary: Boolean(s.isPrimary)
                    }));
                    window.__tacticalRadioState = {
                        ...prev,
                        radioOn: data.radioOn !== undefined ? Boolean(data.radioOn) : prev.radioOn,
                        primarySlot: data.primarySlot !== undefined ? Number(data.primarySlot) : prev.primarySlot,
                        atcOn: data.atcOn !== undefined ? Boolean(data.atcOn) : prev.atcOn,
                        vtsOn: data.vtsOn !== undefined ? Boolean(data.vtsOn) : prev.vtsOn,
                        departmentalOn: data.departmentalOn !== undefined ? Boolean(data.departmentalOn) : prev.departmentalOn,
                        slots: slots
                    };
                } else if (d.type === 'ON_MESSAGE' && d.message && Array.isArray(d.message.args)) {
                    const rawText = d.message.args.join(' ');
                    const clean = rawText.replace(/!\\{[^}]+\\}/g, '').trim();
                    const prev = window.__tacticalRadioState;

                    if (/Your radio is now turned ON/i.test(clean)) {
                        window.__tacticalRadioState = {
                            ...prev,
                            radioOn: true
                        };
                        return;
                    }

                    if (/Your radio is now turned OFF/i.test(clean)) {
                        window.__tacticalRadioState = {
                            ...prev,
                            radioOn: false
                        };
                        return;
                    }

                    const slotMatch = clean.match(/You have set your active slot to (\\d+)\\./i);
                    if (slotMatch) {
                        const newSlot = Number(slotMatch[1]);
                        window.__tacticalRadioState = {
                            ...prev,
                            primarySlot: newSlot,
                            slots: (prev.slots || []).map(s => ({
                                ...s,
                                isPrimary: s.slot === newSlot
                            }))
                        };
                        return;
                    }

                    const depSetMatch = clean.match(/You have changed your department frequency to .+?\\(([^)]+)\\)\\./i);
                    if (depSetMatch) {
                        window.__tacticalRadioState = {
                            ...prev,
                            activeDep: depSetMatch[1].trim()
                        };
                        return;
                    }

                    if (/Your departmental radio frequency has been reset due to inactivity/i.test(clean)) {
                        window.__tacticalRadioState = {
                            ...prev,
                            activeDep: ''
                        };
                    }
                }
            };
            if (!window.__tacticalRadioListenerAttached) {
                window.addEventListener('message', (e) => {
                    if (window.__tacticalRadioHandler) window.__tacticalRadioHandler(e);
                });
                window.__tacticalRadioListenerAttached = true;
            }
            return window.__tacticalRadioState;
        })()
        """
        try:
            res = await self._eval_in_frame(client_fid, script)
            if isinstance(res, dict):
                return {
                    "connected": True,
                    "radioOn": bool(res.get("radioOn", False)),
                    "primarySlot": int(res.get("primarySlot", 0)),
                    "atcOn": bool(res.get("atcOn", False)),
                    "vtsOn": bool(res.get("vtsOn", False)),
                    "departmentalOn": bool(res.get("departmentalOn", False)),
                    "activeDep": str(res.get("activeDep", "")),
                    "slots": res.get("slots", []),
                    "timestamp": time.time(),
                }
        except Exception:
            pass
        return default_state

    async def sync_radio_state(self, force: bool = False) -> dict[str, Any]:
        """Briefly switch to the Radios MinimapBar tab and immediately restore the previous tab."""
        state = await self.poll_radio_state()
        if not state["connected"]:
            return state
        if state["slots"] and not force:
            return state

        client_fid = self._get_client_frame_id()
        if not client_fid:
            return state

        force_js = "true" if force else "false"
        script = f"""
        (async () => {{
            if (!{force_js} && window.__tacticalRadioAutoSynced) {{
                return window.__tacticalRadioState;
            }}
            window.__tacticalRadioAutoSynced = true;

            const tabs = Array.from(document.querySelectorAll('.mmb-tab'));
            const prev = tabs.find(t => t.classList.contains('active'));
            const radios = tabs.find(t => (t.getAttribute('title') || t.textContent.trim()) === 'Radios');
            if (!radios) return window.__tacticalRadioState;

            const prevCount = (window.__tacticalRadioState?.slots || []).length;
            if (prev !== radios) {{
                radios.click();
                const t0 = performance.now();
                while (performance.now() - t0 < 300) {{
                    const curCount = (window.__tacticalRadioState?.slots || []).length;
                    if (curCount > 0 && (!{force_js} || curCount !== prevCount || performance.now() - t0 >= 120)) {{
                        break;
                    }}
                    await new Promise(r => setTimeout(r, 10));
                }}
                if (prev) prev.click();
            }}
            return window.__tacticalRadioState;
        }})()
        """
        try:
            res = await self._eval_in_frame(client_fid, script, await_promise=True)
            if isinstance(res, dict):
                return {
                    "connected": True,
                    "radioOn": bool(res.get("radioOn", False)),
                    "primarySlot": int(res.get("primarySlot", 0)),
                    "atcOn": bool(res.get("atcOn", False)),
                    "vtsOn": bool(res.get("vtsOn", False)),
                    "departmentalOn": bool(res.get("departmentalOn", False)),
                    "slots": res.get("slots", []),
                    "activeDep": str(res.get("activeDep", "")),
                    "timestamp": time.time(),
                }
        except Exception:
            pass
        return state

    async def set_radio_slot(self, slot: int) -> dict[str, Any]:
        """Silently switch the active radio slot via NUI fetch without touching the FiveM UI."""
        if not await self.connect():
            return {"ok": False, "error": "CDP not connected"}

        client_fid = self._get_client_frame_id()
        if not client_fid:
            return {"ok": False, "error": "cfx-nui-client frame not found"}

        payload = json.dumps({
            "action": "Client::MinimapBar::RadioPanel",
            "data": {"action": "setSlot", "slot": int(slot)},
        })
        script = f"""
        (async () => {{
            try {{
                const resp = await fetch('https://client/nui', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: {json.dumps(payload)}
                }});
                return {{ ok: resp.ok, status: resp.status }};
            }} catch (e) {{
                return {{ ok: false, error: String(e) }};
            }}
        }})()
        """
        try:
            res = await self._eval_in_frame(client_fid, script, await_promise=True)
            if isinstance(res, dict):
                return res
            return {"ok": False, "error": "Unexpected evaluation result"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
