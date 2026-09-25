# 硬體實測事實（Hardware Facts）

> **這是本專案唯一的硬體真相來源。** 任何程式碼、計畫或 AI 回覆若與本檔衝突，以本檔為準。
> 本檔只放**量測結果**，不放推測。未量測的一律寫「⏳ 待驗證」，不准填空。
>
> 量測環境：Windows 10/11 開發機（本機 registry / PnP 列舉）。
> 原始輸出（含裝置指紋）留在本機 `artifacts/`，**不入版控**。本檔已去識別化。

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
> P0 必須確認**哪一台**是實際要用的。

---

## 2. 音訊捕獲端點（T1 已通過）

Registry `MMDevices\Audio\Capture` 列舉結果（只列與本專案相關者）：

| 端點 FriendlyName | 端點 Description | DeviceState | 判定 |
|---|---|---|---|
| `Headset` | `AI_VOICE_MAX Hands-Free` | **1 = ACTIVE** | ✅ 這是目標麥克風 |
| （其他內建／USB／網路攝影機麥克風） | — | — | 非目標，但可作為對照組 |

**重點：** 目標麥克風以**標準 Windows 音訊捕獲端點**出現，不需要自寫藍牙驅動。
這驗證了計畫第 2 節的核心假設，**架構不需要走「透傳/監聽模式」分支**（前提見 §4 假設 3）。

---

## 3. 從 HID 節點讀到的事實（T3 已測，但受原廠工具污染 → 見 §8）

```
DeviceDesc  = Bluetooth HID Device     (hidbth.inf)
Service     = HidBth
ClassGUID   = {745a17a0-74d3-11d0-b6fe-00a0c90f57da}   ← HIDClass
ConfigFlags = 0                                          ← 驅動正常啟動，無 Code 系問題
Mfg         = Microsoft
```

HID 列舉樹下存在三個 collection：

```
{00001124-…}_LOCALMFG&03e0&Col01
{00001124-…}_LOCALMFG&03e0&Col02
{00001124-…}_LOCALMFG&03e0&Col03
```

**推論（尚未實測，不可當事實用）：** 三個 collection 通常分別是
①標準鍵盤 ②Consumer Control（多媒體鍵）③廠商自訂或 System Control。

**這正是 P0 必須實測的原因**：按鈕若落在 Col02/Col03，
一般鍵盤 API（`WH_KEYBOARD_LL`、`RegisterHotKey`、`global-hotkey`）**可能完全看不到**，
熱鍵層就必須改用 Raw Input 或 HID API。三棧的「全域熱鍵」評分直接受此影響。

### 3.1 Raw Input 實測（**已量測**）

用 `tools/p0/keycode_logger.py --list-devices --filter 00001124` 實測，
本裝置向 Raw Input 註冊了 **2 個** top-level collection：

| Collection | Raw Input 型別 | HID Usage | 判定 |
|---|---|---|---|
| `…&Col01` | `type=1` | page `0x01` / usage `0x06`，`keys=154` | ✅ **標準鍵盤**（154 鍵） |
| `…&Col02` | `type=2` | page `0x0C`（12）/ usage `0x01`，`vid=0xFFFF pid=0x0000` | ✅ **Consumer Control（多媒體鍵）** |
| `…&Col03` | — | — | ⚠️ **未出現在 Raw Input 清單中**（存在於 HID 列舉樹，但未註冊為 raw input 來源） |

**這件事的意義：** 裝置同時暴露「標準鍵盤」與「Consumer Control」兩種介面，
所以按鈕落在哪一邊**決定了熱鍵層能不能用現成的全域熱鍵函式庫**。
（Col03 未註冊成 raw input，代表它很可能不是輸入用 collection，或 only-feature/report 通道；
v1 不要依賴它。）

### 3.2 T3 第一次擷取（**未定案**）

第一次擷取（51 筆事件，`artifacts/t3-keycodes.jsonl`）的結果：

**已確定的部分 — 裝置確實是一顆可用的 HID 鍵盤：**

藍牙 HID 鍵盤 collection（`…&Col01`）送出了標準鍵盤事件，每次都是乾淨的
1 按下 / 1 放開，VK 碼正確：

