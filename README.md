# VibeTalkie

按住藍牙音訊麥克風上的按鍵 → 錄音 → **語音轉文字（ASR）** → 自動輸入目前視窗。

- **只做 ASR**：不做 LLM 糾錯、不做 Agent、不做螢幕理解。
- **預設全本地辨識**、錄音檔辨識後刪除、零強制上傳。
- 目標平台：Windows 10/11、macOS、Linux（X11 優先；Wayland 記錄限制）。

## 目前狀態

> **Windows 上可以每天使用**（單一指令啟動、系統匣、設定頁面）。
> **但還不是發布版本** —— 沒有安裝包、macOS 與 Linux 尚未實機驗證。

| 階段 | 狀態 | 說明 |
|---|---|---|
| P0 硬體驗證 | ✅ 完成 | 四個假設全部確認，見 [`docs/hardware.md`](docs/hardware.md) §4 |
| P1 Python Spike | ✅ 可用 | 熱鍵 → 錄音 → 辨識 → 注入的完整迴路已通 |
| **P4 技術決策** | ⏳ **未簽署** | [`docs/adr/0001-stack.md`](docs/adr/0001-stack.md) 仍是 `Proposed` |
| P2 Rust Spike／P3 Go Spike | ⬜ 未開始 | |
| P5–P8 正式 MVP → 發布 | ⬜ 未開始 | |

⚠️ **`app/` 是刻意的務實偏離。** 專案規則是「沒跑完 P4 不寫正式產品程式碼」，
但使用者需要一個每天能用的東西，所以先做了 Python 殼層。
**它不代表 P4 已經決定用 Python** —— 若最後選 Rust，這裡的實測結論與文件仍然有效。

### 平台實測狀況（不要當成三平台都好了）

| 平台 | 狀態 |
|---|---|
| Windows | ✅ 實機使用中（Raw Input ＋ winmm/SCO 暖機待命都已實測） |
| macOS | ⚠️ **程式碼完成、未實機驗證**。`tests/test_mac_e2e.py` 與 `test_mac_devices.py` 需要在 Mac 上跑 |
| Linux | ⬜ 未開始 |

## 快速開始（Windows）

```powershell
# 雙擊 VibeTalkie.cmd，或：
python launch.py
```

會自動檢查 Python 版本與相依套件、必要時下載模型，然後開設定頁面
（<http://127.0.0.1:8756/>）。設定頁面裡可以：選麥克風（含優先順序）、
設定錄音鍵（**可以有多組**，例如裝置的按鍵 ＋ 鍵盤的 `F9`）、選模型。

**macOS**：雙擊 `啟動 VibeTalkie.command`（或 `python3 app/mac_vibetalkie.py`）。

### 錄音鍵怎麼設

`config.toml` 的 `hotkeys` 是清單，每一組的寫法是
`開始[,結束][@開始行為][@結束行為]`：

```toml
hotkeys = ["RightCtrl", "F9,Esc", "F8,Space@double@double"]
```

| 寫法 | 意思 |
|---|---|
| `RightCtrl` | 開始＝結束都是它（裝置原生送出的就是右 Ctrl） |
| `F9@double` | 連按兩下 F9 開始 |
| `F9,Esc` | 開始 F9、**結束 Esc**（不同顆鍵） |
| `F9,Esc@toggle` | 同上，但 Esc「再按一下」才停 |
| `~F9` | 這一組**停用**（保留設定，只是不比對） |

設定頁面每一列都有「🎧 錄製」與「測試」按鈕 —— **「測試」只聽不錄、不注入**，
用來驗證「按下去到底有沒有收到」而不會干擾你正在打字的視窗。

⚠️ **不要同時啟動兩個 VibeTalkie。** 兩個行程會各自記一份設定、
存檔時整個檔案重寫，症狀是「設定存了又變回去」。
程式會偵測並拒絕重複啟動（要刻意跑第二個請用 `--port`）。

## 文件

| 文件 | 內容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 給 AI 編碼代理的專案守則（動工前必讀） |
| [`docs/hardware.md`](docs/hardware.md) | **實測硬體事實** — 唯一真相來源，不可讓 AI 重猜 |
| [`docs/plan.md`](docs/plan.md) | 總計畫書（目標、架構、測試規程、里程碑） |
| [`docs/architecture.md`](docs/architecture.md) | 系統架構與模組邊界 |
| [`docs/adr/`](docs/adr/) | 技術決策紀錄 |
| [`docs/spike/`](docs/spike/) | 三棧 Spike 測試報告 |
| [`docs/handoff/`](docs/handoff/) | main × mac 整合的現況分析與完成記錄 |

## 測試

```powershell
python tests/test_trigger.py          # 錄音鍵：多組＋左右側＋每組自己的觸發方式
python tests/test_status_contract.py  # UI ↔ /api/status 欄位契約（兩個平台一起驗）
python tests/test_launch_guard.py     # 拒絕重複啟動（真的開行程驗）
python tests/test_hotkeys_patch.py    # 錄音鍵設定的驗證（兩個平台共用一份）
```

其餘測試見 `AGENTS.md` §9。**改完一定要跑。**

## 隱私

本 repo 是**公開的**。禁止 commit 真實使用者名稱、本機絕對路徑、公司／內部專案名、
截圖，以及**藍牙 MAC 位址等裝置指紋**。詳見 `AGENTS.md`。
