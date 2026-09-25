# 系統架構

> 技術棧無關（stack-agnostic）。Rust / Python / Go 三棧都必須照這份驗證與實作。
> 硬體事實見 [`hardware.md`](hardware.md)；總計畫見 [`plan.md`](plan.md)。

---

## 1. 分層

```
┌─────────────── UI：裝置選擇 / 按鍵綁定 / 設定 / 歷史 / 狀態燈 ───────────────┐
└──────────────────────────────────┬──────────────────────────────────────────┘
                          事件總線（狀態機）
┌──────────┬─────────────┬──────────────┬──────────────┬─────────────┐
│ DeviceMgr│ AudioCapture│     VAD      │    ASR       │  TextOutput │
│ 列舉音訊  │ 指定裝置錄音 │ 頭尾靜音修剪  │ sherpa-onnx/ │ 剪貼簿貼上→  │
│ 裝置     │ 16k/mono/f32│ 保留前後 300ms│ whisper.cpp  │ 逐字輸入    │
└──────────┴─────────────┴──────────────┴──────────────┴─────────────┘
                    共同：Config(TOML) / Hotkey / Tray / Log
```

**鐵則：** 模組之間**不直接互相呼叫**，一律經過事件總線。
唯一例外是 `hotkey` → 事件總線（因為它是全行程唯一的觸發源）。

---

## 2. 狀態機

```
        ┌──────────────────────── cancel / error ────────────────────────┐
        │                                                                │
     IDLE ──press──> RECORDING ──release──> PROCESSING ──ok──> INSERTING ─┴─> IDLE
```

| 狀態 | 進入條件 | 允許的動作 | 逾時保護 |
|---|---|---|---|
| `IDLE` | 初始 / 完成 | 接受 press | — |
| `RECORDING` | 收到 press | 累積音訊；接受 release | 上限 120 秒（防按鍵卡住） |
| `PROCESSING` | 收到 release 且音訊長度 ≥ 200 ms | 執行 VAD → ASR | 上限 30 秒 |
| `INSERTING` | ASR 產出非空文字 | 注入目標視窗 | 上限 5 秒 |

**邊界規則：**

- 音訊 < 200 ms → 視為誤觸，回 `IDLE`，不做 ASR、不報錯。
- ASR 回傳空字串 → 回 `IDLE`，狀態燈顯示「沒聽到」，**不注入**。
- `RECORDING` 中再次收到 press → 忽略（不支援切換式）。
- 任何錯誤 → 記錄 log、回 `IDLE`、**絕不讓行程崩潰**（這是常駐 Tray 程式）。

---

## 3. 模組契約

### `device` — DeviceMgr

| 項目 | 內容 |
|---|---|
| 輸入 | 無 |
| 輸出 | `Vec<AudioInputDevice { id, name, is_default, native_sample_rate }>` |
| 不做 | 不開串流、不錄音 |
| 平台實作 | Windows WASAPI / macOS CoreAudio / Linux ALSA+Pulse |

**注意：** 藍牙 HFP 裝置的 `id` 在重新配對後可能改變 → 設定檔要**同時存 name 當 fallback**。

### `audio` — AudioCapture

| 項目 | 內容 |
|---|---|
| 輸入 | `device_id`、目標格式（16000 Hz / mono / f32） |
| 輸出 | `AudioBuffer { samples: Vec<f32>, sample_rate: u32 }` |
| 職責 | 開串流、收樣本、必要時重採樣（`rubato` / `scipy.signal.resample_poly`） |
| 不做 | 不判斷有沒有人聲（那是 VAD） |

### `vad` — 頭尾修剪

| 項目 | 內容 |
|---|---|
| 輸入 | `AudioBuffer` |
| 輸出 | `AudioBuffer`（已修剪） |
| 規則 | Silero VAD 找語音段；**前後各保留 300 ms** |
| 不做 | **不決定何時開始／結束錄音**（按鍵已決定）；不做中間靜音切除（v1） |

