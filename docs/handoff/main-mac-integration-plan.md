# main × mac 整合實作記錄（選項 ①：抽共用層）

> **狀態：✅ 已完成（2026-09-29，分支 `integrate`，未推送）。**
>
> 這份文件原本是「動工前的計畫」，現在是**已完成工作的記錄**：
> 實際做了什麼、與計畫哪裡不同、踩到哪些計畫沒預料到的坑。
> 對應的「現況分析」在 `main-mac-integration.md`。

---

## 1. 一句話總結

`main`（新行為）與 `origin/mac`（新架構）整合完成：**兩個平台現在共用
同一顆觸發引擎**（`app/core/trigger.py`），mac 從「單一顆鍵 + 全域模式」
變成與 Windows 完全相同的能力（多組／配對／雙邊行為／停用／測試模式）。

---

## 2. 實際做了什麼

| # | Commit | 內容 |
|---|---|---|
| 1 | `merge: 併入 origin/mac 的 macOS 移植架構` | 只有 `AGENTS.md` 衝突（§8.2 規則 4 ＋ §8.7 兩節並存），其餘 20 檔自動合併 |
| 2 | `fix(test): test_mac_keys 在 Windows 分支的 (code, name) 次序顛倒` | mac 分支上就有的 bug（在 mac 上跑不到那一支） |
| 3 | `test: 釘住 mac 模組的可載入性＋共用層不得有平台原生相依` | `tests/test_mac_import.py`（AST 掃 import、`tokenize` 掃程式碼區） |
| 4 | `refactor(hotkey): 內部表示改為 canonical 名稱` | `HotkeySpec` 收 `name`/`side`，`spec.vk` 改推導 property |
| 5 | `feat(trigger): 抽出平台無關的觸發狀態機` | `app/core/trigger.py` ＋ `tests/test_trigger_engine.py`（14 段） |
| 6 | `refactor(ptt): 觸發邏輯改用共用的 trigger.py` | `ptt.py` 淨減 107 行，`test_trigger.py` 只動 2 行 |
| 7 | `refactor(mac): MacPttDaemon 改用共用的 trigger.py` | 刪掉 mac 自己那份狀態機；`test_mac_trigger.py` 變成**不需 pyobjc** |
| 8 | `fix(hotkey): 同一顆開始鍵的多組設定被靜默吃掉／狀態讀錯` | 三個真實 bug（見 §4）＋ `ui_server` 契約對齊 |

---

## 3. 與計畫不同的地方（計畫寫錯／寫得不夠的地方）

| 計畫寫的 | 實際上 |
|---|---|
| 「重用 `keys.py` 的 `name_to_code()`」 | **不能直接用** —— 它回的是「執行平台」的鍵碼，在 Windows 上跑 mac 的驗證會給錯答案。驗證改用 `hotkey.parse_binding()`（平台無關） |
| 抽出 `trigger.py` 後「`test_trigger.py` 應該原樣通過」 | 幾乎是 —— 全部 20 段通過，但**動了 2 行**：修飾鍵由 `{"Ctrl"}` 改小寫 `{"ctrl"}`（刻意的契約變更）、以及 `[13]` 的「模擬放開」改成清引擎的 `pressed`（資料流改變，不是行為改變） |
| 「`keys.py` 的 `generic_modifier()` 可以刪掉」 | **不能刪** —— `test_mac_keys.py` 直接測它。改成轉呼叫 `hotkey._key_name_and_side()` 的薄包裝 |
| 「mac 測試只能保證 import 得動」 | 實際上 `test_mac_trigger.py` 與 `test_mac_keys.py` **在 Windows 上全過**（只要拆掉 pyobjc 閘門），覆蓋率高得多 |

---

## 4. 🔴 計畫沒預料到、實際才發現的三個真實 bug

全部只在「**同一顆開始鍵出現在多組**」時才會現形 —— 而 mac 原本沒有多組，
所以這個組合從來沒被測過。

### 4.1 去重把合法組合吃掉了

```
hotkeys = ["F9@double", "F9,Esc@toggle"]
→ 存完只剩 ["F9@double"]          ← 第二筆靜默消失，沒有任何錯誤訊息
```

原因：去重只比對**開始鍵的規格**，沒把結束鍵與兩邊的行為算進去。

### 4.2 同一個 bug 有兩份實作

`config.apply_hotkeys_patch()`（存檔時）與 `hotkey.bindings()`（啟動時）
各有一份去重 —— **兩份都錯**。只修一份的症狀是
「UI 存得進去，重啟後少一組」。

### 4.3 `HotkeyBindings` 用 `.index()` 查值 → 狀態讀錯

```python
hotkeys = ["~F9", "F9,Esc"]      # 一組停用、一組啟用
→ entries 寫回去變成 ["~F9", "~F9,Esc"]   # 兩組都停用！
```

