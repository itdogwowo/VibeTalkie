# 硬體實測事實（Hardware Facts）

> **這是本專案唯一的硬體真相來源。** 任何程式碼、計畫或 AI 回覆若與本檔衝突，以本檔為準。
> 本檔只放**量測結果**，不放推測。未量測的一律寫「⏳ 待驗證」，不准填空。
>
> 量測環境：Windows 10/11 開發機（本機 registry / PnP / Raw Input 列舉）。
> 原始輸出（含裝置指紋與真實按鍵）留在本機 `artifacts/`，**不入版控**。本檔已去識別化。

---

## 1. 目標裝置

| 欄位 | 值 |
|---|---|
| 裝置名稱（藍牙節點） | `AI_VOICE_MAX` |
| 藍牙位址 | `<BT-MAC>`（已去識別化） |
| 出現的藍牙 profile | HFP（免手持）、HID（鍵盤）、SPP（序列埠）、廠商自訂 |

### 1.1 已綁定的 Bluetooth Profile

| UUID | Profile | 綁定結果 | 對本專案的意義 |
|---|---|---|---|
| `0000111e-…` | HFP — Hands-Free AG | `bthhfenum.sys`，音訊捕獲端點 DeviceState = **1（ACTIVE）** | **麥克風來源** |
| `00001124-…` | HID（HIDP） | `HidBth` 驅動已載入，`ConfigFlags = 0`（無問題旗標），展開 **3 個 HID collection**（Col01 / Col02 / Col03） | **按鍵來源** |
| `00001101-…` | SPP | `Standard Serial over Bluetooth link (COMxx)` | 目前不用；未來可能是廠商資料通道 |
| `fe010000-1234-5678-abcd-00805f9b34fb` | 廠商自訂 | 節點存在，無標準驅動 | 未研究；**不要**在 v1 依賴 |

> ⚠️ 另有第二個名稱相近的 `AI_VOICE` 藍牙節點存在（可能是同款第二台或舊配對紀錄）。
> 尚未確認哪一台是現役目標。

---

## 2. 音訊捕獲端點（T1 ✅ 通過）

Registry `MMDevices\Audio\Capture` 列舉結果（只列與本專案相關者）：

| 端點 FriendlyName | 端點 Description | DeviceState | 判定 |
|---|---|---|---|
| `Headset` | `AI_VOICE_MAX Hands-Free` | **1 = ACTIVE** | ✅ 這是目標麥克風 |
| （其他內建／USB／網路攝影機麥克風） | — | — | 非目標，但可作為對照組 |

waveIn 裝置索引為 **`[1]`**。

**重點：** 目標麥克風以**標準 Windows 音訊捕獲端點**出現，不需要自寫藍牙驅動。
這驗證了計畫 §2 的核心假設。

---

## 3. 按鍵（T3 ✅ 完成）

### 3.1 裝置向 Raw Input 註冊的 collection

用 `tools/p0/keycode_logger.py --list-devices --filter 00001124` 實測：

| Collection | Raw Input 型別 | HID Usage | 判定 |
|---|---|---|---|
| `…&Col01` | `type=1` | page `0x01` / usage `0x06`，`keys=154` | ✅ **標準鍵盤** |
| `…&Col02` | `type=2` | page `0x0C`（12）/ usage `0x01`，`vid=0xFFFF pid=0x0000` | Consumer Control，**但實測從未觸發任何事件** |
| `…&Col03` | — | — | 未註冊為 raw input 來源；**不要依賴** |

### 3.2 T3 定案：裝置有 **7 顆**按鈕，全部是標準鍵盤鍵

在**關閉原廠工具之後**重測（乾淨協定：手離開鍵盤，每顆按鈕各按 3 次），
得到 7 組乾淨的 3 按 3 放，全部來自 `BT-HID/Col01`：

| 按鈕 | 送出的鍵 | Raw Input VK | 備註 |
|---|---|---|---|
| 上 | `VK_UP` | 0x26 | 3 按 3 放 |
| 下 | `VK_DOWN` | 0x28 | 3 按 3 放 |
| 左 | `VK_LEFT` | 0x25 | 3 按 3 放 |
| 右 | `VK_RIGHT` | 0x27 | 3 按 3 放 |
| 確認 | `VK_RETURN` | 0x0D | 3 按 3 放 |
| 返回 | `VK_BACK` | 0x08 | 3 按 3 放 |
| **🎤 錄音** | **`VK_RCONTROL`（Right Ctrl）** | 0x11 + `E0` 旗標 | 3 按 3 放，見 §3.3 |

