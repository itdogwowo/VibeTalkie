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
│  │  ├─ hotkey.py        #   錄音鍵規格解析（**多組**：RightCtrl / F9 / Ctrl+Alt+R）
│  │  ├─ trigger.py       #   ★ **平台無關的觸發狀態機**（兩個平台共用同一顆）
│  │  ├─ launch_guard.py  #   ★ 啟動防護：這個 port 已經有實例就不開第二個
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
│  ├─ run_all.py           #   一鍵跑完（掃目錄，**清單的單一來源**）
│  ├─ _console.py          #   測試輸出一律 UTF-8（Windows 管線下預設是 cp950）
│  ├─ test_shared_layer.py #   ★ 同一條規則只能有一份實作（AST 掃定義）
│  └─ …（共 22 支；每一支在驗什麼見 §9）
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
| `hotkey` | 按鍵**規格**的解析與比對（名稱／側別／多組綁定） | 不維護觸發狀態 |
| `trigger` | 按鍵**事件** → 開始／停止的決策（多組命中／雙邊行為／停用／測試模式） | 不碰音訊、不碰平台 API |
| `launch_guard` | 重複啟動的偵測與拒絕（port 已被佔用 → 不開第二個實例） | 不管音訊、不管 UI、不決定訊息印在哪 |
| `config` | TOML 讀寫（serde） | 不驗證業務邏輯 |

### ⚠️ 為什麼有 `trigger.py`（兩個平台共用同一顆引擎）

`ptt.py`（Windows）與 `mac_vibetalkie.py`（macOS）原本**各有一份**觸發邏輯。
第一份只有「一顆鍵 + 一個全域模式」，第二份只有「單一顆鍵 + 三個 if」——
然後 main 這一邊長出了多組／配對／雙邊行為／停用／測試模式，
而 mac 那一邊**一項都沒有**。兩份實作一定會漂移，所以與 OS 無關的部分
全部搬到 `app/core/trigger.py`。

**邊界**：`trigger.py` 匯入 `hotkey`，**不匯入** `ptt`／`recorder`／`mac_*`，
也沒有 `ctypes`／`Quartz`／`WM_KEYDOWN`。平台層只提供兩個回呼
（`on_start`／`on_finish`）與一個事件（`trigger.Event`）。
`tests/test_mac_import.py` 用 AST 掃 import、用 `tokenize` 掃程式碼區把這件事釘住。

### ⚠️ 同一招也用在 `launch_guard.py`（而且是被咬第二次才補的）

`vibetalkie.py`（Windows）與 `ui_server.py`（mac）原本**各有一份**
`pick_port()`／`port_in_use()`／`AlreadyRunning`。兩份一開始一樣，後來只改了
一份 —— 而這一條規則的漂移症狀是**「設定存了又變回去」**：兩個行程共用
`config.toml`、各自握一份記憶體，`cfg.save()` 每次整個檔案重寫，舊的那個
會把新值蓋掉。

所以規則搬到 `app/core/launch_guard.py`：**規則住共用層、平台差異當參數**
（`explain()` 決定講什麼，Windows 傳「去工作管理員關掉」、mac 傳「⌘Q」）。

> 📌 **教訓：規範寫在文件裡擋不住這件事。** 這個 repo 到處都是「不要各寫一份」
> 的註解，還是多寫了一份。能擋住的是**會失敗的測試** ——
> `tests/test_shared_layer.py` 用 AST 數每一條已共用規則的定義次數，
> 只能有一份；搬位置可以，但要同時改那張表（刻意的摩擦）。
>
> 同一個道理的另一個證據：`AGENTS.md` §9 的測試清單本身在一小時內就漂移過
> （實際 22 支、清單寫 20 支）→ 現在改成掃目錄的 `tests/run_all.py`。

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

### 8.2 四條不可違反的規則

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

**規則 4：錄音鍵是「清單」，每一組是 `開始[,結束][@開始行為][@結束行為]`。**

`config.toml` 的正式來源（舊的單一字串 `hotkey` 仍相容，只在清單為空時當後備）：

```toml
hotkeys = ["RightCtrl", "F9,Esc", "F8,Space@double@double"]
```

