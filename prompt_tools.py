"""常用文字儲存、原生貼上與編輯介面；不建立或送出 AI 對話。"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QToolTip, QVBoxLayout, QWidget,
)


@dataclass(frozen=True)
class Prompt:
    id: str
    title: str
    body: str


def default_prompts() -> list[Prompt]:
    rows = [
        ("commit", "Commit＋推送", "幫我檢查這次的變更，建立詳細的繁體中文 Commit，包含為什麼要改、改了什麼、影響範圍與實際測試結果，再推送到目前分支對應的遠端。只提交本次工作相關內容，完成後告訴我分支名稱與 Commit 編號。"),
        ("sync", "同步遠端", "幫我檢查目前分支與本機修改，保留尚未提交的內容，同步目前分支對應的遠端最新程式碼。完成後簡單說明更新結果；若有衝突，列出需要處理的地方。"),
        ("flow", "查流程", "幫我從目前程式碼確認這個功能的實際流程、觸發條件，以及會讀寫哪些資料表。用白話說明，附上關鍵程式位置，並指出流程是否合理。先分析，不修改程式。"),
        ("error", "查錯誤", "幫我查這個錯誤的真正原因，根據程式碼、錯誤訊息與可取得的資料確認。說明觸發條件、問題位置與最小修正方式，區分已確認和推測的部分。先分析，不修改程式。"),
        ("fix", "修改＋測試", "依照剛剛確認的需求與方案修改程式，沿用現有專案風格，完成後執行相關測試。簡單說明改了什麼、為什麼改，以及測試結果；沒測到的部分請註明。"),
        ("docs", "前端文件", "幫我把這次異動整理成一份簡單的繁體中文前端串接文件，包含哪些 API 要調整、欄位怎麼傳、呼叫順序、請求與回應範例，以及注意事項。以實際程式為準，不要寫太複雜。"),
        ("sql", "查表／SQL", "幫我確認這個功能會讀寫哪些資料表、相關欄位與寫入條件，並提供能驗證問題的唯讀 SQL。清楚標示查詢用的資料庫與需要替換的條件。"),
        ("explain", "白話說明", "我還是不太懂，請用白話重新說明。先講結論，再用一個實際例子說明這功能在做什麼、為什麼需要，以及我接下來要做什麼。不要寫太長。"),
        ("readme", "整理 README", "幫我檢查並更新 README，讓第一次接手的人看得懂。說清楚專案用途、主要流程、如何設定與執行，以及常見問題。依實際程式整理，使用繁體中文，不要寫太複雜。"),
        ("weekly", "整理週報", "根據你能讀取的上週對話與工作紀錄，依專案名稱整理已完成的工作項目，每項用一句話，方便直接貼到週報。註明日期範圍，未完成或無法確認完成的事項另外列出。"),
        ("review", "檢查邏輯漏洞", "幫我檢查這次變更是否有邏輯漏洞、邊界情況或遺漏，優先列出會造成錯誤資料、重複處理或流程中斷的問題。每項附上依據與修正建議；沒有發現也請直接說明。"),
    ]
    return [Prompt(*row) for row in rows]


class PromptStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.warning = ""

    def load(self) -> list[Prompt]:
        if not self.path.exists():
            return default_prompts()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("prompts"), list):
                raise ValueError("格式不正確")
            result = []
            for row in data["prompts"]:
                if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("id", "title", "body")):
                    raise ValueError("指令資料不完整")
                result.append(Prompt(row["id"], row["title"], row["body"]))
            if len({p.id for p in result}) != len(result):
                raise ValueError("指令識別碼重複")
            return result
        except (OSError, ValueError, TypeError):
            self.warning = "指令檔無法讀取，暫用預設內容；原檔仍保留。"
            return default_prompts()

    def save(self, prompts: list[Prompt]) -> None:
        if any(not p.id.strip() or not p.title.strip() or not p.body.strip() for p in prompts) or len({p.id for p in prompts}) != len(prompts):
            raise ValueError("請填寫名稱與內容，且不可重複識別碼。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "prompts": [asdict(p) for p in prompts]}, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
        os.replace(temporary, self.path)


class WindowsPasteTarget:
    """記住別的程式最後一次取得焦點的視窗，忽略自己的主窗與對話框。"""
    def __init__(self) -> None:
        self.previous = 0
        self.hook = 0
        self.user32 = None
        if os.name != "nt":
            return
        from ctypes import wintypes as w
        self.user32 = u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = w.HWND
        u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        u.IsWindow.argtypes = [w.HWND]
        u.IsIconic.argtypes = [w.HWND]
        u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [w.HWND]
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        callback_type = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, ctypes.c_long, ctypes.c_long, w.DWORD, w.DWORD)
        self.callback = callback_type(lambda hook, event, hwnd, obj, child, thread, when: self._remember(hwnd))
        u.SetWinEventHook.argtypes = [w.DWORD, w.DWORD, w.HMODULE, callback_type, w.DWORD, w.DWORD, w.DWORD]
        u.SetWinEventHook.restype = w.HANDLE
        u.UnhookWinEvent.argtypes = [w.HANDLE]
        self._remember(u.GetForegroundWindow())
        self.hook = u.SetWinEventHook(3, 3, None, self.callback, 0, 0, 2)

    def _remember(self, hwnd: int) -> None:
        if not hwnd or self.user32 is None:
            return
        from ctypes import wintypes as w
        pid = w.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value and pid.value != os.getpid():
            self.previous = hwnd

    def activate(self, target: int) -> bool:
        u = self.user32
        if u is None or not target or not u.IsWindow(target):
            return False
        if u.IsIconic(target):
            u.ShowWindow(target, 9)
        u.SetForegroundWindow(target)
        return True

    def paste(self, target: int) -> bool:
        u = self.user32
        if u is None or u.GetForegroundWindow() != target:
            return False
        if any(u.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            return False
        from ctypes import wintypes as w
        class Keyboard(ctypes.Structure):
            _fields_ = [("vk", w.WORD), ("scan", w.WORD), ("flags", w.DWORD), ("time", w.DWORD), ("extra", ctypes.c_size_t)]
        class Mouse(ctypes.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("data", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD), ("extra", ctypes.c_size_t)]
        class Union(ctypes.Union):
            _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]
        class Input(ctypes.Structure):
            _fields_ = [("type", w.DWORD), ("data", Union)]
        def key(vk: int, up: bool = False) -> Input:
            return Input(type=1, data=Union(keyboard=Keyboard(vk=vk, flags=2 if up else 0)))
        events = (Input * 4)(key(0x11), key(0x56), key(0x56, True), key(0x11, True))
        u.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]
        sent = u.SendInput(4, events, ctypes.sizeof(Input))
        if sent != 4:
            releases = (Input * 2)(key(0x56, True), key(0x11, True))
            u.SendInput(2, releases, ctypes.sizeof(Input))
        return sent == 4

    def close(self) -> None:
        if self.hook and self.user32:
            self.user32.UnhookWinEvent(self.hook)
            self.hook = 0


class PasteController(QObject):
    finished = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None, target=None) -> None:
        super().__init__(parent)
        self.target = target if target is not None else WindowsPasteTarget()
        self.busy = False

    def paste(self, text: str) -> None:
        if self.busy:
            return
        self.busy = True
        self.busy_changed.emit(True)
        self._text = text
        self._hwnd = self.target.previous
        try:
            QApplication.clipboard().setText(text)
            activated = self.target.activate(self._hwnd)
        except Exception:
            self._finish("剪貼簿忙碌中，請再試一次。")
            return
        if not activated:
            self._finish("已複製，請回到輸入框按 Ctrl＋V。")
            return
        QTimer.singleShot(100, self._send)

    def _send(self) -> None:
        try:
            # 使用者或其他程式在等待期間改了剪貼簿時，不送出錯誤內容。
            if QApplication.clipboard().text() != self._text:
                self._finish("剪貼簿內容已變更，請重新點選指令。")
            elif self.target.paste(self._hwnd):
                self._finish("已貼上，確認內容後再自行送出。")
            else:
                self._finish("已複製，請回到輸入框按 Ctrl＋V。")
        except Exception:
            self._finish("已複製，請回到輸入框按 Ctrl＋V。")

    def _finish(self, message: str) -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.finished.emit(message)


class PromptRowDelegate(QStyledItemDelegate):
    """整列呈現選取及鍵盤焦點，避免原生文字焦點框形成雙重邊框。"""
    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), 44)

    def paint(self, painter, option, index):
        view = QStyleOptionViewItem(option)
        self.initStyleOption(view, index)
        selected = bool(view.state & QStyle.StateFlag.State_Selected)
        focused = bool(view.state & QStyle.StateFlag.State_HasFocus)
        hovered = bool(view.state & QStyle.StateFlag.State_MouseOver)
        rect = QRectF(view.rect).adjusted(2, 2, -2, -2)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#244B39" if selected else "#182438") if selected or hovered else Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#35E28A"), 2) if focused else Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 7, 7)
        painter.setFont(view.font)
        painter.setPen(QColor("#F8FAFC" if selected else "#CBD5E1"))
        text_rect = view.rect.adjusted(14, 0, -14, 0)
        text = view.fontMetrics.elidedText(view.text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.restore()


class PromptEditor(QDialog):
    def __init__(self, prompts: list[Prompt], style: str, parent=None, *, save_callback: Callable[[list[Prompt]], None] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("編輯常用指令")
        area = (parent.screen() if parent else QApplication.primaryScreen()).availableGeometry()
        self.setMinimumSize(min(580, area.width() - 32), min(440, area.height() - 32))
        self.resize(min(700, area.width() - 32), min(530, area.height() - 32))
        self.setStyleSheet(style + EDITOR_STYLE)
        self.prompts = list(prompts)
        self._save_callback = save_callback
        self._loading = False
        self._current = -1
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        title = QLabel("編輯常用指令"); title.setObjectName("dialogTitle"); root.addWidget(title)
        columns = QHBoxLayout(); columns.setSpacing(18); root.addLayout(columns, 1)
        left = QVBoxLayout(); columns.addLayout(left, 1)
        self.list = QListWidget(); self.list.setAccessibleName("指令清單"); left.addWidget(self.list, 1)
        self.list.setItemDelegate(PromptRowDelegate(self.list))
        self.list.setMouseTracking(True)
        operations = QGridLayout(); left.addLayout(operations)
        self.operation_buttons = []
        for i, (label, action) in enumerate((("新增", self.add), ("刪除", self.delete), ("上移", lambda: self.move(-1)), ("下移", lambda: self.move(1)))):
            button = QPushButton(label); button.setObjectName("secondaryButton"); button.clicked.connect(action); operations.addWidget(button, i // 2, i % 2)
            button.setAutoDefault(False)
            self.operation_buttons.append(button)
        self.fields = QWidget(); form = QVBoxLayout(self.fields); form.setContentsMargins(0, 0, 0, 0); columns.addWidget(self.fields, 2)
        form.addWidget(QLabel("按鈕名稱")); self.name = QLineEdit(); self.name.setMaxLength(80); self.name.setAccessibleName("按鈕名稱"); form.addWidget(self.name)
        form.addWidget(QLabel("貼上的內容")); self.body = QPlainTextEdit(); self.body.setAccessibleName("貼上的內容"); self.body.setTabChangesFocus(True); form.addWidget(self.body, 1)
        self.error = QLabel(""); self.error.setStyleSheet("color: #FCA5A5;"); self.error.setWordWrap(True); root.addWidget(self.error)
        self.error.setAccessibleName("儲存狀態")
        controls = QHBoxLayout(); controls.addStretch(); root.addLayout(controls)
        cancel = QPushButton("取消"); cancel.setObjectName("secondaryButton"); cancel.clicked.connect(self.reject); controls.addWidget(cancel)
        save = QPushButton("儲存變更"); save.clicked.connect(self.save); controls.addWidget(save)
        self.list.currentRowChanged.connect(self.select)
        self.name.textChanged.connect(self.change)
        self.body.textChanged.connect(self.change)
        self.rebuild(0)

    def rebuild(self, selected: int) -> None:
        self._loading = True
        self.list.clear(); self.list.addItems([p.title for p in self.prompts]); self._loading = False
        self.list.setCurrentRow(min(selected, len(self.prompts) - 1))
        self.select(self.list.currentRow())

    def select(self, index: int) -> None:
        if self._loading:
            return
        self._loading = True; self._current = index; self.fields.setEnabled(index >= 0)
        self.name.setText(self.prompts[index].title if index >= 0 else "")
        self.body.setPlainText(self.prompts[index].body if index >= 0 else "")
        self._loading = False
        self.operation_buttons[1].setEnabled(index >= 0)
        self.operation_buttons[2].setEnabled(index > 0)
        self.operation_buttons[3].setEnabled(0 <= index < len(self.prompts) - 1)

    def change(self, *args) -> None:
        if self._loading or self._current < 0:
            return
        index = self._current
        self.prompts[index] = replace(self.prompts[index], title=self.name.text(), body=self.body.toPlainText())
        self.list.item(index).setText(self.name.text() or "未命名指令")

    def add(self) -> None:
        self.prompts.append(Prompt(uuid.uuid4().hex, "新指令", "")); self.rebuild(len(self.prompts) - 1); self.name.setFocus()

    def delete(self) -> None:
        if self._current < 0:
            return
        if QMessageBox.question(self, "刪除指令", "確定刪除這個指令？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        index = self._current; self.prompts.pop(index); self.rebuild(index)

    def move(self, offset: int) -> None:
        i, target = self._current, self._current + offset
        if 0 <= i < len(self.prompts) and 0 <= target < len(self.prompts):
            self.prompts[i], self.prompts[target] = self.prompts[target], self.prompts[i]; self.rebuild(target)

    def save(self) -> None:
        for i, prompt in enumerate(self.prompts):
            if not prompt.title.strip() or not prompt.body.strip():
                self.list.setCurrentRow(i); self.error.setText("請填寫按鈕名稱與貼上的內容。"); return
        self.prompts = [replace(p, title=p.title.strip()) for p in self.prompts]
        try:
            if self._save_callback:
                self._save_callback(self.prompts)
        except (OSError, ValueError):
            self.error.setText("儲存失敗，編輯內容仍保留。請確認資料夾可寫入，再按「儲存變更」重試。")
            return
        self.error.clear()
        self.accept()


class PromptPanel(QFrame):
    def __init__(self, store: PromptStore, controller: PasteController, dialog_style: str, parent=None) -> None:
        super().__init__(parent)
        self.store, self.controller, self.dialog_style = store, controller, dialog_style
        self.prompts = store.load(); self.show_all = False
        self.setObjectName("infoCard")
        layout = QVBoxLayout(self); layout.setContentsMargins(12, 12, 12, 10); layout.setSpacing(8)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.Shape.NoFrame); self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: 0; }")
        self.content = QWidget(); self.grid = QGridLayout(self.content); self.grid.setContentsMargins(0, 0, 0, 0); self.grid.setSpacing(8); self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.content); layout.addWidget(self.scroll, 1)
        toolbar = QHBoxLayout(); toolbar.setSpacing(8)
        self.more = QPushButton("更多指令"); self.more.setObjectName("promptTool"); self.more.clicked.connect(self.toggle)
        self.edit_button = QPushButton("編輯指令"); self.edit_button.setObjectName("promptTool"); self.edit_button.clicked.connect(self.edit)
        toolbar.addWidget(self.more); toolbar.addWidget(self.edit_button); layout.addLayout(toolbar)
        controller.finished.connect(self.show_feedback)
        if store.warning:
            QTimer.singleShot(250, lambda: self.show_feedback(store.warning))
        controller.busy_changed.connect(self.set_busy)
        self.rebuild()

    def show_feedback(self, message: str) -> None:
        self.setAccessibleDescription(message)
        duration = 10000 if any(word in message for word in ("失敗", "Ctrl＋V", "忙碌", "無法", "變更")) else 4500
        QToolTip.showText(self.mapToGlobal(self.rect().topLeft()), message, self, self.rect(), duration)

    def set_busy(self, busy: bool) -> None:
        self.content.setEnabled(not busy); self.more.setEnabled(not busy); self.edit_button.setEnabled(not busy)

    def rebuild(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        rows = self.prompts if self.show_all else self.prompts[:8]
        for i, prompt in enumerate(rows):
            button = QPushButton(prompt.title); button.setObjectName("promptButton"); button.setMinimumWidth(0); button.setAccessibleName(prompt.title)
            button.setToolTip(prompt.title + "\n\n" + prompt.body)
            button.clicked.connect(lambda checked=False, p=prompt: self.controller.paste(p.body))
            self.grid.addWidget(button, i // 2, i % 2)
        if not rows:
            button = QPushButton("新增第一個指令"); button.setObjectName("promptButton"); button.clicked.connect(self.edit); self.grid.addWidget(button, 0, 0, 1, 2)
        self.more.setText("收起更多" if self.show_all else f"更多指令（{len(self.prompts)}）")
        self.more.setVisible(len(self.prompts) > 8)
        QTimer.singleShot(0, self.elide)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event); self.elide()

    def elide(self) -> None:
        rows = self.prompts if self.show_all else self.prompts[:8]
        for i, prompt in enumerate(rows):
            button = self.grid.itemAt(i).widget()
            button.setText(button.fontMetrics().elidedText(prompt.title, Qt.TextElideMode.ElideRight, max(10, button.width() - 20)))

    def toggle(self) -> None:
        self.show_all = not self.show_all; self.rebuild()

    def edit(self) -> None:
        dialog = PromptEditor(self.prompts, self.dialog_style, self, save_callback=self.store.save)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.prompts = dialog.prompts; self.rebuild(); self.show_feedback("指令已儲存。")


EDITOR_STYLE = """
QLineEdit, QPlainTextEdit, QListWidget { background: #111C2E; color: #F8FAFC; border: 1px solid #334155; border-radius: 9px; padding: 9px; font-size: 14px; }
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #35E28A; }
QListWidget { padding: 6px; }
QPushButton:focus { border: 2px solid #F8FAFC; }
"""