| 鍵 | VK | 按下 | 放開 |
|---|---|---|---|
| `VK_LEFT` | 0x25 | 2 | 2 |
| `VK_RETURN` | 0x0D | 1 | 1 |
| `VK_BACK` | 0x08 | 1 | 1 |
| `VK_DOWN` | 0x28 | 1 | 1 |
| `VK_UP` | 0x26 | 1 | 1 |
| `Z` | 0x5A | 1 | 1 |

→ **假設 2 的「裝置是 HID 鍵盤」部分成立**，且事件落在 Col01（鍵盤 collection），
不是 Col02（Consumer Control）。這對熱鍵層是好消息。

**⚠️ T3 結論有重大但書：裝置綁定了原廠工具，見 §8。** §3.3 量到的 6 顆鍵
**可能有一部分是原廠工具重新綁定後的結果，不是裝置韌體的原生輸出**。
必須在關閉原廠工具後重測才能定案。

### 3.3 T3 第二次擷取（乾淨協定）— 表面上定案
第二次擷取（手離開鍵盤，每顆按鈕各按 3 次）共 90 筆事件，結果乾淨：

| 按鈕送出的鍵 | VK | 按下 | 放開 |
|---|---|---|---|
| `VK_RETURN` | 0x0D | 3 | 3 |
| `VK_BACK` | 0x08 | 3 | 3 |
| `VK_UP` | 0x26 | 3 | 3 |
| `VK_DOWN` | 0x28 | 3 | 3 |
| `VK_LEFT` | 0x25 | 3 | 3 |
| `VK_RIGHT` | 0x27 | 3 | 3 |

**六顆按鈕，每顆剛好 3 按 3 放，全部落在 `Col01`（標準鍵盤 collection），VK 碼正確。**
且整份紀錄**沒有任何 `rawhid` 事件** → `Col02`（Consumer Control）從未觸發。

→ 對「全域熱鍵」是好消息：三棧的現成熱鍵函式庫都看得到這些鍵，
   不需要為了 consumer usage 或廠商 collection 改寫底層。

### 3.4 但這帶來兩個計畫沒預期的問題

#### 問題 1：沒有明顯的「按住說話」鍵

按鈕組是 **Enter + Backspace + 上下左右** —— 這是**方向遙控器**的配置
（OK / 返回 / 導覽），不是計畫 §1 假設的「一顆 PTT 鍵」。
核心互動「按住按鍵 → 說 → 放開」**在實際硬體上沒有對應的單一按鈕**。

→ 必須由使用者確認：哪一顆按鈕要當語音鍵？還是有第 7 顆沒按到？

#### 問題 2：這些鍵會「漏」進前景視窗（架構級）

Raw Input 是**平行**的觀察通道；同一批事件**照樣會送到前景視窗**。
所以游標停在輸入框時按 PTT：

| 按鈕 | 對前景視窗的副作用 |
|---|---|
| Enter | 送出表單／插入換行 |
| Backspace | **刪掉一個字** |
| 上下左右 | 移動游標／叫出歷史紀錄 |

→ 直接違反產品核心行為：**錄音的同時也把原始按鍵打進了目標視窗。**

#### 因此熱鍵層必須「吃掉」按鍵，而這排除了幾個方案

| 方案 | 能否抑制按鍵 | 能否辨識裝置 |
|---|---|---|
| Raw Input | ❌ 只能觀察 | ✅ 有 `hDevice` |
| `global-hotkey`（Rust） | ❌ 註冊式，不抑制 | ❌ |
| `WH_KEYBOARD_LL` 回傳非 0 | ✅ 可抑制 | ❌ **hook 不告訴你是哪個裝置** |
| `pynput` `suppress=True`（Python） | ✅ | ❌ |

**真正的難點：`KBDLLHOOKSTRUCT` 沒有裝置欄位。**
要「只抑制裝置的按鍵、不影響使用者真正的鍵盤」，必須把
**hook 事件與 Raw Input 事件做時間關聯**（Raw Input 有 `hDevice`）才能決定要不要抑制。
這是三棧都得自己實作的元件，**沒有現成 crate / 套件可直接用**。

