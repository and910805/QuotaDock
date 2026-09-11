"""驗證資料保存與 Qt 操作；原生貼上以替身隔離，不操作使用者的視窗。"""
import ctypes
import json
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

import app
import prompt_tools as pt


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeTarget:
    previous = 123
    allow = True

    def __init__(self):
        self.sent = []

    def activate(self, hwnd):
        return self.allow

    def paste(self, hwnd):
        self.sent.append(hwnd)
        return True

    def close(self):
        pass


def test_store_roundtrip_reorder_unicode_and_empty(tmp_path):
    store = pt.PromptStore(tmp_path / "prompts.json")
    defaults = store.load()
    assert len(defaults) == 11
    text = '繁體中文\n```sql\nSELECT "$變數", `欄位`;\n```\n第二段'
    rows = [pt.Prompt("custom", "自訂", text)] + defaults[::-1]
    store.save(rows)
    assert pt.PromptStore(store.path).load() == rows
    store.save([])
    assert store.load() == []
    assert json.loads(store.path.with_suffix(".json.bak").read_text(encoding="utf-8"))["prompts"][0]["body"] == text


@pytest.mark.parametrize("raw", ['broken', '{"version":2,"prompts":[]}', '{"version":1,"prompts":[{}]}'])
def test_corruption_keeps_original(tmp_path, raw):
    path = tmp_path / "prompts.json"
    path.write_text(raw, encoding="utf-8")
    store = pt.PromptStore(path)
    assert len(store.load()) == 11
    assert store.warning and path.read_text(encoding="utf-8") == raw