| 寫法 | 意思 |
|---|---|
| `RightCtrl` | 開始＝結束＝RightCtrl，兩邊都用預設行為 |
| `F9@double` | 開始鍵連按兩下開始 |
| `F9,Esc` | **開始 F9、結束 Esc**（不同顆鍵的配對） |
| `F9,Esc@toggle` | 同上，但 Esc「再按一下」才停（`hold`＝鬆開才停，是預設不寫） |
| `F8,Space@double@double` | 連按兩下 F8 開始、連按兩下 Space 結束 |

⚠️ 格式的三條硬規則（都有測試釘住，`tests/test_trigger.py` [15][16]）：

1. **`,` 是配對、`+` 是組合鍵。** 第一版用 `+` 當配對分隔符，結果
   `Ctrl+Alt+R`（**一個**組合鍵）被切成「開始 Ctrl+Alt、結束 R」——
   因為 `Ctrl+F9` 也是合法組合鍵，字串本身分不出來。
2. **行為寫在它修飾的那一顆鍵後面**（`F9@double,Esc`）**或擠在結尾**
   （`F9,Esc@double@double`，依序＝開始、結束）。兩種都吃，但**輸出只採
   擠在結尾那一種**，否則存一次就換一種寫法、很難比對。
   第一版把 `@` 一律當「結尾的一個」，結果連最基本的 `F9,Esc` 都被誤殺
   （整筆設定被當成「認不得的按鍵名稱：'F9,Esc'」）。
3. **結束鍵優先於行為。** 寫了 `F9,Esc` 就代表「按 F9 開始、按 Esc 結束」——
   這時放開 F9 **不會**結束錄音；兩邊的行為只管「怎麼觸發那一邊」。
   沒寫結束鍵（`F9`）時，放開開始鍵就停。

**每一邊的行為是獨立的**（實測回饋：「開始和結束都有他自己的行為，
下拉選單，而且開始和結束的行為會不一樣，結束有鬆開行為」）：

| 邊 | 選項 | 意思 |
|---|---|---|
| 開始 | `hold`（按一下開始）／`double`（連按兩下開始） | 怎麼**開始** |
| 結束 | `hold`（**鬆開**才停）／`toggle`（再按一下停）／`double`（連按兩下停） | 怎麼**結束** |

⚠️ 實作上的三條鐵則：

1. **「結束鍵放開才停」需要 keyup 才收尾**（`_end_armed`）。按下就停的話，
   使用者按 Escape 的瞬間錄音就斷了 —— 但他可能還想多講半句。
2. **「等待第二下」的狀態要每一組各記一份**（`_tap_at` 以規格為 key）。
   用單一變數的話，兩顆雙擊鍵輪流各按一下就會被誤判成雙擊。
3. **按住不放的自動重複要吃掉**（`_main_down` 集合）。Windows 每 ~30ms 送一次
   keydown，算成「點一下」的話，按住一顆 double 鍵反而會開始錄音。

理由（使用者實測回饋）：只綁一顆不夠用、模式不能只有一個、
**開始與結束可以是不一樣的按鍵**，而且**兩邊的行為也要能各選各的**。

**UI 一列只有兩個區域**（實測回饋：「你現在設定了三個區域，實質兩個區域就
足夠了」）—— `開始` 與 `結束` 各自有鍵、行為下拉、🎧 錄製鈕：

```
開始 [RightCtrl ▾]   F9   [🎧 錄製]     結束 [Esc ▾]   Esc  [🎧 錄製]
行為 [按一下開始 ▾]                     行為 [鬆開才停 ▾]
```

**錄製只錄「按鍵」，不猜行為。** 曾經在錄製時用手勢判斷（長按＝hold、
短按＝toggle、連兩下＝double），使用者直接否決：

> 錄製的時候不需要判斷，需要判斷是不是組合就可以了

那個做法本來也不可能對 —— 「短按一下」既可能是 toggle，也可能只是 double 的
第一下，當下無法分辨。所以：**錄到之後由使用者決定這顆是「開始」還是「結束」**，
行為用下拉選單選。

⚠️ **Escape 不可以當「取消鍵」。** Escape 是最常用的結束鍵；只要它代表取消，
「用 Esc 結束錄音」就永遠錄不出來（實測踩到）。取消改用面板上的按鈕。