五條平行 tuple ＋ `self.specs.index(spec)` 查值 —— 同一顆鍵出現兩次時，
`.index()` **永遠回第一筆**，所以第二筆的結束鍵、行為、**啟用狀態**
全部讀成第一筆的值。使用者看到的是「存一次設定就把第二組也關掉」，
而且畫面上每一列看起來都正常。

**修法**：新增 `hotkey.Binding`（一組完整設定），
`HotkeyBindings` 改以它為單一資料來源，查值改用整筆描述當 key。

> 教訓：**「五條平行陣列」＋「用其中一個元素當 key」是結構性缺陷**，
> 不是打補丁能修的。發現時就該換成記錄型別。

---

## 5. 這一輪學到的、值得留在文件裡的

1. **測試要先驗行為、再驗資料。** 4.1 的 bug 是「先寫 `test_hotkeys_patch.py`
   的行為測試」才浮出來的 —— 如果只跑既有測試，它會一直躲在那裡。
2. **同一條規則出現兩次就是漂移的開始。** 4.2 是實例：
   `config.py` 與 `hotkey.py` 各有一份去重，而且**兩份的錯法一樣**。
3. **mac 的模組可以在 Windows 上測。** 前提是「頂層不 import 原生 API」，
   而那個性質需要被釘住（`test_mac_import.py`）—— 否則它會在某次
   「順手加個 import」之後無聲消失。
4. **`~` 停用是純字串前綴，但那不代表它不影響去重。**
   `~F9` 與 `F9,Esc` 的「開始鍵」相同，但它們是**兩個不同的狀態**。

---

## 6. 驗收條件（全部達成）

| # | 條件 | 結果 |
|---|---|---|
| 1 | `test_trigger.py` 全過，`[7]`–`[19]` 沒有被刪減 | ✅ 20 段全過；全檔只動 2 行、共 +13/−3 |
| 2 | `test_status_contract.py` 對**兩個平台**都成立 | ✅ 第 [6][7] 段直接驗 `ui_server.Status` |
| 3 | `app/ui/*` 的每個欄位在 mac 後端都能存能讀 | ✅ `test_mac_ui_contract` ＋ `test_mac_api_contract` |
| 4 | Windows 行為與 `fc9add6` 相同 | ✅ 逐項比對 baseline：**只有兩支從紅轉綠，沒有其他變化** |
| 5 | `origin/mac` 的 mac 測試仍全過 | ✅ 5 支在 Windows 上驗（含原本跑不到的 2 支）；剩 2 支要 Mac |
| 6 | `AGENTS.md` 的 §8.2 規則 4 與 §8.7 兩節都在 | ✅ 兩節並存，§8.7 的實測結晶一字未刪 |

---

## 7. 這台機器上**做不到**、要請使用者在 Mac 上驗的

| 項目 | 指令 |
|---|---|
| 真端到端（合成按鍵→錄音→ASR→注入） | `PYTHONPATH=<pyobjc> python tests/test_mac_e2e.py` |
| 音訊裝置列舉（`system_profiler`） | `python tests/test_mac_devices.py` |
| 真實按住麥克風錄音鍵 → 文字出現 | `python3 app/mac_vibetalkie.py` |
| mac 的「第一次按下有沒有音訊」 | main 的暖機待命是 winmm 專屬解法；mac 要另量（AGENTS.md §7 待驗證） |

⚠️ **`mac_vibetalkie.py` 在 Windows 上「載入得動」不等於「跑得起來」。**
它一碰到 `Quartz` 就會 `ImportError`。所以共用層的驗收只保證
**介面一致與狀態機正確**，原生行為要 Mac 實測。

---

## 8. 已知的既有紅燈（**不是這次整合造成的**）

兩支測試在 `origin/mac` 上就是紅的（已用 worktree 在原始分支上確認）：

| 測試 | 症狀 | 性質 |
|---|---|---|
| `test_wheel_platform.py` | 在 Windows 上選不到 `pyobjc-framework-Quartz` 的 wheel | 測試設計問題（跨平台的套件用「目前平台」的規則去找） |
| `test_launch_python.py` | `PermissionError` 在 `tempfile` 清理時 | 這台機器的沙箱限制（建立子行程／改檔案權限被擋） |

---

## 9. 指令備忘

```powershell
# 這次整合動了什麼
git log --oneline fc9add6..HEAD
git diff --stat fc9add6 HEAD

# 共用層有沒有偷渡平台相依
python tests/test_mac_import.py

# 觸發行為（兩個平台各一支，都不需要 pyobjc）
python tests/test_trigger.py          # Windows（走 PttDaemon）
python tests/test_trigger_engine.py   # 引擎（直接餵 Event）
python tests/test_mac_trigger.py      # mac（走 MacPttDaemon）

# 錄音鍵設定的驗證（存檔與啟動兩層）
python tests/test_hotkeys_patch.py
```

⚠️ 這台機器上 `git fetch`／`git push` 需要**放寬沙箱權限**才會成功
（`schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS` ——
那是環境問題，不是沒憑證）。**推送前必須逐次取得使用者同意。**