def test_failed_atomic_replace_retains_previous_file(tmp_path, monkeypatch):
    store = pt.PromptStore(tmp_path / "prompts.json")
    original = store.load()
    store.save(original)
    monkeypatch.setattr(pt.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        store.save([])
    assert store.load() == original


def test_editor_changes_are_local_until_save(qapp, monkeypatch):
    original = pt.default_prompts()
    editor = pt.PromptEditor(original, app.DIALOG_STYLE)
    editor.name.setText("修改名稱")
    editor.body.setPlainText("中文\n第二行")
    editor.move(1)
    assert editor.prompts[1].title == "修改名稱"
    assert original[0].title == "Commit＋推送"
    editor.add()
    editor.save()
    assert editor.error.text() and editor.result() != QDialog.DialogCode.Accepted
    editor.name.setText("新功能")
    editor.body.setPlainText("請執行\n保留換行")
    count = len(editor.prompts)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    editor.delete()
    assert len(editor.prompts) == count
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    editor.delete()
    assert len(editor.prompts) == count - 1
    editor.save()
    assert editor.result() == QDialog.DialogCode.Accepted


def test_paste_preserves_text_and_prevents_overlap(qapp):
    target = FakeTarget()
    controller = pt.PasteController(target=target)
    messages = []
    controller.finished.connect(messages.append)
    controller.paste("中文\n```程式碼```\n")
    controller.paste("不該覆蓋")
    QTest.qWait(180)
    assert QApplication.clipboard().text() == "中文\n```程式碼```\n"
    assert target.sent == [123] and not controller.busy
    assert "自行送出" in messages[-1]


def test_editor_retains_draft_after_failed_save_and_can_retry(qapp, tmp_path):
    store = pt.PromptStore(tmp_path / "prompts.json")
    original = store.load()
    store.save(original)
    attempts = []
    def save(rows):
        attempts.append(rows)
        if len(attempts) == 1:
            raise PermissionError("測試鎖定")
        store.save(rows)
    editor = pt.PromptEditor(original, app.DIALOG_STYLE, save_callback=save)
    editor.show()
    editor.name.setText("保留這份草稿")
    editor.body.setPlainText("中文\n```sql\nSELECT 1;\n```")
    editor.move(1)
    editor.save()
    assert editor.isVisible() and editor.result() != QDialog.DialogCode.Accepted
    assert "編輯內容仍保留" in editor.error.text()
    assert editor.name.text() == "保留這份草稿" and store.load() == original
    editor.save()
    assert not editor.isVisible() and editor.result() == QDialog.DialogCode.Accepted
    assert store.load()[1].title == "保留這份草稿"
    assert store.load()[1].body == "中文\n```sql\nSELECT 1;\n```"


def test_editor_boundary_actions_and_keyboard_selection(qapp, monkeypatch):
    editor = pt.PromptEditor(pt.default_prompts()[:2], app.DIALOG_STYLE)
    editor.show()
    new, delete, up, down = editor.operation_buttons
    assert new.isEnabled() and delete.isEnabled() and not up.isEnabled() and down.isEnabled()
    editor.list.setFocus()
    QTest.keyClick(editor.list, Qt.Key.Key_Down)
    assert editor.name.text() == "同步遠端" and up.isEnabled() and not down.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    editor.delete()
    assert not up.isEnabled() and not down.isEnabled()
    editor.delete()
    assert new.isEnabled() and not delete.isEnabled() and not editor.fields.isEnabled()
    editor.add()
    assert editor.fields.isEnabled() and delete.isEnabled()
    editor.close()


def test_changed_clipboard_cancels_delayed_paste(qapp):
    target = FakeTarget()
    controller = pt.PasteController(target=target)
    controller.paste("原本")
    QApplication.clipboard().setText("已改變")
    QTest.qWait(180)
    assert not target.sent and not controller.busy


def test_missing_target_falls_back_to_copy(qapp):
    target = FakeTarget()
    target.allow = False
    controller = pt.PasteController(target=target)
    messages = []
    controller.finished.connect(messages.append)
    controller.paste("中文備用")
    assert QApplication.clipboard().text() == "中文備用"
    assert not target.sent and "Ctrl＋V" in messages[-1]


def test_native_input_only_sends_control_v_and_releases_keys():
    """攔截 Win32 呼叫確認按鍵序列，絕不把測試文字貼到實際視窗。"""
    class Send:
        def __init__(self):
            self.calls = []
        def __call__(self, count, events, size):
            self.calls.append([(e.data.keyboard.vk, e.data.keyboard.flags) for e in events])
            assert size == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
            return count
    class Native:
        SendInput = Send()
        def GetForegroundWindow(self): return 123
        def GetAsyncKeyState(self, key): return 0
    target = object.__new__(pt.WindowsPasteTarget)
    target.user32 = Native()
    assert target.paste(123)
    assert target.user32.SendInput.calls == [[(0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2)]]
    assert not target.paste(999)
    target.user32.GetAsyncKeyState = lambda key: 0x8000
    assert not target.paste(123)
    assert len(target.user32.SendInput.calls) == 1


def test_panel_more_empty_and_keyboard_focus(qapp, tmp_path):
    store = pt.PromptStore(tmp_path / "prompts.json")
    panel = pt.PromptPanel(store, pt.PasteController(target=FakeTarget()), app.DIALOG_STYLE)
    panel.setStyleSheet(app.APP_STYLE)
    panel.resize(342, 290)
    panel.show()
    QTest.qWait(30)
    assert panel.grid.count() == 8
    QTest.mouseClick(panel.more, Qt.MouseButton.LeftButton)
    assert panel.grid.count() == 11
    panel.edit_button.setFocus()
    assert panel.edit_button.hasFocus()
    panel.prompts = []
    panel.rebuild()
    QTest.qWait(30)
    assert panel.grid.count() == 1 and not panel.more.isVisible()
    panel.close()


@pytest.mark.parametrize("value", [None, "", "20", True, float("nan"), float("inf")])
def test_unknown_quota_never_becomes_zero(value):
    snapshot = app.UsageSnapshot.from_response({"rateLimits": {"primary": {"usedPercent": value}}})
    assert snapshot.primary is None
    assert app.mini_remaining("codex", snapshot, None) is None


def test_other_model_never_impersonates_main_codex():
    snapshot = app.UsageSnapshot.from_response({"rateLimitsByLimitId": {"review": {"primary": {"usedPercent": 20}}}})
    assert snapshot.primary is None
    assert "review" in snapshot.other_limits


@pytest.mark.parametrize("error,expected", [("not logged in", "尚未登入"), ("network disconnected", "更新失敗")])
def test_local_server_errors_and_shutdown(qapp, monkeypatch, error, expected):
    import io
    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(json.dumps({"id": 1, "result": {}}) + "\n" + json.dumps({"id": 2, "error": {"message": error}}) + "\n")
            self.stderr = io.StringIO()
            self.stopped = False
        def poll(self): return None
        def terminate(self): self.stopped = True
        def wait(self, timeout): return 0
    process = Process()
    monkeypatch.setattr(app.CodexCliLocator, "locate", lambda _: Path("fake.exe"))
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **kw: process)
    with pytest.raises(RuntimeError, match=expected):
        app.CodexUsageClient().fetch()
    methods = [json.loads(line)["method"] for line in process.stdin.getvalue().splitlines()]
    assert methods == ["initialize", "initialized", "account/rateLimits/read"]
    assert process.stopped