**結論：**

- 全部落在 **Col01 標準鍵盤 collection**，`VK` 碼正確。
- **完全沒有 `rawhid` 事件** → Col02（Consumer Control）沒有任何按鈕在用。
- **沒有任何 injected 事件** → 這些都是裝置的**原生輸出**，不是軟體注入。

→ 對「全域熱鍵」是好消息：現成函式庫看得到這些鍵，
   不需要為了 consumer usage 或廠商自訂 collection 改寫底層。

### 3.3 關鍵事實：錄音鍵是**修飾鍵**（Right Ctrl）

錄音鍵送出的是 **Right Control**，證據：

```
rawkb  BT-HID/Col01  VK_CONTROL  make=29  flags=0x02  WM_KEYDOWN   ← 0x02 = RI_KEY_E0（右側）
rawkb  BT-HID/Col01  VK_CONTROL  make=29  flags=0x03  WM_KEYUP     ← BREAK | E0
hook   VK_RCONTROL   scan=29  flags=0x1 / 0x81  injected=False
```

- Raw Input 回報的是**通用** `VK_CONTROL`（0x11），靠 **`E0` 旗標**才區分左右；
  Low-Level Hook 則直接回報 `VK_RCONTROL`（163）。這是 Windows 的既定行為，**已實測確認**。
- Hook 的 `injected=False` → 裝置原生，**不是原廠工具注入的**。
- 關閉原廠工具後行為**不變** → 這是裝置端（韌體或已寫入裝置的設定）的行為。

**這是本專案最重要的單一硬體事實。**

### 3.4 修訂：關於「抑制按鍵」的必要性（更正先前判斷）

⚠️ **本節更正我在 P0 中途做過的判斷。** 先前我認為「按鍵會漏進前景視窗，
所以抑制按鍵是必要條件」。找到錄音鍵的身分後，這個結論**需要分開看**：

| 按鈕 | 漏進前景視窗的後果 | 該不該抑制 |
|---|---|---|
| 上下左右 | 游標移動 | ❌ **不該抑制** —— 那是使用者刻意要的操作 |
| Enter | 送出表單／換行 | ❌ 同上 |
| Backspace | 刪字 | ❌ 同上 |
| **錄音鍵（Right Ctrl）** | 按住 Ctrl 本身**不會輸入任何字元** | 🟡 **建議抑制，但不是致命問題** |

**修正後的結論：**

- **6 顆導覽鍵本來就該正常運作**，不該被吃掉。
- **錄音鍵是 Ctrl，單獨按住無害**，所以「不抑制就毀掉產品」的說法是**錯的**（我先前的判斷過重）。
- 但**仍建議抑制**錄音鍵，理由：
  1. 按住錄音的數秒間，若使用者誤觸其他鍵，會變成 `Ctrl+?` 快捷鍵。
  2. 避免目標程式看到多餘的修飾鍵狀態。
  3. 這也是商業競品的做法（`hardware.md` §8.2 的 `block_keys`）。

**因此「抑制」從『必要條件』降級為『建議的品質改善』**，
但仍有一個**真正的必要條件**：

> **必須能分辨「這顆 Ctrl 是不是目標裝置送的」。**
> 使用者的實體鍵盤也有 Right Ctrl，若不區分，按實體鍵盤的 Ctrl 也會觸發錄音。

而這正是 `KBDLLHOOKSTRUCT` 沒有裝置欄位的問題所在（見 §3.5）。

### 3.5 熱鍵層的必要設計：雙通道時間關聯

| 機制 | 可抑制 | 可辨識裝置 |
|---|---|---|
| Raw Input | ❌ 只能觀察 | ✅ 有 `hDevice` |
| `global-hotkey`（Rust） | ❌ | ❌ |
| `WH_KEYBOARD_LL` 回傳非 0 | ✅ | ❌ **hook 沒有裝置欄位** |
| `pynput` `suppress=True`（Python） | ✅ | ❌ |
| `robotgo` / `go-hook`（Go） | ⚠️ 部分 | ❌ |

