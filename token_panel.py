"""Qt presentation and background scheduling for the local token ledger."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, QPointF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSizePolicy, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QStyledItemDelegate, QStyle, QMessageBox, QProgressBar)

from token_usage import (PERIODS, TAIWAN_TZ, TokenUsageCollector, TokenUsageStore,
                         UsageGroup, UsageReport)
from odometer import OdometerLabel, OdometerItemDelegate, KEY_ROLE

SHOW_TOKEN_SETTING = "tokens/show_panel"
TOKEN_PERIOD_SETTING = "tokens/period"


def compact_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,}"


def full_number(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def group_name(group: UsageGroup) -> str:
    return f"{group.model or '未知模型'} · {group.effort or '未知強度'}"


def coverage_text(report: UsageReport) -> str:
    parts = ["本機已記錄用量；依回合記錄的模型與強度分類。"]
    if report.first_at is not None:
        first = datetime.fromtimestamp(report.first_at, TAIWAN_TZ).strftime("%Y/%m/%d %H:%M")
        last = datetime.fromtimestamp(report.last_at, TAIWAN_TZ).strftime("%Y/%m/%d %H:%M")
        parts.append(f"已收錄紀錄：{first} ～ {last}（台灣時間；不代表期間無缺漏）。")
    else:
        parts.append("尚無可核對的逐次回應紀錄。")
    labels = {"legacy": "舊累計紀錄回合未納入", "conflict": "回應 ID 衝突，已排除",
              "unknown": "回應的模型／強度歸屬不明", "missing_details": "回應缺少快取或推理明細",
              "bad_lines": "損壞或過大的紀錄行", "invalid_usage": "無法核對的紀錄",
              "read_errors": "來源檔案暫時無法讀取，將自動重試"}
    gaps = [f"{label}：{report.issues.get(key, 0):,}" for key, label in labels.items() if report.issues.get(key)]
    if gaps:
        parts.append("歷史涵蓋不完整（以下為全部已掃描來源）：\n" + "\n".join(gaps))
    parts.append("快取輸入已含於輸入；推理已含於輸出。— 代表來源未提供完整明細，不是零。")
    parts.append("用量不換算訂閱額度或費用；未下載到本機的其他裝置／雲端紀錄不包含在內。")
    return "\n".join(parts)


class TokenUsageService(QObject):
    report_ready = Signal(object)
    progress = Signal(object)
    turns_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, period="today", root=None, parent=None,
                 collector_factory=TokenUsageCollector, thread_name="CodexTokenLedger"):
        super().__init__(parent)
        self.path, self.root = path, root
        self.collector_factory = collector_factory
        self.thread_name = thread_name
        self._period = period
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._refresh = True
        self._query = True
        self._turn_query = None
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def refresh(self):
        with self._lock:
            self._refresh = True
        self._wake.set()

    def set_period(self, period):
        if period not in dict((v, k) for k, v in PERIODS):
            return
        with self._lock:
            self._period = period
            self._query = True
        self._wake.set()

    def request_turns(self, request):
        with self._lock:
            self._turn_query = request
        self._wake.set()

    def _publish(self, store, force=False):
        with self._lock:
            period, query, turns = self._period, self._query, self._turn_query
            self._query = False
            self._turn_query = None
        if force or query:
            self.report_ready.emit(store.report(period))
        if turns:
            request_id, period, model, effort, page = turns
            rows, count = store.turns(period, model, effort, page)
            self.turns_ready.emit((request_id, rows, count))

    def _run(self):
        # The worker owns every database read/write, including detail queries.
        store = None
        next_scan = next_discovery = 0.0
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if store is None:
                    store = TokenUsageStore(self.path)
                collector = self.collector_factory(store, self.root)
                with self._lock:
                    refresh, self._refresh = self._refresh, False
                self._publish(store)
                now = time.monotonic()
                if refresh or now >= next_scan:
                    discover = refresh or now >= next_discovery
                    if discover:
                        next_discovery = now + 60
                    last_publish = last_progress = 0.0
                    self.progress.emit((True, 0, 0))
                    for progress in collector.scan(discover, self._stop):
                        now = time.monotonic()
                        if now - last_progress >= 0.25:
                            self.progress.emit((True, progress.completed, progress.files))
                            last_progress = now
                        self._publish(store, force=now - last_publish >= 1)
                        if now - last_publish >= 1:
                            last_publish = now
                    if not self._stop.is_set():
                        self._publish(store, force=True)
                        self.progress.emit((False, 0, 0))
                    next_scan = time.monotonic() + 10
            except Exception:
                # Never display raw log data or exception paths in the interface.
                self.failed.emit("Token 用量更新失敗，已保留上次結果；10 秒後重試。")
                self.progress.emit((False, 0, 0))
                next_scan = time.monotonic() + 10
                self._wake.wait(10)
            self._wake.wait(max(0.05, min(10, next_scan - time.monotonic())))


class ElidedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_text = ""
        self.setMinimumWidth(0)
        self.setTextFormat(Qt.TextFormat.PlainText)
        # 寬度由版面分配，不讓字寬回頭驅動版面——在捲動區裡，
        # 捲軸出現/消失改變寬度時才不會與重排互相遞迴。
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text):
        self.full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._elide()

    def _elide(self):
        text = self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight, max(0, self.width()))
        if text != self.text():
            super().setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class TokenPeriodCombo(QComboBox):
    """Draw a consistent chevron without the native Windows inset arrow button."""
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#A9C5DA"), 1.4, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        x, y = self.width() - 16, self.height() / 2
        painter.drawPolyline([QPointF(x - 3, y - 1.5), QPointF(x, y + 1.5), QPointF(x + 3, y - 1.5)])


class TokenUsagePanel(QFrame):
    period_changed = Signal(str)
    layout_changed = Signal()

    def __init__(self, settings, dialog_style: str, parent=None, *,
                 title="Codex Token 用量", period_setting=TOKEN_PERIOD_SETTING, source_hint="Codex"):
        super().__init__(parent)
        self.settings, self.dialog_style = settings, dialog_style
        self.title_text = title
        self.period_setting = period_setting
        self.source_hint = source_hint
        self.service = None
        self.report = None
        self.dialog = None
        self.is_demo = False
        self.row_limit = 3
        self._busy = None
        self._error = ""
        self.setObjectName("tokenCard")
        self.setAccessibleName(title)
        self.setStyleSheet("""
            #tokenCard { background: #121F30; border: 1px solid #31485B; border-radius: 14px; }
            #tokenCard QLabel { border: 0; background: transparent; color: #CBD5E1; font-size: 12px; }
            #tokenCard #tokenTitle { color: #F8FAFC; font-size: 13px; font-weight: 700; }
            #tokenCard #tokenTotal { color: #86EDC5; font-size: 25px; font-weight: 700; padding: 3px 0; }
            #tokenCard #effortBadge { color: #A5C7ED; background: #203249; border-radius: 4px;
                padding: 1px 5px; font-size: 10px; }
            #tokenCard QProgressBar { border: 0; border-radius: 1px; background: #243247; }
            #tokenCard QProgressBar::chunk { background: #5EBF9A; border-radius: 1px; }
            #tokenCard #tokenStatus { color: #94A3B8; font-size: 11px; }
            #tokenCard QComboBox { background: #1B2D41; color: #E2E8F0; border: 1px solid #385169;
                border-radius: 7px; min-height: 28px; max-height: 28px; padding: 0 26px 0 10px; font-size: 12px; }
            #tokenCard QComboBox:hover { background: #23394F; border-color: #52718A; }
            #tokenCard QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right;
                width: 24px; border: 0; background: transparent; }
            #tokenCard QComboBox::down-arrow { image: none; width: 0; height: 0; }
            #tokenCard QComboBox QAbstractItemView { background: #182438; color: #E2E8F0; selection-background-color: #244B39; }
            #tokenCard QPushButton { background: #17382F; border: 1px solid #33614F; color: #8CE9C2;
                border-radius: 7px; min-height: 28px; max-height: 28px; padding: 0 8px; font-size: 12px; font-weight: 500; }
            #tokenCard QPushButton:hover { color: #EDFFF6; background: #234C3D; border-color: #59977C; }
            #tokenCard QPushButton:pressed { background: #102A23; }
            #tokenCard QPushButton:focus, #tokenCard QComboBox:focus { border: 1px solid #70E5B1; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 8)
        layout.setSpacing(4)
        header = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("tokenTitle")
        header.addWidget(title_label)
        header.addStretch()
        self.period = TokenPeriodCombo()
        self.period.setFixedSize(96, 30)
        self.period.setAccessibleName(f"{source_hint} Token 統計日期區間")
        self.period.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for label, value in PERIODS:
            self.period.addItem(label, value)
        self.period.setCurrentIndex(max(0, self.period.findData(settings.value(period_setting, "today"))))
        self.period.currentIndexChanged.connect(self._change_period)
        header.addWidget(self.period)
        layout.addLayout(header)
        self.total = OdometerLabel("— Token")
        self.total.setObjectName("tokenTotal")
        layout.addWidget(self.total)
        self.rows = []
        self.row_details = []
        for _ in range(3):
            row = QWidget()
            row_stack = QVBoxLayout(row)
            row_stack.setContentsMargins(0, 2, 0, 3)
            row_stack.setSpacing(4)
            row_layout = QHBoxLayout()
            row_layout.setSpacing(6)
            name, value = ElidedLabel(), OdometerLabel()
            badge = QLabel()
            badge.setObjectName("effortBadge")
            row_layout.addWidget(name, 1)
            row_layout.addWidget(badge)
            row_layout.addWidget(value)
            row_stack.addLayout(row_layout)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setTextVisible(False)
            bar.setFixedHeight(3)
            row_stack.addWidget(bar)
            self.row_details.append((badge, bar))
            layout.addWidget(row)
            self.rows.append((row, name, value))
            row.hide()
        footer = QHBoxLayout()
        self.more = OdometerLabel()
        footer.addWidget(self.more, 1)
        self.details = QPushButton("查看全部  ›")
        self.details.setFixedSize(96, 30)
        self.details.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details.setAccessibleName("查看全部模型與強度的 Token 用量")
        self.details.clicked.connect(self.open_details)
        footer.addWidget(self.details)
        layout.addLayout(footer)
        self.status = OdometerLabel("本機已記錄用量 · 準備讀取")
        self.status.setObjectName("tokenStatus")
        layout.addWidget(self.status)

    def attach_service(self, service):
        self.service = service
        service.report_ready.connect(self.apply_report)
        service.progress.connect(self.set_progress)
        service.failed.connect(self.set_error)
        self.period_changed.connect(service.set_period)

    def _change_period(self):
        self.settings.setValue(self.period_setting, self.period.currentData())
        self.settings.sync()
        # Do not label a previous period's totals as the newly selected period.
        self.total.setText("讀取中…")
        for row, _, _ in self.rows:
            row.hide()
        self.more.setText("")
        if self.dialog:
            self.dialog.begin_period(self.period.currentData())
        self.period_changed.emit(self.period.currentData())
        self.layout_changed.emit()

    def apply_report(self, report: UsageReport):
        if report.period != self.period.currentData():
            return
        self.report = report
        self._error = ""
        self.total.setText(f"{compact_tokens(report.total_tokens)} Token" if report.first_at is not None else "— Token")
        self.total.setToolTip(f"{report.total_tokens:,} Token · {report.responses:,} 次回應")
        for index, (row, name, value) in enumerate(self.rows):
            show = index < min(len(report.groups), self.row_limit)
            row.setVisible(show)
            if show:
                group = report.groups[index]
                name.set_full_text(display_model(group.model))
                badge, bar = self.row_details[index]
                badge.setText(group.effort.title() or "未知")
                badge.setToolTip(group_name(group))
                bar.setValue(round(group.total_tokens / report.total_tokens * 1000) if report.total_tokens else 0)
                bar.setToolTip(f"占區間總用量 {group.total_tokens / report.total_tokens:.1%}" if report.total_tokens else "尚無用量")
                value.setText(compact_tokens(group.total_tokens))
                value.setToolTip(f"{group.total_tokens:,} Token")
        extra = max(0, len(report.groups) - self.row_limit)
        self.more.setText(f"另有 {extra} 組" if extra else (f"{len(report.groups)} 個組合" if report.groups else "此區間無用量"))
        self._render_status()
        if self.dialog and self.dialog.isVisible():
            self.dialog.apply_report(report)
        self.layout_changed.emit()

    def set_row_limit(self, limit):
        limit = max(0, min(3, limit))
        if limit != self.row_limit:
            self.row_limit = limit
            if self.report:
                previous_error = self._error
                self.apply_report(self.report)
                if previous_error:
                    self.set_error(previous_error)

    def set_progress(self, progress):
        busy, completed, files = progress
        self._busy = (completed, files) if busy else None
        self._render_status()

    def set_error(self, message):
        self._error = message
        self._render_status()

    def _render_status(self):
        if self.is_demo:
            self.status.setText("示範資料 · 非實際用量")
            self.status.setToolTip("僅展示介面，不讀取或寫入 Token 資料庫。")
            return
        text = "本機已記錄用量"
        if self._error:
            text += " · 更新失敗，稍後重試"
        elif self._busy is not None:
            text += f" · 匯入中 {self._busy[0]}/{self._busy[1]}"
        elif self.report and self.report.issues.get("read_errors"):
            text += " · 部分檔案讀取失敗"
        elif self.report and any(self.report.issues.values()):
            text += " · 統計不完整"
        elif self.report and not self.report.responses:
            text += " · 尚無此區間紀錄"
        elif self.report:
            text += " · " + datetime.fromtimestamp(self.report.updated_at, TAIWAN_TZ).strftime("%H:%M 更新")
        self.status.setText(text)
        detail = self._error or (coverage_text(self.report) if self.report else f"等待讀取 {self.source_hint} 本機紀錄。")
        self.status.setToolTip(detail)
        self.status.setAccessibleDescription(detail)

    def open_details(self):
        if self.dialog is None:
            self.dialog = TokenDetailsDialog(self, self.dialog_style)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        if self.report and self.report.period == self.period.currentData():
            self.dialog.apply_report(self.report)


