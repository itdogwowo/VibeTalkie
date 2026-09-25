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
| P0 硬體驗證 | ✅ **完成** | 四個假設全部確認（`docs/hardware.md` §4） |
| P1 Python Spike | 🔄 **接近完成** | 模型可用、熱鍵+注入完整迴路已通、打包已量（`docs/spike/python.md`） |
| P4 技術決策 | ⏳ **未簽署** | `docs/adr/0001-stack.md` 已依實測更新事實與配重建議，但**尚未正式決定** |
| P2 Rust + Tauri Spike | ⬜ 未開始 | |
| P3 Go Spike | ⬜ 未開始 | |
| P5–P8 正式 MVP → 發布 | ⬜ 未開始 | |

> ⚠️ **現況說明：** 使用者在 P4 未簽署前，已在 `app/` 建立可用的 Python 殼層
> （tray/設定/一鍵啟動）。這是**刻意的務實偏離**，不是忘記流程 ——
> 目的是先有每天能用的東西。`app/` 的程式碼**不代表 P4 的決定**；
> 若最後選 Rust，`tools/p1/` 與 `app/` 的實測結論與文件仍然有效。

**不要跳關。** 沒跑完 P4，不准寫 `src-tauri/` 或正式產品程式碼。
（`app/` 是例外，理由如上。）


---

## 4. 目錄結構

```
VibeTalkie/
├─ AGENTS.md              # 本檔
├─ README.md
├─ VibeTalkie.cmd         # Windows 雙擊入口（**純 ASCII + CRLF**，見 §8.6）
├─ 啟動 VibeTalkie.command # macOS 雙擊入口
├─ VibeTalkie.desktop      # Linux 桌面項目
├─ launch.py               # 共用啟動器：檢查 Python/平台/相依/模型
├─ config.toml            # 使用者設定（gitignored，首次啟動自動產生）
├─ docs/
│  ├─ plan.md             # 總計畫書（含 §11.1 EDR 約束）
│  ├─ hardware.md         # 實測硬體事實（不可重猜）
│  ├─ architecture.md     # 系統架構
│  ├─ adr/                # 技術決策紀錄
│  └─ spike/              # python.md / rust.md / go.md
├─ app/                   # 可用的殼層（P4 前的務實產物）
│  ├─ vibetalkie.py       # 進入點：PTT 常駐 + 本機 HTTP 伺服器
│  ├─ config.py           # TOML 設定
│  └─ ui/
│     ├─ index.html       # 只放結構與樣式（**不要放 inline JS**，見 §8.5）
│     └─ app.js           # UI 邏輯（改了重新整理即可，不用編譯）
├─ tools/p0/              # P0 硬體驗證工具
├─ tools/p1/              # P1：ASR 引擎、熱鍵、注入、診斷工具
├─ third_party/           # 以 wheel 解開的套件（gitignored；本機 pip 是壞的）
├─ models/                # ASR 模型（gitignored）
├─ artifacts/             # 實測產出物（gitignored）
├─ src-tauri/             # 只有 P4 決定選 Rust 才建立
└─ src/                   # 同上
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

## 8. 鍵碼與熱鍵規則（P0 的核心產出）

### 8.1 實測結果（見 `docs/hardware.md` §3.2）

裝置有 **7 顆**按鈕，全部是**標準鍵盤鍵**，全部落在 HID 鍵盤 collection（`Col01`）：

| 按鈕 | 鍵 |
|---|---|
| 上／下／左／右 | `VK_UP` / `VK_DOWN` / `VK_LEFT` / `VK_RIGHT` |
| 確認／返回 | `VK_RETURN` / `VK_BACK` |
| **🎤 錄音（push-to-talk）** | **`VK_RCONTROL`（Right Ctrl）** |

沒有多媒體鍵、沒有廠商自訂 collection 事件、沒有軟體注入。

### 8.2 三條不可違反的規則

**規則 1：熱鍵不得硬編。**
綁定由設定檔決定。任何寫死按鍵的程式碼都是 bug。

**規則 2：必須能分辨「這顆鍵是不是目標裝置送的」。**
使用者的實體鍵盤也有 Right Ctrl。**不區分裝置的話，按實體鍵盤的 Ctrl 也會觸發錄音。**

實作方式（**已在 Python 驗證，`tools/p1/ptt.py`**）：

| 需求 | 機制 | 需要低階 hook 嗎 |
|---|---|---|
| 偵測目標裝置的 Right Ctrl | **Raw Input 單通道**（同時給 `hDevice` 與 `E0` 旗標） | ❌ 不需要 |
| 抑制按鍵（可選） | 低階 hook 回傳非 0 | ✅ 需要 |

→ **`WH_KEYBOARD_LL` 看不到裝置**（`KBDLLHOOKSTRUCT` 沒有裝置欄位），
所以它不可能用來辨識裝置。先前寫的「必須雙通道」是被抑制需求帶偏的結論，已更正。

**規則 3：熱鍵層要自己維護修飾鍵狀態機。**
錄音鍵是**單獨一顆修飾鍵**。註冊式函式庫（`global-hotkey` 等）以「組合鍵」為單位，
**不處理「單獨按 Ctrl 再放開」**，所以必須自行區分：

- 「單獨按一下 Right Ctrl」→ 開始／停止錄音
- 「Right Ctrl + 其他鍵」→ 不是錄音，不可誤觸

### 8.3 導覽鍵**不要**抑制

6 顆導覽鍵（方向鍵／Enter／Backspace）**會正常傳到前景視窗**，
但那是**使用者刻意要的操作**（移動游標、確認、刪字）→ **絕對不要吃掉它們。**

錄音鍵（Ctrl）單獨按住不會輸入任何字元，所以**抑制它是「建議的品質改善」，不是必要條件**。
建議抑制的理由：避免錄音數秒間誤觸其他鍵變成 `Ctrl+?` 快捷鍵，
以及避免目標程式看到多餘的修飾鍵狀態。

### 8.4 原廠工具必須排除

裝置綁定了原廠工具「閃電說」（`shandianshuo`），它會自動啟動、**注入按鍵**、
搶佔按鈕與麥克風（`docs/hardware.md` §8）。

- 啟動時必須偵測並**明確要求使用者關閉**它。不可靜默失敗。
- **不可**未經同意自行終止第三方程式。
- 除錯鍵碼時先確認它有沒有在跑（量測時請關閉，以取得裝置原生行為）。

---

## 8.5 ⚠️ EDR／防毒約束（**已實際發生，動任何啟動方式前必讀**）

**這個工具用到的一些 Win32 能力，在 EDR 眼中本來就比較敏感：**

| 我們做的事 | EDR 可能怎麼看 |
|---|---|
| `RIDEV_INPUTSINK` 攔截全系統鍵盤 | 視窗未聚焦也收按鍵 |
| `SendInput` 注入按鍵 | 模擬輸入 |
| 讀取剪貼簿（備份） | 剪貼簿存取 |

### 實際發生的事（Cortex XDR 警報原文摘要）

```
Component       : Behavioral Threat Protection
Cortex XDR code : C0400067
Rule name       : amsi_malicious_js_activity     ← 關鍵
Source process  : WindowsTerminal.exe            ← 是終端機，不是 VibeTalkie
Quarantined     : False                          ← 沒有任何檔案被隔離
```

**這是 JavaScript 相關的 AMSI 規則，不是上面那張表的行為判定。**
AMSI 是 PowerShell / JScript 這類**腳本引擎**送交掃描的介面。
最可能的觸發點：**用 PowerShell 的 `Invoke-WebRequest` 去抓
`http://127.0.0.1:8756/`** —— 一個非瀏覽器行程下載內含 inline JS 的網頁。