**必要設計：** 低階 hook 負責「抑制」，Raw Input 負責「辨識裝置」，
靠時間戳把兩邊配對，才能決定「這顆 Ctrl 是不是目標裝置送的、要不要吃掉」。

**三棧都沒有現成套件可直接用** —— 這是 `plan.md` §6 評分表「全域熱鍵」25% 的評分基準。

**另外：「修飾鍵單獨當熱鍵」本身就排除了註冊式函式庫。**
`global-hotkey` 之類的註冊機制以「組合鍵」為單位，不處理「單獨一顆 Ctrl 的按下與放開」。
→ 熱鍵層必須自行維護修飾鍵狀態機，並區分「單獨按 Ctrl」與「Ctrl+其他鍵」。

### 3.6 已解釋的早期異常（幽靈 Ctrl）

第一次擷取出現「只有 Ctrl 放開、沒有 Ctrl 按下」，當時以為是原廠工具注入。**現已釐清：**

- 那其實就是**錄音鍵（Right Ctrl）**。
- 缺「按下」的最可能原因：**擷取開始時該鍵已經是按住狀態**（使用者正握著裝置），
  而 Raw Input 只回報**狀態變化**，所以漏掉初始的 DOWN；Hook 則看到自動重複的 DOWN。
- 第二次擷取中 `make_code=0`、`flags=16`（`LLKHF_INJECTED`）的 Ctrl+V，
  是**貼上動作的合成事件**（虛擬裝置），與本裝置無關。
- 第三次乾淨擷取的 3 按 3 放完全正常，**證明工具本身沒有漏事件**。

**工具使用教訓（已寫進 `tools/p0/README.md`）：擷取開始前，確認沒有任何裝置按鍵是按住狀態。**

---

## 4. 四大假設的驗證狀態

| # | 假設 | 狀態 | 證據 |
|---|---|---|---|
| 1 | 麥克風以標準藍牙音訊輸入裝置出現在系統 | ✅ **已確認** | 捕獲端點 `AI_VOICE_MAX Hands-Free`，ACTIVE；waveIn `[1]` |
| 2 | 按鍵是 HID 鍵盤，按下送鍵碼、放開停止 | ✅ **已確認** | 7 顆按鈕，全在 Col01，乾淨的按下／放開配對（§3.2） |
| 3 | 裝置本身**不**內建辨識直接打字 | ✅ **已確認（但見 §8）** | 裝置只送按鍵；**會打字的是原廠工具**，不是裝置 |
| 4 | 麥克風支援 16 kHz 或可被系統重採樣 | 🟡 **初步正面** | 見 §4.1；尚未用語音確認 |

### 4.1 假設 4 的初步量測

`tools/p0/record_wav.py --device 1 --seconds 3 --rate 16000` →
錄到 16 kHz / mono / 16-bit，**頻譜能量一路延伸到 Nyquist（8000 Hz）**。

**推論：** 來源音訊**沒有**被 8 kHz 窄頻限制 ——
若底層是 CVSD 8 kHz，資料裡 4 kHz 以上**不可能**有能量。
所以本裝置應為 HFP 寬頻（mSBC 16 kHz），而非窄頻。

⚠️ **但還不能定案**：該次錄音只有環境底噪（峰值 260–639 / 32767），**沒有說話**；
底噪本身是寬頻的，也會填滿整個頻帶。

**待辦：** 跑 `record_wav.py --device 1 --seconds 6`，說話並發長音「ㄙ」，確認頻寬仍達 Nyquist。

### 4.2 為何這仍是高風險項

裝置走 HFP：窄頻（CVSD）**8 kHz** 不利 ASR；寬頻（mSBC）**16 kHz** 可接受。
若只拿到 8 kHz，「中文準確率 ≥ 95%」的門檻可能不成立。

---

## 5. 開發機工具鏈現況

| 工具 | 版本 / 狀態 |
|---|---|
| Python | 3.14.6（`C:\Python314`） |
| pip | 26.1.2 |
| numpy | 2.5.1 ✅ |
| scipy | 1.18.0 ✅ |
| onnxruntime | 1.28.0 ✅（**sherpa-onnx 可直接用**） |
| Node.js | 已安裝 |
| CMake | 已安裝 |
| Git | 已安裝 |
| **Rust / cargo** | ❌ **未安裝**（P2 前須補） |
| **Go** | ❌ **未安裝**（P3 前須補） |