→ 這改變 `plan.md` §6 評分表中「全域熱鍵」25% 的評分方式：
   不是「有沒有現成函式庫」，而是「能不能寫出『可抑制 + 可辨識裝置』的 hook」。

### 3.5 已釐清：幽靈 Right-Ctrl 來自原廠工具（不是裝置）

兩次擷取都出現「只有 Right Ctrl 放開、沒有按下」的異常。第二次乾淨擷取中：

```
seq=5  rawkb  BT-HID/Col01  VK_CONTROL  make=29  flags=0x03  WM_KEYUP
```

`flags=0x03` = `RI_KEY_BREAK | RI_KEY_E0` → 確認是 **Right Ctrl**。
另外在擷取開頭，hook 見到 `flags=16`（`LLKHF_INJECTED`）的事件。

**成因已由使用者確認：裝置綁定了原廠工具，其中一個綁定是 Control 鍵。**
那些 Ctrl 事件是**原廠工具注入的**，不是裝置韌體輸出。

→ 這同時證明：**有一個第三方程式正在輸入路徑上，並會注入按鍵事件。**
   詳見 §8。


---

## 4. 四大假設的驗證狀態

| # | 假設 | 狀態 | 證據 / 待辦 |
|---|---|---|---|
| 1 | 麥克風以標準藍牙音訊輸入裝置出現在系統 | ✅ **已確認** | 捕獲端點 `AI_VOICE_MAX Hands-Free`，ACTIVE；且是 waveIn 裝置 `[1]` |
| 2 | 按鍵是 HID 鍵盤，按下送鍵碼、放開停止 | 🟡 **部分確認** | Col01 確實送出乾淨的標準鍵盤事件（見 §3.2）；**但「按鈕」本身的鍵碼尚未定案**（第一次擷取有異常）→ 重測 T3 |
| 3 | 裝置本身**不**內建辨識直接打字 | ⏳ **待驗證** | 記事本按鍵，觀察是否自行冒字 → 跑 T4 |
| 4 | 麥克風支援 16 kHz 或可被系統重採樣 | 🟡 **初步正面** | 見下方 §4.1；尚未用語音確認 |

### 4.1 假設 4 的初步量測（重要）

`tools/p0/record_wav.py --device 1 --seconds 3 --rate 16000` →
錄到 16 kHz / mono / 16-bit，**頻譜能量一路延伸到 Nyquist（8000 Hz）**。

**推論：** 來源音訊**沒有**被 8 kHz 窄頻限制 ——
若底層是 CVSD 8 kHz，資料裡 4 kHz 以上**不可能**有能量。
所以本裝置應為 HFP 寬頻（mSBC 16 kHz），而非窄頻。

⚠️ **但這個結論還不能定案**，原因：

- 該次錄音只有環境底噪（峰值 260–639 / 32767），**沒有說話**；
  底噪本身是寬頻的，也會填滿整個頻帶。
- 必須「有說話 + 拉長音發『ㄙ』」再測一次，才可採信。

**待辦：** 跑 `record_wav.py --device 1 --seconds 6`，說話並發長音「ㄙ」，
確認頻寬仍達 Nyquist。

### ⚠️ 假設 4 為何是高風險項（即使初步結果正面）

裝置走的是 **HFP（Hands-Free Profile）**，HFP 的頻寬受限：

- 窄頻（CVSD）：**8 kHz** ← 對 ASR 明顯不利
- 寬頻（mSBC）：**16 kHz** ← 可接受

**若只拿到 8 kHz**，中文 ASR 準確率會掉，且「中文句準確率 ≥ 95%」的門檻可能不成立。
對策（依序）：確認 Windows 是否已啟用 mSBC 寬頻 → 改用 A2DP 收音（多數裝置不支援上行）
→ 放寬門檻或改用手機近場麥克風。**這必須在 P0/P1 就定案，不能拖到 P5。**


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
   registry 列舉（`HKLM:\SYSTEM\CurrentControlSet\Enum`、`MMDevices`）則正常。
   → P0 工具以 registry 為主要來源，不依賴 CIM/PnP。
