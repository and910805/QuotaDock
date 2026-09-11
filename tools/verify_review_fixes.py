"""隔離 Qt 原生審查驗證，保存草稿、模態焦點和錯誤狀態的證據。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
from PIL import Image, ImageChops
import app
import prompt_tools as pt

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
out = parser.parse_args().output
out.mkdir(parents=True, exist_ok=True)
app.APP_DIR = out / "測試資料"
app.APP_DIR.mkdir(exist_ok=True)
app.STATE_PATH = app.APP_DIR / "usage.json"
app.CLAUDE_STATE_PATH = app.APP_DIR / "claude.json"

class Target:
    previous = 0
    def close(self): pass
    def activate(self, hwnd): return False
    def paste(self, hwnd): raise AssertionError("驗證不得送出真實按鍵")

app.PasteController = lambda parent: pt.PasteController(parent, target=Target())
qt = QApplication([])
report = {"驗證方式": "Windows Qt QTest 與 QWidget.grab；不代表 OS 跨程式鍵盤測試"}
pt.PromptStore(app.APP_DIR / "prompts.json").save(pt.default_prompts())
widget = app.UsageWidget(demo=True)
widget.show()
QTest.qWait(180)
modal = {}

def inspect_modal():
    dialog = QApplication.activeModalWidget()
    modal["初始焦點為清單"] = QApplication.focusWidget() is dialog.list
    contained = []
    for modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
        for _ in range(20):
            focused = QApplication.focusWidget()
            contained.append(focused is not None and dialog.isAncestorOf(focused))
            QTest.keyClick(dialog, Qt.Key.Key_Tab, modifiers)
    modal["Tab及ShiftTab保持在對話框"] = all(contained)
    add = dialog.operation_buttons[0]
    dialog.name.setFocus()
    QTest.qWait(30)
    add.grab().save(str(out / "新增未聚焦.png"))
    add.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.qWait(30)
    add.grab().save(str(out / "新增聚焦.png"))
    modal["新增按鈕確實聚焦"] = add.hasFocus()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)

widget.prompt_panel.edit_button.setFocus()
QTimer.singleShot(80, inspect_modal)
widget.prompt_panel.edit_button.click()
QTest.qWait(50)
modal["Esc關閉"] = QApplication.activeModalWidget() is None
modal["焦點返回編輯按鈕"] = widget.prompt_panel.edit_button.hasFocus()
diff = ImageChops.difference(Image.open(out / "新增未聚焦.png").convert("RGB"), Image.open(out / "新增聚焦.png").convert("RGB"))
modal["焦點樣式像素有變化"] = diff.getbbox() is not None
report["編輯視窗"] = modal

original_save = widget.prompt_panel.store.save
attempts = []
def flaky_save(rows):
    attempts.append(rows)
    if len(attempts) == 1:
        raise PermissionError("驗證用模擬檔案鎖定")
    original_save(rows)
widget.prompt_panel.store.save = flaky_save
saved = {}
def retry_save():
    dialog = QApplication.activeModalWidget()
    dialog.name.setText("這份草稿仍會保留")
    dialog.body.setPlainText("修改後的中文草稿\n```sql\nSELECT 1;\n```")
    dialog.move(1)
    save = next(b for b in dialog.findChildren(QPushButton) if b.text() == "儲存變更")
    save.click()
    QTest.qWait(40)
    saved["失敗後編輯視窗保留"] = dialog.isVisible()
    saved["失敗後草稿及順序保留"] = dialog.name.text() == "這份草稿仍會保留" and dialog.list.currentRow() == 1
    saved["錯誤可見且可重試"] = "編輯內容仍保留" in dialog.error.text() and save.isEnabled()
    dialog.grab().save(str(out / "儲存失敗保留草稿.png"))
    save.click()
    saved["重試成功才關閉"] = not dialog.isVisible()
QTimer.singleShot(80, retry_save)
widget.prompt_panel.edit_button.click()
saved["重開檔案保留新內容"] = pt.PromptStore(widget.prompt_panel.store.path).load()[1].title == "這份草稿仍會保留"
saved["主畫面同步成功內容"] = widget.prompt_panel.prompts[1].title == "這份草稿仍會保留"
report["儲存流程"] = saved

widget._force_quit = True
widget.tray.hide()
widget.close()
demo_snapshot = app._demo_snapshot
app._demo_snapshot = lambda: None
widget = app.UsageWidget(demo=True)
widget.show()
QTest.qWait(160)
app._demo_snapshot = demo_snapshot
widget._on_fetch_success({"codex_error": "Codex 尚未登入，請開啟 Codex 完成登入後再按立即更新。", "claude": app.ClaudeUsageSnapshot.unavailable(installed=False)})
QTest.qWait(40)
widget.grab().save(str(out / "首次未登入.png"))
report["未登入徽章"] = widget.plan_badge.text() == "未登入"
report["未登入仍可用指令"] = widget.prompt_panel.isEnabled()
widget._on_fetch_success({"codex": app._demo_snapshot(), "claude": app._demo_claude_snapshot()})
widget._height_limit = 664
widget.setFixedSize(400, 664)
QTest.qWait(40)
widget.surface_scroll.verticalScrollBar().setValue(widget.surface_scroll.verticalScrollBar().maximum())
QTest.qWait(40)
widget.grab().save(str(out / "小視窗捲動至Claude.png"))
report["小視窗指令不需捲動"] = widget.prompt_panel.scroll.verticalScrollBar().maximum() == 0
report["小視窗Claude可完整查看"] = widget.surface_scroll.viewport().rect().contains(widget.claude_card.mapTo(widget.surface_scroll.viewport(), widget.claude_card.rect().topLeft())) and widget.surface_scroll.viewport().rect().contains(widget.claude_card.mapTo(widget.surface_scroll.viewport(), widget.claude_card.rect().bottomRight()))
widget._force_quit = True
widget.tray.hide()
widget.close()
def results(value):
    if isinstance(value, dict):
        return all(results(item) for item in value.values())
    return value if isinstance(value, bool) else True
report["全部通過"] = results(report)
(out / "修正量測.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=True))
raise SystemExit(0 if report["全部通過"] else 1)