**環境觀察（皆為實測）：**

1. `Get-PnpDevice` / `Get-CimInstance Win32_SoundDevice` 在目前 shell 下**不回傳任何裝置**；
   registry 列舉則正常 → P0 工具以 registry 為主要來源。
2. waveIn 列出的 4 個裝置 `dwFormats` **全都是 `0x000FFFFF`**（宣稱支援所有標準格式）。
   → **`dwFormats` 對判斷原生取樣率毫無用處**。
3. 同理，`record_wav.py --probe` 在 8 kHz～48 kHz **全部都會被「接受」**，
   因為 Windows 藍牙音訊堆疊會重採樣。
   → **唯一可信的頻寬判定方法是實際錄音後的頻譜分析**（工具已內建）。
4. 藍牙 HID 裝置的 interface path **不含裝置名稱**，只有 HID UUID
   → 用品牌名過濾抓不到，必須用 `--filter 00001124`。

---

## 6. P0 待辦清單

- [x] T1 列舉音訊輸入裝置
- [x] 列出 Raw Input 的 HID collection
- [x] T3 鍵碼量測（關閉原廠工具後，乾淨協定）
- [x] 釐清幽靈 Ctrl
- [x] 確認原廠工具不是「裝置會打字」的原因（裝置本身只送按鍵）
- [ ] T2 用**語音 + 長音「ㄙ」**重錄，確認頻寬達 Nyquist
- [ ] 確認兩個名稱相近的藍牙節點中，哪一台是現役目標
- [ ] 決定「抑制錄音鍵」是否列入 v1（建議列入，見 §3.4）

---

## 7. 量測紀錄

| 日期 | 項目 | 結果 |
|---|---|---|
| P0 起始 | Profile / 端點列舉 | HFP＋HID＋SPP＋vendor；捕獲端點 ACTIVE |
| P0 起始 | Raw Input collection 列舉 | Col01 = 鍵盤(154 鍵)；Col02 = Consumer Control（未使用）；Col03 未註冊 |
| P0 起始 | 取樣率（環境底噪，3 秒） | 16 kHz mono 成功；頻寬達 Nyquist 8000 Hz → 初步排除 8 kHz 窄頻，**待語音複驗** |
| P0 起始 | T3 第一次擷取（51 筆） | 協定被污染；出現「幽靈 Ctrl」（後證實為錄音鍵，見 §3.6） |
| P0 起始 | T3 第二次擷取（90 筆） | 6 顆導覽鍵各 3 按 3 放 |
| P0 起始 | 原廠工具技術剖析 | 見 §8.2（Tauri、SenseVoice、保留錄音 3.3 MB） |
| P0 起始 | **T3 定案**（關閉原廠工具，87 筆） | **7 顆按鈕**：UP/DOWN/LEFT/RIGHT/RETURN/BACK + **錄音鍵 = Right Ctrl**，全原生、無 injected |
| — | T2 語音複驗 | ⏳ |

---

## 8. 原廠軟體：閃電說（shandianshuo）

### 8.1 事實