2. waveIn（MME）**看得到**目標麥克風（裝置索引 `[1]`），且列出的 4 個裝置
   `dwFormats` **全都是 `0x000FFFFF`**（即宣稱支援所有標準格式）。
   → **`dwFormats` 對判斷原生取樣率毫無用處**，不能用它推論頻寬。
3. 同理，`record_wav.py --probe` 在 8 kHz～48 kHz **全部都會被「接受」**，
   因為 Windows 藍牙音訊堆疊會重採樣。
   → **唯一可信的頻寬判定方法是實際錄音後的頻譜分析**（工具已內建）。

---

## 6. P0 待辦清單

- [x] T1 列舉音訊輸入裝置 → 已看到目標麥克風（registry + waveIn 雙重確認）
- [x] 列出 Raw Input 的 HID collection → Col01 鍵盤 / Col02 Consumer Control
- [x] T3 第一次擷取 → 確認 Col01 是可用鍵盤
- [x] T3 第二次擷取（乾淨協定）→ 6 顆按鈕各 3 按 3 放，全部是標準鍵盤鍵
- [x] 查出幽靈 Ctrl 的來源 → 原廠工具（閃電說）注入，見 §8
- [ ] **確認原廠工具是否就是「裝置會自己打字」的原因**（T4 的真正形式）→ 見 §8
- [ ] **關閉原廠工具後重測 T3**，取得裝置的**原生**鍵碼
- [ ] 確認哪一顆按鈕要當語音鍵（使用者決定）
- [ ] 確認兩個名稱相近的藍牙節點中，哪一台是現役目標
- [ ] T2 用**語音 + 長音「ㄙ」**重錄，確認頻寬達 Nyquist（`record_wav.py`）

---

## 7. 量測紀錄

| 日期 | 項目 | 結果 |
|---|---|---|
| P0 起始 | Profile / 端點列舉 | HFP + HID + SPP + vendor；捕獲端點 ACTIVE |
| P0 起始 | Raw Input collection 列舉 | Col01 = 鍵盤(154 鍵)；Col02 = Consumer Control(page 0x0C)；Col03 未註冊成 raw input |
| P0 起始 | 取樣率（環境底噪，3 秒） | 16 kHz mono 錄音成功；頻寬達 Nyquist 8000 Hz → 初步排除 8 kHz 窄頻，**待語音複驗** |
| P0 起始 | T3 第一次擷取（51 筆） | Col01 送出標準鍵盤事件；hook 見 18 次 VK_RCONTROL 注入 |
| P0 起始 | T3 第二次擷取（90 筆，乾淨協定） | 6 顆按鈕各 3 按 3 放：RETURN / BACK / UP / DOWN / LEFT / RIGHT，全在 Col01，無 rawhid |
| P0 起始 | 查出幽靈 Ctrl 來源 | 原廠工具「閃電說」正在執行並注入按鍵事件（`LLKHF_INJECTED`） |
| — | 關閉原廠工具後重測 T3 | ⏳ |
| — | T2 語音複驗 | ⏳ |
| — | T4 原廠工具是否自行打字 | ⏳ |

---

## 8. ⚠️ 原廠軟體：閃電說（shandianshuo）— 架構分支點

**這是 P0 最重要的發現，會改變整個產品的定位。**

### 8.1 事實

