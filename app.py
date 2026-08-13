from __future__ import annotations

import argparse
import base64
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QPoint, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "QuotaDock"
APP_VERSION = "1.2.1"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexUsageWidget"
STATE_PATH = APP_DIR / "usage_state.json"
CLAUDE_STATE_PATH = APP_DIR / "claude_usage_state.json"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 登入要走瀏覽器授權並在主控台顯示提示，必須給它一個看得到的視窗。
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
# npm 全域安裝在 Windows 產生的是這類包裝檔，不是原生 exe。
SHIM_SUFFIXES = {".cmd", ".bat", ".ps1"}
CLAUDE_CLI_SETTING = "claude/cliPath"
SCAN_TARGETS = {"claude.exe", "claude.cmd", "claude.bat"}
# AnthropicClaude 是桌面版 GUI，它的執行檔也叫 claude.exe，但不吃 CLI 參數，跑下去只會彈視窗。
# 比對單層目錄名而非整條路徑，免得像 AppData\Local\Temp 這種父層被誤殺。
SCAN_SKIP_DIRS = {"anthropicclaude", "cache", ".bin"}


def _version_key(name: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in name.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float
    duration_minutes: int | None
    resets_at: int | None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))


@dataclass(frozen=True)
class UsageSnapshot:
    plan_type: str
    limit_id: str
    limit_name: str | None
    primary: UsageWindow | None
    secondary: UsageWindow | None
    has_credits: bool
    unlimited_credits: bool
    credit_balance: str | None
    reset_credits: int
    reached_type: str | None
    fetched_at: int

    @classmethod
    def from_response(cls, response: dict[str, Any]) -> "UsageSnapshot":
        result = response.get("result", response)
        snapshot = result.get("rateLimits") or {}
        buckets = result.get("rateLimitsByLimitId") or {}
        if buckets:
            snapshot = buckets.get("codex") or next(iter(buckets.values()))

        def parse_window(value: Any) -> UsageWindow | None:
            if not isinstance(value, dict):
                return None
            return UsageWindow(
                used_percent=float(value.get("usedPercent", 0.0)),
                duration_minutes=_optional_int(value.get("windowDurationMins")),
                resets_at=_optional_int(value.get("resetsAt")),
            )

        credits = snapshot.get("credits") or {}
        reset_credits = result.get("rateLimitResetCredits") or {}
        return cls(
            plan_type=str(snapshot.get("planType") or "unknown"),
            limit_id=str(snapshot.get("limitId") or "codex"),
            limit_name=snapshot.get("limitName"),
            primary=parse_window(snapshot.get("primary")),
            secondary=parse_window(snapshot.get("secondary")),
            has_credits=bool(credits.get("hasCredits", False)),
            unlimited_credits=bool(credits.get("unlimited", False)),
            credit_balance=_optional_str(credits.get("balance")),
            reset_credits=int(reset_credits.get("availableCount", 0) or 0),
            reached_type=snapshot.get("rateLimitReachedType"),
            fetched_at=int(time.time()),
        )


@dataclass(frozen=True)
class ClaudeUsageSnapshot:
    installed: bool
    logged_in: bool
    auth_method: str | None
    subscription_type: str | None
    rate_limits_available: bool
    five_hour: UsageWindow | None
    seven_day: UsageWindow | None
    seven_day_sonnet: UsageWindow | None
    seven_day_opus: UsageWindow | None
    fetched_at: int

    @classmethod
    def from_response(
        cls,
        response: dict[str, Any],
        *,
        auth_method: str | None,
        logged_in: bool,
    ) -> "ClaudeUsageSnapshot":
        limits = response.get("rate_limits") or {}

        def parse_window(value: Any) -> UsageWindow | None:
            if not isinstance(value, dict) or value.get("utilization") is None:
                return None
            return UsageWindow(
                used_percent=float(value["utilization"]),
                duration_minutes=None,
                resets_at=_parse_iso_timestamp(value.get("resets_at")),
            )

        return cls(
            installed=True,
            logged_in=logged_in,
            auth_method=auth_method,
            subscription_type=_optional_str(response.get("subscription_type")),
            rate_limits_available=bool(response.get("rate_limits_available", False)),
            five_hour=parse_window(limits.get("five_hour")),
            seven_day=parse_window(limits.get("seven_day")),
            seven_day_sonnet=parse_window(limits.get("seven_day_sonnet")),
            seven_day_opus=parse_window(limits.get("seven_day_opus")),
            fetched_at=int(time.time()),
        )

    @classmethod
    def unavailable(cls, *, installed: bool, logged_in: bool = False, auth_method: str | None = None) -> "ClaudeUsageSnapshot":
        return cls(
            installed=installed,
            logged_in=logged_in,
            auth_method=auth_method,
            subscription_type=None,
            rate_limits_available=False,
            five_hour=None,
            seven_day=None,
            seven_day_sonnet=None,
            seven_day_opus=None,
            fetched_at=int(time.time()),
        )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_iso_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def mini_remaining(
    source: str,
    codex: UsageSnapshot | None,
    claude: "ClaudeUsageSnapshot | None",
) -> float:
    """懸浮圖示要顯示的剩餘百分比；指定來源沒資料時退回看得到的那邊。"""

    def codex_values() -> list[float]:
        if codex and codex.primary:
            return [codex.primary.remaining_percent]
        return []

    def claude_values() -> list[float]:
        if claude is None:
            return []
        return [
            window.remaining_percent
            for window in (claude.five_hour, claude.seven_day)
            if window is not None
        ]

    if source == "codex":
        values = codex_values() or claude_values()
    elif source == "claude":
        values = claude_values() or codex_values()
    else:
        values = codex_values() + claude_values()
    return min(values) if values else 0.0


def detect_window_reset(previous: UsageWindow | None, current: UsageWindow | None) -> bool:
    if previous is None or current is None:
        return False
    reset_advanced = (
        previous.resets_at is not None
        and current.resets_at is not None
        and current.resets_at > previous.resets_at + 60
    )
    usage_dropped = current.used_percent <= previous.used_percent - 5
    strong_reset_drop = (
        current.used_percent <= 20
        and current.used_percent <= previous.used_percent - 20
    )
    return (reset_advanced and usage_dropped) or strong_reset_drop


def crossed_five_percent_level(
    previous: UsageWindow | None, current: UsageWindow
) -> int | None:
    if previous is None or current.remaining_percent >= previous.remaining_percent:
        return None
    old_bucket = math.ceil(previous.remaining_percent / 5)
    new_bucket = math.ceil(current.remaining_percent / 5)
    if new_bucket >= old_bucket:
        return None
    return max(0, new_bucket * 5)


def detect_cycle_reset(previous: UsageSnapshot | None, current: UsageSnapshot) -> bool:
    if previous is None or previous.primary is None or current.primary is None:
        return False
    return detect_window_reset(previous.primary, current.primary)


def duration_label(minutes: int | None) -> str:
    if minutes is None:
        return "用量週期"
    if minutes % 10080 == 0:
        weeks = minutes // 10080
        return f"{weeks * 7} 天額度"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天額度"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小時額度"
    return f"{minutes} 分鐘額度"


