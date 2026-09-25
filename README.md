# VibeTalkie

按住藍牙音訊麥克風上的按鍵 → 錄音 → **語音轉文字（ASR）** → 自動輸入目前視窗。

- **只做 ASR**：不做 LLM 糾錯、不做 Agent、不做螢幕理解。
- **預設全本地辨識**、錄音檔辨識後刪除、零強制上傳。
- 目標平台：Windows 10/11、macOS、Linux（X11 優先；Wayland 記錄限制）。

> 目前狀態：**P0 — 硬體驗證**。還沒有正式程式碼，只有驗證工具。
> 技術棧尚未決定（Rust+Tauri / Python / Go 三棧評比中）。

## 文件

| 文件 | 內容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 給 AI 編碼代理的專案守則（動工前必讀） |
| [`docs/plan.md`](docs/plan.md) | 總計畫書（目標、架構、測試規程、里程碑） |
| [`docs/hardware.md`](docs/hardware.md) | **實測硬體事實** — 唯一真相來源，不可讓 AI 重猜 |
| [`docs/architecture.md`](docs/architecture.md) | 系統架構與模組邊界 |
| [`docs/adr/`](docs/adr/) | 技術決策紀錄 |
| [`docs/spike/`](docs/spike/) | 三棧 Spike 測試報告 |
| [`tools/p0/`](tools/p0/) | P0 硬體驗證工具 |

## 快速開始（P0）

```powershell
# T1：列舉音訊輸入裝置（免安裝依賴）
pwsh -File tools/p0/enumerate_audio.ps1

# T2：錄 5 秒 16 kHz mono WAV
python tools/p0/record_wav.py --list
python tools/p0/record_wav.py --device 0 --seconds 5 --out artifacts/t2-mic.wav

# T3：攔截按鍵的真實鍵碼（Raw Input + Low-Level Hook）
python tools/p0/keycode_logger.py --seconds 30
```

## 隱私

本 repo 是**公開的**。禁止 commit 真實使用者名稱、本機絕對路徑、公司／內部專案名、
截圖，以及**藍牙 MAC 位址等裝置指紋**。詳見 `AGENTS.md`。
