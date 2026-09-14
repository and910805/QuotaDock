import time
from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, QSettings, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

import app
import prompt_tools as pt
import token_panel as tp
from test_token_usage import context, meta, response, write
from token_usage import TokenUsageCollector


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeTarget:
    def close(self):
        pass


@pytest.fixture
def widget(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(app, "CLAUDE_STATE_PATH", tmp_path / "claude.json")
    monkeypatch.setattr(app, "PasteController", lambda parent: pt.PasteController(parent, target=FakeTarget()))
    w = app.UsageWidget(demo=True)
    w.show()
    QTest.qWait(150)
    yield w
    if w.token_panel.dialog:
        w.token_panel.dialog.close()
    w._force_quit = True
    w.tray.hide()
    w.close()


def until(predicate, timeout=6000):
    deadline = time.monotonic() + timeout / 1000
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(20)
    assert predicate()


def test_summary_top_three_total_includes_other_models_and_period_persistence(qapp, tmp_path):
    path = str(tmp_path / "settings.ini")
    settings = QSettings(path, QSettings.Format.IniFormat)
    panel = tp.TokenUsagePanel(settings, app.DIALOG_STYLE)
    panel.show()
    report = tp.demo_report()
    panel.apply_report(report)
    assert panel.total.text() == "4.07M Token"
    assert panel.more.text() == "另有 1 組"
    assert len([r for r, _, _ in panel.rows if not r.isHidden()]) == 3
    panel.period.setCurrentIndex(panel.period.findData("week"))
    assert panel.total.text() == "讀取中…"
    panel.apply_report(report)
    assert panel.total.text() == "讀取中…"  # late result from a previous selection
    panel.apply_report(replace(report, period="week", total_tokens=123456))
    assert panel.total.text() == "123.5K Token"
    recreated = tp.TokenUsagePanel(QSettings(path, QSettings.Format.IniFormat), app.DIALOG_STYLE)
    assert recreated.period.currentData() == "week"
    panel.close()
    recreated.close()


def test_panel_position_visibility_and_no_gap_when_hidden(widget):
    panel = widget.token_panel
    claude_panel = widget.claude_token_panel
    title = next(label for label in widget.findChildren(QLabel) if label.text() == "常用指令")
    assert widget.surface_scroll.geometry().bottom() < panel.geometry().top()
    assert panel.geometry().bottom() < title.mapTo(widget, QPoint()).y()
    assert 0 <= title.mapTo(widget, QPoint()).y() - panel.mapTo(widget, panel.rect().bottomLeft()).y() <= 16
    # Claude Token 卡位於額度捲動區內、緊接 Claude 額度卡之後。
    assert claude_panel.parentWidget() is widget.claude_card.parentWidget()
    assert claude_panel.geometry().top() > widget.claude_card.geometry().bottom()
    widget.settings.setValue(tp.SHOW_TOKEN_SETTING, False)
    widget._reapply_view()
    QTest.qWait(50)
    assert panel.isHidden()
    assert claude_panel.isHidden()
    assert 0 <= title.mapTo(widget, QPoint()).y() - widget.surface_scroll.mapTo(widget, widget.surface_scroll.rect().bottomLeft()).y() <= 16
    widget.settings.setValue(tp.SHOW_TOKEN_SETTING, True)
    widget._reapply_view()
    QTest.qWait(30)
    assert not panel.isHidden()
    assert not claude_panel.isHidden()


def test_codex_quota_card_fully_visible_with_token_summary(widget):
    widget._height_limit = 940
    widget.setFixedSize(400, 940)
    widget._adapt_layout()
    QTest.qWait(100)
    assert not widget._compact_layout
    assert widget.ring.width() == 164
    viewport = widget.surface_scroll.viewport()
    assert viewport.rect().contains(widget.cycle_card.mapTo(viewport, widget.cycle_card.rect().bottomRight()))
    assert viewport.rect().contains(widget.codex_week_reset.mapTo(viewport, widget.codex_week_reset.rect().bottomRight()))


def test_both_codex_windows_fit_without_changing_vertical_layout(widget):
    snapshot = app._demo_snapshot()
    snapshot = replace(snapshot, primary=app.UsageWindow(20, 300, int(time.time()) + 3600),
                       secondary=app.UsageWindow(13, 10080, int(time.time()) + 600000))
    widget._height_limit = 940
    widget.setFixedSize(400, 940)
    widget._on_fetch_success({"codex": snapshot, "claude": app.ClaudeUsageSnapshot.unavailable(installed=False)})
    QTest.qWait(100)
    viewport = widget.surface_scroll.viewport()
    assert not widget._compact_layout
    assert viewport.rect().contains(widget.codex_five_reset.mapTo(viewport, widget.codex_five_reset.rect().bottomRight()))
    assert viewport.rect().contains(widget.cycle_card.mapTo(viewport, widget.cycle_card.rect().bottomRight()))


def test_token_controls_have_matching_hit_areas_and_right_edges(widget):
    panel = widget.token_panel
    assert panel.period.size() == panel.details.size()
    assert panel.period.width() == 96 and panel.period.height() == 30
    assert panel.period.mapTo(panel, panel.period.rect().topRight()).x() == panel.details.mapTo(panel, panel.details.rect().topRight()).x()
    panel.period.setCurrentIndex(panel.period.findData("week"))
    assert panel.period.currentText() == "近 7 天"
    panel.details.click()
    assert panel.dialog.isVisible() and panel.dialog.period.currentData() == "week"


def test_settings_toggle_defaults_cancel_save_and_reload(qapp, tmp_path, monkeypatch):
    path = str(tmp_path / "settings.ini")
    settings = QSettings(path, QSettings.Format.IniFormat)
    monkeypatch.setattr(app, "configure_autostart", lambda enabled: None)
    dialog = app.SettingsDialog(settings)
    assert dialog.show_tokens.isChecked()
    dialog.show_tokens.setChecked(False)
    dialog.reject()
    assert app.SettingsDialog(settings).show_tokens.isChecked()
    dialog = app.SettingsDialog(settings)
    dialog.show_tokens.setChecked(False)
    dialog.save()
    recreated = app.SettingsDialog(QSettings(path, QSettings.Format.IniFormat))
    assert not recreated.show_tokens.isChecked()
    recreated.close()


@pytest.mark.parametrize("height,rows", [(940,3),(740,1),(530,0),(430,0)])
def test_small_screen_panel_and_command_actions_remain_reachable(widget, height, rows):
    widget._height_limit = height
    widget.setFixedSize(400, height)
    widget._adapt_layout()
    QTest.qWait(60)
    panel = widget.token_panel
    assert panel.row_limit == rows
    for target in (panel.total, panel.details, widget.refresh_button):
        assert widget.rect().contains(target.mapTo(widget, target.rect().bottomRight()))
    assert panel.height() >= panel.minimumSizeHint().height()
    for i in range(8):
        button = widget.prompt_panel.grid.itemAt(i).widget()
        widget.prompt_panel.scroll.ensureWidgetVisible(button, 0, 0)
        QTest.qWait(5)
        assert widget.prompt_panel.scroll.viewport().rect().contains(
            button.mapTo(widget.prompt_panel.scroll.viewport(), button.rect().center()))


def test_error_keeps_previous_numbers_and_missing_data_is_explained(qapp, tmp_path):
    panel = tp.TokenUsagePanel(QSettings(str(tmp_path/'prefs.ini'), QSettings.Format.IniFormat), app.DIALOG_STYLE)
    r = replace(tp.demo_report(), issues={"legacy": 2, "unknown": 1})
    panel.apply_report(r)
    previous = panel.total.text()
    assert "統計不完整" in panel.status.text()
    assert "舊累計" in panel.status.toolTip()
    panel.set_error("測試失敗")
    panel.set_row_limit(1)
    assert panel.total.text() == previous and "更新失敗" in panel.status.text()
    panel.set_progress((True, 2, 10))
    panel.apply_report(r)
    assert "匯入中 2/10" in panel.status.text()
    panel.set_progress((False,0,0))
    assert "統計不完整" in panel.status.text()
    panel.close()


def test_demo_details_period_change_remains_labeled_and_populated(widget):
    widget.token_panel.open_details()
    dialog = widget.token_panel.dialog
    dialog.period.setCurrentIndex(dialog.period.findData("month"))
    assert widget.token_panel.period.currentData() == "month"
    assert "示範資料" in widget.token_panel.status.text()
    assert "示範資料" in dialog.summary.text()
    assert dialog.groups.rowCount() == 4


def test_details_replaces_selected_model_even_when_row_zero_stays_selected(widget):
    panel = widget.token_panel
    panel.open_details()
    dialog = panel.dialog
    report = tp.demo_report()
    replacement = replace(report.groups[0], model="gpt-6-astra", effort="xhigh")
    panel.apply_report(replace(report, groups=(replacement, *report.groups[1:])))
    assert dialog._selected == ("gpt-6-astra", "xhigh")
    assert "Astra" in dialog.turn_title.text() or "astra" in dialog.turn_title.text()


def test_details_invalidates_old_requests_when_main_period_changes(widget):
    panel = widget.token_panel
    panel.open_details()
    dialog = panel.dialog
    old_request = dialog._request_id
    panel.period_changed.disconnect()  # emulate a report still being fetched
    panel.period.setCurrentIndex(panel.period.findData("week"))
    assert dialog.period.currentData() == "week"
    assert dialog.groups.rowCount() == dialog.turn_table.rowCount() == 0
    assert all(label.text() == "—" for label in dialog.metrics)
    dialog._show_turns((old_request, [{"invalid": "old response must be ignored"}], 1))
    assert dialog.turn_table.rowCount() == 0
    panel.apply_report(tp.demo_report("week"))
    assert dialog._selected == ("gpt-5.6-sol", "high")


def test_details_preserves_selection_on_reorder_and_exposes_optional_counters(widget):
    panel = widget.token_panel
    panel.open_details()
    dialog = panel.dialog
    dialog.groups.selectRow(2)
    selected = dialog._selected
    report = tp.demo_report()
    panel.apply_report(replace(report, groups=tuple(reversed(report.groups))))
    assert dialog._selected == selected
    assert dialog.groups.currentRow() == 1
    assert dialog.groups.isColumnHidden(5)
    dialog.advanced.click()
    assert not dialog.groups.isColumnHidden(5)
    assert not dialog.legend.isHidden()
    assert dialog.metrics[0].text() == f"{report.total_tokens:,}"


def test_new_group_query_clears_previous_turn_rows(widget):
    panel = widget.token_panel
    panel.open_details()
    dialog = panel.dialog
    dialog.turn_table.setRowCount(3)
    panel.service = type("PendingService", (), {"request_turns": lambda self, request: None})()
    old_request = dialog._request_id
    dialog.groups.selectRow(1)
    assert dialog.turn_table.rowCount() == 0
    assert dialog._request_id > old_request
    assert not dialog.previous.isEnabled() and not dialog.next.isEnabled()


def test_blocked_installer_reports_failure_instead_of_freezing(widget, monkeypatch):
    # 防毒或政策擋下 %TEMP% 執行檔時，畫面不能停在「安裝中」。
    def refuse(*args, **kwargs):
        raise OSError("存取被拒。")
    monkeypatch.setattr(app.subprocess, "Popen", refuse)
    quits = []
    monkeypatch.setattr(widget, "quit_app", lambda: quits.append(True))
    widget._update_version = "9.9.9"
    widget._on_update_ready("C:/fake/installer.exe")
    assert "更新失敗" in widget.update_button.text()
    assert widget.update_button.isEnabled()
    assert not quits


def test_crashed_installer_reports_failure_and_keeps_app_running(widget, monkeypatch):
    class DeadProcess:
        def poll(self):
            return 1
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: DeadProcess())
    quits = []
    monkeypatch.setattr(widget, "quit_app", lambda: quits.append(True))
    widget._update_version = "9.9.9"
    widget._on_update_ready("C:/fake/installer.exe")
    assert widget.update_button.text() == "安裝中…請稍候"
    QTest.qWait(1600)
    assert "更新失敗" in widget.update_button.text()
    assert not quits


