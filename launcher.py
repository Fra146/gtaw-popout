"""Bitfocus Companion-style desktop launcher for GTAW Tactical Popout."""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import urllib.request
import webbrowser

ROOT_DIR = Path(__file__).resolve().parent
LAUNCHER_HTML = ROOT_DIR / "static" / "launcher.html"
UVICORN_BIN = ROOT_DIR / ".venv" / "bin" / "uvicorn"
CHROMIUM_BIN = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser")
LAUNCHER_PROFILE_DIR = Path("/tmp/gtaw-tactical-launcher-profile")


class ServerController:
    """Manages the uvicorn main:app subprocess and captures its output."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._started_at: float | None = None
        self._logs: deque[str] = deque(maxlen=250)

    def _append_log(self, line: str) -> None:
        with self._lock:
            self._logs.append(line.rstrip("\r\n"))

    def _stream_output(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self._append_log(line)
        code = proc.poll()
        self._append_log(f"[launcher] server exited (code={code})")

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self, host: str | None = None, port: int | None = None) -> dict[str, Any]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": True, "already_running": True}
            if host:
                self.host = host
            if port:
                self.port = int(port)

            cmd = [
                str(UVICORN_BIN if UVICORN_BIN.exists() else sys.executable),
                *([] if UVICORN_BIN.exists() else ["-m", "uvicorn"]),
                "main:app",
                "--host",
                self.host,
                "--port",
                str(self.port),
            ]
            self._append_log(f"[launcher] starting: {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._proc = proc
            self._started_at = time.time()
            threading.Thread(target=self._stream_output, args=(proc,), daemon=True).start()
            return {"ok": True, "pid": proc.pid}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                self._started_at = None
                return {"ok": True, "already_stopped": True}
            self._append_log("[launcher] stopping server...")
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        with self._lock:
            self._proc = None
            self._started_at = None
        return {"ok": True}

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()

    @staticmethod
    def _check_cdp() -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:13172/json/version", timeout=0.35) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def _get_lan_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            pid = self._proc.pid if running and self._proc is not None else None
            uptime = (time.time() - self._started_at) if running and self._started_at else None
            logs = list(self._logs)
        return {
            "running": running,
            "host": self.host,
            "port": self.port,
            "lan_ip": self._get_lan_ip(),
            "pid": pid,
            "uptime": uptime,
            "cdp_connected": self._check_cdp(),
            "logs": logs,
        }


def open_in_browser(url: str) -> None:
    """Open a panel URL in the system default browser."""
    webbrowser.open(url)


def make_handler(controller: ServerController) -> type[BaseHTTPRequestHandler]:
    class LauncherHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        def do_GET(self) -> None:
            if self.path == "/" or self.path == "/launcher":
                html = LAUNCHER_HTML.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return
            if self.path == "/api/launcher/status":
                self._send_json(controller.status())
                return
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path == "/api/launcher/start":
                body = self._read_json()
                res = controller.start(
                    host=str(body.get("host") or controller.host),
                    port=int(body.get("port") or controller.port),
                )
                self._send_json(res)
                return
            if self.path == "/api/launcher/stop":
                self._send_json(controller.stop())
                return
            if self.path == "/api/launcher/clear-logs":
                controller.clear_logs()
                self._send_json({"ok": True})
                return
            if self.path == "/api/launcher/open":
                body = self._read_json()
                url = str(body.get("url") or "")
                if url.startswith(("http://", "https://")):
                    open_in_browser(url)
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "error": "Invalid URL"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    return LauncherHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="GTAW Tactical Companion-style Launcher")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--no-gui", action="store_true")
    args = parser.parse_args()

    controller = ServerController(host=args.host, port=args.port)
    if not args.no_autostart:
        controller.start()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controller))
    launcher_port = httpd.server_address[1]
    launcher_url = f"http://127.0.0.1:{launcher_port}/"

    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def _shutdown(*_: Any) -> None:
        controller.stop()
        httpd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if args.no_gui:
        print(f"Launcher UI: {launcher_url}", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _shutdown()
    elif CHROMIUM_BIN:
        LAUNCHER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        gui_proc = subprocess.Popen(
            [
                CHROMIUM_BIN,
                f"--app={launcher_url}",
                "--window-size=460,680",
                f"--user-data-dir={LAUNCHER_PROFILE_DIR}",
                "--class=GTAW-Tactical-Launcher",
                "--no-first-run",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        gui_proc.wait()
        controller.stop()
        httpd.shutdown()
    else:
        webbrowser.open(launcher_url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _shutdown()


if __name__ == "__main__":
    main()
