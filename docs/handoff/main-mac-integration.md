# 整合交接：main 的新行為 → mac 分支的新架構

> **這份文件的用途**：`main` 與 `origin/mac` 各自演化出「新的行為」與「新的架構」，
> 兩邊**不能直接 merge**。這份整理出「誰缺什麼、接縫在哪、動哪些檔案、怎麼驗」，
> 讓接手的 agent 不必重新摸索。
>
> ⚠️ 這是**暫時的交接文件**，整合完成後應刪除（或把它轉成 `docs/` 的正式文件）。

整理時間：2026-09-29　整理者：DSH（Windows 端，**無法執行 macOS 路徑**）

---

## 1. 兩個版本的事實

| | main | origin/mac |
|---|---|---|
| HEAD | `fc9add6` | `a42cd9f` |
| 分岔點 | — | `06d01ab`（＝ main 的**前一個** commit） |
| 內容 | 錄音鍵多組＋雙邊行為＋暖機待命＋UI 修正 | macOS 完整移植（新架構） |

```
06d01ab  ├── fc9add6（main）   ← 行為最新
         └── a42cd9f（mac）    ← 架構最新，但行為停在 06d01ab
```

**兩邊都要保**：main 的**行為** ＋ mac 的**架構**。

### 使用者已確認

- `origin/mac` 的 `a42cd9f` **就是** Mac 上最新的（沒有未推送的變更）
- 整合方式**尚未決定**（①抽共用層／②逕式搬進 mac／③先合併再重構）
  —— 接手前請先跟使用者確認。**建議 ①**（否則觸發邏輯會有兩份，一定漂移）

---

## 2. mac 分支的架構（整合的目標形狀）

`a42cd9f` 相對分岔點新增／修改 21 個檔案。**關鍵是把平台差異抽出去了**：

| 檔案 | 角色 |
|---|---|
| `app/core/keys.py` | **canonical 按鍵名稱 ↔ 各平台鍵碼**。Windows 借 `hotkey._NAME_TO_VK`，macOS 自己維護 Carbon keycode 表；`Cmd`→`Win`、`Option`→`Alt` 別名 |
| `app/core/mac_keylistener.py` | CGEventTap 全域按鍵（`KeyEvent`、`MacKeyListener`、TCC 提示） |
| `app/core/mac_recorder.py` | AVAudioEngine 錄音（`MacCapture`、`list_input_devices`；裝置列舉走 `system_profiler`） |
| `app/core/mac_textin.py` | Cmd+V 注入＋剪貼簿快照還原 |
| `app/core/ui_server.py` | **HTTP 設定頁伺服器**（自己的 `Status`，`start_server`／`pick_port`／`open_browser`） |
| `app/core/autosetup.py` | 檢查／自動補齊第三方套件與模型 |
| `app/mac_vibetalkie.py` | macOS 進入點：`MacPttDaemon` ＋ `_main_key_from_spec` 等 |
| `docs/spike/translation.md` | 移植筆記 |
| `tests/test_mac_*.py`（6 支）＋ `test_autosetup.py`／`test_launch_python.py`／`test_wheel_platform.py` | mac 端測試 |
| `launch.py`、`啟動 VibeTalkie.command`、`tools/p1/fetch_wheels.py`、`AGENTS.md` | 跨平台啟動與說明 |

### AGENTS.md 在 mac 分支新增的 §8.7（**整合時必須保留**）

- 模組對應表
- 🔴 進入點必須自己擋 Python 版本（不能只靠 `launch.py`）
- ⚠️ **§8.2 規則 2 在 macOS 上做不到**（已實測）—— 也就是「分辨是不是目標裝置送的按鍵」
- ⚠️ 麥克風「優先順序」在 macOS 上無效（必須對使用者講出來）
- 📌 裝置列舉用 `system_profiler`，不要用 pyobjc 的 CoreAudio
- 權限（TCC）
- 🔴 **三個會 segfault 的地雷**（都實際踩過，不要「簡化」掉）
- 📌 實測：這支麥克風的正常訊號只有 0.3% FS

---

## 3. main 的「新行為」清單（mac 完全沒有）

`fc9add6` 動了 12 個檔案（+3788 / −333）。mac 分支**沒有跟到任何一項**：