**多組是「同時生效」的，而且每一組可以單獨啟用／停用。**
每一次按鍵都會比對**所有啟用中的**組合，任何一組命中就觸發
（`tests/test_trigger.py` [17] 用三組不同性質的組合釘住這件事）。

停用的寫法是在鍵名前面加 `~` —— `hotkeys = ["RightCtrl", "~F9,Esc"]`：

| 為什麼是前綴而不是刪掉 | 為什麼不是另開欄位 |
|---|---|
| 停用的組合要**看得見**（知道自己有什麼），要恢復時不必重錄 | 兩份清單會走音，手改 TOML 也要改兩個地方 |

⚠️ 三條規則（[18] 釘住）：

1. **停用的組合完全不理**：開始鍵、結束鍵都不比對（含它的結束鍵）。
2. **`entries` 要把停用的寫回去**（帶 `~`）—— 否則「存一次設定就刪掉一個組合」。
3. **全部停用要明講**（`bindings()` 回報「所有錄音鍵都被停用了」）——
   不然使用者只看到「按了沒反應」，而畫面上每一列看起來都正常。

**「測試」模式：只聽、不錄、不注入**（`PttDaemon.start_key_test`，[19] 釘住）。

為什麼要有：設定好之後，唯一的驗證方式是「按下去看有沒有反應」，
但那會**真的錄音並把文字注入**使用者正在打的地方。
所以需要一條沒有副作用的驗證路徑：UI 每一列都有「測試」鈕，
按下後（20 秒內）按任何鍵，狀態列會回報

```
🧪 測試中（剩 19.4s，不會錄音）　請按你要測的鍵…
✓ 收到 Esc → 結束鍵（F9·按一下開始）　（前 1 次：F9）
```

**沒命中的鍵也要回報**（「不相關的鍵（不會觸發）」）—— 「按了沒反應」時
最需要知道的就是「到底是沒收到，還是被邏輯吃掉」。

⚠️ 實作陷阱：判斷測試模式**不能只看時間戳有沒有值**，要比較 `time.monotonic()`；
否則過期後那個時間戳還留著，錄音功能會**永遠失效**（實測踩到）。

四個容易寫錯的地方，都有測試釘住（`tests/test_trigger.py`）：

| 陷阱 | 正確行為 |
|---|---|
| 加了第二組，第一組反而失效 | 每一組**各自**判定，不是取代 |
| 裝置政策被第一組綁死 | 只要**有任一組**不是裝置原生鍵，就允許來自任何裝置 |
| `LeftCtrl` 左右不分（真的踩過） | 側別是三態：`None`＝兩側都算、`"left"`／`"right"`＝限定該側 |
| 兩顆 double 鍵互相配對 | `_tap_at` 每組各記一份 |

解析失敗**不可靜默**：`hotkey.bindings()` 保留認得的那幾組並回報原因，
原因會一路傳到 UI（`/api/status` 的 `hotkey_warning`）。行為打錯字
（`F9@dboule`）或配對任一邊打錯（`F9,Banana`）也一樣要回 400，
不可靜默吃掉。

### 8.2.1 第一次按下收不到音訊（藍牙 SCO）——**暖機不算完成**

藍牙 HFP 的 SCO 音訊連線是**串流開始時**才建立，在那之前 `waveIn` 一個
byte 都拿不到。所以「暖機成功」不等於「下一段錄得起來」。

**使用者抱怨了兩次**（「經常第一段錄音無法錄製」），第一次的修正（自動重錄）
只是把症狀蓋掉；真正的修法在第二次：**串流一直開著待命 + 前捲**。

