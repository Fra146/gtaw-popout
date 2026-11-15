"""Bitfocus Companion-style PySide6 (Qt) desktop launcher for GTAW Tactical Popout."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

try:
    from version import APP_VERSION
except Exception:
    APP_VERSION = "untagged"

GITHUB_REPO = "Fra146/gtaw-popout"
EXE_ASSET_NAME = "GTAW-Tactical-Launcher.exe"
SHA_ASSET_NAME = "GTAW-Tactical-Launcher.exe.sha256"

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ROOT_DIR = Path(__file__).resolve().parent

QSS = """
QWidget#Root {
    background-color: #101216;
    color: #eef2f7;
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
}
QFrame#Card {
    background-color: #171a20;
    border: 1px solid #262b35;
    border-radius: 10px;
}
QLabel#Logo {
    background-color: #d50215;
    color: #ffffff;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 800;
}
QLabel#BrandTitle {
    color: #eef2f7;
    font-size: 14px;
    font-weight: 800;
}
QLabel#BrandSub {
    color: #8b95a5;
    font-size: 11px;
    font-family: "Consolas", "JetBrains Mono", monospace;
}
QLabel#SectionTitle {
    color: #8b95a5;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
}
QLabel#FieldLabel {
    color: #8b95a5;
    font-size: 10px;
    font-weight: 700;
}
QLabel#MetaLabel {
    color: #8b95a5;
    font-size: 11px;
}
QLabel#StatusPill {
    background-color: #12151a;
    border: 1px solid #3f2428;
    color: #fca5a5;
    border-radius: 11px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 700;
}
QLabel#StatusPill[running="true"] {
    background-color: #12261a;
    border: 1px solid #1f5c35;
    color: #86efac;
}
QLabel#CdpLabel {
    color: #fca5a5;
    font-size: 11px;
    font-weight: 700;
    font-family: "Consolas", "JetBrains Mono", monospace;
}
QLabel#CdpLabel[connected="true"] {
    color: #86efac;
}
QLineEdit, QComboBox {
    background-color: #12151a;
    border: 1px solid #2a303c;
    border-radius: 6px;
    padding: 6px 9px;
    color: #eef2f7;
    font-size: 12px;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #38bdf8;
}
QLineEdit:disabled {
    color: #64748b;
    background-color: #0e1014;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background-color: #171a20;
    color: #eef2f7;
    border: 1px solid #2a303c;
    selection-background-color: #252b36;
}
QPushButton {
    background-color: #1d2129;
    border: 1px solid #323946;
    border-radius: 8px;
    padding: 8px 14px;
    color: #eef2f7;
    font-size: 12px;
    font-weight: 700;
}
QPushButton:hover:enabled {
    background-color: #262c37;
    border-color: #424b5c;
}
QPushButton:pressed:enabled {
    background-color: #181c23;
}
QPushButton:disabled {
    color: #5b6472;
    border-color: #222730;
    background-color: #15181e;
}
QPushButton#PrimaryBtn {
    background-color: #d50215;
    border: 1px solid #ef1a2d;
    color: #ffffff;
    font-size: 13px;
    padding: 10px 14px;
}
QPushButton#PrimaryBtn:hover:enabled {
    background-color: #ef1a2d;
}
QPushButton#PrimaryBtn:disabled {
    background-color: #4a161c;
    border-color: #5e1d24;
    color: #9e7a7e;
}
QPushButton#PopoutBtn {
    text-align: left;
    padding: 10px 12px;
}
QPushButton#MiniBtn {
    padding: 3px 9px;
    border-radius: 5px;
    font-size: 10px;
    color: #8b95a5;
}
QPushButton#UpdateBtn {
    background-color: #132538;
    border: 1px solid #2563eb;
    color: #93c5fd;
    border-radius: 11px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 700;
}
QPushButton#UpdateBtn:hover:enabled {
    background-color: #1d3a59;
    color: #dbeafe;
}
QPlainTextEdit#LogBox {
    background-color: #0b0d10;
    border: 1px solid #242933;
    border-radius: 6px;
    color: #b7c2d0;
    font-family: "Consolas", "JetBrains Mono", monospace;
    font-size: 11px;
    padding: 6px;
}
"""


def parse_semver(tag: str) -> tuple[int, ...] | None:
    """Parse 'v0.2' or '0.2.1' into a comparable integer tuple, or None if non-semver."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", tag.strip())
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def check_for_update() -> dict[str, str] | None:
    """Check GitHub Releases for a newer tagged semver release."""
    cur_ver = parse_semver(APP_VERSION)
    if cur_ver is None:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=10",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "GTAW-Tactical-Launcher",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(releases, list):
        return None

    best: tuple[tuple[int, ...], dict[str, str]] | None = None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "")
        ver = parse_semver(tag)
        if ver is None or ver <= cur_ver:
            continue
        assets = {str(a.get("name")): str(a.get("browser_download_url")) for a in rel.get("assets", [])}
        exe_url = assets.get(EXE_ASSET_NAME)
        if not exe_url:
            continue
        info = {
            "tag": tag,
            "html_url": str(rel.get("html_url") or ""),
            "exe_url": exe_url,
            "sha_url": assets.get(SHA_ASSET_NAME, ""),
        }
        if best is None or ver > best[0]:
            best = (ver, info)
    return best[1] if best else None


