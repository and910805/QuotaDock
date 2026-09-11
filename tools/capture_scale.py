"""用保存的介面比例啟動 Qt，驗證整體縮放與設定、編輯視窗的可見性。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
import app
import prompt_tools as pt

parser = argparse.ArgumentParser()
parser.add_argument("percent", type=int, choices=app.UI_SCALE_CHOICES)
parser.add_argument("output", type=Path)
parser.add_argument("--claude-unavailable", action="store_true")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
app.APP_DIR = args.output / "測試資料"
app.APP_DIR.mkdir(exist_ok=True)
app.STATE_PATH = app.APP_DIR / "usage.json"
app.CLAUDE_STATE_PATH = app.APP_DIR / "claude.json"
if args.claude_unavailable:
    app._demo_claude_snapshot = lambda: app.ClaudeUsageSnapshot.unavailable(installed=False)
settings = QSettings(str(app.APP_DIR / "preview.ini"), QSettings.Format.IniFormat)
settings.setValue(app.UI_SCALE_SETTING, args.percent)
settings.sync()
app.configure_ui_scale(settings)
QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

class Target:
    previous = 0
    def close(self): pass
    def activate(self, hwnd): return False
    def paste(self, hwnd): raise AssertionError("截圖不得送出真實按鍵")

app.PasteController = lambda parent: pt.PasteController(parent, target=Target())
qt = QApplication([])
widget = app.UsageWidget(demo=True)
widget.show()
QTest.qWait(180)
widget.grab().save(str(args.output / "主畫面.png"))
ratio = widget.devicePixelRatioF()
button = widget.prompt_panel.grid.itemAt(0).widget()
result = {"保存比例": app.ui_scale_percent(widget.settings), "裝置像素比例": ratio,
          "主視窗實體寬度": widget.width() * ratio,
          "指令按鈕實體高度": button.height() * ratio,
          "指令文字實體高度": button.fontMetrics().height() * ratio}
dialog = app.SettingsDialog(widget.settings, widget)
dialog.show()
QTest.qWait(80)
dialog.ui_scale.setFocus(Qt.FocusReason.TabFocusReason)
dialog.grab().save(str(args.output / "縮放設定.png"))
save = next(b for b in dialog.findChildren(QPushButton) if b.text() == "儲存並套用")
result["設定讀到保存比例"] = dialog.ui_scale.currentData() == args.percent
result["縮放選單完整可見"] = dialog.scroll.viewport().rect().contains(dialog.ui_scale.mapTo(dialog.scroll.viewport(), dialog.ui_scale.rect().bottomRight()))
result["儲存按鈕完整可見"] = dialog.rect().contains(save.mapTo(dialog, save.rect().bottomRight()))
result["設定視窗未超出工作區"] = dialog.height() <= dialog.screen().availableGeometry().height()
dialog.close()
editor = pt.PromptEditor(pt.default_prompts(), app.DIALOG_STYLE, widget)
editor.show()
QTest.qWait(80)
editor.grab().save(str(args.output / "編輯視窗.png"))
result["編輯視窗未超出工作區"] = editor.height() <= editor.screen().availableGeometry().height() and editor.width() <= editor.screen().availableGeometry().width()
editor.close()
result["全部通過"] = all(v for v in result.values() if isinstance(v, bool))
(args.output / "縮放量測.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
widget._force_quit = True
widget.tray.hide()
widget.close()
print(json.dumps(result, ensure_ascii=True))
raise SystemExit(0 if result["全部通過"] else 1)
