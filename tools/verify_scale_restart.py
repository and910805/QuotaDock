"""以隔離設定走過原生設定視窗、儲存、重啟及新程序縮放的完整流程。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from PIL import Image

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
out = parser.parse_args().output.resolve()
out.mkdir(parents=True, exist_ok=True)
os.environ["QUOTA_PROMPTDOCK_DATA_DIR"] = str(out / "測試資料")
os.environ["QT_SCALE_FACTOR"] = "1"
os.environ.pop("QUOTA_PROMPTDOCK_BASE_SCALE_FACTOR", None)
from PySide6.QtCore import QSettings, QTimer
import app
import prompt_tools as pt

settings = QSettings(str(app.APP_DIR / "preview.ini"), QSettings.Format.IniFormat)
settings.setValue(app.UI_SCALE_SETTING, 100)
settings.sync()
class Target:
    previous = 0
    def close(self): pass
    def activate(self, hwnd): return False
    def paste(self, hwnd): raise AssertionError("驗證不得送出真實按鍵")
app.PasteController = lambda parent: pt.PasteController(parent, target=Target())
app.configure_autostart = lambda enabled: None

class AutoDialog(app.SettingsDialog):
    def exec(self):
        def choose():
            self.ui_scale.setCurrentIndex(self.ui_scale.findData(125))
            self.save()
        QTimer.singleShot(80, choose)
        return super().exec()
app.SettingsDialog = AutoDialog

class AutoWidget(app.UsageWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        QTimer.singleShot(250, self.open_settings)
app.UsageWidget = AutoWidget
image_path = out / "重啟後125.png"
app.restart_command = lambda: [sys.executable, str(repo / "app.py"), "--demo", "--screenshot", str(image_path)]
children = []
original_popen = subprocess.Popen
def capture_child(*args, **kwargs):
    process = original_popen(*args, **kwargs)
    children.append(process)
    return process
app.subprocess.Popen = capture_child
sys.argv = [str(repo / "app.py"), "--demo"]
code = app.main()
assert code == 0 and len(children) == 1
assert children[0].wait(timeout=20) == 0
with Image.open(image_path) as picture:
    width = picture.width
settings.sync()
result = {"原程序正常結束": code == 0, "僅重啟一次": len(children) == 1,
          "新程序正常結束": children[0].returncode == 0,
          "新程序套用125比例": width == 500,
          "設定保存為125": app.ui_scale_percent(settings) == 125,
          "驗證範圍": "原始碼程序真實重啟；Qt 模態互動，不送出 OS 滑鼠或鍵盤事件"}
(out / "重啟量測.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=True))
raise SystemExit(0 if all(v for v in result.values() if isinstance(v, bool)) else 1)