def find_uvicorn_cmd(host: str, port: int) -> list[str]:
    """Return the uvicorn launch command for the local venv (Windows or POSIX)."""
    win_uv = ROOT_DIR / ".venv" / "Scripts" / "uvicorn.exe"
    nix_uv = ROOT_DIR / ".venv" / "bin" / "uvicorn"
    if win_uv.exists():
        base = [str(win_uv)]
    elif nix_uv.exists():
        base = [str(nix_uv)]
    else:
        base = [sys.executable, "-m", "uvicorn"]
    return [*base, "main:app", "--host", host, "--port", str(port)]


class ServerController:
    """Manages the uvicorn main:app backend (subprocess in dev, in-process if frozen)."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._uv_server: object | None = None
        self._started_at: float | None = None
        self._logs: deque[str] = deque(maxlen=300)

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
            if self._proc is not None:
                return self._proc.poll() is None
            return self._uv_server is not None

    def start(self, host: str | None = None, port: int | None = None) -> None:
        with self._lock:
            if self.is_running():
                return
            if host:
                self.host = host
            if port:
                self.port = int(port)

            if getattr(sys, "frozen", False):
                import uvicorn
                from main import app as fastapi_app

                class _ControllerLogHandler(logging.Handler):
                    def __init__(self, append_fn) -> None:
                        super().__init__()
                        self._append_fn = append_fn

                    def emit(self, record: logging.LogRecord) -> None:
                        try:
                            self._append_fn(self.format(record))
                        except Exception:
                            pass

                handler = _ControllerLogHandler(self._append_log)
                handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                    lg = logging.getLogger(name)
                    lg.handlers.clear()
                    lg.addHandler(handler)
                    lg.setLevel(logging.INFO)
                    lg.propagate = False

                config = uvicorn.Config(
                    fastapi_app,
                    host=self.host,
                    port=self.port,
                    log_config=None,
                    access_log=True,
                )
                server = uvicorn.Server(config)
                self._uv_server = server
                self._started_at = time.time()
                self._append_log(f"[launcher] starting embedded uvicorn on {self.host}:{self.port}")
                threading.Thread(target=server.run, daemon=True).start()
                return

            cmd = find_uvicorn_cmd(self.host, self.port)
            self._append_log(f"[launcher] starting: {' '.join(cmd)}")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
            )
            self._proc = proc
            self._started_at = time.time()
            threading.Thread(target=self._stream_output, args=(proc,), daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            if self._uv_server is not None:
                self._append_log("[launcher] stopping embedded server...")
                setattr(self._uv_server, "should_exit", True)
                self._uv_server = None
                self._started_at = None
                return

            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                self._started_at = None
                return
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

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()

    @staticmethod
    def check_cdp() -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:13172/json/version", timeout=0.35) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def get_lan_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            running = self.is_running()
            pid = self._proc.pid if running and self._proc is not None else None
            uptime = (time.time() - self._started_at) if running and self._started_at else None
            logs = list(self._logs)
        return {
            "running": running,
            "host": self.host,
            "port": self.port,
            "lan_ip": self.get_lan_ip(),
            "pid": pid,
            "uptime": uptime,
            "logs": logs,
        }


class LauncherWindow(QWidget):
    """Bitfocus Companion-style PySide6 desktop launcher."""

    ELS_PRESETS = {
        "ELS: SmartSiren": "fedsig",
        "ELS: Night-Ops": "interceptor",
        "ELS: MX7000 Rotary": "rotary",
        "ELS: Amber Utility": "amber",
    }

    _update_ready = Signal()
    _update_failed = Signal(str)

    def __init__(self, controller: ServerController, autostart: bool = True) -> None:
        super().__init__()
        self.controller = controller
        self._cdp_ok = False
        self._last_log_text = ""
        self._update_info: dict[str, str] | None = None
        self._updating = False

        self._update_ready.connect(self._on_update_ready)
        self._update_failed.connect(self._on_update_failed)

        self.setObjectName("Root")
        self.setWindowTitle("GTAW Tactical Launcher")
        self.resize(450, 650)
        self.setMinimumSize(410, 560)

        self._build_ui()

        if autostart:
            self.controller.start()

        threading.Thread(target=self._cdp_poll_loop, daemon=True).start()
        if getattr(sys, "frozen", False) and APP_VERSION != "untagged":
            threading.Thread(target=self._update_check_worker, daemon=True).start()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_ui)
        self._timer.start(400)
        self._refresh_ui()

    def _make_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        return card

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(10)

        # Header card
        header = self._make_card()
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 10, 12, 10)
        h_lay.setSpacing(10)

        logo = QLabel("GT")
        logo.setObjectName("Logo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(36, 36)
        h_lay.addWidget(logo)

        brand_lay = QVBoxLayout()
        brand_lay.setSpacing(1)
        version_text = "(untagged)" if APP_VERSION == "untagged" else f"v{APP_VERSION}"
        title = QLabel(f"GTAW Tactical  {version_text}")
        title.setObjectName("BrandTitle")
        self.url_label = QLabel(f"http://{self.controller.get_lan_ip()}:{self.controller.port}")
        self.url_label.setObjectName("BrandSub")
        brand_lay.addWidget(title)
        brand_lay.addWidget(self.url_label)
        h_lay.addLayout(brand_lay, 1)

        self.update_btn = QPushButton("Update")
        self.update_btn.setObjectName("UpdateBtn")
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.setVisible(False)
        self.update_btn.clicked.connect(self._perform_update)
        h_lay.addWidget(self.update_btn)

        self.status_pill = QLabel("● Stopped")
        self.status_pill.setObjectName("StatusPill")
        self.status_pill.setProperty("running", False)
        h_lay.addWidget(self.status_pill)
        root_layout.addWidget(header)

        # Hero actions
        hero_lay = QHBoxLayout()
        hero_lay.setSpacing(8)
        self.launch_btn = QPushButton("Launch GUI")
        self.launch_btn.setObjectName("PrimaryBtn")
        self.launch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_btn.clicked.connect(lambda: self._open_path("/"))

        self.toggle_btn = QPushButton("Start")
        self.toggle_btn.setFixedWidth(96)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(self._toggle_server)

        hero_lay.addWidget(self.launch_btn, 1)
        hero_lay.addWidget(self.toggle_btn)
        root_layout.addLayout(hero_lay)

        # Server & Bridge card
        net_card = self._make_card()
        net_lay = QVBoxLayout(net_card)
        net_lay.setContentsMargins(12, 10, 12, 10)
        net_lay.setSpacing(8)

        net_top = QHBoxLayout()
        net_title = QLabel("SERVER & BRIDGE")
        net_title.setObjectName("SectionTitle")
        self.cdp_label = QLabel("CDP: Checking...")
        self.cdp_label.setObjectName("CdpLabel")
        self.cdp_label.setProperty("connected", False)
        net_top.addWidget(net_title)
        net_top.addStretch(1)
        net_top.addWidget(self.cdp_label)
        net_lay.addLayout(net_top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)

        lbl_host = QLabel("BIND ADDRESS")
        lbl_host.setObjectName("FieldLabel")
        lbl_port = QLabel("PORT")
        lbl_port.setObjectName("FieldLabel")
        self.host_input = QLineEdit(self.controller.host)
        self.host_input.setFont(QFont("Consolas", 10))
        self.port_input = QLineEdit(str(self.controller.port))
        self.port_input.setFont(QFont("Consolas", 10))
        self.port_input.setFixedWidth(90)

        grid.addWidget(lbl_host, 0, 0)
        grid.addWidget(lbl_port, 0, 1)
        grid.addWidget(self.host_input, 1, 0)
        grid.addWidget(self.port_input, 1, 1)
        net_lay.addLayout(grid)

        meta_lay = QHBoxLayout()
        self.pid_label = QLabel("PID: --")
        self.pid_label.setObjectName("MetaLabel")
        self.uptime_label = QLabel("Uptime: --")
        self.uptime_label.setObjectName("MetaLabel")
        meta_lay.addWidget(self.pid_label)
        meta_lay.addStretch(1)
        meta_lay.addWidget(self.uptime_label)
        net_lay.addLayout(meta_lay)
        root_layout.addWidget(net_card)

        # Quick Popouts card
        pop_card = self._make_card()
        pop_lay = QVBoxLayout(pop_card)
        pop_lay.setContentsMargins(12, 10, 12, 10)
        pop_lay.setSpacing(8)

        pop_title = QLabel("QUICK POPOUTS (DEFAULT BROWSER)")
        pop_title.setObjectName("SectionTitle")
        pop_lay.addWidget(pop_title)

        els_row = QHBoxLayout()
        els_row.setSpacing(8)
        self.els_combo = QComboBox()
        self.els_combo.addItems(list(self.ELS_PRESETS.keys()))
        self.els_btn = QPushButton("Open ELS")
        self.els_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.els_btn.clicked.connect(self._open_els)
        els_row.addWidget(self.els_combo, 1)
        els_row.addWidget(self.els_btn)
        pop_lay.addLayout(els_row)

        pop_btns = QHBoxLayout()
        pop_btns.setSpacing(8)
        self.radio_btn = QPushButton("Radio Deck\nChannels & /setslot")
        self.radio_btn.setObjectName("PopoutBtn")
        self.radio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.radio_btn.clicked.connect(lambda: self._open_path("/radio"))

        self.comms_btn = QPushButton("Comms Hub\nRadio & Dispatch")
        self.comms_btn.setObjectName("PopoutBtn")
        self.comms_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.comms_btn.clicked.connect(lambda: self._open_path("/comms"))

        pop_btns.addWidget(self.radio_btn, 1)
        pop_btns.addWidget(self.comms_btn, 1)
        pop_lay.addLayout(pop_btns)
        root_layout.addWidget(pop_card)

        # Server Log card
        log_card = self._make_card()
        log_lay = QVBoxLayout(log_card)
        log_lay.setContentsMargins(12, 10, 12, 10)
        log_lay.setSpacing(6)

        log_top = QHBoxLayout()
        log_title = QLabel("SERVER LOG")
        log_title.setObjectName("SectionTitle")
        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("MiniBtn")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self._clear_logs)
        log_top.addWidget(log_title)
        log_top.addStretch(1)
        log_top.addWidget(clear_btn)
        log_lay.addLayout(log_top)

        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("LogBox")
        self.log_box.setReadOnly(True)
        log_lay.addWidget(self.log_box, 1)
        root_layout.addWidget(log_card, 1)

    def _base_url(self, snap: dict[str, object]) -> str:
        host = str(snap["host"])
        shown_host = str(snap["lan_ip"]) if host == "0.0.0.0" else host
        return f"http://{shown_host}:{snap['port']}"

    def _open_path(self, path: str) -> None:
        snap = self.controller.snapshot()
        webbrowser.open(f"{self._base_url(snap)}{path}")

    def _open_els(self) -> None:
        preset = self.ELS_PRESETS.get(self.els_combo.currentText(), "fedsig")
        self._open_path(f"/els?preset={urllib.parse.quote(preset)}")

    def _toggle_server(self) -> None:
        if self.controller.is_running():
            self.controller.stop()
        else:
            host = self.host_input.text().strip() or "0.0.0.0"
            try:
                port = int(self.port_input.text().strip() or "8000")
            except ValueError:
                port = 8000
                self.port_input.setText("8000")
            self.controller.start(host=host, port=port)
        self._refresh_ui()

    def _clear_logs(self) -> None:
        self.controller.clear_logs()
        self._refresh_ui()

    def _cdp_poll_loop(self) -> None:
        while True:
            self._cdp_ok = self.controller.check_cdp()
            time.sleep(1.5)

    def _update_check_worker(self) -> None:
        info = check_for_update()
        if info:
            self._update_info = info

    def _perform_update(self) -> None:
        info = self._update_info
        if not info or self._updating:
            return
        if not getattr(sys, "frozen", False) or os.name != "nt":
            if info.get("html_url"):
                webbrowser.open(info["html_url"])
            return

        self._updating = True
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Updating...")
        threading.Thread(target=self._download_and_apply_update, args=(info,), daemon=True).start()

    def _download_and_apply_update(self, info: dict[str, str]) -> None:
        cur_exe = Path(sys.executable).resolve()
        new_exe = cur_exe.with_name(cur_exe.name + ".new")
        try:
            self.controller._append_log(f"[updater] downloading {info['tag']}...")
            req = urllib.request.Request(info["exe_url"], headers={"User-Agent": "GTAW-Tactical-Launcher"})
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                data = resp.read()

            sha_url = info.get("sha_url")
            if sha_url:
                sha_req = urllib.request.Request(sha_url, headers={"User-Agent": "GTAW-Tactical-Launcher"})
                with urllib.request.urlopen(sha_req, timeout=15.0) as sresp:
                    expected = sresp.read().decode("utf-8").strip().split()[0].lower()
                actual = hashlib.sha256(data).hexdigest().lower()
                if actual != expected:
                    raise ValueError(f"SHA-256 mismatch: {actual} != {expected}")

            new_exe.write_bytes(data)
            self.controller.stop()

            new_s = str(new_exe).replace("'", "''")
            cur_s = str(cur_exe).replace("'", "''")
            ps_cmd = (
                f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
                f"for ($i = 0; $i -lt 15; $i++) {{ "
                f"  try {{ Move-Item -Force '{new_s}' '{cur_s}'; break }} "
                f"  catch {{ Start-Sleep -Milliseconds 300 }} "
                f"}}; "
                f"Start-Process '{cur_s}'"
            )
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("_MEI", "_PYI", "QT_", "QML", "PYTHON"))
            }
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                creationflags=flags,
                env=clean_env,
            )
            self._update_ready.emit()
        except Exception as exc:
            self.controller._append_log(f"[updater] update failed: {exc}")
            new_exe.unlink(missing_ok=True)
            self._updating = False
            self._update_failed.emit(info["tag"])

    def _on_update_ready(self) -> None:
        self.close()
        QApplication.quit()

    def _on_update_failed(self, tag: str) -> None:
        self.update_btn.setEnabled(True)
        self.update_btn.setText(f"Update {tag}")

    @staticmethod
    def _format_uptime(sec: object) -> str:
        if not isinstance(sec, (int, float)) or sec < 0:
            return "--"
        total = int(sec)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def _repolish(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _refresh_ui(self) -> None:
        snap = self.controller.snapshot()
        running = bool(snap["running"])

        self.url_label.setText(self._base_url(snap))

        self.status_pill.setText("● Running" if running else "● Stopped")
        if self.status_pill.property("running") != running:
            self.status_pill.setProperty("running", running)
            self._repolish(self.status_pill)

        self.toggle_btn.setText("Stop" if running else "Start")
        self.host_input.setEnabled(not running)
        self.port_input.setEnabled(not running)

        for btn in (self.launch_btn, self.els_btn, self.radio_btn, self.comms_btn):
            btn.setEnabled(running)

        self.pid_label.setText(f"PID: {snap['pid'] if snap['pid'] else '--'}")
        self.uptime_label.setText(f"Uptime: {self._format_uptime(snap['uptime']) if running else '--'}")

        self.cdp_label.setText("CDP: Connected" if self._cdp_ok else "CDP: Waiting")
        if self.cdp_label.property("connected") != self._cdp_ok:
            self.cdp_label.setProperty("connected", self._cdp_ok)
            self._repolish(self.cdp_label)

        if self._update_info and not self.update_btn.isVisible():
            self.update_btn.setText(f"Update {self._update_info['tag']}")
            self.update_btn.setVisible(True)

        logs = snap["logs"]
        log_text = "\n".join(logs) if isinstance(logs, list) and logs else "Launcher ready."
        if log_text != self._last_log_text:
            self._last_log_text = log_text
            self.log_box.setPlainText(log_text)
            sb = self.log_box.verticalScrollBar()
            sb.setValue(sb.maximum())

    def closeEvent(self, event) -> None:  # noqa: N802
        self.controller.stop()
        super().closeEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="GTAW Tactical Companion-style Qt Launcher")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-autostart", action="store_true")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    controller = ServerController(host=args.host, port=args.port)
    win = LauncherWindow(controller, autostart=not args.no_autostart)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