| 項目 | 值 |
|---|---|
| 程式名稱 | `shandianshuo.exe`（「閃電說」）v0.7.8 |
| 開發者 | 探未（武漢）科技有限公司 |
| 安裝位置 | `%LOCALAPPDATA%\Shandianshuo\`（單一 78 MB 執行檔 + `uninstall.exe`） |
| 自動啟動 | ✅ 已登錄於 `HKCU\...\CurrentVersion\Run`，帶 `--autostart` |
| 與裝置的關係 | 裝置綁定此程式；錄音鍵（Right Ctrl）是它的觸發鍵 |
| 是否會注入按鍵 | ✅ 是（曾觀察到 `LLKHF_INJECTED` 事件） |

### 8.2 技術剖析（本機實測，未讀取任何個人內容）

| 項目 | 實測結果 |
|---|---|
| 應用框架 | **Tauri** —— exe 內含 `tauri` / `tao` / `wry` / `WebView2` 字串；資料在 `%LOCALAPPDATA%\cn.shandianshuo.desktop\EBWebView` |
| 內建 ASR 模型 | **`sensevoice-small`**（SenseVoiceSmall，`model.onnx` 約 241 MB） |
| 推論引擎 | exe 內含 `onnxruntime`、`SenseVoice`、`whisper`；**未見 `sherpa`** → 很可能是直接用 onnxruntime |
| 本機保留資料 | `recordings/`（**5 個檔案、約 3.3 MB → 有保留錄音**）、`transcriptions.json`、`memory/knowledge/`、`skills/`（空） |

> 🔒 上述僅為**結構與計數**。逐字稿、知識庫、錄音內容、`config.json` 的值
> **一律沒有讀取**，也沒有進入本 repo。

**從 `config.json` 的「鍵名」看出的既有功能：**

| 鍵名 | 對應本計畫的模組 |
|---|---|
| `injection_mode`、`use_direct_input` | `textin`（文字注入） |
| `hotkey`、`recording_hotkey`、`unified_hotkey{keys, **block_keys**}` | `hotkey` |
| `mute_while_recording`、`silent_start` | `audio` |
| `save_transcription_to_clipboard` | `textin`（剪貼簿路徑） |
| `text_normalization_*`、`remove_trailing_period` | ASR 後處理 |
| `windows_recording_key` / `macos_recording_key` | 跨平台按鍵 |
| `asr{provider, local_enabled, local_parakeet_enabled, aliyun, volcengine, stepfun, mimo, soniox, elevenlabs, openai_realtime, custom_providers}` | 多引擎（本地＋大量雲端） |
| `ai{correction, chat, memory_learning, input_assistant, personal_preference, codex_network}` | **LLM 糾錯／聊天／記憶**（本計畫明確不做） |

**兩個關鍵觀察：**

1. **`block_keys` 的存在，佐證了 §3.4 的建議** ——
   認真的商業競品也要處理「按鍵漏出去」，而它的解法就是「設定要阻擋的鍵」。
2. **競品同時支援本地與一大串雲端 ASR** ——
   本計畫的 v1 是全本地、零強制上傳 → **差異化不在「能不能本地」，而在「只有本地」**。

### 8.3 對本專案的意義

1. **裝置已經有一套可用的商業語音輸入軟體。**
   VibeTalkie 不是「讓硬體能動」，而是**取代原廠工具**。
2. **兩個程式會搶同一顆按鈕與同一個麥克風。**
   若不偵測並排除閃電說，會出現雙重插入、按鍵互搶、錄音裝置佔用。
3. **裝置本身只送按鍵，不會自己打字** —— 會打字的是軟體。
   所以計畫 §2 原本擔心的「裝置內建辨識」**不成立**。

### 8.4 定位（**使用者已於 2026-09 決定**）

> **決定：取代原廠工具。目標是「百分之百自主可控」，部分功能不需要，
> 專注於語音轉文字。**

據此，VibeTalkie 的價值主張是：

| 面向 | 閃電說 0.7.8 | VibeTalkie v1 | 差異 |
|---|---|---|---|
| ASR | 本地 SenseVoice **＋ 多雲端** | 本地 sherpa-onnx | ✅ **只有本地** |
| 文字注入 | 有 | 有 | ❌ 無差異 |
| 錄音檔 | **有保留**（實測 3.3 MB） | 辨識後刪除 | ✅ **隱私** |
| LLM 糾錯／聊天／記憶／技能 | **有** | **明確不做** | ✅ **自主可控、可預測** |
| 開源可稽核 | ❌ 閉源 | 可 | ✅ **可稽核** |

→ 這是一個**清楚的利基**，不是「功能更少的閃電說」。

### 8.5 連帶修改

- `plan.md` §2 架構分支：**不成立**（裝置不內建辨識），改為「原廠工具競爭」風險。
- `plan.md` §11 風險表：已更新為「已發生」的三項。
- `architecture.md`：新增 `vendor` 模組（偵測並要求關閉原廠工具）；
  `hotkey` 契約改為「雙通道 + 修飾鍵狀態機」。
- `adr/0001-stack.md`：「全域熱鍵」評分基準已更新；
  並新增外部實證（**同利基的商業產品用 Tauri 打造**）。