### `asr` — 語音轉文字

| 項目 | 內容 |
|---|---|
| 介面 | `trait SpeechEngine { fn transcribe(&self, audio: &AudioBuffer) -> Result<String, AsrError>; }` |
| v1 實作 | `SherpaOnnxEngine`（中文 Paraformer / 英文 Zipformer） |
| 備選 | `WhisperCppEngine`（Rust/Go）、`FasterWhisperEngine`（Python） |
| 後處理 | 熱詞替換表 + 標點保留。**嚴禁 LLM** |
| 雲端 | 只留介面位置，**v1 不實作**任何雲端引擎 |

### `textin` — 文字注入

| 項目 | 內容 |
|---|---|
| 主路徑 | 備份剪貼簿 → 寫入文字 → 送 `Ctrl+V` / `Cmd+V` → **還原剪貼簿** |
| 退路 | 主路徑失敗 → 逐字鍵盤輸入（`SendInput` / CGEvent / XTEST） |
| 平台限制 | **Wayland 主路徑與退路都可能失敗** → 回報明確錯誤，UI 指引安裝 `wtype`/`ydotool` |
| 鐵則 | 剪貼簿**必須**還原，即使注入失敗也要還原（`finally` 語意） |

### `hotkey` — 全域按鍵

| 項目 | 內容 |
|---|---|
| 輸入 | 裝置的 **Right Ctrl**（錄音鍵）按下／放開 |
| 輸出 | `press` / `release` 事件 |
| 鐵則 1 | **不得硬編**；綁定由設定檔決定（`hardware.md` §3.2） |
| 鐵則 2 | **必須能分辨裝置** —— 使用者的實體鍵盤也有 Right Ctrl |
| 鐵則 3 | **必須自行維護修飾鍵狀態機** —— 註冊式函式庫不處理「單獨一顆修飾鍵」 |

#### 3.1 為什麼不能直接用 `global-hotkey` 這類函式庫

錄音鍵是**單獨一顆修飾鍵（Right Ctrl）**。註冊式熱鍵函式庫以「組合鍵」為單位，
**不支援「單獨按 Ctrl 再放開」**。所以熱鍵層必須自己：

1. 追蹤修飾鍵的 down/up 狀態
2. 區分「**單獨**按 Right Ctrl」與「Right Ctrl **+ 其他鍵**」（後者不是錄音，不可誤觸）
3. 只認**目標裝置**送出的 Right Ctrl

#### 3.2 建議：抑制錄音鍵（不是必要條件）

| 按鈕 | 漏進前景視窗的後果 | 該不該抑制 |
|---|---|---|
| 上下左右 / Enter / Backspace | 游標移動、換行、刪字 | ❌ **不要抑制** —— 那是使用者要的操作 |
| **錄音鍵（Right Ctrl）** | 單獨按住**不輸入任何字元** | 🟡 **建議抑制** |

建議抑制的理由：錄音的數秒間若誤觸其他鍵，會變成 `Ctrl+?` 快捷鍵；
也避免目標程式看到多餘的修飾鍵狀態。

> ⚠️ **注意：** 先前曾把「抑制」寫成必要條件，那是**過重的判斷**。
> 真正的必要條件只有一個：**能分辨裝置**。

#### 3.3 實作方式（**已在 Python 驗證**）

> ⚠️ **本節修正先前的結論。** 先前寫「必須雙通道時間關聯」，
> 那是被「抑制」需求帶偏的結論。實際上：

**偵測只需要一條通道：Raw Input。**
因為它同時提供：

- **裝置身分**：`hDevice`（可解析成 interface path）
- **按鍵**：`VK_CONTROL` + `E0` 旗標 = **Right Ctrl**

`WH_KEYBOARD_LL` **看不到裝置**（`KBDLLHOOKSTRUCT` 沒有裝置欄位），
所以它根本無法用來辨識裝置 —— 靠 hook 做裝置辨識是不可能的。

