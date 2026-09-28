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

> 📌 **翻譯已經被評估過一次（`docs/spike/translation.md`）。**
> 結論：**演算法很近，資源很遠。** 兩段式（ASR → Opus-MT）總延遲約 384 ms
> （門檻 1500 ms），但要多背 169 MB 模型檔與約 375 MB RSS，
> 而現況閒置記憶體**已經超標**。**§1「v1 不做翻譯」的決定沒有改變** ——
> 那是產品決策，不是留給 AI「順手加」的空缺。

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
│
├─ app/                   # ★ 產品本體
│  ├─ vibetalkie.py       # 進入點：PTT 常駐 + 本機 HTTP 伺服器
│  ├─ config.py           # TOML 設定
│  ├─ models.py           # 模型下載管理（背景執行、可取消）
│  ├─ model_index.py      # 官方模型清單：抓取 → 分類 → 快取
│  ├─ core/               # ★ 執行期模組（產品依賴，**不是工具**）
│  │  ├─ speech_engine.py #   ASR 引擎（sherpa-onnx，多模型家族）
│  │  ├─ hotkey.py        #   錄音鍵規格解析（RightCtrl / F9 / Ctrl+Alt+R）
│  │  ├─ ptt.py           #   PTT 常駐：Raw Input → 錄音 → 辨識 → 注入
│  │  ├─ recorder.py      #   擷取封裝（自動停止、音量分析）
│  │  ├─ record_wav.py    #   winmm waveIn 底層
│  │  ├─ keycode_logger.py#   Raw Input 底層（ptt.py 從這裡匯入）
│  │  ├─ textin.py        #   文字注入：剪貼簿貼上／逐字輸入
│  │  │
│  │  │  ── 以下是 macOS 實作（見下方說明）──
│  │  ├─ keys.py          #   可攜鍵名 ↔ 平台鍵碼（VK / Carbon keycode）
│  │  ├─ mac_keylistener.py # CGEventTap 監聽（取代 keycode_logger）
│  │  ├─ mac_recorder.py  #   AVAudioEngine 錄音（取代 record_wav）
│  │  ├─ mac_textin.py    #   NSPasteboard + Cmd+V（取代 textin）
│  │  ├─ autosetup.py     #   缺套件／缺模型時自動下載安裝
│  │  └─ ui_server.py     #   設定頁面的 HTTP server（兩平台共用 UI）
│  ├─ mac_vibetalkie.py   # ★ macOS 進入點（狀態機，對應 ptt.py）
│  └─ ui/
│     ├─ index.html       # 分頁結構（**不要放 inline JS**，見 §8.5）
│     ├─ style.css        # 樣式
│     └─ app.js           # UI 邏輯（改了重新整理即可，不用編譯）
│
├─ tests/                 # ★ 所有測試（見 §9）
│  ├─ test_status_contract.py   test_model_index.py   test_models_api.py
│  └─ test_speech_engine.py     test_bandwidth.py
│
├─ tools/                 # ★ 只有工具（**不被產品匯入**）
│  ├─ p0/enumerate_audio.ps1            # T1 端點列舉
│  └─ p1/                               # 診斷與一次性工具
│     ├─ measure_bt_profile_switch.py   # 產出 hardware.md §2.2 的數據
│     ├─ diagnose_bt_output.py          # §2.3：端點現況＋藍牙拓撲（誰跟誰共用無線電）
│     ├─ measure_bt_playback.py         # §2.3：播放到底送不送得出去（waveOut 實測）
│     ├─ measure_bt_stream_hold.py      # §2.4：串流常開 vs 每次開的中斷次數比較
│     ├─ measure_bt_exclusive.py        # §2.5：錄音期間播放端點是否還開得起來（獨占性）
│     ├─ measure_ptt_modes.py           # §2.4.1b：用真實 ptt.py 驅動三模式並對齊時間軸
│     ├─ measure_session_reuse.py       # session 模式是否真的重用串流（查「每段都斷」）
│     ├─ watch_bt_events.py             # 被動監看藍牙端點（邊用 app 邊看，會印出當前模式）
│     ├─ diagnose_zero_audio.py         # §7.1.11：連續錄音診斷（串流重用＋0 bytes）
│     ├─ eval_yue_models.py             # §7.1：多模型粵語對照（速度＋逐句並排輸出）
│     ├─ record_eval_set.py             # §7.1：錄自己的評測語料（計時自動換句）
│     ├─ measure_audio_latency.py       # 產出 hardware.md §2.1 的數據
│     ├─ inspect_recordings.py          # 錄音品質診斷
│     ├─ fetch_wheels.py                # 以 wheel 解開套件（本機 pip 壞了）
│     ├─ fetch_model.py                 # 命令列下載模型
│     └─ （其餘為 P0/P1 期工具，見下方「待評估移除」）
│
├─ docs/                  # plan / hardware / architecture / adr / spike
├─ third_party/           # wheel 解開的套件（gitignored；本機 pip 是壞的）
├─ models/                # ASR 模型（gitignored）
├─ artifacts/             # 實測產出物、錄音（gitignored）
├─ src-tauri/             # 只有 P4 決定選 Rust 才建立
└─ src/                   # 同上
```

### ⚠️ 為什麼有 `app/core/`（不要把它的內容當成 `tools/`）

原本執行期模組住在 `tools/p1/` 與 `tools/p0/`，結果 **42% 的產品程式碼
（2862 行）住在一個叫「工具」的資料夾裡** —— 任何人打開 repo 都會誤判，
連 AI 都得先查 import 關係才敢動。

已重組，判準只有一條：

> **會被 `app/` 匯入的 → `app/core/`；不會被匯入的 → `tools/`。**
> `tools/` 底下任何檔案被 `app/` 匯入，就是放錯位置了。

**待評估移除**（P0/P1 期產物，產品不使用，使用者已表示不需要）：

| 檔案 | 行數 | 狀態 |
|---|---|---|
| `tools/p1/asr_bench.py` + `record_testset.py` + `testset_zh.txt` | ~550 | 20 句基準測試；使用者決定不做 |
| `tools/p1/transcribe.py` | 191 | 最小 CLI，已被 `app/` 取代 |
| `tools/p1/inject_test_key.py` | 71 | 合成按鍵；已記錄無法驗證完整迴路 |

> 📌 **已知設計瑕疵（尚未處理）：** `app/core/keycode_logger.py` 與
> `app/core/record_wav.py` 同時是「執行期底層」與「獨立 CLI 工具」。
> 理想上應拆成 `app/core/rawinput.py`（純函式庫）+ `tools/keycode_logger.py`
> （薄 CLI），但那是較大的重構，暫時先放 `app/core/` 並保留 CLI。


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

實作方式（**已在 Python 驗證，`app/core/ptt.py`**）：

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

## 8.7 macOS 實作（**與 §8.2 規則 2 的關係必讀**）

進入點是 `app/mac_vibetalkie.py`（狀態機與 §5 完全一致，只是換掉平台層）。

### 模組對應

| 職責 | Windows | macOS |
|---|---|---|
| 按鍵監聽 | `keycode_logger.py`（Raw Input） | `mac_keylistener.py`（CGEventTap） |
| 錄音 | `record_wav.py`（winmm waveIn） | `mac_recorder.py`（AVAudioEngine） |
| 文字注入 | `textin.py`（SendInput） | `mac_textin.py`（NSPasteboard + Cmd+V） |
| 鍵名對照 | VK 碼（`hotkey.py`） | `keys.py`（Carbon keycode） |
| **設定頁面** | `vibetalkie.py` 內建 HTTP server | **`ui_server.py`（共用同一份 `app/ui/`）** |

### 設定頁面（兩個平台共用同一份 UI）

UI 是純靜態檔（`app/ui/index.html` + `app.js` + `style.css`，943 行），
macOS 版由 **`app/core/ui_server.py`** 服務，預設 `http://127.0.0.1:8756/`。