def reset_time_label(timestamp: int | None, now: int | None = None) -> str:
    if timestamp is None:
        return "重置時間未提供"
    now = int(time.time()) if now is None else now
    remaining = max(0, timestamp - now)
    days, remainder = divmod(remaining, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    reset_clock = datetime.fromtimestamp(timestamp).strftime("%m/%d %H:%M")
    if days:
        countdown = f"{days} 天 {hours} 小時"
    elif hours:
        countdown = f"{hours} 小時 {minutes} 分"
    else:
        countdown = f"{minutes} 分鐘"
    return f"{countdown}後重置 · {reset_clock}"


def percent_text(value: float) -> str:
    rounded = round(value)
    return f"{rounded}%"


class CodexCliLocator:
    def __init__(self) -> None:
        self.runtime_dir = APP_DIR / "runtime"

    def locate(self) -> Path:
        override = os.environ.get("CODEX_USAGE_CLI")
        if override and Path(override).is_file():
            return Path(override)

        development_copy = Path(__file__).resolve().parent.parent / "codex-local.exe"
        if development_copy.is_file() and not getattr(sys, "frozen", False):
            return development_copy

        version, install_location = self._installed_package()
        source = install_location / "app" / "resources" / "codex.exe"
        if not source.is_file():
            raise RuntimeError("找不到 Codex 本機服務，請先安裝或更新 Codex 桌面版。")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        destination = self.runtime_dir / f"codex-cli-{version}.exe"
        if not destination.is_file() or destination.stat().st_size != source.stat().st_size:
            temporary = destination.with_suffix(".tmp")
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        return destination

    @staticmethod
    def _installed_package() -> tuple[str, Path]:
        command = (
            "$p=Get-AppxPackage -Name 'OpenAI.Codex' | "
            "Sort-Object Version -Descending | Select-Object -First 1; "
            "if($p){Write-Output ($p.Version.ToString()+'|'+$p.InstallLocation)}"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
        if "|" not in line:
            raise RuntimeError("未偵測到 Codex 桌面版。")
        version, location = line.split("|", 1)
        return version.strip(), Path(location.strip())


class CodexUsageClient:
    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.locator = CodexCliLocator()

    def fetch(self) -> UsageSnapshot:
        cli = self.locator.locate()
        process = subprocess.Popen(
            [str(cli), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        messages: queue.Queue[str] = queue.Queue()

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                messages.put(line)

        threading.Thread(target=read_stdout, daemon=True).start()

        def send(payload: dict[str, Any]) -> None:
            if process.stdin is None:
                raise RuntimeError("Codex 本機服務未啟動。")
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()

        try:
            send(
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "codex-usage-widget",
                            "title": APP_NAME,
                            "version": APP_VERSION,
                        },
                        "capabilities": {
                            "experimentalApi": True,
                            "requestAttestation": False,
                            "optOutNotificationMethods": [],
                        },
                    },
                }
            )
            deadline = time.monotonic() + self.timeout_seconds
            initialized = False
            while time.monotonic() < deadline:
                if process.poll() is not None and messages.empty():
                    raise RuntimeError("Codex 本機服務已意外停止。")
                try:
                    raw = messages.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == 1:
                    if "error" in message:
                        raise RuntimeError("無法連線 Codex 帳號。")
                    initialized = True
                    send({"method": "initialized"})
                    send({"method": "account/rateLimits/read", "id": 2, "params": None})
                elif message.get("id") == 2:
                    if "error" in message:
                        detail = message.get("error", {}).get("message", "用量服務暫時無法使用")
                        raise RuntimeError(str(detail))
                    return UsageSnapshot.from_response(message)
            if not initialized:
                raise RuntimeError("Codex 連線逾時，請確認桌面版已登入。")
            raise RuntimeError("讀取用量逾時，稍後會自動重試。")
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


class ClaudeCliLocator:
    @staticmethod
    def locate() -> Path | None:
        override = os.environ.get("CLAUDE_USAGE_CLI")
        if override and Path(override).is_file():
            return Path(override)

        manual = _setting_str(QSettings("EricTools", "CodexUsageWidget"), CLAUDE_CLI_SETTING)
        if manual and Path(manual).is_file():
            return Path(manual)

        candidates: list[Path] = []
        for command in ("claude.exe", "claude"):
            located = shutil.which(command)
            if located:
                path = Path(located)
                candidates.append(path)
                if path.suffix.lower() in {".cmd", ".ps1"}:
                    candidates.append(
                        path.parent
                        / "node_modules"
                        / "@anthropic-ai"
                        / "claude-code"
                        / "bin"
                        / "claude.exe"
                    )

        home = Path.home()
        local_app_data = Path(os.environ.get("LOCALAPPDATA", str(home)))
        app_data = Path(os.environ.get("APPDATA", str(home)))
        candidates.extend(
            [
                home / ".local" / "bin" / "claude.exe",
                home / ".claude" / "local" / "claude.exe",
                local_app_data / "Programs" / "claude-code" / "claude.exe",
                app_data
                / "npm"
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "bin"
                / "claude.exe",
            ]
        )
        candidates.extend(ClaudeCliLocator._desktop_managed(app_data / "Claude" / "claude-code"))
        # 原生執行檔優先，找不到才退回 npm 之類的批次包裝檔。
        for suffixes in ({".exe"}, SHIM_SUFFIXES):
            for candidate in candidates:
                if candidate.suffix.lower() in suffixes and candidate.is_file():
                    return candidate

        # 已知路徑全部落空，才去掃描；掃到的結果記起來，下次不用再掃。
        scanned = ClaudeCliLocator._scan([local_app_data, app_data, home / ".local", home / ".claude"])
        if scanned:
            QSettings("EricTools", "CodexUsageWidget").setValue(CLAUDE_CLI_SETTING, str(scanned))
        return scanned

    @staticmethod
    def _scan(roots: list[Path], max_depth: int = 4, limit: int = 40) -> Path | None:
        found: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            base = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root):
                current = Path(dirpath)
                if current.name.lower() in SCAN_SKIP_DIRS:
                    dirnames.clear()
                    continue
                if len(current.parts) - base >= max_depth:
                    dirnames.clear()
                    continue
                for name in filenames:
                    if name.lower() in SCAN_TARGETS:
                        found.append(current / name)
                if len(found) >= limit:
                    break
        if not found:
            return None

        def rank(path: Path) -> tuple[int, int, tuple[int, ...]]:
            text = str(path).lower()
            return (
                0 if "claude-code" in text else 1,
                0 if path.suffix.lower() == ".exe" else 1,
                # 同一套安裝可能留著多個版本目錄，取版本號大的。
                tuple(-part for part in _version_key(path.parent.name)),
            )

        return sorted(found, key=rank)[0]

    @staticmethod
    def command(cli: Path, args: list[str]) -> list[str]:
        """批次或 PowerShell 包裝檔不能直接交給 CreateProcess，要用直譯器帶起來。"""
        suffix = cli.suffix.lower()
        if suffix in {".cmd", ".bat"}:
            return ["cmd.exe", "/c", str(cli), *args]
        if suffix == ".ps1":
            return [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(cli),
                *args,
            ]
        return [str(cli), *args]

    @staticmethod
    def _desktop_managed(root: Path) -> list[Path]:
        """Claude 桌面版把 CLI 裝在 <APPDATA>/Claude/claude-code/<版本>/，且不加入 PATH。"""
        if not root.is_dir():
            return []

        versions = [child for child in root.iterdir() if child.is_dir()]
        ordered = sorted(versions, key=lambda child: _version_key(child.name), reverse=True)
        return [child / "claude.exe" for child in ordered]