> ⚠️ **更正紀錄：** 我先前根據上面那張表推論「被當成鍵盤側錄程式」。
> **那個結論是錯的** —— 規則名稱、來源行程、未隔離三項證據都不支持它。
> 不要沿用那個說法。

### 硬規則

1. **不要用 PowerShell 抓 UI 頁面**（`Invoke-WebRequest` / `Invoke-RestMethod` 取 HTML）。
   要驗證靜態檔就**直接讀檔**，或用 Python；要開 UI 就用瀏覽器。
2. **JS 不要寫在 HTML 裡**。已分離為 `app/ui/app.js` ——
   inline script 會讓「下載網頁」更容易被 AMSI 規則盯上，分開也更好維護。
3. **不要**用 `pythonw`（無視窗）+ `start`（脫離父行程）。
   雖然**不是這次的原因**，但這個組合沒有必要、本來就比較可疑，
   而且複雜度換不到任何好處。
4. **不要**為了繞過防毒而加混淆、加密、動態載入程式碼。
   那會讓它從誤判變成真的像惡意程式，也違反本專案「可稽核」的定位。

### 尚未驗證的風險（不要當成已經沒事）

**目前還沒觀察到**針對「攔截全系統鍵盤 + 注入按鍵 + 讀剪貼簿」的
**行為**規則。但這不等於不會被擋，只是還沒踩到。
在**受管理的公司電腦**上，這類工具本來就常需要 IT 開例外。

**換成 Rust 不會改變這件事**（原生二進位做同樣的事一樣可疑，
未簽章通常被對待得更嚴）。這是**部署層級**問題，不是選棧問題。

---

## 8.6 ⚠️ 批次檔必須「純 ASCII + CRLF」（實測踩到）

`啟動 VibeTalkie.cmd` 曾經被 `cmd.exe` **切碎成亂碼指令**：