```bash
python3 app/mac_vibetalkie.py            # 會自動開瀏覽器
python3 app/mac_vibetalkie.py --no-ui    # 純命令列模式
python3 app/mac_vibetalkie.py --port 9000 --no-browser
```

也可以在 Finder **雙擊 `啟動 VibeTalkie.command`** —— 那個入口直接呼叫
`app/mac_vibetalkie.py`（**不經過 `launch.py`**，因為 launch.py 是
跨平台啟動器，macOS 在它眼中仍是「尚未支援」）。

可以在頁面上改：**錄音鍵**（含預設清單）、**觸發方式**（hold/toggle/double）、
輸出模式、簡繁轉換、模型下載與切換。改完寫進 `config.toml`。

> ⚠️ **`snapshot()` 是 UI 的契約，兩個平台各自實作、各自要驗證。**
> mac 版由 `tests/test_mac_ui_contract.py` 檢查（讀 `app.js` 抓出實際用到的
> `s.<欄位>`，比對 snapshot 有沒有全部提供）。Windows 版是
> `tests/test_status_contract.py`。
>
> 少一個欄位的症狀是「UI 顯示 undefined，而後端毫無錯誤」——
> 這種漂移用眼睛看不出來。

> 🔴 **HTTP 回應的「形狀」也是契約，不只是欄位。**
>
> **實際踩到：** `/api/models` 我自作主張回了一個不同的結構：
>
> ```
> 我回的：  {"installed": [...], "catalog": [...], "current": "..."}
> UI 要的： {"models": [{name, title, state, download, active, supported, ...}]}
> ```
>
> 後果：`app.js` 讀 `d.models` 得到 `undefined` → **模型分頁整片空白**，
> 而且 HTTP 200、JSON 合法、後端毫無錯誤訊息。
>
> 同一個 commit 裡我還把 `snapshot().downloads` 寫死成 `[]`，
> 於是「按了下載完全沒反應」——其實下載有在跑，只是 UI 看不到進度
> （`app.js` 靠這個欄位畫進度條並在完成時自動刷新清單）。
>
> **兩個平台共用同一份 UI，所以回應格式不是實作細節。**
> `tests/test_mac_api_contract.py` 專門驗證這件事：它從 `app.js`
> 抓出實際呼叫的端點、以及 `MODEL_STATE` 認得的狀態值，
> 再比對後端的回應形狀。**要對齊 Windows 版的做法，不要自己設計格式。**