def display_model(model):
    # Keep the complete version visible; this is presentation only, never a grouping key.
    return model.replace("gpt-", "GPT-", 1).replace("-astra", " Astra").replace("-sol", " Sol").replace("-luna", " Luna") if model else "未知模型"


class TokenCellDelegate(OdometerItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        # The whole selected row remains visible, including during keyboard navigation.
        option.state &= ~QStyle.StateFlag.State_HasFocus


class TokenDetailsDialog(QDialog):
    def __init__(self, panel: TokenUsagePanel, style: str):
        super().__init__(panel)
        self.panel = panel
        self.report = None
        self._selected = None
        self._page = 0
        self._request_id = 0
        self.setWindowTitle(f"{panel.title_text}明細")
        self.setObjectName("tokenDetails")
        self.setStyleSheet(style + """
            QDialog#tokenDetails { background: #0C1421; }
            #tokenDetails QLabel { background: transparent; border: 0; color: #DDE7F2; font-size: 13px; }
            #tokenDetails #dialogTitle { color: #F4F8FC; font-size: 23px; font-weight: 700; }
            #tokenDetails #muted { color: #92A4BA; font-size: 12px; }
            #tokenDetails #sectionTitle { color: #E4ECF5; font-size: 14px; font-weight: 600; }
            #tokenDetails #metricCard { background: #141F30; border: 1px solid #29364A; border-radius: 12px; }
            #tokenDetails #primaryMetric { background: #112B2B; border: 1px solid #28534B; border-radius: 12px; }
            #tokenDetails #metricValue { font-size: 25px; font-weight: 600; color: #F1F5FA; }
            #tokenDetails #primaryMetric #metricValue { color: #79E8BD; font-size: 29px; }
            #tokenDetails QTableWidget { background: #111C2C; alternate-background-color: #152132;
                color: #DDE7F2; selection-background-color: #21483F; selection-color: #EAFFF6;
                border: 1px solid #29384A; border-radius: 8px; font-size: 13px; outline: 0; }
            #tokenDetails QTableWidget::item { padding: 0 10px; border: 0; }
            #tokenDetails QTableWidget::item:selected { background: #21483F; color: #EAFFF6; border: 0; }
            #tokenDetails QHeaderView::section { background: #1B293B; color: #9EB0C5;
                font-size: 12px; font-weight: 500; border: 0; padding: 8px 10px; }
            #tokenDetails QTableCornerButton::section { background: #1B293B; border: 0; }
            #tokenDetails QPushButton { background: #192638; color: #C9D7E8; border: 1px solid #304057;
                border-radius: 7px; min-height: 28px; padding: 2px 13px; font-size: 12px; }
            #tokenDetails QPushButton:hover { background: #24374C; border-color: #537087; }
            #tokenDetails QPushButton:checked { color: #85E8BE; border-color: #36715C; background: #17392F; }
            #tokenDetails QPushButton:disabled { color: #586A80; background: #111C2C; border-color: #233146; }
            #tokenDetails QPushButton:focus, #tokenDetails QComboBox:focus { border-color: #79E8BD; }
            #tokenDetails QComboBox { background: #1A293B; color: #ECF3FA; border: 1px solid #3A5169;
                border-radius: 8px; min-height: 32px; padding: 2px 12px; min-width: 88px; font-size: 13px; }
            #tokenDetails QComboBox QAbstractItemView { background: #1A293B; color: #ECF3FA; selection-background-color: #285246; }
        """)
        available = self.screen().availableGeometry()
        self.resize(min(1080, available.width() - 32), min(800, available.height() - 48))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(12)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.title = QLabel(panel.title_text)
        self.title.setObjectName("dialogTitle")
        heading.addWidget(self.title)
        self.summary = OdometerLabel("本機已記錄用量 · 讀取中…")
        self.summary.setObjectName("muted")
        heading.addWidget(self.summary)
        header.addLayout(heading, 1)
        self.period = QComboBox()
        for label, value in PERIODS:
            self.period.addItem(label, value)
        self.period.setCurrentIndex(self.period.findData(panel.period.currentData()))
        self.period.currentIndexChanged.connect(self._change_period)
        self.period.setAccessibleName("明細日期區間")
        header.addWidget(self.period)
        layout.addLayout(header)
        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.metrics = []
        for index, (label, hint) in enumerate((("總 Token", "輸入 + 輸出"), ("輸入 Token", "包含快取輸入"), ("輸出 Token", "包含推理 Token"))):
            card = QFrame()
            card.setObjectName("primaryMetric" if index == 0 else "metricCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 12)
            card_layout.setSpacing(3)
            caption = QLabel(label)
            caption.setObjectName("muted")
            number = OdometerLabel("—")
            number.setObjectName("metricValue")
            note = QLabel(hint)
            note.setObjectName("muted")
            for child in (caption, number, note):
                card_layout.addWidget(child)
            metrics.addWidget(card, 1)
            self.metrics.append(number)
        layout.addLayout(metrics)
        section = QHBoxLayout()
        self.group_heading = OdometerLabel("模型與推理強度")
        self.group_heading.setObjectName("sectionTitle")
        section.addWidget(self.group_heading, 1)
        self.advanced = QPushButton("快取與推理明細")
        self.advanced.setCheckable(True)
        self.advanced.toggled.connect(self._toggle_advanced)
        section.addWidget(self.advanced)
        layout.addLayout(section)
        self.groups = self._table(["模型", "強度", "輸入", "輸出", "總 Token", "快取輸入¹", "快取寫入¹", "推理²"])
        self.groups.setAccessibleName("各模型與推理強度用量")
        self._toggle_advanced(False)
        self.groups.itemSelectionChanged.connect(self._select_group)
        layout.addWidget(self.groups)
        self.turn_title = QLabel("選擇模型與強度，查看各回合用量")
        self.turn_title.setObjectName("sectionTitle")
        layout.addWidget(self.turn_title)
        self.turn_table = self._table(["時間（台灣）", "回合", "輸入", "輸出", "總 Token", "回應次數"])
        self.turn_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.turn_table.setAccessibleName("選取模型的各回合用量")
        self.turn_table.setMinimumHeight(90)
        layout.addWidget(self.turn_table, 1)
        self.legend = QLabel("¹ 快取已含於輸入　 ² 推理已含於輸出　 ·　 — 表示來源明細缺漏")
        self.legend.setObjectName("muted")
        self.legend.setVisible(False)
        layout.addWidget(self.legend)
        navigation = QHBoxLayout()
        self.previous = QPushButton("上一頁")
        self.next = QPushButton("下一頁")
        self.previous.setObjectName("secondaryButton")
        self.next.setObjectName("secondaryButton")
        self.previous.clicked.connect(lambda: self._move_page(-1))
        self.next.clicked.connect(lambda: self._move_page(1))
        self.page_label = OdometerLabel()
        self.page_label.setObjectName("muted")
        navigation.addWidget(self.page_label, 1)
        navigation.addWidget(self.previous)
        navigation.addWidget(self.next)
        layout.addLayout(navigation)
        self.coverage = QLabel()
        self.coverage.setWordWrap(True)
        self.coverage.setObjectName("muted")
        footer = QHBoxLayout()
        footer.addWidget(self.coverage, 1)
        self.coverage_button = QPushButton("統計範圍與說明")
        self.coverage_button.clicked.connect(self._show_coverage)
        footer.addWidget(self.coverage_button)
        close = QPushButton("關閉")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)
        if panel.service:
            panel.service.turns_ready.connect(self._show_turns)

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(38)
        table.horizontalHeader().setMinimumSectionSize(68)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(False)
        for column in range(2, len(headers)):
            table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setItemDelegate(TokenCellDelegate(table))
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        return table

    def _toggle_advanced(self, checked):
        for column in range(5, 8):
            self.groups.setColumnHidden(column, not checked)
        if hasattr(self, "legend"):
            self.legend.setVisible(checked)
        self._fit_group_rows()

    def _fit_group_rows(self):
        if hasattr(self, "groups"):
            height = min(self.height(), self.screen().availableGeometry().height() - 48)
            reserve = 550 if self.advanced.isChecked() else 510
            limit = max(1, min(5, (height - reserve) // 38))
            self.groups.setFixedHeight(38 * max(1, min(limit, self.groups.rowCount())) + 52)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_group_rows()

    def _show_coverage(self):
        if self.report:
            QMessageBox.information(self, "統計範圍與說明", coverage_text(self.report))

    def _change_period(self):
        self._selected = None
        self._request_id += 1
        self.groups.setRowCount(0)
        self.turn_table.setRowCount(0)
        self.summary.setText("讀取中…")
        self.panel.period.setCurrentIndex(self.panel.period.findData(self.period.currentData()))

    def apply_report(self, report):
        changed = not self.report or self.report.period != report.period
        self.report = report
        self.period.blockSignals(True)
        self.period.setCurrentIndex(self.period.findData(report.period))
        self.period.blockSignals(False)
        self.summary.setText(("示範資料 · " if self.panel.is_demo else "") +
                             f"本機已記錄用量 · {len(report.groups)} 個組合 · {report.responses:,} 次回應")
        for label, value in zip(self.metrics, (report.total_tokens, sum(g.input_tokens for g in report.groups), sum(g.output_tokens for g in report.groups))):
            label.setText(full_number(value))
        self.group_heading.setText(f"模型與推理強度  ·  {len(report.groups)} 組")
        self.groups.blockSignals(True)
        self.groups.setRowCount(len(report.groups))
        selected_row = -1
        for index, group in enumerate(report.groups):
            cells = [display_model(group.model), group.effort.title() or "未知強度",
                     *[full_number(getattr(group, k)) for k in ("input_tokens", "output_tokens", "total_tokens",
                         "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")]]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 0:
                    item.setToolTip(group.model or "未知模型")
                if column == 1:
                    item.setForeground(QColor("#9BC4F5"))
                if column >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    item.setData(KEY_ROLE, repr((report.period, group.model, group.effort, column)))
                if column == 4:
                    item.setForeground(QColor("#85E8BE"))
                    font = QFont(self.font())
                    font.setBold(True)
                    item.setFont(font)
                self.groups.setItem(index, column, item)
            if self._selected == (group.model, group.effort) and not changed:
                selected_row = index
        self.groups.blockSignals(False)
        self.groups.itemDelegate().animate_items()
        detail = coverage_text(report)
        self._fit_group_rows()
        self.coverage.setText(("部分歷史未納入 · 查看說明" if any(report.issues.values()) else "本機紀錄 · 台灣時間") +
                             "\n" + datetime.fromtimestamp(report.updated_at, TAIWAN_TZ).strftime("%H:%M 更新"))
        self.coverage.setToolTip(detail)
        self.coverage.setAccessibleDescription(detail)
        if selected_row >= 0:
            self.groups.selectRow(selected_row)
            self._query_turns()
        elif report.groups:
            group = report.groups[0]
            self._selected = (group.model, group.effort)
            self._page = 0
            self.groups.selectRow(0)
            self._query_turns()
        else:
            self._selected = None
            self._request_id += 1
            self.turn_table.setRowCount(0)
            self.turn_title.setText("此區間尚無可核對的用量")
            self.previous.setEnabled(False)
            self.next.setEnabled(False)
            self.page_label.clear()

    def _select_group(self):
        index = self.groups.currentRow()
        if not self.report or not 0 <= index < len(self.report.groups):
            return
        group = self.report.groups[index]
        key = (group.model, group.effort)
        if key != self._selected:
            self._selected = key
            self._page = 0
            self._query_turns()

    def begin_period(self, period):
        self._selected = None
        self.report = None
        self._request_id += 1
        self.groups.setRowCount(0)
        self.turn_table.setRowCount(0)
        self.turn_title.setText("各回合紀錄 · 讀取中…")
        self.summary.setText("讀取中…")
        for label in self.metrics:
            label.setText("—")
        self.coverage.clear()
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        self.page_label.clear()
        self.period.blockSignals(True)
        self.period.setCurrentIndex(self.period.findData(period))
        self.period.blockSignals(False)

    def _move_page(self, delta):
        self._page = max(0, self._page + delta)
        self._query_turns()

    def _query_turns(self):
        if not self._selected or not self.report:
            return
        self._request_id += 1
        self.turn_table.setRowCount(0)
        self.page_label.setText("讀取回合紀錄…")
        self.turn_title.setText(f"各回合紀錄  /  {display_model(self._selected[0])} · {self._selected[1].title() or '未知強度'}")
        self.turn_title.setTextFormat(Qt.TextFormat.PlainText)
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        if self.panel.service:
            self.panel.service.request_turns((self._request_id, self.report.period, *self._selected, self._page))
        else:
            self._show_turns((self._request_id, [], 0))

    def _show_turns(self, result):
        request_id, rows, count = result
        if request_id != self._request_id:
            return
        self.turn_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            cells = [datetime.fromtimestamp(row["first_at"], TAIWAN_TZ).strftime("%Y/%m/%d %H:%M:%S"),
                     row["turn_id"][:12] or "未知回合", *[full_number(row[k]) for k in
                     ("input_tokens", "output_tokens", "total_tokens", "responses")]]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(f"對話：{row['thread_id']}\n回合：{row['turn_id']}" if column == 1 else text)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    item.setData(KEY_ROLE, repr((self.report.period, self._selected, row['thread_id'], row['turn_id'], column)))
                if column == 4:
                    item.setForeground(QColor("#85E8BE"))
                self.turn_table.setItem(index, column, item)
        pages = max(1, (count + 99) // 100)
        self.turn_table.itemDelegate().animate_items()
        self.page_label.setText(f"共 {count:,} 回合  ·  第 {self._page + 1} / {pages} 頁" if count else "此區間沒有可顯示的回合紀錄")
        self.previous.setEnabled(self._page > 0)
        self.next.setEnabled(self._page + 1 < pages)


def demo_report(period="today") -> UsageReport:
    groups = (
        UsageGroup("gpt-5.6-sol", "high", 1824000, 146000, 1970000, 1216000, 0, 87000, 35, 8),
        UsageGroup("gpt-5.6-luna", "low", 924000, 68000, 992000, 680000, 0, 12000, 28, 12),
        UsageGroup("gpt-5.6-luna", "high", 641000, 113000, 754000, 384000, 0, 64000, 17, 6),
        UsageGroup("gpt-5.6-sol", "low", 328000, 29000, 357000, 128000, 0, 4000, 9, 4),
    )
    now = datetime.now(TAIWAN_TZ).timestamp()
    return UsageReport(period, groups, sum(g.total_tokens for g in groups), 89, now, now, {}, 4, now)


def demo_claude_report(period="today") -> UsageReport:
    groups = (
        UsageGroup("claude-fable-5", "xhigh", 2114000, 188000, 2302000, 1723000, 262000, 41000, 42, 11),
        UsageGroup("claude-fable-5", "high", 873000, 96000, 969000, 702000, 84000, 9000, 24, 9),
        UsageGroup("claude-haiku-4-5", "medium", 214000, 31000, 245000, 118000, 22000, 0, 12, 5),
    )
    now = datetime.now(TAIWAN_TZ).timestamp()
    return UsageReport(period, groups, sum(g.total_tokens for g in groups), 78, now, now, {}, 3, now)