def test_modern_codex_cli_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = tmp_path / "OpenAI" / "Codex" / "bin" / "version" / "codex.exe"
    path.parent.mkdir(parents=True)
    path.touch()
    assert app.CodexCliLocator().locate() == path


def test_reset_is_explicitly_taiwan_time():
    assert "01/01 08:00" in app.reset_time_label(0, now=0)


def test_widget_stale_data_and_small_screen(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(app, "CLAUDE_STATE_PATH", tmp_path / "claude.json")
    monkeypatch.setattr(app, "PasteController", lambda parent: pt.PasteController(parent, target=FakeTarget()))
    widget = app.UsageWidget(demo=True)
    widget.show()
    QTest.qWait(130)
    before = widget.ring._remaining
    widget._on_fetch_success({"codex_error": "測試斷線"})
    assert widget.ring._remaining == before
    assert "同步失敗" in widget.sync_label.text()
    assert widget.prompt_panel.isEnabled()
    widget._height_limit = 530
    widget.setFixedSize(400, 530)
    QTest.qWait(30)
    assert widget.surface_scroll.verticalScrollBar().maximum() > 0
    assert widget.surface_scroll.geometry().bottom() < widget.height()
    assert widget.prompt_panel.scroll.verticalScrollBar().maximum() == 0
    for i in range(8):
        button = widget.prompt_panel.grid.itemAt(i).widget()
        assert widget.prompt_panel.scroll.viewport().rect().contains(button.mapTo(widget.prompt_panel.scroll.viewport(), button.rect().bottomRight()))
    assert widget.rect().contains(widget.refresh_button.mapTo(widget, widget.refresh_button.rect().bottomRight()))
    assert not widget.surface_scroll.isAncestorOf(widget.prompt_panel)
    widget._render_codex(app.UsageSnapshot.from_response({}))
    assert widget.ring._remaining is None
    assert "沒有" in widget.used_label.text()
    widget._force_quit = True
    widget.tray.hide()
    widget.close()


def test_short_quota_card_removes_gap_and_keeps_bottom_position(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(app, "CLAUDE_STATE_PATH", tmp_path / "claude.json")
    monkeypatch.setattr(app, "PasteController", lambda parent: pt.PasteController(parent, target=FakeTarget()))
    widget = app.UsageWidget(demo=True)
    widget.show()
    QTest.qWait(160)
    full_height = widget.height()
    bottom = widget.geometry().bottom()
    snapshot = {"codex": app._demo_snapshot(), "claude": app.ClaudeUsageSnapshot.unavailable(installed=False)}
    widget._on_fetch_success(snapshot)
    QTest.qWait(60)
    title = next(label for label in widget.findChildren(QLabel) if label.text() == "常用指令")
    gap = title.mapTo(widget, QPoint()).y() - widget.claude_card.mapTo(widget, widget.claude_card.rect().bottomLeft()).y() - 1
    assert 0 <= gap <= 16
    assert widget.height() < full_height
    assert widget.geometry().bottom() == bottom
    compact_height = widget.height()
    widget._fit_to_screen()
    widget._on_fetch_success(snapshot)
    QTest.qWait(60)
    assert widget.height() == compact_height
    widget._on_fetch_success({"codex": app._demo_snapshot(), "claude": app._demo_claude_snapshot()})
    QTest.qWait(60)
    assert widget.height() > compact_height and widget.height() <= widget._height_limit
    widget._force_quit = True
    widget.tray.hide()
    widget.close()


@pytest.mark.parametrize("message,badge", [("Codex 尚未登入，請先登入。", "未登入"), ("找不到 Codex 執行檔", "未找到"), ("網路連線中斷", "更新失敗")])
def test_first_quota_failure_exits_connecting_state(qapp, tmp_path, monkeypatch, message, badge):
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(app, "CLAUDE_STATE_PATH", tmp_path / "claude.json")
    monkeypatch.setattr(app, "PasteController", lambda parent: pt.PasteController(parent, target=FakeTarget()))
    widget = app.UsageWidget(demo=True)
    widget._on_fetch_success({"codex_error": message})
    assert widget.plan_badge.text() == badge
    assert widget.ring._remaining is None and widget.prompt_panel.isEnabled()
    snapshot = app._demo_snapshot()
    widget._on_fetch_success({"codex": snapshot, "claude": app._demo_claude_snapshot()})
    assert widget.plan_badge.text() == "PLUS" and not widget.plan_badge.styleSheet()
    widget._on_fetch_success({"codex_error": message})
    assert widget.plan_badge.text() == "PLUS" and widget.ring._remaining == snapshot.primary.remaining_percent
    assert "上次" in widget.sync_label.text()
    widget._force_quit = True
    widget.tray.hide()
    widget.close()


@pytest.mark.parametrize("value,expected", [(75, 75), (150, 150), ("125", 125), ("壞掉", 100), (0, 100), (500, 100)])
def test_ui_scale_setting_validation(tmp_path, value, expected):
    settings = QSettings(str(tmp_path / "scale.ini"), QSettings.Format.IniFormat)
    settings.setValue(app.UI_SCALE_SETTING, value)
    assert app.ui_scale_percent(settings) == expected


def test_scale_survives_reload_without_compounding_environment(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "scale.ini"), QSettings.Format.IniFormat)
    monkeypatch.delenv("QUOTA_PROMPTDOCK_BASE_SCALE_FACTOR", raising=False)
    monkeypatch.setenv("QT_SCALE_FACTOR", "1.5")
    settings.setValue(app.UI_SCALE_SETTING, 125)
    settings.sync()
    app.configure_ui_scale(settings)
    assert float(app.os.environ["QT_SCALE_FACTOR"]) == 1.875
    settings = QSettings(str(tmp_path / "scale.ini"), QSettings.Format.IniFormat)
    app.configure_ui_scale(settings)
    assert float(app.os.environ["QT_SCALE_FACTOR"]) == 1.875
    settings.setValue(app.UI_SCALE_SETTING, 100)
    app.configure_ui_scale(settings)
    assert float(app.os.environ["QT_SCALE_FACTOR"]) == 1.5


def test_scale_settings_cancel_and_save(qapp, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "scale.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(app, "configure_autostart", lambda enabled: None)
    dialog = app.SettingsDialog(settings)
    dialog.ui_scale.setCurrentIndex(dialog.ui_scale.findData(150))
    dialog.reject()
    assert app.ui_scale_percent(settings) == 100
    dialog = app.SettingsDialog(settings)
    dialog.ui_scale.setCurrentIndex(dialog.ui_scale.findData(125))
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert app.ui_scale_percent(settings) == 125


def test_scale_arrow_opens_menu_and_keyboard_selects(qapp, tmp_path):
    settings = QSettings(str(tmp_path / "scale.ini"), QSettings.Format.IniFormat)
    dialog = app.SettingsDialog(settings)
    dialog.show()
    QTest.qWait(30)
    combo = dialog.ui_scale
    QTest.mouseClick(combo, Qt.MouseButton.LeftButton, pos=QPoint(combo.width() - 16, combo.height() // 2))
    assert combo.view().isVisible()
    QTest.keyClick(combo.view(), Qt.Key.Key_Down)
    QTest.keyClick(combo.view(), Qt.Key.Key_Return)
    assert combo.currentData() == 110 and not combo.view().isVisible()
    dialog.close()


def test_scale_change_requests_restart_only_after_accept(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "PasteController", lambda parent: pt.PasteController(parent, target=FakeTarget()))
    widget = app.UsageWidget(demo=True)
    quit_requests = []
    monkeypatch.setattr(widget, "quit_app", lambda: quit_requests.append(True))
    class Dialog:
        settings_changed = widget.signals.login_finished
        def __init__(self, settings, parent): self.settings = settings
        def exec(self):
            self.settings.setValue(app.UI_SCALE_SETTING, 125)
            return QDialog.DialogCode.Accepted
    monkeypatch.setattr(app, "SettingsDialog", Dialog)
    widget.open_settings()
    assert widget._restart_requested and quit_requests == [True]
    widget._restart_requested = False
    widget.open_settings()
    assert not widget._restart_requested and quit_requests == [True]
    widget._force_quit = True
    widget.tray.hide()
    widget.close()
