<div align="center">

# QuotaDock

**常駐 Windows 桌面的 AI 開發工具額度監控器**

即時掌握 OpenAI Codex 與 Claude Code 的訂閱額度、重置倒數與低額度提醒

[![Release](https://img.shields.io/github/v/release/and910805/QuotaDock?label=release&color=35E28A)](https://github.com/and910805/QuotaDock/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows)](https://github.com/and910805/QuotaDock/releases/latest)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/windows/)

![QuotaDock 主畫面](widget-demo-new.png)

</div>

---

## 目錄

- [功能特色](#功能特色)
- [運作原理](#運作原理)
- [安裝](#安裝)
- [使用說明](#使用說明)
- [設定選項](#設定選項)
- [疑難排解](#疑難排解)
- [開發](#開發)
- [隱私](#隱私)
- [授權](#授權)

## 功能特色

| | |
|---|---|
| 📊 **雙服務監控** | 同一面板顯示 Codex 與 Claude Code 的 5 小時／7 天額度與重置倒數 |
| 🔔 **智慧提醒** | 額度每下降 5%、額度偏低、新週期重置時桌面通知 |
| 🎯 **側邊懸浮圓環** | 縮小後貼齊螢幕邊緣，一眼看到剩餘百分比，點擊展開 |
| 🔑 **內建登入流程** | Claude Code 未登入時一鍵啟動官方授權流程，完成後自動更新 |
| 🔍 **自動尋找 CLI** | 自動偵測 PATH、npm、Claude 桌面版安裝路徑，找不到時自動掃描 |
| 🔄 **自動檢查更新** | 每天問一次 GitHub release，有新版就在面板顯示更新按鈕，一鍵下載安裝 |
| 🚀 **開機自動啟動** | 支援登入 Windows 時自動啟動、系統匣常駐 |
| 🔒 **零遙測** | 資料只留在本機，不上傳提示詞、程式碼或登入憑證 |

![QuotaDock 側邊提醒](mini-bubble.png)

## 運作原理

QuotaDock **不碰你的帳號密碼**。它只是把兩套官方 CLI 已經提供的用量介面變成看得到的桌面面板：

```
┌─────────────┐     codex (本機服務)      ┌──────────────┐
│  QuotaDock  │ ───────────────────────▶ │  Codex CLI   │──▶ 額度百分比
│  (PySide6)  │     claude (stream-json) ┌──────────────┐
│             │ ───────────────────────▶ │ Claude Code  │──▶ 5h / 7d 額度
└─────────────┘                          └──────────────┘
```

- **Codex**：透過本機 Codex CLI 服務讀取 5 小時與 7 天視窗。哪個視窗放在 `primary` 並不固定，所以是用視窗長度分類，不看回傳順序
- **Claude Code**：以 `--safe-mode` 非互動模式送出 `get_usage` 控制請求，讀取 5 小時與 7 天視窗
- 登入憑證全程由各 CLI 自行保管，QuotaDock 只讀回應中的百分比

## 安裝

### 方法一：下載 Release（推薦）

1. 前往 [**Releases**](https://github.com/and910805/QuotaDock/releases/latest)
2. 下載 `QuotaDock-Windows-x64.exe`
3. 執行後會自動建立桌面捷徑並設定開機啟動

> [!NOTE]
> 本專案尚未購買程式碼簽章憑證，Windows SmartScreen 可能顯示警告。
> 請確認下載來源為本儲存庫後，選擇「其他資訊」→「仍要執行」。

### 方法二：從原始碼安裝

**需求**：Windows 10/11、[Python 3.11+](https://www.python.org/downloads/windows/)、已登入的 Codex 或 Claude Code CLI

```powershell
git clone https://github.com/and910805/QuotaDock.git
```

接著雙擊專案資料夾中的 `install.bat`，安裝程式會自動建立虛擬環境、安裝相依套件、執行測試、打包 EXE 並建立桌面捷徑。

## 使用說明

| 操作 | 效果 |
|---|---|
| 按右上角「—」 | 縮成側邊懸浮圓環 |
| 點懸浮圓環 | 展開完整面板 |
| 拖曳懸浮圓環 | 放開後自動貼齊最近的螢幕邊緣 |
| 右鍵懸浮圓環 / 系統匣 | 立即更新、開啟設定、結束程式 |
| 按右上角「已置頂 / 未置頂」 | 切換面板是否永遠顯示在其他視窗上方（按鈕顯示的是目前狀態） |

## 設定選項

| 選項 | 說明 | 預設 |
|---|---|---|
| 自動更新頻率 | 每 1 / 5 / 15 分鐘 | 5 分鐘 |
| 低額度門檻 | 剩餘 20 / 15 / 10 / 5 % 時通知 | 15% |
| 每下降 5% 提醒 | 額度每過一個 5% 關卡就提醒 | 開 |
| 額度重置通知 | 新週期回到 100% 時通知 | 開 |
| 提醒停留時間 | 5 / 10 / 15 / 30 秒 | 15 秒 |
| **懸浮圖示顯示的數值** | 兩者取最低 / 只看 Codex / 只看 Claude Code | 取最低 |
| **主圓環顯示的 Codex 額度** | 自動（剩餘較低者）/ 5 小時額度 / 7 天額度 | 自動 |
| 開機自動啟動 | 登入 Windows 時自動啟動 | 開 |
| 檢查 QuotaDock 新版本 | 每天向 GitHub 查一次 release 清單 | 開 |

## 疑難排解

<details>
<summary><b>Claude Code 顯示「未登入」</b></summary>

只登入 **Claude 桌面版**不會建立 Claude Code CLI 的憑證——桌面版是透過內部代理帶著授權執行 CLI，QuotaDock 獨立呼叫時仍是未登入狀態。

卡片會出現 **「登入 Claude Code」** 按鈕：按下後開啟主控台視窗執行官方登入流程，在瀏覽器完成授權後 QuotaDock 會自動重新讀取額度，全程不需要手打指令。

</details>

<details>
<summary><b>Claude Code 顯示「未安裝」</b></summary>

QuotaDock 依序嘗試：

1. 環境變數 `CLAUDE_USAGE_CLI` 指定的路徑
2. 先前記住的手動指定路徑
3. PATH 上的 `claude`（原生 `.exe` 優先，其次 npm 的 `.cmd` 包裝檔）
4. npm 全域安裝目錄
5. MSIX 封裝的 Claude 桌面版：`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\<版本>\claude.exe`（取最新版本）
6. Claude 桌面版管理的 `%APPDATA%\Claude\claude-code\<版本>\claude.exe`（取最新版本）
7. 以上皆無 → **自動掃描**常見安裝根目錄（約 1–2 秒，結果會記住）

第 5 項要排在第 6 項前面：桌面版若是 MSIX 安裝，`%APPDATA%\Claude` 只是套件資料的重導向投影（`fsutil hardlink list` 只會列出 `LocalCache` 那條），非封裝的程序不保證看得到，提升權限後更常直接消失 — 這也是為什麼在系統管理員的 PowerShell 裡跑 `%APPDATA%` 那條路徑會得到 `CommandNotFoundException`。

查詢失敗（防毒攔截、檔案被鎖、I/O 抖動）與「真的沒安裝」會分開處理：前者顯示 **「暫時無法讀取」** 並在 `%LOCALAPPDATA%\CodexUsageWidget\claude_locate.log` 記下失敗的 syscall 與 WinError，不會誤報成「未安裝」。

仍找不到時，卡片會出現 **「指定 claude 執行檔」** 按鈕讓你直接選檔案。確定已安裝卻掃不到的話，請開 issue 告知安裝路徑。

</details>

<details>
<summary><b>顯示「已連線」但沒有百分比</b></summary>

該帳號使用 API 計費而非訂閱制，本來就沒有訂閱額度百分比可顯示。

</details>

<details>
<summary><b>更新按鈕沒出現 / 更新失敗</b></summary>

更新檢查只在**打包後的執行檔**上執行（從原始碼跑的時候換掉 exe 沒有意義），啟動 5 秒後查一次，之後每 24 小時一次。草稿版、預發行版、沒有 `QuotaDock-Windows-x64.exe` 的 release 都會被略過。

按下更新後會把安裝檔下載到 `%TEMP%`，再交由它自己關掉舊程式、覆蓋安裝並重新啟動 — 跟你手動下載執行是同一條路徑。下載不完整（小於 5 MB）會中止並保留現有版本。

</details>

<details>
<summary><b>點開面板卻看不到視窗</b></summary>

面板記得上次的座標。如果那個位置是拔掉的第二螢幕（或解析度變小了），舊座標會整個落在畫面外，視窗就等於消失。

v1.2.4 起，還原位置與每次現身前都會把座標夾回目前螢幕的可見範圍，並把修正後的值寫回設定，所以只會發生一次；迷你泡泡本來就會自動吸附邊緣，不受影響。

</details>

## 開發

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q      # 執行測試
python app.py            # 直接執行
python app.py --demo     # 以展示資料執行（不需要任何 CLI）
```

打包 Windows EXE：

```powershell
.\build_release.ps1
```

產出位於 `release\QuotaDock.exe`。

### 專案結構

```
app.py              # 主程式：UI、CLI 偵測、用量讀取、通知
test_app.py         # pytest 測試
build_release.ps1   # PyInstaller 打包腳本
install.bat         # 原始碼一鍵安裝
make_icon.py        # 產生應用程式圖示
```

## 隱私

- 只透過 Codex 與 Claude Code 的**本機**服務讀取用量回應
- 不讀取、不保存、不傳輸登入憑證
- 沒有遙測、沒有分析服務、不上傳任何使用資料
- 唯一的對外連線是更新檢查：每天一次向 `api.github.com` 讀取本專案的 release 清單，只送出 `QuotaDock/<版本>` 這個 User-Agent，可在設定關閉。安裝檔只會從 `github.com/and910805/QuotaDock/releases/download/` 下載，網址不符就中止
- 用量狀態快取只存在本機 `%LOCALAPPDATA%\CodexUsageWidget`

> 這是非官方開源工具，與 OpenAI、Anthropic 皆無隸屬關係。服務商更新本機介面後，讀取方式可能需要跟著調整。

## 授權

本專案採用 [MIT License](LICENSE) 授權。
