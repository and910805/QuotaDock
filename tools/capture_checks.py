"""Qt 原生渲染驗證；示範資料、隔離設定，不送出真實鍵盤事件。"""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
import app
import prompt_tools as pt

parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
app.APP_DIR = args.output / '測試資料'
app.APP_DIR.mkdir(exist_ok=True)
app.STATE_PATH = app.APP_DIR / 'usage.json'
app.CLAUDE_STATE_PATH = app.APP_DIR / 'claude.json'
class Target:
    previous = 0
    def close(self): pass
    def activate(self, hwnd): return False
    def paste(self, hwnd): raise AssertionError('截圖不應送出按鍵')
app.PasteController = lambda parent: pt.PasteController(parent, target=Target())
qt = QApplication([])
widget = app.UsageWidget(demo=True)
widget.show()
QTest.qWait(200)
widget.grab().save(str(args.output / '常用指令.png'))
geometry = {}
for name, obj in [('額度區', widget.surface_scroll), ('圓環', widget.ring), ('週期', widget.cycle_card), ('Claude', widget.claude_card), ('指令', widget.prompt_panel), ('更新', widget.refresh_button)]:
    geometry[name] = [obj.width(), obj.height(), obj.mapTo(widget, obj.rect().topLeft()).y()]
geometry['指令捲動範圍'] = widget.prompt_panel.scroll.verticalScrollBar().maximum()
geometry['額度捲動範圍'] = widget.surface_scroll.verticalScrollBar().maximum()
widget.prompt_panel.toggle()
QTest.qWait(80)
widget.grab().save(str(args.output / '更多指令.png'))
editor = pt.PromptEditor(pt.default_prompts(), app.DIALOG_STYLE)
editor.show()
QTest.qWait(80)
editor.list.setFocus(Qt.FocusReason.TabFocusReason)
QTest.qWait(40)
editor.grab().save(str(args.output / '編輯.png'))
editor.operation_buttons[0].setFocus(Qt.FocusReason.TabFocusReason)
QTest.qWait(40)
editor.grab().save(str(args.output / '按鈕焦點.png'))
editor.close()
QTest.qWait(80)
widget.grab().save(str(args.output / 'Claude.png'))
widget._on_fetch_success({'codex_error': '連線中斷，請確認網路後再按立即更新。'})
QTest.qWait(80)
widget.grab().save(str(args.output / '舊資料.png'))
widget._on_fetch_success({'codex': app.UsageSnapshot.from_response({}), 'claude': app._demo_claude_snapshot()})
widget.prompt_panel.prompts = []
widget.prompt_panel.rebuild()
QTest.qWait(80)
widget.grab().save(str(args.output / '空白.png'))
metadata = {'縮放參數': os.environ.get('QT_SCALE_FACTOR', '1'), '裝置像素比例': widget.devicePixelRatioF(), '視窗邏輯尺寸': [widget.width(), widget.height()], '元件幾何': geometry, '畫面來源': 'Qt QWidget.grab，示範額度；非實機貼上測試'}
(args.output / '截圖資訊.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
widget.tray.hide()
widget._force_quit = True
widget.close()