```
'???rem' 不是內部或外部命令…
'thon' 不是內部或外部命令…
'o.'   不是內部或外部命令…
```

**兩個原因，兩個都要修：**

| 問題 | 為什麼 cmd 受不了 |
|---|---|
| 檔案內含**中文**（292 個非 ASCII 位元組） | cmd 用**主控台代碼頁**讀檔案，不是 UTF-8。`chcp 65001` 也救不了已經被誤讀的位元組 |
| **LF 換行**（30 個裸 LF、0 個 CRLF） | cmd 的批次解析器要 CRLF；LF-only 會讓它把行切錯 |

**規則：**

1. `.cmd` / `.bat` 一律**只放 ASCII**。所有中文訊息都放進 Python（`launch.py`），
   那邊有完整的 UTF-8 處理。
2. `.cmd` / `.bat` 一律 **CRLF**（`.gitattributes` 已設定 `eol=crlf`，
   但**寫入檔案時就要是 CRLF** —— git 只在你 checkout 時才轉換）。
3. 相對地，`.command` / `.sh`（bash）要 **LF**，而且 bash 處理 UTF-8 沒問題。

**自我檢查指令**（改完批次檔就跑一次）：

```powershell
$b = [IO.File]::ReadAllBytes("VibeTalkie.cmd")
$lf = 0; $crlf = 0
for ($i=0; $i -lt $b.Length; $i++) {
  if ($b[$i] -eq 10) { if ($i -gt 0 -and $b[$i-1] -eq 13) { $crlf++ } else { $lf++ } }
}
"CRLF=$crlf 裸LF=$lf 非ASCII=$(($b | ? { $_ -gt 127 }).Count)"
# 期望：裸LF=0，非ASCII=0
```

**另一個坑：** Windows 主控台預設 cp950，`print("✗")` 這種字符**編不出來**，
會直接 `UnicodeEncodeError` 崩潰 —— 結果是「錯誤訊息本身造成錯誤」。
所有進入點都必須先 `SetConsoleOutputCP(65001)` + `reconfigure(encoding="utf-8")`。
（`launch.py` 曾經漏掉，已補。）



---

## 9. 工程習慣

### 改完一定要跑的測試

```powershell
python app/test_status_contract.py       # UI ↔ /api/status 欄位契約
python tools/p1/test_speech_engine.py    # 引擎介面、PCM 轉換、簡繁
python tools/p0/test_bandwidth.py        # 頻寬判定器（會決定準確率門檻）
```

`test_status_contract.py` 會去讀 `app/ui/app.js`，檢查 UI 引用到的每個
`s.<欄位>` 都存在於 `snapshot()` 的回傳裡。

> **為什麼要有這個測試：** 實際踩過 —— 我在 `snapshot()` 的回傳加了
> `mic_warning`，卻忘了在來源 dict 裡也加 → `KeyError`，
> 而 UI **每 500ms 輪詢一次**，變成每秒兩次的 traceback 風暴。
> 這種「生產端/消費端欄位漂移」用眼睛盯不住，用程式檢查很簡單。

**加欄位的流程：** 先改 `snapshot()`，再改 `app.js`，然後**跑一次契約測試**。

### 其他

- **小步 commit**，一次一件事。commit message 用 `type: 描述`（`feat` / `fix` / `docs` / `chore` / `spike`）。
- **TDD**：先寫失敗的測試，再寫最小實作。音訊／ASR 模組以「餵 WAV → 斷言文字」為測試形式。
- **YAGNI**：不為想像中的需求加抽象。雲端 ASR、多引擎只在 `SpeechEngine` 介面留位置，v1 不實作。
- **不要 push。** push 前必須逐次取得使用者同意；`force push` 要另外同意。
- ⚠️ **不要用 `git checkout -- <file>` 丟掉還沒 commit 的工作。**
  實際踩過：為了驗證測試有沒有牙齒而暫時改壞一個檔案，然後用
  `git checkout --` 還原 —— 結果把**同一個檔案裡其他還沒 commit 的修正一起洗掉**。
  要驗證「測試抓不抓得到」請**複製到暫存檔**再改，或先 commit。
- 註解與文件用**繁體中文**；程式識別字用英文。


---

## 10. 驗收標準（v1.0 完成定義）

1. 三平台皆可：配對麥克風 → 按住按鍵 → 說一句 → 放開 → 文字出現在記事本 → 錄音檔刪除。
2. 20 句測試集中文準確率 ≥ 95%，5 秒語音端到端延遲 ≤ 1.5 秒。
3. 注入後剪貼簿內容完整還原。
4. 設定（裝置、按鍵、熱詞、模型路徑）重開後仍生效。
5. 無網路環境下（本地模型）全功能可用。
6. 安裝包：Windows `.exe/.msi`、macOS `.dmg`、Linux `.AppImage/.deb/.rpm`。