def test_running_installer_lets_the_old_app_quit(widget, monkeypatch):
    class LiveProcess:
        def poll(self):
            return None
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: LiveProcess())
    quits = []
    monkeypatch.setattr(widget, "quit_app", lambda: quits.append(True))
    widget._update_version = "9.9.9"
    widget._on_update_ready("C:/fake/installer.exe")
    QTest.qWait(1600)
    assert quits == [True]


def test_copy_with_retry_waits_out_a_file_lock(tmp_path, monkeypatch):
    source = tmp_path / "new.exe"
    source.write_bytes(b"new build")
    destination = tmp_path / "target.exe"
    failures = iter([PermissionError("locked"), PermissionError("locked")])
    original = app.shutil.copy2

    def flaky(src, dst):
        try:
            raise next(failures)
        except StopIteration:
            return original(src, dst)

    monkeypatch.setattr(app.shutil, "copy2", flaky)
    app._copy_with_retry(source, destination, attempts=5, delay=0)
    assert destination.read_bytes() == b"new build"

    monkeypatch.setattr(app.shutil, "copy2",
                        lambda src, dst: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError):
        app._copy_with_retry(source, destination, attempts=3, delay=0)


def test_update_checker_reads_both_repositories() -> None:
    assert app.GITHUB_REPO == "and910805/QuotaDock"
    assert app.UPDATE_REPOS == ("and910805/QuotaDock", "Andy61490963/Quota-PromptDock")
    assert app.RELEASE_DOWNLOAD_PREFIX.startswith("https://github.com/and910805/QuotaDock/")