| # | 行為 | 位置（main） | mac 現況 |
|---|---|---|---|
| 1 | 錄音鍵是**多組清單**，寫法 `開始[,結束][@開始行為][@結束行為]` | `hotkey.py` `bindings()`／`parse_binding()` | 只有 `cfg.hotkey` 單一字串 |
| 2 | **開始／結束可不同顆鍵**（`F9,Esc`） | `HotkeyBindings.ends` | 無 |
| 3 | **兩邊各有自己的行為**：開始＝按一下／連按兩下；結束＝鬆開／再按一下／連按兩下 | `START_MODES`／`END_MODES`（UI）＋ `hotkey.MODE_*` | 只有全域 `cfg.trigger_mode` |
| 4 | **單獨啟用／停用**（`~` 前綴） | `hotkey.DISABLED_PREFIX`、`HotkeyBindings.active` | 無 |
| 5 | **測試模式**：只聽不錄，回報收到什麼 | `ptt.start_key_test()`／`test_state()` | 無 |
| 6 | **串流暖機待命＋前捲**（根治「第一段沒錄到」） | `ptt._tick_capture()`、`_PREROLL_S` | 不適用（winmm 專有），但 **mac 的「第一次按下」是否也有同類問題要量** |
| 7 | 換裝置清前捲、`_WARM_GRACE_S` 內自動重試 | `ptt._ensure_capture()`／`_finish_recording()` | 無 |
| 8 | 設定頁後端新欄位 | `vibetalkie.py` `snapshot()` | `ui_server.py` 的 `Status` 只認 `patch["hotkey"]` |
| 9 | UI：麥克風挑選器不再假裝已加入；所有欄位改動都提示未儲存 | `app/ui/app.js` | **UI 兩平台共用同一份 `app/ui/`** → mac 可能已有部分新 UI，但後端欄位沒跟上 |
| 10 | 裝置名稱不再寫死在提示裡 | 各處 | mac 分支的 §8.7 有提到 |

> ⚠️ 第 9 點要注意：`app/ui/*` 是**兩個平台共用**的。
> mac 分支有沒有動 UI 檔案？—— 沒有（`git diff --name-only 06d01ab origin/mac` 不含 `app/ui/`）。
> 所以 mac 上的設定頁是**舊 UI ＋ 舊後端**，而 main 是**新 UI ＋ 新後端**。
> 整合後 mac 一定要能滿足新 UI 的欄位需求，否則會出現「畫面有欄位、後端不認」的靜默失敗。

---

## 4. 接縫：共用層現在被拉開了

| 元件 | main | mac | 問題 |
|---|---|---|---|
| 按鍵表示 | `HotkeySpec.vk`（Windows VK 碼） | `keys.py` 的 canonical 名稱 | `hotkey.py` 仍以 VK 為中心；mac 靠 `_main_key_from_spec()` 事後翻譯 |
| 觸發狀態機 | `ptt.PttDaemon`（Windows 專用） | `MacPttDaemon`（**自己一份**：`_last_tap`／`_spec_matches`） | 🔴 **兩份實作**，新增的行為（配對／雙邊行為／停用）要在兩邊各做一次 |
| 設定頁後端 | `vibetalkie.py` 的 `Status` | `ui_server.py` 的 `Status` | 兩份 `snapshot()`／驗證邏輯 |
| 裝置／錄音 | `recorder.py`（winmm） | `mac_recorder.py`（AVAudioEngine） | 平台差異，**本來就該分開**（這部分沒問題） |

### 具體的「翻譯層」痕跡（現有架構的痛點證據）

```python
# ui_server.py（mac）
def _main_key_from_spec(spec, hotkey_mod) -> str:
    """`HotkeySpec` → 本平台的主鍵名稱（給 keys.name_to_code 驗證用）。"""
    name = (hotkey_mod.VK_TO_NAME.get(spec.vk) or "").lower()
    if spec.require_e0 and name in ("ctrl", "shift", "alt"):
        name = "right" + name
    return name
```

這正是「`hotkey.py` 講 VK、平台層要自己翻譯」的症狀。
**`keys.py` 就是為了消掉這一段而存在的。**

---

## 5. 建議的整合方式（選項 ①）

**原則：把共用層抽出來，兩個平台用同一顆引擎。不要把新程式碼複製進 mac。**

### 步驟 1 — `hotkey.py` 改講 canonical 名稱
- `HotkeySpec` 用名稱（`"rightctrl"`／`"f9"`／`"r"`）＋ `side`，**VK 只留在 Windows 平台層**
- 重用 `keys.py` 的 `name_to_code()`／`code_to_name()`；Windows 的 VK 表仍由 `hotkey` 擁有，`keys.py` 繼續借用（現在就是這樣，別做出第三份）
- 目標：`_main_key_from_spec()` 這種翻譯函式**可以整段刪掉**

### 步驟 2 — 抽出 `app/core/trigger.py`（平台無關的觸發狀態機）
把 main `ptt.py` 裡**與 OS 無關**的部分搬進去：

- 多組綁定比對（`HotkeyBindings` 已在 `hotkey.py`，這裡是「事件 → 命中哪一組」）
- 開始／結束鍵、兩邊各自的行為（hold／toggle／double）
- 明確結束鍵優先於行為；「鬆開才停」需要 keyup 才收尾
- `~` 停用（停用的完全不理，含它的結束鍵）
- double 配對視窗（**每一組各記一份**，不能用單一變數）
- 吃掉按住不放的自動重複
- 測試模式（只回報、不觸發）
- 修飾鍵狀態機