> 📌 **實作時踩到的三個坑：**
> 1. `TRIGGER_MODES` 是**給 UI 顯示的 dict 清單**（含 label/note），
>    不是字串集合。拿它來驗證字串會**永遠不相等**，症狀是
>    「合法的 `toggle` 被回『未知的觸發方式』」。要用
>    **`TRIGGER_MODES_VALUES`** / **`MIC_STREAM_VALUES`**。
> 2. `ui_server.py` 直接執行時 `app/core` 不在 `sys.path`（那是「被 import
>    時」才有的待遇），少了自建路徑會得到 `ModuleNotFoundError: config`。
> 3. **狀態值必須轉小寫。** `app.js` 的 `STATE_TEXT` 用小寫鍵
>    （`idle`/`recording`/…），但狀態機內部用大寫（`IDLE`/`RECORDING`）。
>    Windows 版在 `Status.on_state` 有 `state.lower()`，mac 版漏了 →
>    UI 顯示「未知」，而且狀態燈的 CSS class 也對不上。
>    **共用同一份 UI 時，這種「一邊有轉、一邊沒轉」會變成平台專屬怪症狀。**
>
>    同理：`mic_device` **不能是空字串** —— `app.js` 的判斷是
>    `s.mic_device != null`，空字串會通過，畫面就顯示成「device 」（空的）。
>
>    `tests/test_mac_ui_contract.py` 會從 `app.js` **實際解析出**
>    `STATE_TEXT` 的鍵來驗證（不自己抄一份清單 —— 抄一份就會漂移，
>    而那正是這個 bug 的成因）。