| 陷阱 | 症狀 | 正確做法 |
|---|---|---|
| 暖機後把串流關掉 | log 顯示暖機成功（0.6s），**第一次按下還是 `音訊 0.00s`** | 暖機完**一律留著**（`_ensure_capture()` 之後沿用同一個） |
| 每次按下才開串流 | 每次都要重新協商 SCO → 第一次幾乎一定失敗 | 串流常駐待命；待命期間維護**前捲**（`_PREROLL_S`＝0.25s） |
| 暖機跑在主迴圈之前 | 那時 `mic_stream` 還是 `_live()` 的後備值 `per_press` | `run()` 先 `_sync_config()` 再 `warmup_audio()` |
| 命令列模式不讀設定檔 | 設定寫 `session`，CLI 卻用 `per_press`（`_live()` 只有後備值） | `main()` 讀 `config.toml` 並傳 `cfg_provider` |
| 空錄音只印「請再按一次」 | 使用者第一句白講，而且他不知道要重講 | **同一口氣自動重錄**（`_retry_empty_capture`）仍保留當保險 |
| 暖機判斷「有沒有比開始時多」 | 暖機前就累積了音訊 → 明明連線好了卻等到逾時 | 「已經有音訊」就算成功（`first = 0.0`） |

**實測證據**（`tools/p1/measure_standby_audio.py`，2026-09）：

```
靜置 15 秒結束：累計 463938 bytes（約 14.5 秒的音訊）
沒有新資料的次數：1        → 待命期間音訊持續進來 ✅
再等 5 秒：累計 640000 bytes（增加 176062）→ 閒置 20 秒也沒斷
```

端到端（真實裝置）：暖機 1.23s → 待命 0.6s（前捲 0.25 秒）→
**按下時立刻就有 8000 bytes** → 錄 1.5s 得到 1.65s 的音訊（前捲有接上）。

**自動重錄的規則**（`tests/test_trigger.py` [13] 釘住）：

- 只在使用者**還按著**時重錄（放開後重錄會錄到環境音）
- 一次按下**最多重試一次**（`_retry_armed` 只在 `_on_main_down` 武裝；
  `_retrying` 標記「正在重試」）—— 否則收不到音訊時會無窮迴圈
- 統計口徑：救回來的那次記 `retried`，重試後仍空白才記 `failed`

**音訊資料流**（`_tick_capture()`，每個 tick 跑一次）：

```
cap.recorded() ──► 新音訊（用 _cap_seen 切）
                     ├─ IDLE  → 前捲環形緩衝（保留最近 0.25s）+ 緩衝區回收
                     └─ 其他  → _rec_pcm 累積（錄音中）
按下時：_rec_pcm = 前捲（蓋掉 SCO 剛建立那段空窗）
放開時：pcm = bytes(_rec_pcm)
```

⚠️ **回收判斷要在「沒有新音訊就 return」之前**：緩衝區滿的時候往往正是
「沒有新音訊」的那一輪（擷取已經停了），先 return 就永遠不會回收。
⚠️ **回收門檻要用舊索引**判斷「上一輪之後累積了多少」，用新的會失準。

**換裝置（藍牙麥克風省電休眠回來）是另一個坑**（`tests/test_mic_stream.py` [14]）。

實測 log：

```
目標麥克風離線（省電休眠？）→ 已重新上線 → 下次錄音會用它（device 0）
🔴 錄音中…
⏹  停止（2.6s，音訊 0.25s）      ← 0.25 秒＝前捲
⚠️ 沒有辨識出文字（22 ms）
```

兩個錯誤疊在一起：

| 問題 | 後果 | 修正 |
|---|---|---|
| 換裝置時沒有清前捲 | `_rec_pcm` 開頭是**舊裝置**的聲音 | `_ensure_capture()` 換裝置後 `_preroll = b""` |
| 新串流還沒送音就按下 | 只有前捲那 0.25 秒 → 被當成「太短」丟掉 | `_WARM_GRACE_S`（1.5s）內音訊不足 → **自動重試**而不是放棄 |