class ClaudeUsageClient:
    def __init__(self, timeout_seconds: float = 25.0) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch(self) -> ClaudeUsageSnapshot:
        cli = ClaudeCliLocator.locate()
        if cli is None:
            return ClaudeUsageSnapshot.unavailable(installed=False)

        auth = self._auth_status(cli)
        logged_in = bool(auth.get("loggedIn", False))
        auth_method = _optional_str(auth.get("authMethod"))
        if not logged_in:
            return ClaudeUsageSnapshot.unavailable(
                installed=True, logged_in=False, auth_method=auth_method
            )

        request_id = "ai-usage-widget"
        request = json.dumps(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "get_usage"},
            },
            separators=(",", ":"),
        )
        completed = subprocess.run(
            ClaudeCliLocator.command(
                cli,
                [
                    "-p",
                    "--safe-mode",
                    "--input-format",
                    "stream-json",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--no-session-persistence",
                ],
            ),
            input=request + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in completed.stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = message.get("response") or {}
            if message.get("type") != "control_response" or response.get("request_id") != request_id:
                continue
            if response.get("subtype") != "success":
                raise RuntimeError(str(response.get("error") or "Claude Code 用量讀取失敗。"))
            payload = response.get("response") or {}
            return ClaudeUsageSnapshot.from_response(
                payload,
                auth_method=auth_method,
                logged_in=True,
            )
        raise RuntimeError("Claude Code 未回傳用量資料。")

    def _auth_status(self, cli: Path) -> dict[str, Any]:
        completed = subprocess.run(
            ClaudeCliLocator.command(cli, ["auth", "status", "--json"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # claude.exe 是 290MB 的單檔包，冷啟動常超過 10 秒，太短會被誤判成失敗。
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"loggedIn": False, "authMethod": "unknown"}


class FetchSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    login_finished = Signal(str)


class UsageRing(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._remaining = 0.0
        self.setMinimumSize(160, 160)
        self.setAccessibleName("Codex 剩餘用量")

    def set_remaining(self, value: float) -> None:
        self._remaining = max(0.0, min(100.0, value))
        self.setAccessibleDescription(f"剩餘 {percent_text(self._remaining)}")
        self.update()

    def _accent(self) -> QColor:
        if self._remaining <= 15:
            return QColor("#F87171")
        if self._remaining <= 30:
            return QColor("#FBBF24")
        return QColor("#35E28A")

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 22
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        painter.setPen(QPen(QColor("#223047"), 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, 90 * 16, -360 * 16)
        painter.setPen(QPen(self._accent(), 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, 90 * 16, int(-360 * 16 * self._remaining / 100))

        painter.setPen(QColor("#F8FAFC"))
        value_font = QFont("Segoe UI Variable", 31, QFont.Weight.Bold)
        value_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        painter.setFont(value_font)
        value_rect = QRectF(0, self.height() / 2 - 36, self.width(), 58)
        painter.drawText(value_rect, Qt.AlignmentFlag.AlignCenter, percent_text(self._remaining))

        painter.setPen(QColor("#94A3B8"))
        painter.setFont(QFont("Segoe UI Variable", 10, QFont.Weight.Medium))
        label_rect = QRectF(0, self.height() / 2 + 22, self.width(), 25)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, "剩餘可用")


class AlertBubble(QFrame):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(304, 94)
        self.setStyleSheet(
            """
            #bubbleShell { background: #111C2E; border: 1px solid #3A4B65; border-radius: 15px; }
            #bubbleTitle { color: #F8FAFC; font-family: 'Segoe UI Variable'; font-size: 13px; font-weight: 750; }
            #bubbleMessage { color: #CBD5E1; font-family: 'Segoe UI Variable'; font-size: 11px; }
            #bubbleClose { min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; border: 0; border-radius: 8px; background: transparent; color: #94A3B8; font-size: 16px; }
            #bubbleClose:hover { background: #223047; color: #FFFFFF; }
            """
        )
        shell = QFrame(self)
        shell.setObjectName("bubbleShell")
        shell.setGeometry(2, 2, 300, 90)
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(15, 10, 10, 10)
        layout.setSpacing(3)
        header = QHBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setObjectName("bubbleTitle")
        close = QPushButton("×")
        close.setObjectName("bubbleClose")
        close.setAccessibleName("關閉提醒")
        close.clicked.connect(self.hide)
        header.addWidget(self.title_label)
        header.addStretch()
        header.addWidget(close)
        layout.addLayout(header)
        self.message_label = QLabel("")
        self.message_label.setObjectName("bubbleMessage")
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def show_message(self, title: str, message: str, duration_ms: int, anchor: QRectF) -> None:
        self.title_label.setText(title)
        self.message_label.setText(message)
        anchor_center = QPoint(round(anchor.center().x()), round(anchor.center().y()))
        screen = QApplication.screenAt(anchor_center) or QApplication.primaryScreen()
        available = screen.availableGeometry()
        if anchor_center.x() >= available.center().x():
            x = round(anchor.left()) - self.width() - 12
        else:
            x = round(anchor.right()) + 12
        y = round(anchor.center().y() - self.height() / 2)
        x = max(available.left() + 8, min(x, available.right() - self.width() - 8))
        y = max(available.top() + 8, min(y, available.bottom() - self.height() - 8))
        self.move(x, y)
        self.show()
        self.raise_()
        self.timer.start(max(3_000, duration_ms))


class MiniUsageWidget(QWidget):
    def __init__(self, owner: "UsageWidget") -> None:
        super().__init__(None)
        self.owner = owner
        self._remaining = 0.0
        self._press_global: QPoint | None = None
        self._start_position: QPoint | None = None
        self._dragged = False
        self.setWindowTitle("AI 用量懸浮圖示")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(74, 74)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("點一下展開 AI 用量小工具")
        self.setAccessibleName("AI 用量懸浮圖示")

    def set_remaining(self, value: float) -> None:
        self._remaining = max(0.0, min(100.0, value))
        self.setAccessibleDescription(f"目前最低剩餘量 {percent_text(self._remaining)}")
        self.update()

    def accent(self) -> QColor:
        if self._remaining <= 15:
            return QColor("#F87171")
        if self._remaining <= 30:
            return QColor("#FBBF24")
        return QColor("#35E28A")

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.setBrush(QColor("#0B1220"))
        painter.drawEllipse(QRectF(3, 3, 68, 68))
        ring = QRectF(9, 9, 56, 56)
        painter.setPen(QPen(QColor("#26344A"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, 90 * 16, -360 * 16)
        painter.setPen(QPen(self.accent(), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, 90 * 16, round(-360 * 16 * self._remaining / 100))
        painter.setPen(QColor("#F8FAFC"))
        painter.setFont(QFont("Segoe UI Variable", 13, QFont.Weight.Bold))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, percent_text(self._remaining))

    def show_docked(self) -> None:
        saved = self.owner.settings.value("mini_position")
        if isinstance(saved, QPoint):
            self.move(saved)
        else:
            screen = QApplication.screenAt(self.owner.frameGeometry().center()) or QApplication.primaryScreen()
            area = screen.availableGeometry()
            y = max(area.top() + 12, min(self.owner.y() + 40, area.bottom() - self.height() - 12))
            self.move(area.right() - self.width() - 8, y)
        self._snap_to_edge()
        self.show()
        self.raise_()

    def _snap_to_edge(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = area.left() + 8 if self.frameGeometry().center().x() < area.center().x() else area.right() - self.width() - 8
        y = max(area.top() + 8, min(self.y(), area.bottom() - self.height() - 8))
        self.move(x, y)
        self.owner.settings.setValue("mini_position", self.pos())

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._start_position = self.pos()
            self._dragged = False
            event.accept()

    def mouseMoveEvent(self, event: Any) -> None:
        if self._press_global is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._press_global
            if delta.manhattanLength() > 4:
                self._dragged = True
            if self._start_position is not None:
                self.move(self._start_position + delta)
            event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dragged:
                self._snap_to_edge()
            else:
                self.owner.expand_from_mini()
            self._press_global = None
            self._start_position = None
            event.accept()

    def contextMenuEvent(self, event: Any) -> None:
        menu = QMenu()
        show_action = menu.addAction("展開小工具")
        refresh_action = menu.addAction("立即更新")
        settings_action = menu.addAction("設定")
        menu.addSeparator()
        quit_action = menu.addAction("結束")
        selected = menu.exec(event.globalPos())
        if selected == show_action:
            self.owner.expand_from_mini()
        elif selected == refresh_action:
            self.owner.refresh()
        elif selected == settings_action:
            self.owner.open_settings()
        elif selected == quit_action:
            self.owner.quit_app()


class SettingsDialog(QDialog):
    settings_changed = Signal()

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("小工具設定")
        self.setFixedWidth(360)
        self.setModal(True)
        self.setStyleSheet(DIALOG_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("通知與更新")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        layout.addWidget(QLabel("自動更新頻率"))
        self.interval = QComboBox()
        self.interval.addItem("每 1 分鐘", 60)
        self.interval.addItem("每 5 分鐘", 300)
        self.interval.addItem("每 15 分鐘", 900)
        current_interval = int(settings.value("refresh_interval", 300))
        index = self.interval.findData(current_interval)
        self.interval.setCurrentIndex(max(0, index))
        layout.addWidget(self.interval)

        layout.addWidget(QLabel("剩餘多少時提醒"))
        self.threshold = QComboBox()
        for label, value in [("剩餘 20%", 20), ("剩餘 15%", 15), ("剩餘 10%", 10), ("剩餘 5%", 5)]:
            self.threshold.addItem(label, value)
        threshold_value = int(settings.value("low_threshold", 15))
        self.threshold.setCurrentIndex(max(0, self.threshold.findData(threshold_value)))
        layout.addWidget(self.threshold)

        self.notify_reset = QCheckBox("額度重置後通知我")
        self.notify_reset.setChecked(_setting_bool(settings, "notify_reset", True))
        layout.addWidget(self.notify_reset)

        self.notify_every_five = QCheckBox("每下降 5% 顯示提醒")
        self.notify_every_five.setChecked(_setting_bool(settings, "notify_every_five", True))
        layout.addWidget(self.notify_every_five)

        self.notify_low = QCheckBox("額度偏低時通知我")
        self.notify_low.setChecked(_setting_bool(settings, "notify_low", True))
        layout.addWidget(self.notify_low)

        layout.addWidget(QLabel("提醒訊息顯示時間"))
        self.bubble_duration = QComboBox()
        for label, value in [("5 秒", 5), ("10 秒", 10), ("15 秒", 15), ("30 秒", 30)]:
            self.bubble_duration.addItem(label, value)
        duration_value = int(settings.value("bubble_duration", 15))
        self.bubble_duration.setCurrentIndex(
            max(0, self.bubble_duration.findData(duration_value))
        )
        layout.addWidget(self.bubble_duration)

        layout.addWidget(QLabel("懸浮圖示顯示的數值"))
        self.mini_source = QComboBox()
        for label, value in [
            ("兩者取最低", "min"),
            ("只看 Codex", "codex"),
            ("只看 Claude Code", "claude"),
        ]:
            self.mini_source.addItem(label, value)
        source_value = str(settings.value("mini_source", "min"))
        self.mini_source.setCurrentIndex(max(0, self.mini_source.findData(source_value)))
        layout.addWidget(self.mini_source)

        self.autostart = QCheckBox("登入 Windows 時自動啟動")
        self.autostart.setChecked(_setting_bool(settings, "autostart", True))
        layout.addWidget(self.autostart)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("取消")
        cancel.setObjectName("secondaryButton")
        cancel.clicked.connect(self.reject)
        save = QPushButton("儲存設定")
        save.clicked.connect(self.save)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def save(self) -> None:
        self.settings.setValue("refresh_interval", self.interval.currentData())
        self.settings.setValue("low_threshold", self.threshold.currentData())
        self.settings.setValue("notify_reset", self.notify_reset.isChecked())
        self.settings.setValue("notify_every_five", self.notify_every_five.isChecked())
        self.settings.setValue("notify_low", self.notify_low.isChecked())
        self.settings.setValue("bubble_duration", self.bubble_duration.currentData())
        self.settings.setValue("mini_source", self.mini_source.currentData())
        self.settings.setValue("autostart", self.autostart.isChecked())
        configure_autostart(self.autostart.isChecked())
        self.settings.sync()
        self.settings_changed.emit()
        self.accept()


class UsageWidget(QWidget):
    def __init__(self, screenshot_path: Path | None = None, demo: bool = False) -> None:
        super().__init__()
        self.settings = QSettings("EricTools", "CodexUsageWidget")
        self.signals = FetchSignals()
        self.signals.succeeded.connect(self._on_fetch_success)
        self.signals.failed.connect(self._on_fetch_failure)
        self.signals.login_finished.connect(self._on_claude_login_finished)
        self._fetching = False
        self._claude_logging_in = False
        self._claude_action = "login"
        self._drag_offset: QPoint | None = None
        self._snapshot: UsageSnapshot | None = None
        self._claude_snapshot: ClaudeUsageSnapshot | None = None
        self._screenshot_path = screenshot_path
        self._demo = demo
        self._force_quit = False

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(400, 678)
        self.setStyleSheet(APP_STYLE)
        self.setAccessibleName(APP_NAME)

        self._build_ui()
        self._build_tray()
        self.alert_bubble = AlertBubble()
        self.mini = MiniUsageWidget(self)
        self._restore_position()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self._apply_timer_setting()
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self._update_time_labels)
        self.countdown_timer.start(30_000)

        if demo:
            QTimer.singleShot(
                100,
                lambda: self._on_fetch_success(
                    {"codex": _demo_snapshot(), "claude": _demo_claude_snapshot()}
                ),
            )
        else:
            QTimer.singleShot(100, self.refresh)

        if screenshot_path:
            QTimer.singleShot(9000 if not demo else 900, self._save_screenshot_and_quit)

    def _build_ui(self) -> None:
        shell = QFrame(self)
        shell.setObjectName("shell")
        shell.setGeometry(6, 6, 388, 666)

        root = QVBoxLayout(shell)
        root.setContentsMargins(22, 16, 22, 20)
        root.setSpacing(8)

        header = QHBoxLayout()
        brand = QLabel("AI  用量")
        brand.setObjectName("brand")
        header.addWidget(brand)
        header.addStretch()

        self.pin_button = QPushButton("固定")
        self.pin_button.setObjectName("textButton")
        self.pin_button.setCheckable(True)
        self.pin_button.setChecked(True)
        self.pin_button.setToolTip("保持在其他視窗上方")
        self.pin_button.setAccessibleName("切換固定在最上層")
        self.pin_button.clicked.connect(self._toggle_pin)
        header.addWidget(self.pin_button)

        self.hide_button = QPushButton("—")
        self.hide_button.setObjectName("windowButton")
        self.hide_button.setToolTip("縮成側邊懸浮圖示")
        self.hide_button.setAccessibleName("縮成側邊懸浮圖示")
        self.hide_button.clicked.connect(self.collapse_to_mini)
        header.addWidget(self.hide_button)
        root.addLayout(header)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 6, 0, 0)
        codex_name = QLabel("CODEX")
        codex_name.setObjectName("providerName")
        meta.addWidget(codex_name)
        self.plan_badge = QLabel("連線中")
        self.plan_badge.setObjectName("planBadge")
        meta.addWidget(self.plan_badge)
        meta.addStretch()
        self.sync_label = QLabel("正在取得最新用量…")
        self.sync_label.setObjectName("muted")
        meta.addWidget(self.sync_label)
        root.addLayout(meta)

        self.ring = UsageRing()
        self.ring.setFixedSize(164, 164)
        root.addWidget(self.ring, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.used_label = QLabel("已使用 —")
        self.used_label.setObjectName("usedLabel")
        self.used_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.used_label)

        self.cycle_card = QFrame()
        self.cycle_card.setObjectName("infoCard")
        cycle_layout = QVBoxLayout(self.cycle_card)
        cycle_layout.setContentsMargins(16, 12, 16, 12)
        cycle_layout.setSpacing(4)
        self.cycle_label = QLabel("額度週期")
        self.cycle_label.setObjectName("cardTitle")
        self.reset_label = QLabel("等待 Codex 回傳重置時間")
        self.reset_label.setObjectName("cardValue")
        cycle_layout.addWidget(self.cycle_label)
        cycle_layout.addWidget(self.reset_label)
        root.addWidget(self.cycle_card)

        self.claude_card = QFrame()
        self.claude_card.setObjectName("claudeCard")
        claude_layout = QVBoxLayout(self.claude_card)
        claude_layout.setContentsMargins(16, 12, 16, 12)
        claude_layout.setSpacing(7)

        claude_header = QHBoxLayout()
        claude_name = QLabel("CLAUDE CODE")
        claude_name.setObjectName("claudeName")
        claude_header.addWidget(claude_name)
        self.claude_badge = QLabel("偵測中")
        self.claude_badge.setObjectName("claudeBadge")
        claude_header.addWidget(self.claude_badge)
        claude_header.addStretch()
        claude_layout.addLayout(claude_header)

        self.claude_status = QLabel("正在檢查 Claude Code…")
        self.claude_status.setObjectName("claudeStatus")
        self.claude_status.setWordWrap(True)
        claude_layout.addWidget(self.claude_status)

        self.claude_login_button = QPushButton("登入 Claude Code")
        self.claude_login_button.setObjectName("claudeLoginButton")
        self.claude_login_button.setAccessibleName("開啟 Claude Code 登入流程")
        self.claude_login_button.clicked.connect(self._on_claude_button_clicked)
        self.claude_login_button.hide()
        claude_layout.addWidget(self.claude_login_button)

        (
            self.claude_five_section,
            self.claude_five_value,
            self.claude_five_bar,
            self.claude_five_reset,
        ) = self._create_quota_section("5 小時額度")
        claude_layout.addWidget(self.claude_five_section)
        (
            self.claude_week_section,
            self.claude_week_value,
            self.claude_week_bar,
            self.claude_week_reset,
        ) = self._create_quota_section("7 天額度 · 全部模型")
        claude_layout.addWidget(self.claude_week_section)
        self.claude_five_section.hide()
        self.claude_week_section.hide()
        root.addWidget(self.claude_card)

        root.addStretch()
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.refresh_button = QPushButton("立即更新")
        self.refresh_button.setAccessibleName("立即更新 Codex 與 Claude Code 用量")
        self.refresh_button.clicked.connect(self.refresh)
        settings_button = QPushButton("設定")
        settings_button.setObjectName("secondaryButton")
        settings_button.setAccessibleName("開啟小工具設定")
        settings_button.clicked.connect(self.open_settings)
        controls.addWidget(self.refresh_button, 2)
        controls.addWidget(settings_button, 1)
        root.addLayout(controls)

        self.error_label = QLabel("")
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)

    def _create_quota_section(
        self, title: str
    ) -> tuple[QFrame, QLabel, QProgressBar, QLabel]:
        section = QFrame()
        section.setObjectName("quotaSection")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        header = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName("quotaName")
        value = QLabel("剩餘 —")
        value.setObjectName("quotaValue")
        header.addWidget(name)
        header.addStretch()
        header.addWidget(value)
        layout.addLayout(header)
        bar = QProgressBar()
        bar.setObjectName("claudeProgress")
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(7)
        layout.addWidget(bar)
        reset = QLabel("重置時間未提供")
        reset.setObjectName("quotaReset")
        layout.addWidget(reset)
        return section, value, bar, reset

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        show_action = QAction("顯示小工具", self)
        show_action.triggered.connect(self.show_and_raise)
        refresh_action = QAction("立即更新", self)
        refresh_action.triggered.connect(self.refresh)
        settings_action = QAction("設定", self)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("結束", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show_action)
        menu.addAction(refresh_action)
        menu.addAction(settings_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def refresh(self) -> None:
        if self._fetching or self._demo:
            return
        self._fetching = True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("更新中…")
        self.sync_label.setText("正在同步")

        def task() -> None:
            result: dict[str, Any] = {}
            try:
                result["codex"] = CodexUsageClient().fetch()
            except Exception as exc:
                result["codex_error"] = str(exc)
            try:
                result["claude"] = ClaudeUsageClient().fetch()
            except subprocess.TimeoutExpired:
                result["claude_error"] = "Claude Code 回應逾時，下次自動更新會再試一次。"
            except Exception as exc:
                # 讀取失敗不等於未安裝：不在這裡偽造 unavailable 快照，
                # 也不再呼叫 locate()——它若出錯會讓執行緒死在 except 裡，
                # succeeded 訊號發不出去，整個面板就永遠凍結。
                result["claude_error"] = str(exc)
            self.signals.succeeded.emit(result)

        threading.Thread(target=task, daemon=True).start()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_claude_button_clicked(self) -> None:
        if self._claude_action == "locate":
            self._pick_claude_cli()
        else:
            self._start_claude_login()

    def _pick_claude_cli(self) -> None:
        if self._demo:
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "選擇 Claude Code 執行檔",
            str(Path(os.environ.get("APPDATA", str(Path.home()))) / "Claude" / "claude-code"),
            "Claude Code (claude.exe claude.cmd claude.bat);;所有檔案 (*)",
        )
        if not chosen:
            return
        self.settings.setValue(CLAUDE_CLI_SETTING, chosen)
        self.refresh()

    def _start_claude_login(self) -> None:
        if self._claude_logging_in or self._demo:
            return
        cli = ClaudeCliLocator.locate()
        if cli is None:
            self._show_error("找不到 Claude Code 執行檔，請先安裝或手動指定路徑。")
            return

        self._claude_logging_in = True
        self.claude_login_button.setEnabled(False)
        self.claude_login_button.setText("登入中…請在開啟的視窗完成")

        def task() -> None:
            message = ""
            try:
                # 授權流程需要使用者互動，開一個獨立主控台讓他們看得到並操作。
                process = subprocess.Popen(
                    ClaudeCliLocator.command(cli, ["auth", "login"]),
                    creationflags=CREATE_NEW_CONSOLE,
                )
                process.wait()
            except Exception as exc:
                message = str(exc)
            self.signals.login_finished.emit(message)

        threading.Thread(target=task, daemon=True).start()

    def _on_claude_login_finished(self, message: str) -> None:
        self._claude_logging_in = False
        self.claude_login_button.setEnabled(True)
        self.claude_login_button.setText("登入 Claude Code")
        if message:
            self._show_error(f"Claude Code 登入失敗：{message}")
            return
        self.refresh()

    def _on_fetch_success(self, result: dict[str, Any]) -> None:
        codex = result.get("codex")
        claude = result.get("claude")
        previous: UsageSnapshot | None = None
        previous_claude: ClaudeUsageSnapshot | None = None
        self._fetching = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("立即更新")
        self.error_label.hide()

        if isinstance(codex, UsageSnapshot):
            previous = self._load_previous_snapshot()
            self._snapshot = codex
            self._render_codex(codex)
            self._save_snapshot(codex)
        else:
            message = str(result.get("codex_error") or "Codex 用量暫時無法讀取。")
            self.sync_label.setText("Codex 同步失敗")
            self.error_label.setText(message)
            self.error_label.show()

        if isinstance(claude, ClaudeUsageSnapshot):
            previous_claude = self._load_previous_claude_snapshot()
            self._claude_snapshot = claude
            self._render_claude(claude, str(result.get("claude_error") or ""))
            if claude.rate_limits_available:
                self._save_claude_snapshot(claude)
        else:
            self._render_claude_error(
                str(result.get("claude_error") or "Claude Code 用量暫時無法讀取。")
            )

        self._update_mini_usage()
        if isinstance(codex, UsageSnapshot):
            self._handle_notifications(previous, codex)
        if isinstance(claude, ClaudeUsageSnapshot):
            self._handle_claude_notifications(previous_claude, claude)
        self.tray.setToolTip(self._tray_tooltip())

    def _update_mini_usage(self) -> None:
        source = _setting_str(self.settings, "mini_source") or "min"
        self.mini.set_remaining(
            mini_remaining(source, self._snapshot, self._claude_snapshot)
        )

    def _render_codex(self, snapshot: UsageSnapshot) -> None:
        plan_label = snapshot.plan_type.upper().replace("_", " ")
        self.plan_badge.setText(plan_label)
        self.sync_label.setText(datetime.fromtimestamp(snapshot.fetched_at).strftime("%H:%M 已更新"))

        primary = snapshot.primary
        if primary is not None:
            self.ring.set_remaining(primary.remaining_percent)
            self.used_label.setText(f"已使用 {percent_text(primary.used_percent)}")
            self.cycle_label.setText(duration_label(primary.duration_minutes))
            self.reset_label.setText(reset_time_label(primary.resets_at))
        else:
            self.ring.set_remaining(0)
            self.used_label.setText("目前沒有可顯示的額度")
            self.cycle_label.setText("額度週期")
            self.reset_label.setText("Codex 未提供此資料")

    def _render_claude_error(self, message: str) -> None:
        """暫時性讀取失敗：明說讀不到，不冒充「未安裝」，也不沿用舊畫面。"""
        self.claude_five_section.hide()
        self.claude_week_section.hide()
        self.claude_login_button.hide()
        self.claude_badge.setText("暫時無法讀取")
        self.claude_status.setText(message)

    def _render_claude(self, snapshot: ClaudeUsageSnapshot, error: str = "") -> None:
        self.claude_five_section.hide()
        self.claude_week_section.hide()
        self.claude_login_button.hide()
        if not snapshot.installed:
            self.claude_badge.setText("未安裝")
            self.claude_status.setText(
                "找不到 Claude Code 執行檔。裝好後按「立即更新」，"
                "或用下方按鈕直接指定 claude 的位置。"
            )
            self._claude_action = "locate"
            self.claude_login_button.setText("指定 claude 執行檔")
            self.claude_login_button.show()
            return
        if not snapshot.logged_in:
            self._claude_action = "login"
            self.claude_login_button.setText("登入 Claude Code")
            self.claude_badge.setText("未登入")
            self.claude_status.setText(
                "只在 Claude 桌面版登入不會建立 CLI 憑證。"
                "按下方按鈕完成登入後會自動更新。"
            )
            self.claude_login_button.show()
            return
        if not snapshot.rate_limits_available:
            self.claude_badge.setText((snapshot.subscription_type or "已連線").upper())
            if snapshot.auth_method and "api" in snapshot.auth_method.lower():
                self.claude_status.setText("目前使用 API 計費，沒有訂閱額度百分比。")
            elif error:
                self.claude_status.setText(error)
            else:
                self.claude_status.setText("Claude Code 目前沒有提供訂閱額度資料。")
            return

        self.claude_badge.setText((snapshot.subscription_type or "訂閱").upper())
        self.claude_status.setText("Claude 與 Claude Code 共用此訂閱額度")
        if snapshot.five_hour:
            self._set_quota_section(
                self.claude_five_section,
                self.claude_five_value,
                self.claude_five_bar,
                self.claude_five_reset,
                snapshot.five_hour,
            )
        if snapshot.seven_day:
            self._set_quota_section(
                self.claude_week_section,
                self.claude_week_value,
                self.claude_week_bar,
                self.claude_week_reset,
                snapshot.seven_day,
            )

    @staticmethod
    def _set_quota_section(
        section: QFrame,
        value: QLabel,
        bar: QProgressBar,
        reset: QLabel,
        window: UsageWindow,
    ) -> None:
        value.setText(f"剩餘 {percent_text(window.remaining_percent)}")
        bar.setValue(round(window.remaining_percent))
        reset.setText(reset_time_label(window.resets_at))
        section.show()

    def _on_fetch_failure(self, message: str) -> None:
        self._fetching = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("重新連線")
        self.sync_label.setText("同步失敗")
        friendly = message.strip() or "暫時無法讀取用量，稍後會自動重試。"
        self.error_label.setText(friendly)
        self.error_label.show()

    def _handle_notifications(
        self, previous: UsageSnapshot | None, current: UsageSnapshot
    ) -> None:
        if current.primary is None:
            return
        was_reset = detect_cycle_reset(previous, current)
        if _setting_bool(self.settings, "notify_reset", True) and was_reset:
            self.notify(
                "Codex 額度已重置",
                f"新的用量週期已開始，目前剩餘 {percent_text(current.primary.remaining_percent)}。",
            )

        old_window = previous.primary if previous else None
        every_five = _setting_bool(self.settings, "notify_every_five", True)
        if every_five and not was_reset:
            self._notify_five_percent_drop("Codex", old_window, current.primary)

        threshold = int(self.settings.value("low_threshold", 15))
        remaining = current.primary.remaining_percent
        previous_remaining = previous.primary.remaining_percent if previous and previous.primary else 101
        if (
            _setting_bool(self.settings, "notify_low", True)
            and not every_five
            and remaining <= threshold
            and previous_remaining > threshold
        ):
            self.notify(
                "Codex 額度偏低",
                f"目前剩餘 {percent_text(remaining)}，{reset_time_label(current.primary.resets_at)}。",
            )

    def _handle_claude_notifications(
        self,
        previous: ClaudeUsageSnapshot | None,
        current: ClaudeUsageSnapshot,
    ) -> None:
        if not current.rate_limits_available:
            return
        notify_reset = _setting_bool(self.settings, "notify_reset", True)
        notify_low = _setting_bool(self.settings, "notify_low", True)
        every_five = _setting_bool(self.settings, "notify_every_five", True)
        threshold = int(self.settings.value("low_threshold", 15))
        windows = [
            ("5 小時", previous.five_hour if previous else None, current.five_hour),
            ("7 天", previous.seven_day if previous else None, current.seven_day),
        ]
        for label, old_window, new_window in windows:
            if new_window is None:
                continue
            was_reset = detect_window_reset(old_window, new_window)
            if notify_reset and was_reset:
                self.notify(
                    f"Claude Code {label}額度已重置",
                    f"目前剩餘 {percent_text(new_window.remaining_percent)}。",
                )
            if every_five and not was_reset:
                self._notify_five_percent_drop(
                    f"Claude Code {label}", old_window, new_window
                )
            old_remaining = old_window.remaining_percent if old_window else 101
            if (
                notify_low
                and not every_five
                and new_window.remaining_percent <= threshold < old_remaining
            ):
                self.notify(
                    f"Claude Code {label}額度偏低",
                    f"目前剩餘 {percent_text(new_window.remaining_percent)}，{reset_time_label(new_window.resets_at)}。",
                )

    def _notify_five_percent_drop(
        self,
        service_label: str,
        previous: UsageWindow | None,
        current: UsageWindow,
    ) -> None:
        crossed_level = crossed_five_percent_level(previous, current)
        if crossed_level is None:
            return
        self.notify(
            f"{service_label} 用量提醒",
            f"剩餘 {percent_text(current.remaining_percent)}，已降到 {crossed_level}% 關卡。",
        )

    def notify(self, title: str, message: str) -> None:
        duration_ms = int(self.settings.value("bubble_duration", 15)) * 1000
        anchor_widget = self.mini if self.mini.isVisible() else self
        self.alert_bubble.show_message(
            title,
            message,
            duration_ms,
            QRectF(anchor_widget.frameGeometry()),
        )

    def _load_previous_snapshot(self) -> UsageSnapshot | None:
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return snapshot_from_state(raw)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _load_previous_claude_snapshot(self) -> ClaudeUsageSnapshot | None:
        try:
            raw = json.loads(CLAUDE_STATE_PATH.read_text(encoding="utf-8"))
            return claude_snapshot_from_state(raw)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    @staticmethod
    def _save_snapshot(snapshot: UsageSnapshot) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, STATE_PATH)

    @staticmethod
    def _save_claude_snapshot(snapshot: ClaudeUsageSnapshot) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CLAUDE_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(snapshot), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, CLAUDE_STATE_PATH)

    def _tray_tooltip(self) -> str:
        parts: list[str] = []
        if self._snapshot and self._snapshot.primary:
            parts.append(f"Codex {percent_text(self._snapshot.primary.remaining_percent)}")
        if self._claude_snapshot and self._claude_snapshot.five_hour:
            parts.append(
                f"Claude 5h {percent_text(self._claude_snapshot.five_hour.remaining_percent)}"
            )
        return " · ".join(parts) if parts else APP_NAME

    def _update_time_labels(self) -> None:
        if self._snapshot and self._snapshot.primary:
            self.reset_label.setText(reset_time_label(self._snapshot.primary.resets_at))
        if self._claude_snapshot and self._claude_snapshot.five_hour:
            self.claude_five_reset.setText(
                reset_time_label(self._claude_snapshot.five_hour.resets_at)
            )
        if self._claude_snapshot and self._claude_snapshot.seven_day:
            self.claude_week_reset.setText(
                reset_time_label(self._claude_snapshot.seven_day.resets_at)
            )

    def _apply_timer_setting(self) -> None:
        interval = int(self.settings.value("refresh_interval", 300))
        self.timer.start(max(60, interval) * 1000)
        self._update_mini_usage()

    def open_settings(self) -> None:
        if self.mini.isVisible():
            self.expand_from_mini()
        dialog = SettingsDialog(self.settings, self)
        dialog.settings_changed.connect(self._apply_timer_setting)
        dialog.exec()

    def _toggle_pin(self, checked: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.pin_button.setText("固定" if checked else "一般")
        self.show()

    def collapse_to_mini(self) -> None:
        self.settings.setValue("window_position", self.pos())
        self.alert_bubble.hide()
        self.hide()
        self._update_mini_usage()
        self.mini.show_docked()

    def expand_from_mini(self) -> None:
        self.alert_bubble.hide()
        self.mini.hide()
        self.show_and_raise()

    def show_and_raise(self) -> None:
        self.mini.hide()
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible():
                self.collapse_to_mini()
            else:
                self.expand_from_mini()

    def quit_app(self) -> None:
        self._force_quit = True
        self.alert_bubble.close()
        self.mini.close()
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event: Any) -> None:
        if self._force_quit:
            event.accept()
        else:
            event.ignore()
            self.collapse_to_mini()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 70:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:
        self._drag_offset = None
        self.settings.setValue("window_position", self.pos())
        super().mouseReleaseEvent(event)

    def _restore_position(self) -> None:
        saved = self.settings.value("window_position")
        if isinstance(saved, QPoint):
            self.move(saved)
            return
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.top() + 24)

    def _save_screenshot_and_quit(self) -> None:
        if self._screenshot_path:
            self._screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            self.grab().save(str(self._screenshot_path))
        self._force_quit = True
        QApplication.instance().quit()


def snapshot_from_state(raw: dict[str, Any]) -> UsageSnapshot:
    def window(value: Any) -> UsageWindow | None:
        if not isinstance(value, dict):
            return None
        return UsageWindow(
            used_percent=float(value["used_percent"]),
            duration_minutes=_optional_int(value.get("duration_minutes")),
            resets_at=_optional_int(value.get("resets_at")),
        )

    return UsageSnapshot(
        plan_type=str(raw["plan_type"]),
        limit_id=str(raw["limit_id"]),
        limit_name=raw.get("limit_name"),
        primary=window(raw.get("primary")),
        secondary=window(raw.get("secondary")),
        has_credits=bool(raw.get("has_credits", False)),
        unlimited_credits=bool(raw.get("unlimited_credits", False)),
        credit_balance=_optional_str(raw.get("credit_balance")),
        reset_credits=int(raw.get("reset_credits", 0)),
        reached_type=raw.get("reached_type"),
        fetched_at=int(raw.get("fetched_at", 0)),
    )


def claude_snapshot_from_state(raw: dict[str, Any]) -> ClaudeUsageSnapshot:
    def window(value: Any) -> UsageWindow | None:
        if not isinstance(value, dict):
            return None
        return UsageWindow(
            used_percent=float(value["used_percent"]),
            duration_minutes=_optional_int(value.get("duration_minutes")),
            resets_at=_optional_int(value.get("resets_at")),
        )

    return ClaudeUsageSnapshot(
        installed=bool(raw.get("installed", True)),
        logged_in=bool(raw.get("logged_in", True)),
        auth_method=_optional_str(raw.get("auth_method")),
        subscription_type=_optional_str(raw.get("subscription_type")),
        rate_limits_available=bool(raw.get("rate_limits_available", False)),
        five_hour=window(raw.get("five_hour")),
        seven_day=window(raw.get("seven_day")),
        seven_day_sonnet=window(raw.get("seven_day_sonnet")),
        seven_day_opus=window(raw.get("seven_day_opus")),
        fetched_at=int(raw.get("fetched_at", 0)),
    )


def _setting_str(settings: QSettings, key: str) -> str:
    value = settings.value(key, "")
    return str(value or "").strip()


def _setting_bool(settings: QSettings, key: str, default: bool) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes"}


def _set_frozen_autostart(executable: Path, enabled: bool) -> None:
    import winreg

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(
                key, "QuotaDock", 0, winreg.REG_SZ, f'"{executable.resolve()}"'
            )
        else:
            try:
                winreg.DeleteValue(key, "QuotaDock")
            except FileNotFoundError:
                pass
        try:
            winreg.DeleteValue(key, "CodexUsageWidget")
        except FileNotFoundError:
            pass


def configure_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return

    if getattr(sys, "frozen", False):
        _set_frozen_autostart(Path(sys.executable), enabled)
        return

    import winreg

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = pythonw if pythonw.is_file() else Path(sys.executable)
    command = f'"{launcher.resolve()}" "{Path(__file__).resolve()}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "QuotaDock", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "QuotaDock")
            except FileNotFoundError:
                pass
        try:
            winreg.DeleteValue(key, "CodexUsageWidget")
        except FileNotFoundError:
            pass


def install_frozen_release() -> bool:
    """Install a downloaded one-file release, launch the installed copy, then exit."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False

    current = Path(sys.executable).resolve()
    install_dir = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "QuotaDock"
    target = install_dir / "QuotaDock.exe"
    if str(current).casefold() == str(target.resolve()).casefold():
        return False

    install_dir.mkdir(parents=True, exist_ok=True)
    installer_env = os.environ.copy()
    installer_env["QUOTADOCK_INSTALL_TARGET"] = str(target)
    if target.exists():
        stop_script = (
            "$target = [IO.Path]::GetFullPath($env:QUOTADOCK_INSTALL_TARGET); "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.ExecutablePath -eq $target } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
            "Start-Sleep -Milliseconds 500"
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", stop_script],
            check=False,
            creationflags=CREATE_NO_WINDOW,
            env=installer_env,
        )

    shutil.copy2(current, target)
    shortcut_script = (
        "$target = $env:QUOTADOCK_INSTALL_TARGET; $folder = Split-Path -Parent $target; "
        "$path = Join-Path ([Environment]::GetFolderPath('Desktop')) 'QuotaDock.lnk'; "
        "$shell = New-Object -ComObject WScript.Shell; "
        "$shortcut = $shell.CreateShortcut($path); "
        "$shortcut.TargetPath = $target; $shortcut.WorkingDirectory = $folder; "
        "$shortcut.Description = 'Codex and Claude Code quota monitor'; "
        "$shortcut.IconLocation = \"$target,0\"; $shortcut.Save()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", shortcut_script],
        check=True,
        creationflags=CREATE_NO_WINDOW,
        env=installer_env,
    )
    _set_frozen_autostart(target, True)
    subprocess.Popen(
        [str(target)],
        cwd=str(install_dir),
        close_fds=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return True


def make_app_icon() -> QIcon:
    from PySide6.QtGui import QPixmap

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#0F172A"))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 16, 16)
    painter.setPen(QPen(QColor("#35E28A"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawArc(QRectF(13, 13, 38, 38), 45 * 16, 270 * 16)
    painter.end()
    return QIcon(pixmap)


def _demo_snapshot() -> UsageSnapshot:
    now = int(time.time())
    return UsageSnapshot(
        plan_type="plus",
        limit_id="codex",
        limit_name=None,
        primary=UsageWindow(52, 10080, now + 6 * 86400 + 5 * 3600),
        secondary=None,
        has_credits=False,
        unlimited_credits=False,
        credit_balance="0",
        reset_credits=0,
        reached_type=None,
        fetched_at=now,
    )


def _demo_claude_snapshot() -> ClaudeUsageSnapshot:
    now = int(time.time())
    return ClaudeUsageSnapshot(
        installed=True,
        logged_in=True,
        auth_method="claude.ai",
        subscription_type="max",
        rate_limits_available=True,
        five_hour=UsageWindow(34, 300, now + 3 * 3600 + 18 * 60),
        seven_day=UsageWindow(61, 10080, now + 4 * 86400 + 9 * 3600),
        seven_day_sonnet=None,
        seven_day_opus=None,
        fetched_at=now,
    )


APP_STYLE = """
QWidget { color: #F8FAFC; font-family: "Segoe UI Variable", "Segoe UI"; font-size: 13px; }
#shell { background: #0B1220; border: 1px solid #26344A; border-radius: 24px; }
#brand { font-size: 16px; font-weight: 700; letter-spacing: 1px; }
#providerName { color: #E2E8F0; font-size: 12px; font-weight: 800; letter-spacing: 1px; }
#planBadge { color: #0B1220; background: #35E28A; border-radius: 9px; padding: 3px 9px; font-size: 11px; font-weight: 800; }
#muted { color: #94A3B8; font-size: 11px; }
#usedLabel { color: #CBD5E1; font-size: 13px; font-weight: 600; margin-bottom: 2px; }
#infoCard { background: #111C2E; border: 1px solid #26344A; border-radius: 14px; }
#cardTitle { color: #94A3B8; font-size: 11px; font-weight: 600; }
#cardValue { color: #F8FAFC; font-size: 13px; font-weight: 650; }
#claudeCard { background: #171A20; border: 1px solid #5B4935; border-radius: 14px; }
#claudeName { color: #F3E8D5; font-size: 12px; font-weight: 800; letter-spacing: 1px; }
#claudeBadge { color: #17120B; background: #D8A96A; border-radius: 9px; padding: 3px 9px; font-size: 10px; font-weight: 800; }
#claudeStatus { color: #B8AA96; font-size: 11px; }
#quotaName { color: #D9D1C5; font-size: 11px; font-weight: 600; }
#quotaValue { color: #FFF8ED; font-size: 11px; font-weight: 700; }
#quotaReset { color: #9E9283; font-size: 10px; }
#claudeProgress { border: 0; border-radius: 3px; background: #342D25; }
#claudeProgress::chunk { border-radius: 3px; background: #D8A96A; }
#errorLabel { color: #FCA5A5; background: #2A151A; border: 1px solid #7F1D1D; border-radius: 10px; padding: 8px; margin-top: 8px; }
QPushButton { min-height: 38px; border: 0; border-radius: 11px; background: #35E28A; color: #07130D; font-weight: 750; padding: 0 14px; }
QPushButton:hover { background: #57E99D; }
QPushButton:pressed { background: #21C875; }
QPushButton:focus { border: 2px solid #FFFFFF; }
QPushButton:disabled { background: #26344A; color: #94A3B8; }
#claudeLoginButton { min-height: 30px; border-radius: 9px; background: #D8A96A; color: #17120B; font-size: 11px; font-weight: 800; padding: 0 12px; }
#claudeLoginButton:hover { background: #E5BC85; }
#claudeLoginButton:pressed { background: #C0904F; }
#claudeLoginButton:disabled { background: #342D25; color: #B8AA96; }
#secondaryButton { background: #182438; color: #E2E8F0; border: 1px solid #334155; }
#secondaryButton:hover { background: #223047; }
#textButton { min-width: 46px; min-height: 30px; padding: 0 8px; background: transparent; color: #94A3B8; font-size: 11px; border: 1px solid #26344A; border-radius: 9px; }
#textButton:checked { color: #35E28A; border-color: #2E8B61; background: #10271E; }
#windowButton { min-width: 32px; max-width: 32px; min-height: 30px; padding: 0; background: transparent; color: #94A3B8; font-size: 18px; }
#windowButton:hover { color: #FFFFFF; background: #1B293D; }
QToolTip { background: #111C2E; color: #F8FAFC; border: 1px solid #334155; padding: 6px; }
"""


DIALOG_STYLE = """
QDialog { background: #0B1220; color: #F8FAFC; }
QLabel { color: #CBD5E1; font-family: "Segoe UI Variable", "Segoe UI"; }
#dialogTitle { color: #F8FAFC; font-size: 20px; font-weight: 750; }
QComboBox { min-height: 38px; padding: 0 10px; border: 1px solid #334155; border-radius: 9px; background: #111C2E; color: #F8FAFC; }
QComboBox QAbstractItemView { background: #111C2E; color: #F8FAFC; selection-background-color: #244B39; }
QCheckBox { min-height: 30px; color: #E2E8F0; spacing: 9px; }
QPushButton { min-height: 38px; border: 0; border-radius: 10px; background: #35E28A; color: #07130D; font-weight: 700; padding: 0 15px; }
#secondaryButton { background: #182438; color: #E2E8F0; border: 1px solid #334155; }
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--demo", action="store_true", help="使用展示資料")
    parser.add_argument("--screenshot", type=Path, help="儲存介面截圖後結束")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.demo and not args.screenshot and install_frozen_release():
        return 0
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_app_icon())
    widget = UsageWidget(screenshot_path=args.screenshot, demo=args.demo)
    widget.show()
    if not args.demo and not args.screenshot and _setting_bool(widget.settings, "autostart", True):
        configure_autostart(True)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
