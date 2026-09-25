# AGENTS.md — VibeTalkie 專案守則

給 AI 編碼代理（Claude Code / Cursor / Gemini CLI / DSH）的專案上下文。
**動工前先讀完這份，再讀 `docs/hardware.md`。**

---

## 1. 這個專案是什麼

按住藍牙麥克風上的按鍵 → 錄音 → **語音轉文字** → 把文字輸入目前視窗。

**唯一的 AI 能力是 ASR。** 以下都是明確不做（v1）：

- ❌ 不做 LLM 整理／改寫／回覆／翻譯
- ❌ 不做記憶、知識庫、技能、Agent、螢幕理解
- ❌ 不自訓練語音模型（只用開源權重 + 開源推理引擎）
- ❌ 不寫 BLE 藍牙驅動（硬體走**標準音訊** + **標準 HID 鍵盤**）

若你（AI）覺得「順手加個 LLM 潤稿會更好」——**不要**。那是產品決策，不是你的。

---

## 2. 唯一真相來源：實測，不要猜

`docs/hardware.md` 記錄的是**實機量測結果**，不是推測。

- 任何關於「裝置叫什麼、送什麼鍵碼、支援幾 Hz」的問題，**只准引用該檔**。
- 若該檔標記為「待驗證」，就**去跑 `tools/p0/` 的工具**，然後把結果寫回去。
- **嚴禁**為了讓程式跑起來而編造裝置 ID、鍵碼或取樣率。

---

## 3. 目前階段（會變，動手前先確認）

| 階段 | 狀態 | 說明 |
|---|---|---|
| P0 硬體驗證 | 🔄 進行中 | `tools/p0/` |
| P1 Python Spike | ⬜ 未開始 | 探路，不做正式產品 |
| P2 Rust + Tauri Spike | ⬜ 未開始 | 預期勝出者 |
| P3 Go Spike | ⬜ 未開始 | 驗證 cgo 限制 |
| P4 技術決策 | ⬜ 未開始 | 寫 `docs/adr/0001-stack.md` |
| P5–P8 正式 MVP → 發布 | ⬜ 未開始 | |

**不要跳關。** 沒跑完 P4，不准寫 `src-tauri/` 或正式產品程式碼。

---

## 4. 目錄結構

```
VibeTalkie/
├─ AGENTS.md              # 本檔
├─ README.md
├─ docs/
│  ├─ plan.md             # 總計畫書
│  ├─ hardware.md         # 實測硬體事實（不可重猜）
│  ├─ architecture.md     # 系統架構
│  ├─ adr/                # 技術決策紀錄
│  └─ spike/              # python.md / rust.md / go.md
├─ tools/p0/              # P0 驗證工具（目前唯一的程式碼）
├─ src-tauri/             # P5 之後才建立
├─ src/                   # P5 之後才建立
├─ models/                # ASR 模型（gitignored）
└─ artifacts/             # 實測產出物（gitignored）
```

---

## 5. 狀態機（貫穿所有模組）

```
IDLE ──press──> RECORDING ──release──> PROCESSING ──ok──> INSERTING ──> IDLE
                    │                        │
                    └──── cancel/error ──────┴──────────> IDLE
```

模組邊界（單一職責，不得互相呼叫繞過事件總線）：

| 模組 | 職責 | 不做 |
|---|---|---|
| `device` | 列舉／選定音訊輸入裝置 | 不錄音 |
| `audio` | 從指定裝置錄音、重採樣 16 kHz mono f32 | 不判斷靜音 |
| `vad` | 頭尾靜音修剪（保留前後 300 ms） | 不決定何時開始錄（按鍵決定） |
| `asr` | 音訊 → 文字（含標點） | 不改寫語意 |
| `textin` | 剪貼簿貼上 → 失敗則逐字輸入 | 不產生文字 |
| `hotkey` | 全域按鍵按下／放開 | 不錄音 |
| `config` | TOML 讀寫（serde） | 不驗證業務邏輯 |

---

## 6. 跨平台差異（實作前必看）

| 平台 | 錄音 | 熱鍵 | 文字注入 | 權限／打包 |
|---|---|---|---|---|
| Windows | WASAPI | 全域 hook | SendInput / 剪貼簿貼上 | 麥克風隱私設定；防毒白名單；建議簽章 |
| macOS | CoreAudio | CGEvent | Accessibility API / 貼上 | TCC：麥克風 + 輔助使用；notarization |
| Linux X11 | ALSA / PulseAudio | X11 | XTEST / 貼上 | 加入 `audio` 群組 |
| Linux Wayland | PipeWire | 依合成器 | **受限**：需 wtype / ydotool 或 portal | UI 顯示警告與指引 |

**Wayland 是已知的降級區，不是 bug。** 遇到時：剪貼簿貼上優先，UI 給明確指引，不要假裝成功。

---

## 7. 隱私紅線（本 repo 是公開的）

**不要 commit：**

1. 真實使用者名稱或本機絕對路徑 → 用 `C:\Users\<你的帳號>\…`、`~/.dsh/…` 樣板。
2. 公司名、內部專案名、客戶名、同事名。
3. **藍牙 MAC 位址、序號等裝置指紋** → 寫 `<BT-MAC>` 或 `XX:XX:XX:XX:XX:XX`。
4. **截圖**（會連作業系統的裝置清單、使用者名稱一起拍進去）→ 用文字描述。
5. 真實錄音檔（`*.wav`）與測試語料中的個資。
6. 同時出現在畫面上的**其他**藍牙裝置名稱（可能是私人物品）。

`artifacts/`、`*.wav`、`*.jsonl` 已列入 `.gitignore`。實測原始輸出留在本機，**只把結論**寫進 `docs/hardware.md`。

---

## 8. 鍵碼綁定規則（P0 的核心產出）

裝置按鈕**不一定是標準鍵**。可能落在：

- 一般鍵（`VK_*`，例：F9）
- 多媒體／consumer control（HID Usage Page `0x0C`）
- 廠商自訂 collection（**標準鍵盤 API 看不到**）

因此熱鍵層必須能吃「**任意鍵碼**」，不得硬編 F9。任何寫死熱鍵的程式碼都是 bug。

---

## 9. 工程習慣

- **小步 commit**，一次一件事。commit message 用 `type: 描述`（`feat` / `fix` / `docs` / `chore` / `spike`）。
- **TDD**：先寫失敗的測試，再寫最小實作。音訊／ASR 模組以「餵 WAV → 斷言文字」為測試形式。
- **YAGNI**：不為想像中的需求加抽象。雲端 ASR、多引擎只在 `SpeechEngine` 介面留位置，v1 不實作。
- **不要 push。** push 前必須逐次取得使用者同意；`force push` 要另外同意。
- 註解與文件用**繁體中文**；程式識別字用英文。

---

## 10. 驗收標準（v1.0 完成定義）

1. 三平台皆可：配對麥克風 → 按住按鍵 → 說一句 → 放開 → 文字出現在記事本 → 錄音檔刪除。
2. 20 句測試集中文準確率 ≥ 95%，5 秒語音端到端延遲 ≤ 1.5 秒。
3. 注入後剪貼簿內容完整還原。
4. 設定（裝置、按鍵、熱詞、模型路徑）重開後仍生效。
5. 無網路環境下（本地模型）全功能可用。
6. 安裝包：Windows `.exe/.msi`、macOS `.dmg`、Linux `.AppImage/.deb/.rpm`。