### 🔴 進入點必須自己擋 Python 版本（不能只靠 launch.py）

**實際踩到：** 使用者的 `python3` 是 conda 的 3.10（PATH 最前面），
直接跑 `python3 app/mac_vibetalkie.py` 會得到：

```
ModuleNotFoundError: No module named 'tomllib'      ← 3.11 才進標準庫
```

那個錯誤指向 import，**完全看不出「你只是用錯 Python」**。
而 `launch.py` 明明已經有「找合格的 Python 並重新執行」的邏輯 ——
只是被繞過了（使用者不會知道要先跑啟動器）。

所以 `mac_vibetalkie.py` 在最前面（**早於任何需要 3.11+ 的 import**）
呼叫 `launch.reexec_if_python_too_old(自己)`。
`reexec_if_python_too_old` 因此接受一個 `script` 參數 ——
否則會把使用者丟回 `launch.py`，然後被回「macOS 尚未支援」。

`tests/test_launch_python.py` 會用**真的舊解譯器**跑一次驗證
（不是只檢查原始碼裡有沒有那行字）。

### ⚠️ §8.2 規則 2 在 macOS 上**做不到**（已實測）

> 規則 2：必須能分辨「這顆鍵是不是目標裝置送的」。

Windows 靠 Raw Input 的 `hDevice`。**macOS 的 CGEventTap 沒有等價物** ——
實測結果（用同一支程式分別按藍牙麥克風錄音鍵與實體鍵盤）：

| 欄位 | 藍牙裝置 | 實體鍵盤 |
|---|---|---|
| `kCGKeyboardEventKeyboardType` | 40 | 40 |
| `kCGEventSourceUserData` | 0 | 0 |
| `kCGEventSourceStateID` | 1 | 1 |
| `kCGTabletEventDeviceID` | 0 | 0 |

**四個候選欄位全部相同 → 無法區分。**
（IOKit 的 `IOHIDManager` 能看到裝置身分，但收不到事件；實測需要
「輸入監控」權限且非特權行程拿不到，不列為可行路徑。）

**mac 版的策略：不做裝置辨識，靠「錄音鍵可自由設定」達到同樣效果。**
這與 `hotkey.py` 開頭的設計原則一致（「完全脫離硬體綁定」）。
→ **在 mac 上請不要用 `RightCtrl` 當錄音鍵**（會與實體鍵盤衝突且無法過濾），
建議 `F9` 或 `Ctrl+Shift+Space`。

### ⚠️ 麥克風「優先順序」在 macOS 上無效（必須講出來）

macOS 的 `AVAudioEngine` **只能用系統預設輸入裝置** —— 程式無法指定
要用哪一支（那由「系統設定 → 聲音 → 輸入」決定）。

但設定頁有一個「麥克風優先順序」清單（那是為 Windows 做的）。
在 mac 上那個清單**可以編輯但不會生效**。

**所以 `mic_warning` 必須有內容**（不是 None）—— UI 有現成的警告框
（`app.js` 收集 `mic_warning` / `bt_warning` / `vendor_warning` 顯示），
用那個管道告訴使用者「要換麥克風請到系統設定」。
**不講的話，使用者會排了半天順序然後發現完全沒作用。**

`vendor_warning` / `bt_warning` 則維持 `None`（mac 真的沒有那些偵測，
**不假裝有**）。`tests/test_mac_ui_contract.py` 對這兩類有不同斷言。

### 📌 裝置列舉：用 `system_profiler`，不要用 pyobjc 的 CoreAudio

設定頁的下拉選單需要**列出所有麥克風**。第一版只回一個
「系統預設輸入裝置」→ 使用者說「**裝置現在沒有列出來**」。

列舉走 `system_profiler SPAudioDataType`（macOS 內建、輸出穩定），
**不要用 pyobjc 的 `CoreAudio` 綁定** —— 它的簽章在不同版本間不一致，
而且錯誤訊息完全指不到真正的問題：

