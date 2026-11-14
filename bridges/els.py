"""ELS state adapter inheriting from FiveMBridge."""

from __future__ import annotations

import time
from typing import Any

from bridges.fivem_cdp import FiveMBridge


class ELSBridge(FiveMBridge):
    """Listens to NUI::Speedometer::State events to track GTAW ELS code and siren tone."""

    def __init__(
        self,
        cdp_url: str = "http://127.0.0.1:13172",
        command_cooldown: float = 5.0,
    ) -> None:
        super().__init__(cdp_url=cdp_url, command_cooldown=command_cooldown)
        self._last_els_state: dict[str, Any] = {
            "connected": False,
            "elsVisible": False,
            "elsCode": 1,
            "elsTone": 0,
        }

    async def poll_els_state(self) -> dict[str, Any]:
        """Install the NUI::Speedometer::State listener if needed and return latest ELS state."""
        if not await self.connect():
            self._last_els_state = {
                "connected": False,
                "elsVisible": False,
                "elsCode": 1,
                "elsTone": 0,
                "timestamp": time.time(),
            }
            return self._last_els_state

        client_fid = next(
            (fid for fid, info in self._frames.items() if info.get("resource") == "client"),
            None,
        )
        if not client_fid:
            return {**self._last_els_state, "connected": False, "timestamp": time.time()}

        js = """(() => {
            if (!window.__tacticalElsState) {
                window.__tacticalElsState = { hasEls: false, elsCode: 1, elsTone: 0 };
            }
            window.__tacticalElsHandler = (ev) => {
                const d = ev.data;
                if (!d || !d.action) return;
                const cur = window.__tacticalElsState;
                if (d.action === 'NUI::Speedometer::Hide') {
                    cur.hasEls = false;
                    cur.elsCode = 1;
                    cur.elsTone = 0;
                } else if (d.action === 'NUI::Speedometer::State' && d.data) {
                    if (d.data.elsVisible === true || (Number(d.data.elsCode ?? 1) > 1)) {
                        cur.hasEls = true;
                    }
                    if ('elsTone' in d.data) cur.elsTone = Number(d.data.elsTone ?? 0);
                    if ('elsCode' in d.data) cur.elsCode = Number(d.data.elsCode ?? 1);
                    if (cur.elsCode === 3 && cur.elsTone === 0) {
                        cur.elsCode = 2;
                    }
                }
            };
            if (!window.__tacticalElsListenerAttached) {
                window.__tacticalElsListenerAttached = true;
                window.addEventListener('message', (ev) => window.__tacticalElsHandler?.(ev));
            }
            const s = window.__tacticalElsState;
            return { elsVisible: Boolean(s.hasEls), elsCode: s.elsCode, elsTone: s.elsTone };
        })()"""

        try:
            data = await self._eval_in_frame(client_fid, js)
        except Exception:
            return {**self._last_els_state, "connected": False, "timestamp": time.time()}

        if data is not None:
            self._last_els_state = {
                "connected": True,
                "elsVisible": bool(data.get("elsVisible", False)),
                "elsCode": int(data.get("elsCode", 1)),
                "elsTone": int(data.get("elsTone", 0)),
                "timestamp": time.time(),
            }
        else:
            self._last_els_state = {
                **self._last_els_state,
                "connected": True,
                "timestamp": time.time(),
            }
        return self._last_els_state