| 需求 | 機制 | 需要 hook 嗎 |
|---|---|---|
| 偵測目標裝置的 Right Ctrl | **Raw Input 單通道** | ❌ 不需要 |
| 抑制按鍵 | 低階 hook（回傳非 0） | ✅ 需要 |

**抑制是可選的**（見 §3.2），所以 v1 用單通道即可。
若日後要加抑制，才需要「hook 負責抑制 + Raw Input 負責辨識裝置」的時間關聯。

**已實作於** [`app/core/ptt.py`](../app/core/ptt.py)：
預設 `--device-filter 00001124` + 要求 `E0`，
所以按實體鍵盤的 Right Ctrl **不會**誤觸（`AppleKbd` 的 path 不含該 UUID）。


#### 3.4 已知限制：注入與修飾鍵的競態

產品的主要注入路徑是 `Ctrl+V`。錄音鍵本身就是 Ctrl，所以必須確保
**在送出 `Ctrl+V` 之前，裝置的 Right Ctrl 已經確認放開**（或已被抑制），
否則會產生 `Ctrl(+Ctrl)+V` 的競態。狀態機的 `RECORDING → PROCESSING`
轉換必須先等修飾鍵狀態歸零。


### `vendor` — 原廠工具偵測與排除（P0 新增）

`hardware.md` §8 確認：裝置綁定了原廠工具「閃電說」（`shandianshuo`），
它會自動啟動、注入按鍵、並有自己的按鈕綁定。

| 項目 | 內容 |
|---|---|
| 職責 | 啟動時偵測原廠工具是否在執行／已安裝 |
| 行為 | 若在執行 → **明確告知使用者並要求關閉**（不可靜默失敗） |
| 為何必要 | 兩者會搶同一顆按鈕與同一個麥克風 → 雙重插入、按鍵互搶、裝置被佔用 |
| 不做 | 不自行砍掉別人的行程（未經同意終止第三方程式是不可接受的） |

啟動流程建議：

```
啟動 → 偵測 shandianshuo 行程
      ├─ 未執行 → 正常啟動
      └─ 執行中 → 顯示引導頁：「請先關閉閃電說，否則按鍵會互相搶佔」
                  ├─ 使用者關閉後 → 重新偵測 → 正常啟動
                  └─ 使用者不關 → 只提供「熱鍵以外的功能」，並明確標示限制
```


### `config` — 設定

TOML，路徑由平台慣例決定。範例：

```toml
[device]
mic_id   = "…"          # 主要識別
mic_name = "Headset"    # fallback：重新配對後 id 可能變
hotkey   = "F9"

[asr]
engine     = "sherpa-onnx"
model_dir  = "./models/paraformer-zh"
language   = "zh"
hotwords   = ["閃電說", "GPT", "API"]

[output]
mode = "auto"           # auto | paste | type

[privacy]
delete_audio_after = true
```

---

## 4. 隱私設計

| 要求 | 實作方式 |
|---|---|
| 預設全本機 | 預設引擎為 sherpa-onnx 本地模型；雲端引擎 v1 不存在 |
| 錄音檔辨識後刪除 | 音訊只存在記憶體；若因除錯落地，`delete_audio_after` 控制辨識後立即刪除 |
| 零強制上傳 | 程式**不得**在使用者未選擇雲端引擎時發出任何網路請求 |
| 歷史紀錄 | 只存文字，不存音訊；提供一鍵清除 |

---

## 5. 錯誤處理原則

1. **常駐程式不得崩潰。** 所有 `ASR`／`audio` 錯誤轉為狀態機事件，不 `panic`/`unwrap`。
2. **裝置消失要能復原。** 藍牙斷線 → 回到 `IDLE` → 自動重試列舉（指數退避，最多 5 次）。
3. **注入失敗不可靜默。** 必須在 Tray 顯示「文字已複製到剪貼簿，請自行貼上」。
4. **Log 只寫事實。** 不記音訊內容；文字內容預設不記（隱私）。