| 項目 | 值 |
|---|---|
| 程式名稱 | `shandianshuo.exe`（「閃電說」） |
| 開發者 | 探未（武漢）科技有限公司 |
| 安裝位置 | `%LOCALAPPDATA%\Shandianshuo\`（單一 78 MB 執行檔 + `uninstall.exe`） |
| 自動啟動 | ✅ 已登錄於 `HKCU\...\CurrentVersion\Run`，帶 `--autostart` |
| 現況 | **正在執行**（擷取時 PID 33260） |
| 與裝置的關係 | 裝置綁定此程式；**其中一個綁定是 Control 鍵**（使用者確認） |
| 證據 | 按鍵紀錄出現 `LLKHF_INJECTED`（`flags=16`）與 `make_code=0` 的合成事件 |

> 註：使用者的 `docs/plan.md` 設定範例中，熱詞表本來就寫了 `"閃電說"` ——
> 代表這個工具**本來就在預期脈絡裡**，只是沒被列為架構風險。

### 8.2 這代表什麼

1. **裝置已經有一套可用的商業語音輸入軟體。**
   VibeTalkie 不是「讓硬體能動」，而是**取代／重寫原廠工具**。
2. **計畫 §2 標記的架構分支點已經觸發。**
   原文：「⚠️ 若『裝置自己會打字』成立，架構需改為透傳/監聽模式」。
   現在確認：不是「裝置」會打字，是**原廠工具**會打字。
3. **T3 的鍵碼量測受到污染。**
   閃電說有自己的綁定（含 Control）。我們量到的 6 顆鍵
   可能部分是閃電說重綁後的結果 → **必須關掉它重測**。
4. **兩個程式會搶同一顆按鈕與同一個麥克風。**
   VibeTalkie 若不偵測並排除閃電說，會出現雙重插入、按鍵互搶、錄音裝置佔用。

### 8.3 因此必須先回答的產品問題（**由使用者決定，不可由 AI 代答**）

| 問題 | 為什麼關鍵 |
|---|---|
| VibeTalkie 是「**取代**閃電說」還是「**與它並存**」？ | 決定要不要做「偵測到原廠工具就要求關閉」的引導流程 |
| 主要動機是不是**全本地 + 隱私 + 零上傳**？ | 若是，這就是產品的核心價值主張，必須寫進 ADR 與 README |
| 願不願意接受「**使用前必須先關掉／移除閃電說**」？ | 這是最大的 UX 摩擦點，直接影響驗收標準 |
| 裝置的按鈕是**原生 HID** 還是**靠閃電說透過 SPP 實作**？ | 若是後者，整個「全域熱鍵」路線要重寫（見 §8.4） |

### 8.4 下一個決定性實驗

**把閃電說完全關閉（含自動啟動），然後重跑 T3。**

| 結果 | 意義 | 對計畫的影響 |
|---|---|---|
| 6 顆按鈕**照樣**送出 RETURN/BACK/方向鍵 | 按鈕是**裝置原生 HID** | ✅ 原架構可行，只要處理「抑制按鍵」 |
| 按鈕**完全沒反應** | 按鈕事件是**閃電說經 SPP 實作**的 | ❌ 「全域熱鍵」路線不成立，必須自行實作裝置協議 |
| 送出**不同的**鍵碼 | 閃電說有重綁定 | 以原生鍵碼為準，並把原廠綁定視為衝突來源 |

第三種情況最可能，也最需要知道。

**關閉方式：**

```powershell
# 停止目前執行中的實例
Stop-Process -Name shandianshuo -Force