```
ValueError: argument 4 must be None or objc.NULL      ← 位址要 tuple
TypeError: depythonifying struct, got no sequence     ← 又說要 struct
```

解析時的四個坑（都實測踩到，見 `tests/test_mac_devices.py`）：

1. 取樣率的鍵是 **`Current SampleRate`**（沒有空格）—— 寫成
   `Current Sample Rate` 就永遠是 0。
2. 清單**同時包含輸出裝置**（喇叭、HDMI）→ 要按 `Input Channels` 過濾。
3. 但**某些虛擬音訊裝置真的有 `Input Channels`**（雙向的虛擬裝置）
   → 只能靠 **`Transport: Virtual`** 分辨，光看通道數分不出來。
4. 「預設裝置」要看 **`Default Input Device: Yes`**，不是取第一支。
   顯示「目前使用的麥克風」時也要挑那一支（取 `devs[0]` 會顯示錯的）。

有 30 秒快取（`system_profiler` 約 0.3–0.6 秒，設定頁不該每次重打）。

### 權限（TCC）

| 權限 | 用途 | 沒給的症狀 |
|---|---|---|
| **輔助使用** | CGEventTap 監聽 + 送出 Cmd+V | 啟動時就報錯（有做檢查） |
| **麥克風** | AVAudioEngine 錄音 | 錄到 0 bytes |

改完權限必須 **⌘Q 完全結束再重開** 該程式，權限才會生效。

> ⚠️ **權限無法自動化。** macOS 不提供程式化授權（TCC 的設計就是如此），
> 所以這一項一定要使用者自己動手。**但相依套件與模型會自動處理**（見下）。

### 自動補齊相依套件與模型（`app/core/autosetup.py`）

**每次啟動都會檢查一次**，缺什麼就補什麼，補完直接繼續（不用重跑）：

| 缺什麼 | 自動處理 |
|---|---|
| Python 套件（sherpa-onnx / numpy / opencc / pyobjc×3） | pip → 失敗則退回 `tools/p1/fetch_wheels.py` |
| ASR 模型（163 MB） | 呼叫 `tools/p1/fetch_model.py --get` |

- 一律裝到 **`third_party/`**（gitignored），不污染使用者的 site-packages。
- 東西齊的時候**只花約 0.3 秒**（純 import + probe），不會每次重裝。
- `--no-install` 可關掉（公司電腦／離線環境）。
- `--check` 只檢查不啟動，並列出每個相依的狀態。

> 🔴 **判斷「有沒有裝」不能只看 import —— 要真的動用它（probe）。**
>
> 實際的失敗模式：套件 import 成功，但**傳遞依賴缺了，用到才爆**。
> 例如 `opencc-python-reimplemented` 需要 `pkg_resources`（setuptools）。
> 只檢查 import 會把它判成「已就緒」，然後在**使用者第一次講話時**
> 才失敗 —— 最糟的時機，而且訊息通常指向別的地方。
>
> 所以 `PACKAGES` 的每個項目都是 `(PyPI 名, probe)`：
> `check_one()` 分三層判斷（import 不到 / import 到但 probe 失敗 / 可用），
> probe 失敗一律視為缺少 → 觸發安裝修復。
> `tests/test_autosetup.py` 有對照組驗證這件事。

> 📌 **`fetch_wheels.py` 修過一個潛伏的 bug。**
> 它的 `PLATFORM_PRIORITY` 原本寫死 `("win_amd64", "win32", "any")`，
> 所以在 macOS / Linux 上 `pick_wheel()` **永遠回 None** ——
> 症狀是「PyPI 明明有 wheel，工具卻說找不到」。
> 現在由 `platform_patterns()` 依平台決定，而且參數可注入
> （這樣三個平台的分支才都測得到，見 `tests/test_wheel_platform.py`）。
> **Windows 的優先序與修改前完全相同。**

