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

## 3. 從 HID 節點讀到的事實（T3 尚未通過）

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

### 3.1 Raw Input 實測（T3 前置，**已量測**）

用 `tools/p0/keycode_logger.py --list-devices --filter 00001124` 實測，
本裝置向 Raw Input 註冊了 **2 個** top-level collection：

| Collection | Raw Input 型別 | HID Usage | 判定 |
|---|---|---|---|
| `…&Col01` | `type=1` | page `0x01` / usage `0x06`，`keys=154` | ✅ **標準鍵盤**（154 鍵） |
| `…&Col02` | `type=2` | page `0x0C`（12）/ usage `0x01`，`vid=0xFFFF pid=0x0000` | ✅ **Consumer Control（多媒體鍵）** |
| `…&Col03` | — | — | ⚠️ **未出現在 Raw Input 清單中**（存在於 HID 列舉樹，但未註冊為 raw input 來源） |

**這件事的意義：** 裝置同時暴露「標準鍵盤」與「Consumer Control」兩種介面，
所以按鈕落在哪一邊**決定了熱鍵層能不能用現成的全域熱鍵函式庫**。
兩種情形都可能，**必須靠 T3 實際按鍵才能定案**。
（Col03 未註冊成 raw input，代表它很可能不是輸入用 collection，或 only-feature/report 通道；
v1 不要依賴它。）


---

## 4. 四大假設的驗證狀態

| # | 假設 | 狀態 | 證據 / 待辦 |
|---|---|---|---|
| 1 | 麥克風以標準藍牙音訊輸入裝置出現在系統 | ✅ **已確認** | 捕獲端點 `AI_VOICE_MAX Hands-Free`，ACTIVE；且是 waveIn 裝置 `[1]` |
| 2 | 按鍵是 HID 鍵盤，按下送鍵碼、放開停止 | 🟡 **部分確認** | HID profile 已綁定；Raw Input 看到 **Col01 鍵盤(154鍵) + Col02 Consumer Control**；**但鍵碼本身未量測** → 跑 T3 |
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
- [ ] T2 用**語音 + 長音「ㄙ」**重錄，確認頻寬達 Nyquist（`record_wav.py`）
- [ ] T3 記錄按鈕的真實鍵碼：usage page / usage id / VK / scan code（`keycode_logger.py`）
- [ ] T4 記事本按鍵，確認裝置不會自行打字
- [ ] 確認兩個名稱相近的藍牙節點中，哪一台是現役目標
- [ ] 把以上結果回填本檔

---

## 7. 量測紀錄

| 日期 | 項目 | 結果 |
|---|---|---|
| P0 起始 | Profile / 端點列舉 | HFP + HID + SPP + vendor；捕獲端點 ACTIVE |
| P0 起始 | Raw Input collection 列舉 | Col01 = 鍵盤(154 鍵)；Col02 = Consumer Control(page 0x0C)；Col03 未註冊成 raw input |
| P0 起始 | 取樣率（環境底噪，3 秒） | 16 kHz mono 錄音成功；頻寬達 Nyquist 8000 Hz → 初步排除 8 kHz 窄頻，**待語音複驗** |
| — | T2 語音複驗 | ⏳ |
| — | T3 鍵碼 | ⏳ |
| — | T4 自行打字 | ⏳ |