# 暫時移除自動啟動（要復原就把值加回去）
Remove-ItemProperty -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run' -Name '闪电说'
```

之後再跑 `python tools/p0/keycode_logger.py --seconds 30`，
每顆按鈕各按 3 次，比較鍵碼是否與 §3.3 相同。

### 8.5 對計畫文件的連帶修改

- `plan.md` §2 的架構分支：**已觸發**，不再是「低機率風險」。
- `plan.md` §11 風險表：「裝置本身會直接打字」→ 應改為
  「**原廠工具會直接打字，且會注入按鍵、搶佔按鈕**」，機率由「低」改為「**已發生**」。
- `architecture.md` 需要新增「**原廠工具偵測與排除**」的模組職責。
- `adr/0001-stack.md` 的「全域熱鍵」評分基準要加上「可抑制 + 可辨識裝置 + 能與第三方 hook 共存」。

---

### 8.6 原廠工具技術剖析（本機實測，未讀取任何個人內容）

| 項目 | 實測結果 |
|---|---|
| 版本 | `0.7.8`，開發者 探未（武汉）科技有限公司 |
| 應用框架 | **Tauri** —— exe 內含 `tauri` / `tao` / `wry` / `WebView2` 字串；資料在 `%LOCALAPPDATA%\cn.shandianshuo.desktop\EBWebView` |
| 內建 ASR 模型 | **`sensevoice-small`**（SenseVoiceSmall，`model.onnx` 約 241 MB） |
| 推論引擎 | exe 內含 `onnxruntime`、`SenseVoice`、`whisper` 字串；**未見 `sherpa`** → 很可能是直接用 onnxruntime |
| 設定檔位置 | `%APPDATA%\Shandianshuo\config.json`（**已存在的功能清單見下**） |
| 本機保留資料 | `recordings/`（**5 個檔案、約 3.3 MB → 有保留錄音**）、`transcriptions.json`（逐字稿）、`memory/knowledge/`（個人知識庫）、`skills/`（空） |

> 🔒 上述僅為**結構與計數**。逐字稿、知識庫、錄音內容、`config.json` 的值
> **一律沒有讀取**，也沒有進入本 repo。

#### 從 `config.json` 的「鍵名」看出的既有功能

原廠工具**已經實作**了本計畫打算做的事，而且更多：

| 鍵名 | 對應到本計畫的哪個模組 |
|---|---|
| `injection_mode`、`use_direct_input` | `textin`（文字注入） |
| `hotkey`、`recording_hotkey`、`unified_hotkey{keys, **block_keys**}` | `hotkey` |
| `mute_while_recording`、`silent_start` | `audio` |
| `save_transcription_to_clipboard` | `textin`（剪貼簿路徑） |
| `text_normalization_*`、`remove_trailing_period` | ASR 後處理 |
| `windows_recording_key` / `macos_recording_key` | 跨平台按鍵 |
| `asr{provider, local_enabled, local_parakeet_enabled, aliyun, volcengine, stepfun, mimo, soniox, elevenlabs, openai_realtime, custom_providers}` | 多引擎（本地 + 大量雲端） |
| `ai{correction, chat, memory_learning, input_assistant, personal_preference, codex_network}` | **LLM 糾錯／聊天／記憶**（本計畫明確不做） |

**兩個關鍵發現：**

1. **`block_keys` 的存在，反證了 §3.4 的結論。**
   一個認真的商業競品也要處理「按鍵漏進前景視窗」，而它的解法就是「設定要阻擋的鍵」。
   → 我們的分析方向正確，而且這是**必要功能**，不是加分項。

2. **競品同時支援「本地」與「一大串雲端」ASR。**
   本計畫的 v1 是全本地、零強制上傳 → **差異化不在「能不能本地」，而在「只有本地」**。

### 8.7 定位結論（提供給使用者決策，不是代替決策）

| 面向 | 閃電說 0.7.8 | VibeTalkie（計畫 v1） | 有差異嗎 |
|---|---|---|---|
| ASR | 本地 SenseVoice **＋ 多雲端** | 本地 sherpa-onnx | ✅ 差異在「**只有本地**」 |
| 文字注入 | 有 | 有 | ❌ 無差異 |
| 熱鍵阻擋按鍵 | 有（`block_keys`） | 必須做 | ❌ 無差異（但證明必要性） |
| LLM 整理／聊天／記憶／技能 | **有** | **明確不做** | ✅ 差異：更小、更可預測 |
| 錄音檔 | **有保留**（3.3 MB） | 辨識後刪除 | ✅ **差異：隱私** |
| 開源可稽核 | ❌ 閉源 | 可 | ✅ 差異：可稽核 |
| 應用框架 | **Tauri** | 計畫選 Tauri | — |

→ **VibeTalkie 的正當理由是：只做 ASR、完全本地、不留錄音、可稽核、不做 LLM 與記憶。**

若這個理由成立，它就是一個**清楚的利基產品**，而不是重造一個功能更少的閃電說。
**這件事必須由使用者確認**（見 §8.3），因為它決定整個專案是否值得做。

> 📌 **對 P2/ADR 的附加價值：** 一個在完全相同利基上出貨的商業產品用 **Tauri** 打造。
> 這是「Rust + Tauri 在本領域可行」的**實證**，比任何 benchmark 都有說服力。