def test_worker_hidden_collection_detail_queries_and_ui_responsiveness(qapp, tmp_path, monkeypatch):
    root = tmp_path / "codex"
    path = root / "sessions" / "session.jsonl"
    write(path, [meta(), context(), response()])
    original = TokenUsageCollector.scan
    def slow_scan(self, *args):
        for progress in original(self, *args):
            time.sleep(0.12)
            yield progress
    monkeypatch.setattr(TokenUsageCollector, "scan", slow_scan)
    settings = QSettings(str(tmp_path/'prefs.ini'), QSettings.Format.IniFormat)
    settings.setValue(tp.TOKEN_PERIOD_SETTING, "all")
    panel = tp.TokenUsagePanel(settings, app.DIALOG_STYLE)
    panel.hide()
    service = tp.TokenUsageService(tmp_path/'ledger.sqlite3', "all", root)
    panel.attach_service(service)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(10)
    try:
        service.start()
        until(lambda: panel.report is not None and panel.report.responses == 1 and panel._busy is None)
        assert len(ticks) >= 10 and panel.isHidden()
        write(path, [response("second")], "a")
        service.refresh()
        until(lambda: panel.report.responses == 2 and panel._busy is None)
        panel.show()
        panel.open_details()
        until(lambda: panel.dialog.turn_table.rowCount() == 1)
        assert panel.dialog.groups.item(0, 4).text() == "240"
        assert panel.dialog.turn_table.item(0, 4).text() == "240"
        assert panel.dialog.turn_table.item(0, 5).text() == "2"
    finally:
        timer.stop()
        service.stop()
        if panel.dialog:
            panel.dialog.close()
        panel.close()
    assert not service._thread.is_alive()