**平台只提供**：`{device_id, key_name, side, down, mods}` 這種事件。
Windows 由 `ptt._handle_key` 轉、mac 由 `MacKeyListener` 轉。

### 步驟 3 — 兩個 daemon 都用它
- `ptt.PttDaemon`（Windows）：行為不變，只是把狀態機換成呼叫 `trigger.py`
- `MacPttDaemon`：**刪掉自己那一份**，改用同一顆

### 步驟 4 — 兩份 `Status` 對齊
- 用 `tests/test_status_contract.py` 的同一套做法，讓**兩個平台回傳同一個形狀**
- mac 的 `Status` 補上：`hotkeys`／`key_enabled`／`key_modes`／`hotkeys_label`／`hotkey_warning`／`key_test`
- mac 的 `do_POST` 要接受 `hotkeys`（清單）並沿用 main 的驗證（400＋具體原因）
- 保留 mac 獨有的 `platform`／`models`／`mic_warning`（mac 的麥克風說明）

### 步驟 5 — 測試
- `test_trigger.py` 的 [7]–[19] 是**純邏輯**，抽到 `trigger.py` 後應該**原樣通過**（這是重構成功的判準）
- mac 新增／沿用：`test_mac_trigger.py` 必須覆蓋新的行為（配對、雙邊行為、停用、測試模式）

---

## 6. 不要動的東西（mac 的實測結晶）

- 🔴 AGENTS.md §8.7 的三個 segfault 地雷
- ⚠️ §8.2 規則 2 在 macOS 上**做不到**（裝置辨識）—— 不要在整合時「順手補上」
- 📌 用 `system_profiler` 列舉裝置，不要改成 pyobjc CoreAudio
- 📌 麥克風優先順序在 macOS 無效 —— 要對使用者明講
- 🔴 進入點自己擋 Python 版本
- TCC 權限提示文案

---

## 7. 風險

| 風險 | 說明 | 對策 |
|---|---|---|
| **沒有 Mac 可測** | 執行端在這台 Windows 上跑不到 CGEventTap／AVAudioEngine | 共用層在 Windows 測到底（純邏輯），mac 原生層只保證介面一致＋import 得動；實際行為請使用者在 Mac 驗 |
| 兩份狀態機漂移 | 若採選項 ②，之後每次改行為都要改兩次 | 選項 ①（抽共用層） |
| UI 兩平台共用 | mac 的設定頁是舊 UI；整合後新 UI 會要求新欄位 | 步驟 4 的契約測試 |
| 「第一次按下」問題在 mac 未知 | main 的修正（暖機待命＋前捲）是 winmm 的解法 | 在 mac 上量：`tools/p1/measure_standby_audio.py` 的邏輯可以照抄成 CoreAudio 版 |

---

## 8. 驗收條件

1. `python tests/test_trigger.py`（抽出的狀態機）**全過**，且 [7]–[19] 的案例**沒有被刪減**
2. `python tests/test_status_contract.py` 對**兩個平台**都成立（欄位契約）
3. `app/ui/*` 的設定頁在 mac 後端上**每個欄位都能存能讀**（尤其 `hotkeys` 清單）
4. Windows 端行為**與 `fc9add6` 相同**（不能因為重構而退化）
5. mac 端：`origin/mac` 原本的 9 支 mac 測試**仍然全過**
6. AGENTS.md 合併兩邊：main 的 §8.2 規則 4（多組／配對／雙邊行為／停用／測試）
   ＋ mac 的 §8.7（macOS 實作）**兩節都在**

---

## 9. 需要跟使用者確認的事

1. 整合方式要 ①／②／③ 哪一種（**建議 ①**）
2. 整合後的成果要推去**哪個分支**（`main`？`mac`？還是新的 `integrate`？）
3. **推送前必須另外取得同意**（`AGENTS.md` 的鐵則：一次同意只對那一次有效）

---

## 10. 指令備忘

```sh
# 看兩邊的分岔與差異
git merge-base origin/main origin/mac          # → 06d01ab
git diff --stat 06d01ab origin/main            # main 新增的行為
git diff --stat 06d01ab origin/mac             # mac 新增的架構

# 讀 mac 分支的檔案（不必切分支）
git show origin/mac:app/core/ui_server.py

# 直接比對同一份檔案在兩邊的差異（會很大，建議用 GUI）
git diff origin/mac:app/core/hotkey.py origin/main:app/core/hotkey.py
```

⚠️ 這台機器上 `git fetch`／`git push` 需要**放寬沙箱權限**才會成功
（憑證存放區被擋住，錯誤訊息是 `schannel: AcquireCredentialsHandle failed:
SEC_E_NO_CREDENTIALS` —— 那是環境問題，不是沒憑證）。