「太短」與「串流還沒暖」是**兩件不同的事**：前者是誤觸（真的按太快），
後者是裝置問題（重開就會好）。用 `_warm_deadline` 分辨。

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
python tests/run_all.py             # 一鍵跑完 tests/ 底下所有測試
python tests/run_all.py --list      # 只列出會跑哪些（含跳過的與原因）
python tests/run_all.py --only ptt  # 只跑檔名含這個字串的
```

> ⚠️ **不要再手寫測試清單。** 這裡原本是一串 20 條指令，而實際發生過：
> `tests/` 裡已經 22 支，清單只列 20 支 —— 漏掉的那兩支（`test_launch_guard.py`、
> `test_mac_keys.py`）從此沒人跑，而且沒有任何東西會告訴你。
> 現在由 `run_all.py` 掃目錄，清單不會再漂移。
>
> ⚠️ **每一支測試都要呼叫 `tests/_console.py`**（`_console.setup()`）。
> Windows 的 Python 在輸出**被管線接走**時用的是地區設定（cp950）而不是
> 主控台代碼頁，測試會崩在印 `✅` 那一行 —— 實測 22 支裡有 12 支是這樣紅的，
> 看起來像產品壞了，其實是測試的輸出壞了（同 §8.6 的規則，只是對象是測試）。
> `run_all.py` 也會把 `PYTHONUTF8=1` 傳給子行程；**兩邊都要做**，
> 因為直接跑單支時沒有那個環境變數。

各支測試在驗什麼：

| 測試 | 內容 |
|---|---|
| `test_status_contract.py` | UI ↔ `/api/status` 欄位契約（讀 `app/ui/app.js`） |
| `test_model_index.py` | 模型分類器（`--live` 對真實 499 筆跑統計，需連網） |
| `test_models_api.py` | 模型下載／切換 API |
| `test_speech_engine.py` | 引擎介面、PCM 轉換、簡繁 |
| `test_bandwidth.py` | 頻寬判定器（會決定準確率門檻） |
| `test_mic_stream.py` | 串流模式、緩衝區回收、裝置復歸（§8.2.1） |
| `test_config_api.py` | `/api/config` 欄位契約與驗證 |
| `test_trigger.py` | 錄音鍵：多組＋左右側＋每組自己的觸發方式（§8.2 規則 4） |
| `test_hotkey_names.py` | canonical 名稱契約（名稱／側別／平台鍵碼） |
| `test_trigger_engine.py` | 觸發引擎（平台無關，直接餵 `Event`） |
| `test_hotkeys_patch.py` | `/api/config` 的 hotkeys 驗證（兩平台共用一份） |
| `test_launch_guard.py` | 重複啟動要拒絕（**真的開行程佔 port**，不只看原始碼） |
| `test_shared_layer.py` | ★ 同一條規則只能有一份實作（AST 掃定義，見 §5） |
| `test_launch_python.py` | 啟動器的 Python 自動尋找（挑最舊合格版／防無限迴圈） |
| `test_wheel_platform.py` | wheel 平台選擇（mac/windows/linux 三分支都測） |
| `test_autosetup.py` | 相依自動安裝（偵測／`--no-install`／依賴展開） |
| `test_mac_import.py` | mac 模組的可載入性＋共用層不得有平台相依 |
| `test_mac_trigger.py` / `test_mac_keys.py` | mac 的觸發行為與鍵名對照（**不需要 pyobjc**） |
| `test_mac_ui_contract.py` / `test_mac_api_contract.py` | mac 的 `snapshot()` 與 HTTP 回應形狀 |

> ⚠️ `test_mac_trigger.py` 與 `test_mac_keys.py` **不需要 pyobjc** ——
> `mac_vibetalkie.py` 與三個 mac 模組都刻意不在頂層 import 原生 API，
> 所以狀態機與鍵名對照在 Windows 上也能驗（`test_mac_import.py` 把這個
> 性質釘住）。原本它們各自有一個「import 不到 Quartz 就 return 2」的
> 閘門，那等於讓整合期間的每一次重構都失去這兩層保護。

**真的 macOS 專用**（需要 pyobjc 或 `system_profiler`，見 §8.7）：

```bash
PYTHONPATH=<pyobjc 目錄> python tests/test_mac_e2e.py   # 真端到端（合成按鍵→錄音→ASR→注入）
python tests/test_mac_devices.py                        # 音訊裝置列舉（解析 system_profiler）
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
- **測試不准動使用者的 `config.toml`。** 自己的設定自己存暫存檔
  （`cfg.save = lambda path=None: Config.save(cfg, TMP)`，見 `test_config_api.py`）。
  實測踩到：`test_models_api.py` 的「切換模型」會讓 handler 寫到**真的**
  `config.toml` —— 跑一次測試就改掉使用者選的模型，而且與常駐程式搶同一個檔案
  （症狀是**偶爾紅、重跑就過**，最難查的那一種；間歇性紅燈會訓練人忽略紅燈）。
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