### 🔴 三個會 segfault 的地雷（都實際踩過，不要「簡化」掉）

1. **`AVAudioConverter` 在 pyobjc 下會 segfault。**
   `mac_recorder.py` 因此自己做線性插值重採樣。
   最小隔離測試：拿掉 converter → 回呼正常（19 次 / 83790 frames）；加回去 → 崩。
2. **`floatChannelData()` 回傳的是 tuple，不是 ctypes 指標。**
   `t[0]` 是 `objc.varlist`，要用 **`as_buffer(count)`**（count 是樣本數，
   不給會 `TypeError`）。用 `raw[0][:n]` 切片會越界讀取 → segfault。
3. **tap 的 block 必須保留參照**（`self._tap_block`）。
   被 GC 回收而原生層還握著指標 → segfault。

另外：`installTapOnBus` **不接受**與硬體不同的格式
（指定 16 kHz 會得到 `Failed to create tap due to format mismatch`），
所以 tap 用原生格式（實測 44.1 kHz / 2ch / Float32），轉換自己做。

### 📌 實測：這支麥克風的正常訊號只有 0.3% FS

mac 內建麥克風講話時峰值實測 **82–110（0.25–0.34% FS）**。
**不要用「峰值大小」當判斷麥克風好不好的門檻** —— 200 會誤報正常語音。
`--test-mic` 的判準是「輸出內容像不像話」（長句且連貫 vs 空字串／極短），
訊號強度只當附註。

### 已知問題（尚未修，mac 與 Windows 都有）

**`config.toml` 的 `language` 完全沒有作用。**
`speech_engine._build()` 把 `language="auto"` 寫死，而 `ptt.py` 呼叫
`transcribe()` 時也沒傳。實測 `zh` / `yue` / `auto` 三種設定輸出**逐字相同**。
好在 `auto` 對粵語有效，所以不影響使用，但這個設定值是騙人的。



---

## 9. 工程習慣

### 改完一定要跑的測試

```powershell
python tests/test_status_contract.py       # UI ↔ /api/status 欄位契約
python tests/test_model_index.py           # 模型分類器（可用/不可用判斷）
python tests/test_model_index.py --live    # 對真實 499 筆跑統計（需連網）
python tests/test_models_api.py            # 模型下載／切換 API
python tests/test_speech_engine.py    # 引擎介面、PCM 轉換、簡繁
python tests/test_bandwidth.py        # 頻寬判定器（會決定準確率門檻）
python tests/test_mic_stream.py       # 串流模式、緩衝區回收、裝置復歸
python tests/test_config_api.py       # /api/config 欄位契約與驗證
python tests/test_trigger.py          # 錄音鍵解析＋三種觸發方式
python tests/test_launch_python.py    # 啟動器的 Python 自動尋找（挑最舊合格版／防無限迴圈）
python tests/test_wheel_platform.py   # wheel 平台選擇（mac/windows/linux 三分支都測）
python tests/test_autosetup.py        # 相依自動安裝（偵測／--no-install／依賴展開）
```

**macOS 專用**（需要 pyobjc，見 §8.7）：

```bash
PYTHONPATH=<pyobjc 目錄> python tests/test_mac_keys.py      # 鍵名對照＋熱鍵比對（42 項）
PYTHONPATH=<pyobjc 目錄> python tests/test_mac_trigger.py   # hold/toggle/double＋設定熱重載
PYTHONPATH=<pyobjc 目錄> python tests/test_mac_e2e.py       # 真端到端（合成按鍵→錄音→ASR→注入）
python tests/test_mac_devices.py                            # 音訊裝置列舉（解析 system_profiler）
```

`test_mac_ui_contract.py` 與 `test_mac_api_contract.py` 不需要 pyobjc
（`ui_server` 只用標準函式庫），它們驗證 mac 的 `snapshot()` 與
HTTP 回應形狀有沒有符合 `app/ui/app.js` 的期待 ——
**兩個平台共用同一份 UI，所以兩邊都要各自驗證契約**。

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
