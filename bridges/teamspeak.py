"""Async TeamSpeak 3 ClientQuery bridge (127.0.0.1:25639)."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import re
from typing import Any

from models import CommsChannelGroup, CommsSpeaker, CommsStateMessage

_UNESCAPE_MAP = {
    r"\s": " ",
    r"\p": "|",
    r"\/": "/",
    r"\\": "\\",
    r"\n": "\n",
    r"\r": "\r",
    r"\t": "\t",
}
_UNESCAPE_RE = re.compile(r"\\(s|p|/|\\|n|r|t)")


def ts_unescape(val: str) -> str:
    return _UNESCAPE_RE.sub(lambda m: _UNESCAPE_MAP.get(m.group(0), m.group(0)), val)


def parse_kv(token_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in token_str.split(" "):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = ts_unescape(v)
        else:
            out[part] = ""
    return out


def parse_list(line: str) -> list[dict[str, str]]:
    if not line.strip():
        return []
    return [parse_kv(chunk) for chunk in line.split("|") if chunk.strip()]


class TeamSpeakBridge:
    """Maintains a live ClientQuery connection for whisper & channel speaker tracking."""

    def __init__(self, host: str = "127.0.0.1", port: int = 25639, api_key: str | None = None) -> None:
        self.host = host
        self.port = port
        self.api_key = (api_key or "").strip()

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._cmd_lock = asyncio.Lock()
        self._resp_queue: asyncio.Queue[tuple[list[str], dict[str, str]]] = asyncio.Queue()

        self.connected = False
        self.auth_failed = False
        self.error: str | None = None

        self._self_clid: str | None = None
        self._current_cid: str | None = None
        self._channels: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, dict[str, Any]] = {}
        self._whisper_clids: set[str] = set()
        self._talking_clids: set[str] = set()
        self._last_refresh: float = 0.0

    @property
    def status(self) -> str:
        if self.connected:
            return "connected"
        if self.auth_failed:
            return "invalid_key"
        return "disconnected"

    def configure(self, host: str, port: int, api_key: str | None = None) -> None:
        self.host = host
        self.port = port
        if api_key is not None:
            self.api_key = api_key.strip()
        self.auth_failed = False

    async def disconnect(self) -> None:
        self.connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None

    async def connect(self) -> bool:
        if self.connected and self._writer is not None and not self._writer.is_closing():
            return True

        await self.disconnect()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=1.5,
            )
        except Exception as exc:
            self.auth_failed = False
            self.error = f"TeamSpeak ClientQuery offline ({self.host}:{self.port}): {exc}"
            return False

        self._reader = reader
        self._writer = writer
        while not self._resp_queue.empty():
            self._resp_queue.get_nowait()

        # Consume TS3 ClientQuery greeting lines ("TS3 Client\nWelcome...")
        try:
            for _ in range(4):
                line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if "Welcome" in text or "selected schandlerid" in text:
                    break
        except asyncio.TimeoutError:
            pass

        self._reader_task = asyncio.create_task(self._read_loop(), name="ts3-reader-loop")

        if self.api_key:
            _, err = await self._send_command(f"auth apikey={self.api_key}")
            if err.get("id") not in (None, "0"):
                self.auth_failed = True
                self.error = f"TeamSpeak auth failed: {err.get('msg', 'invalid api key')} (check ClientQuery key)"
                await self.disconnect()
                return False

        # Test command to verify ClientQuery session is authorized
        lines, err = await self._send_command("whoami")
        if err.get("id") == "1796":
            self.auth_failed = True
            self.error = "TeamSpeak ClientQuery API key missing or invalid."
            await self.disconnect()
            return False

        self.auth_failed = False
        self.error = None
        self.connected = True

        for ev in (
            "notifytalkstatuschange",
            "notifyclientmoved",
            "notifyclientupdated",
            "notifyconnectstatuschange",
            "notifychannelcreated",
            "notifychanneldeleted",
            "notifychanneledited",
        ):
            await self._send_command(f"clientnotifyregister schandlerid=0 event={ev}")

        await self.refresh_snapshot()
        return True

    async def _read_loop(self) -> None:
        assert self._reader is not None
        data_lines: list[str] = []
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("notify"):
                    self._handle_notify(line)
                elif line.startswith("error "):
                    err = parse_kv(line[6:])
                    await self._resp_queue.put((data_lines, err))
                    data_lines = []
                else:
                    data_lines.append(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"TeamSpeak connection lost: {exc}"
        finally:
            self.connected = False

    def _handle_notify(self, line: str) -> None:
        parts = line.split(" ", 1)
        ev = parts[0]
        kv = parse_kv(parts[1]) if len(parts) > 1 else {}

        if ev == "notifytalkstatuschange":
            clid = kv.get("clid")
            if not clid:
                return
            talking = kv.get("status") == "1"
            whispering = talking and kv.get("isreceivedwhisper") == "1"
            if talking:
                self._talking_clids.add(clid)
            else:
                self._talking_clids.discard(clid)
            if whispering:
                self._whisper_clids.add(clid)
            else:
                self._whisper_clids.discard(clid)
            if clid in self._clients:
                self._clients[clid]["speaking"] = talking
                self._clients[clid]["whispering"] = whispering
        elif ev == "notifyclientmoved":
            clid = kv.get("clid")
            ctid = kv.get("ctid")
            if clid and ctid:
                if clid == self._self_clid:
                    self._current_cid = ctid
                if clid in self._clients:
                    self._clients[clid]["cid"] = ctid
            self._last_refresh = 0.0
        elif ev in ("notifyclientupdated", "notifychannelcreated", "notifychanneldeleted", "notifychanneledited"):
            self._last_refresh = 0.0
        elif ev == "notifyconnectstatuschange":
            if kv.get("status") == "disconnected":
                self._channels.clear()
                self._clients.clear()
                self._whisper_clids.clear()
                self._talking_clids.clear()
                self._current_cid = None

    async def _send_command(self, cmd: str) -> tuple[list[str], dict[str, str]]:
        if self._writer is None or self._writer.is_closing():
            return [], {"id": "-1", "msg": "not_connected"}
        async with self._cmd_lock:
            self._writer.write((cmd + "\n").encode("utf-8"))
            await self._writer.drain()
            try:
                return await asyncio.wait_for(self._resp_queue.get(), timeout=1.5)
            except asyncio.TimeoutError:
                return [], {"id": "-1", "msg": "timeout"}

    async def refresh_snapshot(self) -> None:
        if not self.connected:
            return
        loop = asyncio.get_running_loop()
        self._last_refresh = loop.time()

        who_lines, err = await self._send_command("whoami")
        if err.get("id") == "0" and who_lines:
            who = parse_kv(who_lines[0])
            self._self_clid = who.get("clid")
            self._current_cid = who.get("cid")

        ch_lines, ch_err = await self._send_command("channellist")
        if ch_err.get("id") == "0" and ch_lines:
            channels: dict[str, dict[str, Any]] = {}
            for item in parse_list(ch_lines[0]):
                cid = item.get("cid")
                if cid:
                    channels[cid] = {
                        "id": cid,
                        "name": item.get("channel_name", f"Channel {cid}"),
                    }
            self._channels = channels

        cl_lines, cl_err = await self._send_command("clientlist -voice")
        if cl_err.get("id") == "0" and cl_lines:
            clients: dict[str, dict[str, Any]] = {}
            for item in parse_list(cl_lines[0]):
                clid = item.get("clid")
                if not clid or item.get("client_type") == "1":
                    continue
                talking = item.get("client_flag_talking") == "1" or clid in self._talking_clids
                whispering = clid in self._whisper_clids
                clients[clid] = {
                    "id": clid,
                    "cid": item.get("cid", ""),
                    "name": item.get("client_nickname", f"Client {clid}"),
                    "speaking": talking,
                    "whispering": whispering,
                    "muted": item.get("client_input_muted") == "1" or item.get("client_input_hardware") == "0",
                    "deafened": item.get("client_output_muted") == "1" or item.get("client_output_hardware") == "0",
                }
            self._clients = clients

    async def poll(self, tracked_queries: list[str] | None = None) -> CommsStateMessage:
        if not await self.connect():
            return CommsStateMessage(
                source="teamspeak",
                connected=False,
                error=self.error or "Disconnected",
            )

        now = asyncio.get_running_loop().time()
        if now - self._last_refresh >= 2.0:
            await self.refresh_snapshot()

        return self.build_state(tracked_queries or [])

    def _match_channel_id(self, query: str) -> str | None:
        q = query.strip().lower()
        if not q or not self._channels:
            return None
        # 1. Exact case-insensitive match
        for cid, ch in self._channels.items():
            if ch["name"].lower() == q:
                return cid
        # 2. Substring match
        for cid, ch in self._channels.items():
            if q in ch["name"].lower():
                return cid
        # 3. Fuzzy closest match
        best_cid: str | None = None
        best_score = 0.0
        for cid, ch in self._channels.items():
            score = SequenceMatcher(None, q, ch["name"].lower()).ratio()
            if score > best_score:
                best_score = score
                best_cid = cid
        return best_cid if best_score >= 0.45 else None

    def _speaker_from_client(self, cl: dict[str, Any]) -> CommsSpeaker:
        ch = self._channels.get(cl.get("cid", ""))
        return CommsSpeaker(
            id=cl["id"],
            name=cl["name"],
            speaking=bool(cl.get("speaking")),
            whispering=bool(cl.get("whispering")),
            muted=bool(cl.get("muted")),
            deafened=bool(cl.get("deafened")),
            channel_name=ch["name"] if ch else None,
        )

    def build_state(self, tracked_queries: list[str]) -> CommsStateMessage:
        cur_cid = self._current_cid
        cur_ch = self._channels.get(cur_cid or "")
        cur_name = cur_ch["name"] if cur_ch else None

        # Resolve tracked channels without duplicating current_cid
        ordered_cids: list[str] = []
        matched_cids: list[str] = []
        for q in tracked_queries:
            cid = self._match_channel_id(q)
            if cid and cid not in matched_cids:
                matched_cids.append(cid)

        if cur_cid and cur_cid not in matched_cids:
            ordered_cids.append(cur_cid)
        for cid in matched_cids:
            if cid not in ordered_cids:
                ordered_cids.append(cid)

        channel_groups: list[CommsChannelGroup] = []
        for cid in ordered_cids:
            ch = self._channels.get(cid)
            if not ch:
                continue
            members = [
                self._speaker_from_client(cl)
                for cl in self._clients.values()
                if cl.get("cid") == cid
            ]
            members.sort(key=lambda s: (not s.speaking, s.name.lower()))
            channel_groups.append(
                CommsChannelGroup(
                    id=cid,
                    name=ch["name"],
                    is_current=(cid == cur_cid),
                    speakers=members,
                )
            )

        current_speakers = next((g.speakers for g in channel_groups if g.is_current), [])

        whisperers: list[CommsSpeaker] = []
        for clid in sorted(self._whisper_clids):
            cl = self._clients.get(clid)
            if cl:
                whisperers.append(self._speaker_from_client(cl))
            else:
                whisperers.append(CommsSpeaker(id=clid, name=f"Client {clid}", speaking=True, whispering=True))

        return CommsStateMessage(
            source="teamspeak",
            connected=self.connected,
            channel=cur_name,
            speakers=current_speakers,
            channels=channel_groups,
            whisperers=whisperers,
            error=self.error,
        )
