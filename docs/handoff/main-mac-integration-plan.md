# main × mac 整合實作計畫（選項 ①：抽共用層）

> **本檔的用途**：`main-mac-integration.md` 回答「誰缺什麼」；**本檔回答「怎麼做」**。
> 每一項都是可直接執行的步驟（檔案路徑、指令、期望輸出、commit 切點）。
>
> 前置決策（使用者 2026-09-29 已確認）：
> 1. 整合方式＝**① 抽共用層**（兩個平台共用同一顆觸發引擎，不做兩份）
> 2. 先在 `integrate` 分支做，`main` 與 `origin/mac` 都不動
> 3. **推送前逐次另外取得同意**（`AGENTS.md` 鐵則）

整理時間：2026-09-29｜整理者：DSH（Windows 端）

---

## 0. 這份計畫的四個新增事實（先讀，會改動原交接文件的建議）

交接文件是「讀 commit 差異」寫出來的。實際拉下來驗證之後，有四個事實
會**改變計畫的形狀**：

| # | 驗證到的事實 | 對計畫的影響 |
|---|---|---|
| 1 | `integrate` 從 `main` 開出來之後，`git merge origin/mac` **只有 `AGENTS.md` 一個衝突**，其餘 20 個檔案自動合併乾淨 | 不需要「搬檔案」——直接 merge 就好（§2 Phase A） |
| 2 | mac 的 `mac_keylistener.py`／`mac_recorder.py`／`mac_textin.py` **沒有頂層 pyobjc import**（延後到實際使用才載）；實測連 `app/mac_vibetalkie.py` 都**能在 Windows 上 import 成功** | 🔴 **`MacPttDaemon` 的狀態機邏輯可以在這台 Windows 上測**，不必等 Mac。共用層的驗收不只有「import 得動」 |
| 3 | `main` 的 `HotkeySpec.vk` 是**欄位**，但 `hotkey.py` 內部**零處**直接讀 `spec.vk`（`ptt.py` 也沒有） | 改成 canonical 名稱的內部表示是**低風險**的，不必先做「雙軌並存」 |
| 4 | `main` 的 `HotkeyBindings.quads()` 被**定義兩次**（後者覆蓋前者，回傳含停用項） | 現有行為是對的，但要在重構時**只留一個**並把契約寫在 docstring 裡，否則很容易「順手修好」而弄壞 `entries` |

另外兩個必須先講清楚的**設計取捨**（與交接文件 §5 的字面建議不同）：

### 取捨 1：`hotkey.py` 直接改講 canonical 名稱（不做雙軌）

交接文件寫「重用 `keys.py` 的 `name_to_code()`／`code_to_name()`」。
**但 `keys.py` 在 mac 上回傳的是 Carbon keycode，在 Windows 上回傳 VK** ——
同一份 `HotkeySpec` 的 `vk` 欄位會在兩個平台裝兩種完全不同的數字
（F9 = `0x78` / `0x65`），而 `hotkey.spec_hit()` 的比對依賴 VK 家族
（`{0x11,0xA2,0xA3}`），在 mac 上必然全錯。

所以順序必須是：**先把 `hotkey.py` 的內部表示換成 canonical 名稱**，
`keys.py` 只負責「名稱 ⇄ 本平台鍵碼」。這正是 `keys.py` 檔頭寫的設計原則。

### 取捨 2：共用層放 `app/core/trigger.py`，但**事件側別**由平台層解析

`trigger.py` 需要知道「這顆鍵是左邊還是右邊的 Ctrl」。
Windows 的依據是 `E0` 旗標，mac 的依據是**不同的 keycode**。
`trigger.py` 不該同時認得兩者。

做法：`trigger.py` 提供一個**平台無關**的側別解析函式
（吃 canonical 名稱，並接受「旗標說自己在右邊」的提示），
Windows 傳 `e0` 旗標、mac 什麼都不用傳（名稱已經帶了 `right` 前綴）。

---

## 1. 目標形狀

```
                     ┌──────────────────────────────┐
   鍵盤事件 ────────► │  ptt.py（Windows 平台層）      │
   （Raw Input）      │  VK + E0 → Event             │
                     └──────────┬───────────────────┘
                                │  Event{key, side, down, mods, device}
                     ┌──────────▼───────────────────┐
                     │  trigger.py（平台無關）        │
                     │  多組比對／雙邊行為／          │
                     │  結束鍵／停用／測試模式        │
                     └──────────┬───────────────────┘
                                │  callbacks: on_start(label) / on_finish()
                     ┌──────────▼───────────────────┐
                     │  ptt.PttDaemon：錄音→ASR→注入  │
                     └──────────────────────────────┘

                     ┌──────────────────────────────┐
   CGEventTap ──────► │  mac_vibetalkie.py（平台層）   │
                     │  KeyEvent → Event            │
                     └──────────┬───────────────────┘
                                └──► 同一顆 trigger.py
```

**判準**：`trigger.py` 匯入 `hotkey`，**不匯入** `keys`／`ptt`／`record_wav`／
`mac_*`。任何 `import ctypes`／`import Quartz` 都不該出現在裡面。

---

## 2. 分階段任務

每一階段結束都要能跑測試、能 commit。**不要跳階段**。

```
Phase A  merge mac 架構進 integrate（只有 AGENTS.md 要解衝突）
Phase B  hotkey.py 改講 canonical 名稱（行為 100% 不變）
Phase C  抽出 app/core/trigger.py（平台無關狀態機）
Phase D  ptt.PttDaemon 改用 trigger.py（Windows 行為不變）
Phase E  MacPttDaemon 改用 trigger.py（刪掉自己那一份）
Phase F  ui_server.Status / do_POST 對齊（mac 設定頁）
Phase G  測試：mac 測試在 Windows 上可跑的部分
Phase H  文件：AGENTS.md / README / 刪除交接文件
```

---

## Phase A — merge mac 架構進 `integrate`

### Task A1：開分支並 merge

**Files**：無（純 git）

- [ ] **Step 1：確認起點乾淨**

```powershell
git status --short          # 期望：只有 ?? docs/handoff/
git log --oneline -1        # 期望：fc9add6 feat: 錄音鍵可同時綁多組…
```

- [ ] **Step 2：merge**

```powershell
git merge origin/mac
```

**期望輸出**：`CONFLICT (content): Merge conflict in AGENTS.md`，
其餘 20 個檔案 `Auto-merging` 或 `A`（新增）。

**驗證沒有其他衝突**：

```powershell
git diff --name-only --diff-filter=U     # 期望：只有 AGENTS.md
```

- [ ] **Step 3：解掉 `AGENTS.md` 的衝突**

**原則：兩邊都要，不是二選一。**

| 區塊 | 取哪一邊 |
|---|---|
| §3 階段表 | **main**（含 P1 現況）＋ mac 的「翻譯已被評估過」註記 |
| §4 目錄結構 | **main 的版本 ＋ mac 新增的 6 個檔案**（`keys.py`／`mac_keylistener.py`／`mac_recorder.py`／`mac_textin.py`／`autosetup.py`／`ui_server.py`）＋ `mac_vibetalkie.py` |
| §8.2 規則 4（多組／配對／雙邊行為／停用／測試模式） | **main**（mac 完全沒有這一節） |
| §8.2.1（暖機待命＋前捲） | **main** |
| §8.5（EDR） | **main**（含更正紀錄） |
| §8.6（批次檔） | 兩邊相同，取 main |
| **§8.7（macOS 實作）** | **mac 全節**（模組對應／設定頁面／Python 版本／§8.2 規則 2 做不到／麥克風優先順序／system_profiler／TCC／自動補齊／三個 segfault 地雷／0.3% FS） |
| §9 測試清單 | **兩邊合併**：main 的 9 支 ＋ mac 的 4 支非 pyobjc 測試 |

⚠️ **易錯點**：§8.7 的「🔴 進入點必須自己擋 Python 版本」「⚠️ §8.2 規則 2 在
macOS 上做不到」「📌 用 system_profiler 不要用 pyobjc CoreAudio」「🔴 三個
segfault 地雷」是**實測結晶**，一個都不能少（交接文件 §6 明列）。

**檢查沒有殘留衝突標記**：

```powershell
Select-String -Path AGENTS.md -Pattern '^(<<<<<<<|=======|>>>>>>>)'
# 期望：沒有輸出
```

- [ ] **Step 4：驗證**

```powershell
python tests/test_status_contract.py
python tests/test_trigger.py
python tests/test_config_api.py
```

**期望**：三支都 `✅ 全部通過`（merge 不該改變任何行為）。

- [ ] **Step 5：commit**

```powershell
git add -A
git commit -m "merge: 併入 origin/mac 的 macOS 移植架構（AGENTS.md 兩邊並存）"
```

### Task A2：加一支「mac 模組在 Windows 上 import 得動」的測試

**Files**：Create `tests/test_mac_import.py`

**為什麼要有**：這是 Phase E 之後能在 Windows 上測 `MacPttDaemon` 的**前提**。
沒有這支測試，未來有人在 `mac_recorder.py` 頂層加一行 `import Quartz`，
症狀會是「mac 測試全部無法在 CI／Windows 上跑」，而且沒人知道為什麼。

- [ ] **Step 1：寫測試**

```python
#!/usr/bin/env python3
"""macOS 模組的「可載入性」測試（**不需要 pyobjc，任何平台都能跑**）。

## 為什麼要有這一支

`app/core/mac_*.py` 與 `app/mac_vibetalkie.py` 刻意**不在頂層 import pyobjc** ——
`Quartz` / `AVFoundation` 都是延後到「真的要用了」才 import。
這個性質讓 `MacPttDaemon` 的**狀態機邏輯**可以在 Windows 上測試
（見 `tests/test_mac_trigger.py`）。

但那個性質很脆弱：任何人在 `mac_recorder.py` 頂層加一行 `import Quartz`，
就會讓 mac 的測試在非 macOS 上一次全掛，而且錯誤訊息會指向 pyobjc 而不是
「你加了不該加的 import」。

所以這支測試把它釘住：**載入模組（不呼叫任何原生路徑）** 必須成功。

執行：python tests/test_mac_import.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def load_dotted(name: str) -> None:
    """import 一個在 app/core 底下的模組（用模組名，不靠 sys.path 順序）。"""
    __import__(name)


def main() -> int:
    print("=" * 68)
    print("macOS 模組可載入性測試（不需要 pyobjc）")
    print("=" * 68)

    print("\n[1] app/core 的 mac 模組在頂層不碰 pyobjc")
    for name in ("keys", "mac_keylistener", "mac_recorder", "mac_textin", "ui_server"):
        try:
            load_dotted(name)
            check(f"import {name}", True)
        except Exception as exc:                       # noqa: BLE001
            check(f"import {name}", False, f"{type(exc).__name__}: {exc}")

    print("\n[2] 進入點載入得動（不代表能在本平台跑）")
    spec = importlib.util.spec_from_file_location(
        "mac_entry_probe", ROOT / "app" / "mac_vibetalkie.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)                   # type: ignore[union-attr]
        check("載入 app/mac_vibetalkie.py", True)
        check("MacPttDaemon 類別存在", hasattr(mod, "MacPttDaemon"))
    except Exception as exc:                           # noqa: BLE001
        check("載入 app/mac_vibetalkie.py", False, f"{type(exc).__name__}: {exc}")

    print("\n[3] 共用層（trigger/hotkey）不可以依賴平台原生模組")
    import ast
    for rel in ("app/core/trigger.py", "app/core/hotkey.py"):
        path = ROOT / rel
        if not path.exists():
            check(f"{rel} 存在", False, "檔案不存在")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        banned = {"quartz", "AVFoundation", "AppKit", "ctypes", "objc", "mac_keylistener",
                  "mac_recorder", "mac_textin", "record_wav", "keycode_logger"}
        bad = sorted(imported & banned)
        check(f"{rel} 沒有平台原生相依", not bad, str(bad))

    print("\n" + "=" * 68)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2：跑它（此時 `trigger.py` 還不存在 → 第 [3] 段會失敗，這是預期的）**

```powershell
python tests/test_mac_import.py
```

**期望**：`[1]`、`[2]` 全過；`[3]` 回報 `app/core/trigger.py 存在 — 檔案不存在`。
（這正是「先寫失敗的測試」——Phase C 會讓它變綠。）

- [ ] **Step 3：確認 `[1]`／`[2]` 真的會抓錯（把測試的牙齒驗出來）**

⚠️ **不要用 `git checkout --` 還原**（`AGENTS.md` 有紀錄：會把同檔案其他
未 commit 的修正一起洗掉）。用複製檔：

```powershell
Copy-Item app\core\mac_recorder.py $env:TEMP\mac_recorder.bak
Add-Content app\core\mac_recorder.py "`nimport Quartz  # 故意加的"
python tests/test_mac_import.py      # 期望：[1] 的 mac_recorder 失敗
Copy-Item $env:TEMP\mac_recorder.bak app\core\mac_recorder.py -Force
python tests/test_mac_import.py      # 期望：恢復
```

- [ ] **Step 4：commit**

```powershell
git add tests/test_mac_import.py
git commit -m "test: 釘住 mac 模組的可載入性（不需 pyobjc，任何平台可跑）"
```

### Task A3：建立 baseline 紀錄

- [ ] **Step 1：跑全部測試，把結果貼進 commit message 或 `docs/handoff/` 的暫存檔**

```powershell
python tests/test_status_contract.py; python tests/test_trigger.py
python tests/test_config_api.py;      python tests/test_mic_stream.py
python tests/test_speech_engine.py;   python tests/test_bandwidth.py
python tests/test_models_api.py;      python tests/test_model_index.py
python tests/test_mac_import.py
```

**期望**：除了 `test_mac_import.py` 的第 [3] 段之外全過。

**這份輸出是 Phase D 之後「行為沒有退化」的比對基準。** 存成
`artifacts/integration-baseline.txt`（`artifacts/` 已 gitignored）。

---

## Phase B — `hotkey.py` 改講 canonical 名稱

**檔案**：Modify `app/core/hotkey.py`、Modify `app/core/keys.py`
**目標**：行為**完全不變**，只是內部表示從 VK 換成名稱。
**驗收**：`tests/test_trigger.py` 不動一行且全過。

### Task B1：在 `tests/` 加一組「名稱 ↔ 側別」的單元測試

**Files**：Create `tests/test_hotkey_names.py`

- [ ] **Step 1：寫測試（先寫，會失敗）**

```python
#!/usr/bin/env python3
"""`hotkey.py` 的 canonical 名稱契約：名稱、側別、平台鍵碼三者要對得起來。

## 為什麼要測這個

`hotkey.py` 原本把按鍵存成 **Windows VK 碼**（`vk=0xA3`＝RightCtrl）。
macOS 沒有 VK 碼，CGEventTap 送的是 Carbon keycode —— 所以內部表示必須
改成**平台無關的名稱**，VK 只留在 Windows 平台層。

這支測試釘住三件事：

  1. `HotkeySpec` 的相等性只看（名稱、側別、修飾鍵）—— 與寫法無關
  2. `spec.vk` 是**推導出來的**（給 Windows 平台層用），資料來源是 `_NAME_TO_VK`
  3. 側別是三態：`None`＝兩側都算、`"left"`／`"right"`＝限定該側

第 3 點是實測踩過的 bug：`LeftCtrl` 曾經左右不分，按右 Ctrl 也會觸發。

執行：python tests/test_hotkey_names.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import hotkey as hk  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    print("=" * 68)
    print("hotkey canonical 名稱契約")
    print("=" * 68)

    print("\n[1] 內部表示是名稱，不是平台鍵碼")
    s = hk.parse("RightCtrl")
    check("name 是 canonical 名稱", s.name == "ctrl", repr(s.name))
    check("side 是 'right'", s.side == "right", repr(s.side))
    check("side_name 帶側別前綴", s.side_name == "rightctrl", repr(s.side_name))
    f9 = hk.parse("F9")
    check("F9 → name='f9' side=None", (f9.name, f9.side) == ("f9", None), str(f9))

    print("\n[2] spec.vk 是推導出來的（Windows 平台層用）")
    check("RightCtrl → 0x11（通用 VK，靠 side 分左右）", s.vk == 0x11, hex(s.vk))
    check("F9 → 0x78", f9.vk == 0x78, hex(f9.vk))
    check("LeftCtrl → 0xA2", hk.parse("LeftCtrl").vk == 0xA2,
          hex(hk.parse("LeftCtrl").vk))
    check("組合鍵 Ctrl+Alt+R → 0x52", hk.parse("Ctrl+Alt+R").vk == 0x52,
          hex(hk.parse("Ctrl+Alt+R").vk))

    print("\n[3] 相等性只看（名稱、側別、修飾鍵）")
    check("大小寫不影響", hk.parse("f9") == hk.parse("F9"))
    check("修飾鍵順序不影響",
          hk.parse("Ctrl+Alt+R") == hk.parse("Alt+Ctrl+R"))
    check("左右不同就是不相等",
          hk.parse("LeftCtrl") != hk.parse("RightCtrl"))
    check("沒寫左右 ≠ 限定左側", hk.parse("Ctrl") != hk.parse("LeftCtrl"))

    print("\n[4] 修飾鍵用 canonical 小寫名稱（跨平台一致）")
    check("Ctrl+Alt+R 的修飾鍵", hk.parse("Ctrl+Alt+R").modifiers == frozenset({"ctrl", "alt"}),
          str(sorted(hk.parse("Ctrl+Alt+R").modifiers)))
    check("MODIFIER_VKS 回傳小寫名稱",
          hk.MODIFIER_VKS[0x11] == "ctrl" and hk.MODIFIER_VKS[0xA3] == "ctrl",
          f"{hk.MODIFIER_VKS.get(0x11)}/{hk.MODIFIER_VKS.get(0xA3)}")

    print("\n[5] 側別解析：旗標只是「提示」，名稱優先")
    check("0xA3（右專用）→ right", hk.side_of("ctrl", False, e0=True) == "right")
    check("0xA2 or 名稱 leftctrl → left", hk.side_of("leftctrl", False) == "left")
    check("通用名稱 + e0 → right", hk.side_of("ctrl", False, e0=True) == "right")
    check("通用名稱 + 沒 e0 → left", hk.side_of("ctrl", False, e0=False) == "left")
    check("F9 沒有側別", hk.side_of("f9", False, e0=True) is None)

    print("\n[6] spec_hit 同時接受 VK 整數與 canonical 名稱")
    rc = hk.parse("RightCtrl")
    check("VK 形式（0x11 + e0）", hk.spec_hit(rc, 0x11, True))
    check("VK 形式（0xA3）", hk.spec_hit(rc, 0xA3, False))
    check("名稱形式", hk.spec_hit(rc, "rightctrl", False))
    check("名稱形式（左邊不命中）", not hk.spec_hit(rc, "ctrl", False))
    check("VK 形式（左邊不命中）", not hk.spec_hit(rc, 0xA2, False))

    print("\n[7] BINDABLE_NAMES 是本平台可綁的清單（UI 錄製用）")
    names = hk.BINDABLE_NAMES
    check("含 rightctrl 與 f9", "rightctrl" in names and "f9" in names, str(names[:8]))
    check("沒有重複", len(names) == len(set(names)))

    print("\n" + "=" * 68)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2：跑，確認失敗**

```powershell
python tests/test_hotkey_names.py
```

**期望**：`AttributeError: 'HotkeySpec' object has no attribute 'name'`。

### Task B2：改 `HotkeySpec` 為名稱中心

**Files**：Modify `app/core/hotkey.py`（`HotkeySpec` 與 `parse()` 一帶）

- [ ] **Step 1：換掉 `HotkeySpec`**

```python
@dataclass(frozen=True)
class HotkeySpec:
    """一組按鍵：canonical 名稱 + 側別 + 需要按住的修飾鍵。

    ## 為什麼是**名稱**而不是 VK 碼

    原本這裡是 `vk: int`（Windows 虛擬鍵碼）。只有 Windows 時沒問題，
    但 macOS 的 CGEventTap 送的是完全另一套 Carbon keycode：

        右 Ctrl：Windows VK = 0xA3      macOS keycode = 62
        F9     ：Windows VK = 0x78      macOS keycode = 101

    同一個 `HotkeySpec` 在兩個平台裝兩種數字，`spec_hit()` 的 VK 家族比對
    （`{0x11, 0xA2, 0xA3}` 是同一顆 Ctrl）在 mac 上必然全錯。

    所以內部表示改成**平台無關的名稱**（`"ctrl"`／`"f9"`），
    平台鍵碼只由 `keys.py` 在平台層換算（見 `app/core/keys.py` 檔頭）。
    """

    name: str                        # canonical 名稱，小寫：ctrl / rightctrl / f9 / r
    modifiers: frozenset[str]        # 需要按住的修飾鍵，小寫：{"ctrl","alt"}
    side: str | None = None          # None＝左右都算；"left"／"right"＝限定該側
    # ⚠️ `compare=False`：語意相同但寫法不同的字串（`Ctrl+Alt+R` 與
    #    `Alt+Ctrl+R`）應該被視為**同一組按鍵**。若把 raw 算進相等性，
    #    「順序不影響」就會失效 —— 實測被測試抓到。
    raw: str = dc_field(default="", compare=False)

    # ---------------------------------------------------------- 推導欄位
    @property
    def side_name(self) -> str:
        """帶側別前綴的名稱（`rightctrl`／`leftshift`／`f9`）。

        這是**平台層要比對的字串** —— macOS 的 keycode 本身就分左右，
        所以 mac 不需要另外看旗標（見 `app/core/keys.py`）。
        """
        return f"{self.side}{self.name}" if self.side else self.name

    @property
    def vk(self) -> int:
        """Windows 虛擬鍵碼（**推導出來的**，只給 Windows 平台層用）。

        ⚠️ 通用名稱一律指向**通用 VK**（`ctrl` → `0x11`），側別交給 `side` ——
        因為 Raw Input 送的就是通用 VK ＋ `E0` 旗標，不是 `VK_RCONTROL`。
        """
        if self.side and self.name in ("ctrl", "shift", "alt"):
            # 限定側別時，優先用「專用 VK」（Low-Level Hook 會送這種）
            key = self.side_name
            if key in _NAME_TO_VK:
                return _NAME_TO_VK[key]
        return _NAME_TO_VK.get(self.name, 0)

    # 舊欄位的相容視圖（`require_e0` == 限定右側）。
    @property
    def require_e0(self) -> bool:
        return self.side == "right"

    @property
    def main_is_modifier(self) -> bool:
        """主鍵本身是不是修飾鍵（例如單獨一顆 Ctrl 當熱鍵）。"""
        return self.name in _MODIFIER_NAMES

    @property
    def is_native_device_key(self) -> bool:
        """是不是「裝置原生」的那顆鍵（右 Ctrl，單獨一顆、沒有其他修飾鍵）。"""
        return self.name == "ctrl" and self.side == "right" and not self.modifiers

    @property
    def label(self) -> str:
        mods = "+".join(DISPLAY_NAMES.get(m, m) for m in sorted(self.modifiers))
        main = _name_label(self.name)
        s = f"{mods}+{main}" if mods else main
        return f"{s}（限定右側）" if self.require_e0 else s
```

- [ ] **Step 2：加 `_MODIFIER_NAMES` 並把 `parse()` 改成收集名稱**

```python
# 修飾鍵的 canonical 名稱（小寫）。與 MODIFIER_VKS 的值同一個集合，
# 但這裡是「名稱」而不是 VK —— 跨平台共用的是這一份。
_MODIFIER_NAMES: frozenset[str] = frozenset({"ctrl", "shift", "alt", "win"})


def parse(spec: str) -> HotkeySpec:
    """把 `"Ctrl+Alt+R"` 這種字串解析成 `HotkeySpec`。

    認不出來就丟 `HotkeyError` —— **不要靜默退回預設值**，
    否則使用者會以為設定生效了，實際上還在用舊鍵。
    """
    raw = (spec or "").strip()
    if not raw:
        raise HotkeyError("按鍵不能是空的")
    tokens = _split(raw)
    if not tokens:
        raise HotkeyError(f"看不懂的按鍵：{spec!r}")

    modifiers: set[str] = set()
    main_name: str | None = None
    side: str | None = None

    for tok in tokens:
        key = _norm(tok)
        if key not in _NAME_TO_VK:
            raise HotkeyError(f"認不得的按鍵名稱：{tok!r}")
        base = _canonical_name(key)          # rightctrl → ("ctrl", "right")
        if base[0] in _MODIFIER_NAMES and key not in _MAIN_KEY_EXCEPTIONS:
            modifiers.add(base[0])
            # 明確寫「RightCtrl」時要限定右側（裝置原生送的就是右 Ctrl）；
            # 「LeftCtrl」同理限定左側 —— 否則左右不分，設定形同虛設。
            if key in _RIGHT_NAMES:
                side = "right"
            elif key in _LEFT_NAMES:
                side = "left"
            continue
        if main_name is not None:
            raise HotkeyError(f"只能有一顆主鍵，但看到 {tok!r} 與另一顆")
        main_name, side_from_main = base
        if side_from_main:
            side = side_from_main
    ...
```

⚠️ **`_MAIN_KEY_EXCEPTIONS` 的用途**：`Ctrl`／`Shift`／`Alt` 本身也可以是
**主鍵**（「單獨一顆 Ctrl 當熱鍵」）。原本的寫法是「修飾鍵都先收進 modifiers，
最後若 `main_vk is None` 且只有一個修飾鍵，就讓它當主鍵」。

**最小改動版**（建議照這個做，不要引入新集合）：

```python
    # 收集到的修飾鍵名稱（小寫）與它們各自的側別
    mod_names: list[str] = []
    mod_sides: list[str | None] = []
    main_name: str | None = None

    for tok in tokens:
        key = _norm(tok)
        if key not in _NAME_TO_VK:
            raise HotkeyError(f"認不得的按鍵名稱：{tok!r}")
        name, tok_side = _key_name_and_side(key)
        if name in _MODIFIER_NAMES:
            mod_names.append(name)
            mod_sides.append(tok_side)
            if tok_side:
                side = tok_side
            continue
        if main_name is not None:
            raise HotkeyError(f"只能有一顆主鍵，但看到 {tok!r} 與另一顆")
        main_name = name
        if tok_side:
            side = tok_side

    if main_name is None:
        # 只有修飾鍵 → 允許（例如單獨一顆 Ctrl 當熱鍵，這正是裝置的行為）
        if len(mod_names) == 1:
            return HotkeySpec(name=mod_names[0], modifiers=frozenset(),
                              side=side, raw=raw)
        raise HotkeyError("至少要有一顆主鍵（或單獨一顆 Ctrl／Shift／Alt）")

    # ⚠️ 主鍵自己若是修飾鍵家族的一員時不可能走到這裡（上面 continue 掉了），
    #    所以 modifiers 就是「另外按住的修飾鍵」。
    return HotkeySpec(name=main_name, modifiers=frozenset(mod_names),
                      side=side, raw=raw)
```

並新增：

```python
def _key_name_and_side(key: str) -> tuple[str, str | None]:
    """`"rightctrl"` → `("ctrl", "right")`；`"f9"` → `("f9", None)`。

    ⚠️ 側別只看**字串**，不要用 `vk == 0xA2` 之類的判斷：`0xA2` 同時可能是
    「使用者寫了 leftctrl」也可能是「UI 錄到 LeftCtrl 後存成 0xA2」。
    """
    if key in _RIGHT_NAMES:
        return key[5:], "right"
    if key in _LEFT_NAMES:
        return key[4:], "left"
    if key in ("lwin", "rwin"):
        return "win", ("left" if key == "lwin" else "right")
    return _ALIASES.get(key, key), None
```

- [ ] **Step 3：把 `spec_hit`／`event_side` 換成名稱比對，並保留 VK 入口**

```python
def side_of(name: str, by_name_side: str | None = None,
            e0: bool = False) -> str | None:
    """這顆鍵是左邊還是右邊的那一顆？（`None`＝左右無意義）

    判準（實測）：
      · **名稱已經帶側別**（mac 的 keycode、或 `event_side()` 解析過的）→ 名稱優先
      · **通用名稱 + E0 旗標**（Windows Raw Input）→ 由旗標決定
      · 不是修飾鍵家族的鍵 → None（左右無意義）
    """
    if by_name_side:
        return by_name_side
    if name in _MODIFIER_NAMES:
        return "right" if e0 else "left"
    return None


def event_side(vk: int, e0: bool) -> str | None:
    """**Windows 入口**：VK ＋ E0 旗標 → 側別（保留給舊呼叫端與測試）。"""
    name, by_name = _vk_to_name_side(vk)
    return side_of(name, by_name, e0)


def spec_hit(spec: HotkeySpec, key, e0: bool = False) -> bool:
    """這顆鍵是不是 `spec` 的主鍵（含 VK 家族對應與左右側判定）。

    `key` 可以是 **VK 整數**（Windows 呼叫端）或 **canonical 名稱字串**
    （mac 呼叫端、以及抽出來的 `trigger.py`）。

    ⚠️ 這是**主鍵**的比對，不含修飾鍵條件（那個要用 `mods_ok`）。
    """
    if isinstance(key, str):
        name, by_name = _key_name_and_side(_norm(key))
    else:
        name, by_name = _vk_to_name_side(int(key))
    if name != spec.name:
        return False
    if spec.side is not None:
        got = side_of(name, by_name, e0)
        if got is not None and got != spec.side:
            return False
    return True
```

`_vk_to_name_side` 的實作放在 `hotkey.py`（VK 表的主人就是它）：

```python
# VK → (canonical 名稱, 名稱自帶的側別)
_VK_TO_NAME_SIDE: dict[int, tuple[str, str | None]] = {}
for _n, _v in _NAME_TO_VK.items():
    _VK_TO_NAME_SIDE.setdefault(_v, _key_name_and_side(_n))


def _vk_to_name_side(vk: int) -> tuple[str, str | None]:
    return _VK_TO_NAME_SIDE.get(vk, ("", None))
```

- [ ] **Step 4：`mods_ok` 改成小寫集合比對**

```python
def mods_ok(spec: HotkeySpec, mods_down) -> bool:
    """修飾鍵是否**剛好**符合（不多也不少）。

    `mods_down` 是「目前按住的修飾鍵集合」，**小寫 canonical 名稱**
    （`{"ctrl","alt"}`）—— 不分左右，左右由 `spec.side` 判定。

    ⚠️ 主鍵本身就是修飾鍵時（例如單獨一顆 Ctrl 當錄音鍵），它自己一定
    會出現在 `mods_down` 裡 —— 那不是「需要另外按住的修飾鍵」，要先扣掉。
    """
    have = {str(m).lower() for m in mods_down}
    if spec.main_is_modifier:
        have.discard(spec.name)
    return have == set(spec.modifiers)
```

- [ ] **Step 5：跑 Phase B 的兩支測試**

```powershell
python tests/test_hotkey_names.py     # 期望：✅ 全部通過
python tests/test_trigger.py          # 期望：✅ 全部通過（一行都沒改！）
python tests/test_config_api.py       # 期望：✅ 全部通過
```

⚠️ **若 `test_trigger.py` 有紅燈，修的是 `hotkey.py`，不是測試。**
（`[1]` 段有 `parse("RightCtrl").vk == 0x11` 與 `parse("F9").vk == 0x78`，
`[8]` 段有 `spec_hit` 的 VK 呼叫 —— 那些都必須原樣成立。）

- [ ] **Step 6：commit**

```powershell
git add app/core/hotkey.py tests/test_hotkey_names.py
git commit -m "refactor(hotkey): 內部表示改為 canonical 名稱（VK 只留在 Windows 平台層）"
```

### Task B3：`keys.py` 跟著對齊（不要做出第三份表）

**Files**：Modify `app/core/keys.py`

- [ ] **Step 1：釐清 `keys.py` 的職責邊界**

`keys.py` **只做**三件事：

1. 「使用者／平台寫的鍵名」→ canonical 名稱（別名、側別）
2. canonical 名稱 ⇄ **本平台鍵碼**（Windows VK／macOS Carbon keycode）
3. `all_names()`：本平台認得的名稱清單（給 UI 與驗證用）

⚠️ **`generic_modifier()` 與 `is_modifier()` 要留著，而且改成轉呼叫 `hotkey`**
（不要刪、也不要抄一份）：

```python
def is_modifier(name: str) -> bool:
    """這個名稱是不是修飾鍵（Ctrl／Shift／Alt／Win，含左右寫法）。"""
    import hotkey as _hotkey
    return _hotkey._key_name_and_side(_canonical(name))[0] in _hotkey._MODIFIER_NAMES


def generic_modifier(name: str) -> str:
    """把左／右的寫法收斂成通用名：`rightctrl` → `ctrl`。

    ⚠️ 這是 `hotkey._key_name_and_side()` 的**薄包裝，不是第二份實作**。
    兩份的漂移症狀是「同一顆鍵在 mac 命中、在 Windows 不命中」。
    """
    import hotkey as _hotkey
    return _hotkey._key_name_and_side(_canonical(name))[0]
```

**為什麼不能刪**：`tests/test_mac_keys.py` 直接測這兩個函式，
而且**已經期望小寫回傳**（`{"rightctrl": "ctrl", "f9": "f9"}`）——
與 Phase B 的 canonical 小寫表示完全一致。刪掉會讓一支現有的 mac 測試
失去被測對象（那比測試失敗更糟：**測試還綠，但測的東西不見了**）。

`_windows_tables()` 借用 `hotkey._NAME_TO_VK` 的做法留著，
它是 `keys.py` 對 `hotkey.py` 的依賴之一（另一個是上面兩個包裝）。

- [ ] **Step 2：加一個快取，避免每個事件都重建反轉表**

```python
_WIN_CACHE: tuple[dict[str, int], dict[int, str]] | None = None


def _windows_tables() -> tuple[dict[str, int], dict[int, str]]:
    """跟 `hotkey.py` 借用既有的 VK 表（避免兩份清單漂移）。

    ⚠️ 有快取：這個函式會在**每一個按鍵事件**被呼叫（`code_to_name`），
    沒有快取的話每個事件都重建一次反轉 dict。
    """
    global _WIN_CACHE
    if _WIN_CACHE is None:
        import hotkey as _hotkey
        name_to_vk = dict(_hotkey._NAME_TO_VK)          # noqa: SLF001
        vk_to_name: dict[int, str] = {}
        for name, vk in name_to_vk.items():
            cur = vk_to_name.get(vk)
            if cur is None or _rank(name) < _rank(cur):
                vk_to_name[vk] = name
        _WIN_CACHE = (name_to_vk, vk_to_name)
    return _WIN_CACHE
```

- [ ] **Step 3：驗證**

```powershell
python tests/test_mac_keys.py   # 需要 pyobjc → 這台會回報「需要 pyobjc」，正常
python -c "import sys; sys.path[:0]=['app','app/core']; import keys; print(keys.all_names()[:10])"
```

**期望**：印出 Windows 認得的名稱清單（`['0','1','2',…]` 之類），沒有例外。
若 `test_mac_keys.py` 在 Windows 上直接爆掉而不是友善回報，**那是 Phase G 的任務**。

- [ ] **Step 4：commit**

```powershell
git add app/core/keys.py
git commit -m "refactor(keys): 收斂職責為「名稱⇄平台鍵碼」＋加快取"
```

---

## Phase C — 抽出 `app/core/trigger.py`

**這是整個整合的核心**：把 `ptt.py` 裡與 OS 無關的觸發狀態機搬到這裡，
兩個平台共用。搬完之後 `trigger.py` 必須**一行 `ctypes` 都沒有**。

### Task C1：介面先寫死（含完整 docstring）

**Files**：Create `app/core/trigger.py`

- [ ] **Step 1：寫模組（含完整行為，見 Task C2 的分段貼上）**

模組的公開介面（**這就是兩個平台要遵守的契約**）：

```python
@dataclass(frozen=True)
class Event:
    """一個**平台無關**的按鍵事件。"""
    key: str                      # canonical 名稱（可帶側別：rightctrl）
    down: bool
    mods: frozenset[str] = frozenset()   # 事件**之前**已按住的修飾鍵（小寫）
    e0: bool = False              # Windows 的右側旗標（mac 恆為 False）
    device: str | None = None     # 來源裝置標籤（**只給 log 用**，mac 恆為 None）
    # ⚠️ 裝置**政策**的判斷留在平台層（Windows 才知道 `hDevice` 與 device_filter）：
    #    平台層算完之後把結論放在這裡。`trigger.py` 永遠不碰裝置 API。
    #    mac 恆為 True（§8.7：macOS 做不到裝置辨識）。
    device_ok: bool = True
    autorepeat: bool = False      # 按住產生的重複事件


class TriggerEngine:
    def __init__(self, *, get_bindings, on_start, on_finish,
                 double_window: float = 0.4, debug=False): ...
    # 事件入口
    def feed(self, ev: Event) -> None
    # 主迴圈（double 配對窗過期）
    def tick(self) -> None
    # 測試模式（只聽不錄）
    def start_key_test(self, seconds: float = 20.0, which: str = "") -> dict
    def cancel_key_test(self) -> None
    def test_state(self) -> dict
    # 查詢
    def allow_any_device(self) -> bool
    def end_owner(self, key, e0: bool = False)
    def label_of(self, spec) -> str
    # 狀態
    capturing: bool
    mods_down: set[str]
    key_held: bool
    tap_at: dict
    main_down: set[int]           # 以「名稱+側別」的雜湊當 key（見下）
```

⚠️ **`main_down` 的元素型別**：`ptt.py` 原本用 VK 整數，測試直接讀
`d._main_down`（只判斷「有沒有東西」）。改成 **canonical token 字串**
（`"f9"`／`"rightctrl"`，由 `token_of()` 產生）更乾淨，且兩個平台一致。
**既有測試只做 in/not-in 與 discard，字串一樣成立。**

⚠️ **修飾鍵狀態（`mods_down`）的 canonical 化** —— 這是 mac／Windows
兩邊形狀不同的地方，由引擎統一：

```python
    def _update_mods(self, ev: Event) -> None:
        """修飾鍵狀態機（**平台無關**）。

        Windows 的 Raw Input 只送單一事件，不告訴我們「現在 Ctrl 有沒有按住」，
        所以要自己維護；mac 的 CGEventTap 同理（`FlagsChanged` 也要自己判）。

        ⚠️ 存的是**通用小寫名稱**（`"ctrl"`），不是 `"rightctrl"` ——
        側別由事件的 `key` 決定（`spec_hit(spec, "rightctrl")` 會判成右邊）。
        存側別名稱的話，`ctrl+alt+r` 這種組合鍵在 mac 上永遠不吻合
        （mac 送的是 `"ctrl"`／`"leftctrl"` 混用）。
        """
        name = hotkey._key_name_and_side(_norm(ev.key))[0]
        if name not in hotkey._MODIFIER_NAMES:
            return
        if ev.down:
            self.mods_down.add(name)
        else:
            self.mods_down.discard(name)
```

（`_norm()` 在 `trigger.py` 內自己寫一個 3 行的版本，或直接呼叫
`hotkey._key_name_and_side(ev.key.strip().lower())` —— **不要為此在
`hotkey.py` 開新的公開 API**。）

- [ ] **Step 2：寫 `tests/test_trigger_engine.py`（直接測引擎，不經過 daemon）**

**為什麼要有一支「直接測引擎」的測試**：`test_trigger.py` 走 `PttDaemon`，
是 Windows 路徑。引擎自己的契約（含 mac 會用的 `Event` 形式）需要獨立釘住，
否則 Phase E 的 mac 端就是在「沒有人測過」的介面上接。

沿用 `tests/test_trigger.py` 的 `check()` 風格（這個專案不用 pytest），
把 `[1]`–`[19]` 的精神用**引擎直接呼叫**重寫一遍，**重點是這幾條**
（其餘與 `test_trigger.py` 重疊，不重複抄）：

| 測試 | 內容 |
|---|---|
| `feed` 吃 canonical 名稱 | `Event(key="rightctrl", down=True)` 能命中 `RightCtrl` |
| `feed` 吃側別 | `Event(key="ctrl", down=True)` **不**命中 `RightCtrl` |
| `autorepeat=True` 被吃掉 | 連續 6 次 keydown 不算連點（`double` 模式） |
| `tick()` 清配對窗 | 過期後 `tap_at` 全為 falsy |
| `device_ok=False` 時不觸發 | 平台層否決的裝置 → 引擎直接跳過（mac 恆為 True） |
| `on_start`／`on_finish` 被呼叫 | 用 list 收集呼叫記錄 |

```powershell
python tests/test_trigger_engine.py
```

- [ ] **Step 3：commit（介面＋測試先進去，實作下一 Task）**

⚠️ 這一階段刻意讓測試**紅**（引擎還不完整）會擋住 `git commit` 的習慣嗎？
不會 —— 這個 repo 沒有 pre-commit hook。但**不要在這裡停太久**，
Task C2 立刻補完。

### Task C2：把 `ptt.py` 的狀態機搬進來

**Files**：Modify `app/core/trigger.py`

逐段搬（**來源：`app/core/ptt.py` 的下列區塊**，行號以 `fc9add6` 為準）：

| 來源（ptt.py） | 搬到 trigger.py 的 |
|---|---|
| `_on_main_down()`（1060–1090） | `TriggerEngine._on_main_down()` |
| `_on_main_up()`（1092–1109） | `TriggerEngine._on_main_up()` |
| `_toggle_capture()`（1043–1058） | `TriggerEngine._toggle_capture()` |
| `tick_trigger()`（1111–1121） | `TriggerEngine.tick()` |
| `_finish_explicit()`（1017–1024） | `TriggerEngine._finish_explicit()` |
| `_end_owner()`（937–950） | `TriggerEngine.end_owner()` |
| `_note_test_hit()`（977–1002） | `TriggerEngine._note_test_hit()` |
| `start_key_test()`／`cancel_key_test()`／`test_state()`（953–1015） | 同名方法 |
| `_handle_key()`（1123–1222）的**決策部分** | `TriggerEngine.feed()` |

**搬移時必須保留的四條註解（它們是實測教訓，不是裝飾）**：

1. `_on_main_up()` 的「**只有 hold 模式該在這裡動作**」＋「**設定了結束鍵時
   也不能在放開時停**」
2. `tick_trigger()` 的「待配對的狀態是**每一組各記一份**」
3. `feed()` 的「⚠️ 時間判斷要在這裡做（不能只看 `_test_until` 有沒有值）」
4. `feed()` 的「結束鍵優先於行為」與 `_end_armed` 的「鬆開才停需要 keyup 收尾」

**新的 `feed()` 骨架**：

```python
    def feed(self, ev: Event) -> None:
        """一個按鍵事件進來。**這裡不做任何平台判斷。**

        順序與 `ptt._handle_key()` 原本的順序**完全一致**（改了會壞事）：

          1. 修飾鍵狀態先更新（主鍵判定要用）
          2. 自動重複直接丟掉（double 模式靠這條才不會被誤判成連點）
          3. 算出命中哪幾組
          4. 測試模式：只回報、不觸發
          5. 結束鍵（優先於開始行為）
          6. 起點：裝置政策 → 自動重複 → 交給行為
        """
        self._update_mods(ev)
        if ev.autorepeat:
            return                      # 平台層已經濾過，這裡再保險一次
        bindings = self._bindings()
        hits = [s for s in bindings.active
                if hotkey.spec_hit(s, ev.key, ev.e0) and self._mods_ok(s)]
        if self.test_active():
            self._note_test_hit(ev, hits)
            if self._test_hits:
                return
        ...
```

- [ ] **Step 1：搬完之後跑**

```powershell
python tests/test_trigger_engine.py
python tests/test_trigger.py          # 這支還是走 ptt.py → 這個階段可能仍綠
python tests/test_mac_import.py       # [3] 段現在該變綠了
```

- [ ] **Step 2：確認共用層真的沒有平台相依**

```powershell
Select-String -Path app\core\trigger.py -Pattern "ctypes|windll|Quartz|raw_input|RAWKEYBOARD|WM_"
# 期望：沒有輸出
```

⚠️ `hotkey.py` 也不行有（`_NAME_TO_VK` 是**資料**，不是 Win32 呼叫，允許）。

- [ ] **Step 3：commit**

```powershell
git add app/core/trigger.py tests/test_trigger_engine.py
git commit -m "feat(trigger): 抽出平台無關的觸發狀態機（多組／雙邊行為／停用／測試模式）"
```

---

## Phase D — `ptt.PttDaemon` 改用 `trigger.py`

**鐵則：Windows 行為必須與 `fc9add6` 完全相同。** 驗收＝`test_trigger.py`
（20 段、989 行）**一行都不改**且全過。

### Task D1：把 daemon 的觸發相關狀態改成轉發

**Files**：Modify `app/core/ptt.py`

- [ ] **Step 1：`__init__` 建立引擎**

```python
        # 觸發狀態機抽到 `app/core/trigger.py`（平台無關，mac 共用同一顆）。
        # ⚠️ 這裡**不再**自己維護 `_mods_down` / `_tap_at` / `_main_down` / `_capturing` ——
        #    它們都是引擎的狀態，daemon 只是轉發（見下面的 property）。
        #    兩邊各留一份的話，重構完就會出現「一邊更新、一邊沒更新」的鬼症狀。
        self.trigger = trigger.TriggerEngine(
            get_bindings=self._bindings,
            on_start=self._on_trigger_start,
            on_finish=self._on_trigger_finish,
            double_window=self._live()["double_tap_ms"] / 1000.0,
            debug=debug,
        )
```

⚠️ **`double_window` 的來源**：`_live()["double_tap_ms"]`（**不是**
`self._cfg()` —— `PttDaemon` 沒有那個方法）。`_live()` 沒有 provider 時
會回後備值 `400`，所以純終端機模式也成立。

⚠️ **使用者改了 `double_tap_ms` 之後不必重啟**：`_live()` 是每次重讀的。
既然 `_bindings()` 每次都會重算並快取，就在那裡順手同步（一行）：

```python
        self._hotkey_bindings = bind
        self._hotkey_spec = bind.specs[0] if bind.specs else None
        # 使用者可能在 UI 改過「雙擊間隔」—— 每次重讀設定時一起同步
        self.trigger.double_window = self._live()["double_tap_ms"] / 1000.0
```

- [ ] **Step 2：加相容 property（測試與 UI 都靠它們）**

```python
    # ------------------------------------------------ 觸發狀態（轉發給引擎）
    #
    # ⚠️ 這些 property 是**為了相容既有呼叫端與測試**存在的
    #    （`tests/test_trigger.py` 直接讀 `d._tap_at`／`d._mods_down`／
    #    `d._capturing`／`d._double_window`，`tests/test_mic_stream.py` 也是）。
    #    值一律來自 `self.trigger`，**不要在 daemon 再存一份**。
    @property
    def _capturing(self) -> bool:
        return self.trigger.capturing

    @_capturing.setter
    def _capturing(self, value: bool) -> None:
        self.trigger.capturing = bool(value)

    @property
    def _mods_down(self) -> set[str]:
        return self.trigger.mods_down

    @_mods_down.setter
    def _mods_down(self, value) -> None:
        self.trigger.mods_down = set(value)

    @property
    def _tap_at(self) -> dict:
        return self.trigger.tap_at

    @_tap_at.setter
    def _tap_at(self, value: dict) -> None:
        self.trigger.tap_at = dict(value)

    @property
    def _double_window(self) -> float:
        return self.trigger.double_window

    @_double_window.setter
    def _double_window(self, value: float) -> None:
        self.trigger.double_window = float(value)

    @property
    def _main_down(self) -> set:
        return self.trigger.main_down
```

- [ ] **Step 3：加錄音動作的回呼**

```python
    def _on_trigger_start(self, label: str) -> None:
        """引擎說「開始」→ 這裡只負責錄音。"""
        self.stats["presses"] += 1
        self._start_recording(label)

    def _on_trigger_finish(self) -> None:
        """引擎說「停止」→ 這裡只負責收尾。"""
        self._key_held = False
        self._finish_recording()
```

⚠️ **注意 `_key_held` 的歸屬**：`_finish_recording()` 內部的自動重錄要靠它
（`test_trigger.py` `[13]` 直接檢查 `d._key_held`）。**保留在 daemon**
（它與音訊重試邏輯綁在一起），由 `_on_trigger_finish` 清掉 ——
這與原本 `_toggle_capture()` 停錄時 `self._key_held = False` 的行為相同。

⚠️ **`stats["presses"]` 的位置**：原本在 `_on_main_down()` 裡
（`hold` 與 `toggle/double` 兩條路都會加一）。搬到 `_on_trigger_start()`
之後語意相同（引擎只在真正要開始時呼叫）。**但要確認 `[13]` 的
`d.stats["retried"]`／`failed` 計數不受影響** —— 那兩個在
`_finish_recording()` 裡，不動。

- [ ] **Step 4：`_handle_key` 縮成「平台轉接」**

```python
    def _handle_key(self, hdevice, kb: RAWKEYBOARD) -> None:
        """Raw Input 事件 → 平台無關的 `trigger.Event`。

        ⚠️ 這一層**只做轉譯**，不做任何觸發判斷 —— 判斷都在 `trigger.py`
        （兩個平台共用同一顆，見 `app/core/trigger.py` 檔頭）。
        留在這裡的只有 Windows 專屬的資訊：`E0` 旗標與來源裝置。
        """
        vk = int(kb.VKey)
        e0 = bool(int(kb.Flags) & RI_KEY_E0)
        down = kb.Message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        name, by_name = hotkey._vk_to_name_side(vk)
        if not name:
            # 認不得的 VK：不猜，直接忽略（並在 debug 時說出來）。
            if self.debug:
                print(f"  · 忽略未知的虛擬鍵碼 0x{vk:02X}")
            return
        # ⚠️ **自動重複的判斷也要用 canonical 名稱**，不能用 VK：
        #    同一個事件可能是通用 VK（0x11 + E0）也可能是專用 VK（0xA3），
        #    兩者的 VK 不同但是**同一顆鍵** —— 用 VK 當 key 會讓「按住右 Ctrl
        #    不放」的自動重複漏掉一半，double 模式就會被誤判成連點。
        token = trigger.token_of(name, e0, by_name)
        target, label = self._is_target_device(hdevice)
        allowed = target or not self.device_filter or self._allow_any_device()
        with self._lock:
            self.trigger.feed(trigger.Event(
                key=name, down=down, e0=e0,
                mods=frozenset(self.trigger.mods_down),
                device=label,
                device_ok=allowed,
                autorepeat=(down and token in self.trigger.main_down),
            ))
```

⚠️ **`trigger.token_of(name, e0, by_name)`**：`trigger.py` 提供的模組層小函式，
回傳「這顆鍵的 canonical token」（`"f9"`／`"rightctrl"`）。
`trigger.main_down` 存的元素**就是這個 token**（不是 VK），
而且引擎自己在 `feed()` 內也用同一個函式算 —— **兩邊算出來的一定一樣**，
不必在呼叫端重寫一次側別邏輯。實作：

```python
def token_of(name: str, e0: bool = False, by_name: str | None = None) -> str:
    """事件的 canonical token（含側別）：`("ctrl", True)` → `"rightctrl"`。"""
    side = hotkey.side_of(name, by_name, e0)
    return f"{side}{name}" if side else name
```

⚠️ **`test_trigger.py` 對 `_main_down` 只做「有沒有東西」的判斷**，
所以元素從 VK 整數換成字串一樣成立（`[12]` 連送 6 次 keydown 那段）。

⚠️ **`device_ok` 的算式與原本的判斷等價**：
原本是 `if not target and self.device_filter and not self._allow_any_device(): return`
→ 放行條件就是 `target or (not self.device_filter) or self._allow_any_device()`。

⚠️ **`_is_target_device()` 在鎖外呼叫** —— 原本它就在鎖外（`_handle_key`
是進 `with self._lock:` 之後才呼叫 `_on_main_down`）。不要在鎖內做裝置列舉。

⚠️ **`autorepeat` 的來源**：引擎原本用「`vk in self._main_down`」自己判斷。
改成事件帶進來之後，**引擎仍要保留「自己再檢查一次」的保險**
（見 Task C2 的 `feed()` 骨架），否則 `test_trigger.py` `[12]`
（連送 6 次 keydown）在呼叫端沒標記時會壞掉。

⚠️ **`device` 與 `device_ok` 的分工**：`_is_target_device()` 回傳 `(bool, label)`。
`label` 只放進 `Event.device`（給 log 與「不是目標裝置」的除錯訊息用），
**政策結論**放 `device_ok`。引擎不碰裝置 API，只看 `device_ok`。

⚠️ **`state_of_live()` 這條線索**：`test_trigger.py` `[6]`／`[10]` 會把
`d._is_target_device` 換成假的（回 `(False, "USB鍵盤")`），所以
`device_ok` 必須**每次事件重新算**，不能在 `__init__` 快取。

- [ ] **Step 5：刪掉搬走的私有方法**

`_spec_matches()`／`_mods_ok()`／`_toggle_capture()`／`_on_main_down()`／
`_on_main_up()`／`tick_trigger()`／`_finish_explicit()`／`_end_owner()`／
`_note_test_hit()` —— **確認沒有其他呼叫端之後才刪**：

```powershell
Select-String -Path app\core\ptt.py,app\vibetalkie.py,tools\p1\*.py -Pattern "_on_main_down|_toggle_capture|tick_trigger|_finish_explicit|_end_owner|_note_test_hit|_spec_matches|_mods_ok"
```

留下的**薄轉發**（測試與 UI 會用）：

```python
    def tick_trigger(self) -> None:
        """主迴圈呼叫（double 配對窗過期）。"""
        self.trigger.tick()

    def start_key_test(self, seconds: float = 20.0, which: str = "") -> dict:
        return self.trigger.start_key_test(seconds, which)

    def cancel_key_test(self) -> None:
        self.trigger.cancel_key_test()

    def test_state(self) -> dict:
        return self.trigger.test_state()

    def _end_owner(self, key, e0: bool = False):
        return self.trigger.end_owner(key, e0)

    def _allow_any_device(self) -> bool:
        return self.trigger.allow_any_device()

    def _mode_of(self, spec) -> str:
        return self._bindings().mode_of(spec)
```

- [ ] **Step 6：跑驗收**

```powershell
python tests/test_trigger.py
```

**期望**：`✅ 全部通過`，**而且沒有修改測試檔**（`git diff --stat tests/` 不含它）。

```powershell
python tests/test_mic_stream.py
python tests/test_status_contract.py
python tests/test_config_api.py
```

- [ ] **Step 7：commit**

```powershell
git add app/core/ptt.py
git commit -m "refactor(ptt): 觸發邏輯改用共用的 trigger.py（Windows 行為不變）"
```

### Task D2：確認「行為完全沒變」的證據

- [ ] **Step 1：與 baseline 逐項比對**

```powershell
python tests/test_trigger.py > artifacts/integration-after-d.txt
Compare-Object (Get-Content artifacts/integration-baseline.txt) (Get-Content artifacts/integration-after-d.txt)
```

**期望**：`test_trigger.py` 那一段的輸出**逐字相同**（都是 `✅ 全部通過`）。

- [ ] **Step 2：實機煙霧測試（可選，但強烈建議）**

```powershell
python app/vibetalkie.py --dry-run
```

按一次裝置的錄音鍵 → 看 log 是否為
`🔴 錄音中…` → `⏹ 停止（…）` → `📝 …`。
（`--dry-run` 不注入文字，不會干擾正在打字的視窗。）

---

## Phase E — `MacPttDaemon` 改用 `trigger.py`

**Files**：Modify `app/mac_vibetalkie.py`

### Task E1：刪掉 mac 自己那一份狀態機

- [ ] **Step 1：`__init__` 建立同一顆引擎**

```python
        # 觸發狀態機與 Windows 版**共用同一顆**（`app/core/trigger.py`）。
        # ⚠️ 這裡原本有一份自己的實作（`_last_tap` + `_spec_matches` + 一個
        #    `mode` 分支），但那一份只有「單一顆鍵 + 全域模式」——
        #    main 新增的「多組／配對／雙邊行為／停用／測試模式」它全都沒有。
        #    兩份實作一定會漂移，所以刪掉自己的，改用共用的。
        self.trigger = trigger.TriggerEngine(
            get_bindings=self._bindings,
            on_start=self._on_trigger_start,
            on_finish=self._on_trigger_finish,
            double_window=(getattr(cfg, "double_tap_ms", 400) / 1000.0),
            debug=debug,
        )
```

- [ ] **Step 2：`_bindings()`（mac 版，跟 Windows 同一套）**

```python
    def _bindings(self):
        """目前生效的錄音鍵（多組、每組自己的行為）。

        與 Windows 版的差別**只有**資料來源（這裡直接讀 `self.cfg`，
        Windows 版走 `_live()` 的熱重載快取）。解析規則完全共用。
        """
        raw = tuple(getattr(self.cfg, "effective_hotkeys", lambda: [])() or [])
        if raw == self._hotkey_raw and self._hotkey_bindings is not None:
            return self._hotkey_bindings
        bind, err = hotkey_mod.bindings(
            list(raw), default_mode=getattr(self.cfg, "trigger_mode", "hold"))
        self._hotkey_raw = raw
        self._hotkey_bindings = bind
        self._hotkey_error = err
        self._tap_at = {}          # 綁定換了就清掉配對殘留（與 Windows 版一致）
        if err:
            print(f"  ⚠️ 錄音鍵設定有問題（{err}）—— 目前生效的是 {bind.label}")
        return bind
```

- [ ] **Step 3：`_handle_key` 縮成平台轉接**

```python
    def _handle_key(self, ev: KeyEvent) -> None:
        """CGEventTap 事件 → 平台無關的 `trigger.Event`（只做轉譯）。"""
        if ev.autorepeat:
            return
        self.trigger.feed(trigger.Event(
            key=ev.key,                    # mac 的 keycode 本身就分左右
            down=ev.down,
            mods=frozenset(self.trigger.mods_down),
            device=ev.device,              # macOS 恆為 None（實測無法取得）
            # ⚠️ 恆為 True：macOS **做不到**裝置辨識（§8.7 已實測四個候選
            #    欄位全部相同）。這裡不假裝有 —— 維持 True 只是表示
            #    「不做裝置政策」，與原本 `_allow_any_device()` 回 True 同義。
            device_ok=True,
        ))
```

⚠️ **修飾鍵的 canonical 化**：mac 送的是 `"rightctrl"`／`"ctrl"`。
引擎的 `_update_mods()` 要把它們收斂成通用的 `"ctrl"` 存進 `mods_down`，
但**側別資訊不能丟** —— 由 `spec_hit()` 從**事件的 `key`** 判定側別
（`Event.key="rightctrl"` → `side_of()` 回 `"right"`）。
這正是為什麼「側別由名稱推導」比「靠旗標」乾淨。

- [ ] **Step 4：`_start_recording`／`_finish_recording` 維持原樣**

mac 的錄音流程（AVAudioEngine、`min_s` 誤觸保護、`too_short` 統計）
**完全不要動** —— 那是 mac 的實測結晶。只把「誰決定開始／停止」換成引擎。

⚠️ **`_pressed` 的相容**：`test_mac_trigger.py` 讀 `d._pressed`。
加 property：

```python
    @property
    def _pressed(self) -> bool:
        """主鍵現在是不是按著（引擎的 `key_held`，相容舊名稱）。"""
        return self.trigger.key_held
```

- [ ] **Step 5：`spec()` 的相容（給 `test_mac_trigger.py` 的舊斷言）**

mac 的測試檢查 `d.spec().raw != "這不是按鍵"`（壞字串要退回預設）。
`spec()` 改成回**第一組**綁定：

```python
    def spec(self) -> hotkey_mod.HotkeySpec:
        """目前錄音鍵的**第一組**規格（相容舊呼叫端；新程式請用 `_bindings()`）。"""
        bind = self._bindings()
        return bind.specs[0] if bind.specs else hotkey_mod.parse(hotkey_mod.DEFAULT_SPEC)
```

- [ ] **Step 6：跑**

```powershell
python tests/test_mac_import.py       # 期望：全綠
python -m py_compile app/mac_vibetalkie.py
python tests/test_trigger.py          # Windows 端不能被 mac 的改動影響
```

**在 Mac 上**（使用者執行）：

```bash
PYTHONPATH=<pyobjc 目錄> python tests/test_mac_trigger.py
PYTHONPATH=<pyobjc 目錄> python tests/test_mac_keys.py
PYTHONPATH=<pyobjc 目錄> python tests/test_mac_e2e.py
python tests/test_mac_ui_contract.py
python tests/test_mac_api_contract.py
```

- [ ] **Step 7：commit**

```bash
git add app/mac_vibetalkie.py
git commit -m "refactor(mac): MacPttDaemon 改用共用的 trigger.py，刪掉自己那一份狀態機"
```

---

## Phase F — `ui_server.py` 的 `Status` / `do_POST` 對齊

**問題**：`app/ui/*` 是**兩個平台共用**的，而 mac 的設定頁後端
（`app/core/ui_server.py` 的 `Status`）還是舊欄位 —— 症狀是
「畫面有欄位、後端不認」的**靜默失敗**。

### Task F1：先把契約測試改成「可同時驗兩個平台」

**Files**：Modify `tests/test_status_contract.py`

- [ ] **Step 1：把平台特定部分抽成可重複使用**

```python
def check_snapshot(snap: dict, label: str) -> None:
    """一個 snapshot 物件與 app.js 的欄位契約（兩個平台共用同一份檢查）。"""
    used = sorted(fields_used_by_ui())
    missing = [f for f in used if f not in snap]
    check(f"[{label}] 沒有缺少的欄位", not missing,
          f"缺少 {missing}" if missing else f"{len(used)} 個欄位全部具備")
    import json
    try:
        json.dumps(snap, ensure_ascii=False)
        check(f"[{label}] 可 JSON 序列化", True)
    except Exception as exc:                           # noqa: BLE001
        check(f"[{label}] 可 JSON 序列化", False, f"{type(exc).__name__}: {exc}")
```

- [ ] **Step 2：加第二段：驗 mac 的 `ui_server.Status`**

```python
    print("\n[6] macOS 的 ui_server.Status 也要滿足同一份契約")
    try:
        from ui_server import Status as MacStatus
        mac_snap = MacStatus(Config(), platform="macos").snapshot()
        check_snapshot(mac_snap, "macos")
    except Exception as exc:                           # noqa: BLE001
        check("[macos] snapshot() 可呼叫", False, f"{type(exc).__name__}: {exc}")

    print("\n[7] mac 平台的 mic_warning 必須有內容（§8.7 的硬規則）")
    check("macos 的 mic_warning 不是 None", mac_snap.get("mic_warning") is not None,
          repr(mac_snap.get("mic_warning")))
    check("macos 的 vendor_warning 是 None（不假裝有）",
          mac_snap.get("vendor_warning") is None)
```

⚠️ **注意 §9 原有斷言**：`[5]` 段寫「`mic_warning` 預設 None」——
那是**只對 Windows 成立**的斷言。mac 的 `mic_warning` 一定要有內容
（§8.7：「不講的話，使用者會排了半天順序然後發現完全沒作用」）。
所以 `[5]` 要標明是 Windows 的，不要在 `[6]` 沿用。

- [ ] **Step 3：跑（會失敗，這是預期的）**

```powershell
python tests/test_status_contract.py
```

**期望**：`[6]` 回報缺少 `hotkeys`／`key_enabled`／`hotkeys_label`／
`hotkey_warning`／`key_test` 等欄位。

### Task F2：補 `ui_server.Status` 的欄位

**Files**：Modify `app/core/ui_server.py`

- [ ] **Step 1：`__init__` 加欄位（值一律來自「執行期事實」，不要造假的）**

```python
        # 錄音鍵（多組）—— 與 Windows 版 `vibetalkie.Status` 同一個形狀。
        self.hotkeys_label = ""          # 多組的完整標籤（含停用）
        self.hotkey_warning: str | None = None   # 解析失敗的原因（一路傳到 UI）
        self.key_test: dict | None = None        # 測試模式的狀態
```

- [ ] **Step 2：`snapshot()` 補欄位**

```python
            # 錄音鍵（**多組**）—— 欄位名與 Windows 版一字不差。
            # ⚠️ 兩個平台共用同一份 app.js：欄位名不一樣 = 靜默失敗。
            "hotkeys": list(self.cfg.effective_hotkeys()),
            "key_enabled": list(self.cfg.hotkey_enabled()),
            "hotkey": (self.cfg.effective_hotkeys() or [""])[0],
            "hotkeys_label": self.hotkeys_label,
            "hotkey_label": self.hotkey_label,
            "hotkey_error": self.hotkey_error,
            "hotkey_warning": self.hotkey_warning,
            "key_test": self.key_test,
```

⚠️ `effective_hotkeys()` / `hotkey_enabled()` 是 `config.py` 的方法 ——
**merge 之後 mac 用的就是 main 的 `config.py`**（`origin/mac` 沒改它），
所以這兩個方法在 mac 上一樣存在。**先確認**：

```powershell
python -c "import sys; sys.path[:0]=['app','app/core']; from config import Config; c=Config(); print(c.effective_hotkeys(), c.hotkey_enabled())"
```

- [ ] **Step 3：`_sync_ui()` 也要餵這些欄位（mac 版）**

`mac_vibetalkie.py` 的 `_sync_ui()` 是唯一的同步點，補上：

```python
        bind = self._bindings()
        self.ui.hotkeys_label = bind.label
        self.ui.hotkey_warning = (f"錄音鍵設定有問題：{self._hotkey_error}"
                                 if self._hotkey_error else None)
        self.ui.key_test = self.trigger.test_state()
```

### Task F3：`do_POST` 支援 `hotkeys` 清單

**Files**：Modify `app/core/ui_server.py`

- [ ] **Step 1：把 mac 的 `hotkey` 驗證換成與 Windows 版同一套**

**Windows 的實作在 `app/vibetalkie.py` 的 `do_POST`**（`fc9add6`）。
**不要重寫一份** —— 把那段邏輯抽成 `config.py` 的共用函式，兩邊都呼叫它：

```python
# app/config.py（新增）
def apply_hotkeys_patch(cfg, raw) -> str | None:
    """把 UI 送來的 `hotkeys` 清單套進設定。**回傳錯誤訊息，成功回 None。**

    為什麼放在這裡而不是各自的 HTTP handler：兩個平台（Windows 的
    `vibetalkie.py` 與 mac 的 `ui_server.py`）都要用同一套驗證 ——
    各寫一份就會漂移，而漂移的症狀是「同一筆設定在 Windows 存得進去、
    在 mac 被拒」，使用者完全無法理解。

    驗證規則（與 main `fc9add6` 完全相同）：
      · 每一筆都要 `hotkey.parse_binding()` 過得去（開始／結束／行為都驗）
      · 去重比**解析後**的規格（`F9` 與 `f9` 是同一組）
      · 空清單是**拒絕**（不是「等於沒設」）
    """
    if not isinstance(raw, list):
        return "hotkeys 必須是陣列（例如 [\"RightCtrl\", \"F9,Escape\"]）"
    keys: list[str] = []
    seen: list = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            spec, _sm, _end, _em = hotkey.parse_binding(text, cfg.trigger_mode)
        except hotkey.HotkeyError as exc:
            return f"錄音鍵無法解析：{exc}"
        if any(spec == s for s in seen):
            continue
        seen.append(spec)
        keys.append(text)
    if not keys:
        return "至少要有一個錄音鍵"
    cfg.hotkeys = keys
    cfg.hotkey = start_key_text(keys[0])       # 舊欄位只留開始鍵
    return None
```

⚠️ `start_key_text()` 目前在 `app/vibetalkie.py`（模組層函式）。
**搬到 `config.py`** 並讓 `vibetalkie.py` 改為 `from config import start_key_text`
（或直接刪掉舊的、只留一份）—— 兩份的漂移症狀是「舊欄位存成 `F9@double`」，
`vibetalkie.py` 的 docstring 已經記錄過這個 bug。

- [ ] **Step 2：`ui_server.py` 的 `do_POST` 改用它**

```python
            # 錄音鍵：**多組**，驗證與 Windows 版共用（見 config.apply_hotkeys_patch）
            if "hotkeys" in patch or "hotkey" in patch:
                raw = patch.get("hotkeys", None)
                if raw is None:
                    one = str(patch.get("hotkey") or "").strip()
                    raw = [one] if one else []
                err = config_module.apply_hotkeys_patch(cfg, raw)
                if err:
                    return self._json({"error": err}, 400)
                bind = hotkey_mod.bindings(list(cfg.effective_hotkeys()))
                status.hotkey_label = bind.label
                status.hotkey_error = None
```

- [ ] **Step 3：`_main_key_from_spec()` 整段刪掉**

交接文件 §4 說「`keys.py` 就是為了消掉這一段而存在的」。
Phase B 之後 `hotkey.py` 自己就講 canonical 名稱，這段翻譯層**沒有存在必要**：

```powershell
Select-String -Path app\core\ui_server.py,app\mac_vibetalkie.py -Pattern "_main_key_from_spec|_main_key_name"
```

兩個都刪（`mac_vibetalkie.py` 的 `_main_key_name()` 也一樣）。

- [ ] **Step 4：跑**

```powershell
python tests/test_status_contract.py
python tests/test_config_api.py
python tests/test_mac_ui_contract.py
python tests/test_mac_api_contract.py
```

- [ ] **Step 5：commit**

```powershell
git add app/core/ui_server.py app/config.py app/vibetalkie.py app/mac_vibetalkie.py tests/test_status_contract.py
git commit -m "fix(ui): 兩個平台的 Status 欄位契約對齊（hotkeys/key_enabled/key_test）"
```

---

## Phase G — 測試盤點：哪些能在 Windows 跑、哪些一定要 Mac

### Task G1：把「純邏輯」的 mac 測試改成不需要 pyobjc

**Files**：Modify `tests/test_mac_trigger.py`、`tests/test_mac_keys.py`

- [ ] **Step 1：`test_mac_trigger.py` 拿掉「沒有 Quartz 就跳過」的閘門**

現況：

```python
    try:
        import Quartz  # noqa: F401
    except ImportError as exc:
        print(f"\n  ❌ 需要 pyobjc：{exc}")
        return 2
```

那段會讓這支測試**在 Windows 上完全無法執行**，但它測的東西
（hold/toggle/double、設定熱重載）**一行 pyobjc 都不需要** ——
`mac_vibetalkie.py` 的狀態機路徑不碰原生 API（實測載入成功）。

改成：

```python
    # ⚠️ 這支測試**不需要 pyobjc** —— 它只測狀態機。
    #    `mac_vibetalkie.py` 與三個 mac 模組都刻意不在頂層 import 原生 API
    #    （見 tests/test_mac_import.py），所以任何平台都能跑。
    #    真的需要原生的測試是 test_mac_e2e.py / test_mac_devices.py。
```

- [ ] **Step 2：補上新行為的測試（**這是驗收條件 5 的核心**）**

`test_mac_trigger.py` 要覆蓋 `trigger.py` 的每一項新行為（mac 端的入口）：

| 測試 | 內容 |
|---|---|
| 多組 | `cfg.hotkeys = ["F9", "RightCtrl"]` → 兩顆都能觸發 |
| 配對 | `cfg.hotkeys = ["F9,Esc"]` → 按 F9 開始、**放開 F9 不停**、按 Esc 停 |
| 雙邊行為 | `cfg.hotkeys = ["F9,Esc@toggle"]` → Esc 一按下就停 |
| 停用 | `cfg.hotkeys = ["~F9", "F8"]` → F9 完全沒反應、F8 照常 |
| 測試模式 | `d.trigger.start_key_test(5, "")` → 按鍵只回報，`FakeCapture.opened == 0` |
| 側別 | `cfg.hotkeys = ["LeftCtrl"]` → 送 `key="rightctrl"` **不**觸發 |

⚠️ **`FakeCapture.opened` 是最有力的斷言** —— 它直接證明「有沒有真的開麥克風」，
比看 `d.state` 更接近使用者的實際體驗。

- [ ] **Step 3：跑**

```powershell
python tests/test_mac_trigger.py
```

**期望**：這台 Windows 上就能全綠。

- [ ] **Step 4：commit**

```powershell
git add tests/test_mac_trigger.py
git commit -m "test(mac): test_mac_trigger 改成不需 pyobjc，並覆蓋多組／配對／停用／測試模式"
```

### Task G2：mac 專屬測試的分流與說明

- [ ] **Step 1：確認哪些一定要 Mac**

| 測試 | 需要 macOS | 原因 |
|---|---|---|
| `test_mac_keys.py` | ⚠️ 需要 pyobjc（但 `keys.py` 本身可測） | Carbon keycode 表在 mac 上才有意義 |
| `test_mac_devices.py` | ✅ 需要 | 解析 `system_profiler` 輸出 |
| `test_mac_e2e.py` | ✅ 需要 | 真 CGEventTap + AVAudioEngine |
| `test_mac_ui_contract.py` | ❌ | 純標準函式庫 |
| `test_mac_api_contract.py` | ❌ | 純標準函式庫 |
| `test_mac_trigger.py` | ❌（Phase G1 之後） | 只測狀態機 |
| `test_mac_import.py` | ❌ | 這支就是為了「不需 pyobjc」而寫的 |

- [ ] **Step 2：把分流寫進 `AGENTS.md` §9**（Phase H 一起做）

---

## Phase H — 文件與收尾

### Task H1：`AGENTS.md` 合併後的補充

- [ ] **Step 1：§4 目錄結構補上新檔案**

```
│  │  ├─ trigger.py      #   ★ 平台無關的觸發狀態機（兩個平台共用）
```

- [ ] **Step 2：§5 模組邊界表補一列**

| 模組 | 職責 | 不做 |
|---|---|---|
| `trigger` | 按鍵事件 → 開始／停止的決策（多組／雙邊行為／停用／測試模式） | 不碰音訊、不碰平台 API |

- [ ] **Step 3：§9 測試清單補上新測試**

```powershell
python tests/test_hotkey_names.py     # canonical 名稱契約（名稱／側別／鍵碼）
python tests/test_trigger_engine.py   # 觸發引擎（平台無關，直接餵 Event）
python tests/test_mac_import.py       # mac 模組可載入性（不需 pyobjc）
python tests/test_mac_trigger.py      # mac 的觸發行為（不需 pyobjc）
```

### Task H2：刪掉暫時的交接文件

- [ ] **Step 1：確認驗收條件全部達成之後**

```powershell
Remove-Item docs/handoff/main-mac-integration.md
Remove-Item docs/handoff/main-mac-integration-plan.md
```

（交接文件 §7 自己寫的：「整合完成後應刪除」。**若還沒全部達成就留著。**）

- [ ] **Step 2：commit**

---

## 3. 驗收條件（對應交接文件 §8）

| # | 條件 | 怎麼驗 | 這台 Windows 驗得到嗎 |
|---|---|---|---|
| 1 | `python tests/test_trigger.py` 全過，`[7]`–`[19]` 沒有被刪減 | `git diff --stat tests/test_trigger.py` 應為空 | ✅ |
| 2 | `test_status_contract.py` 對**兩個平台**都成立 | Phase F 的 `[6]`／`[7]` 段 | ✅ |
| 3 | `app/ui/*` 每個欄位在 mac 後端都能存能讀（尤其 `hotkeys`） | `test_mac_ui_contract.py` ＋ `test_mac_api_contract.py` | ✅（純標準函式庫） |
| 4 | Windows 行為與 `fc9add6` 相同 | Phase D Task D2 的 baseline 比對 | ✅ |
| 5 | `origin/mac` 原本的 9 支 mac 測試仍全過 | 在 Mac 上跑（見 §4） | ⚠️ 部分（`test_mac_trigger`／`ui_contract`／`api_contract`／`import` 可） |
| 6 | `AGENTS.md` 的 §8.2 規則 4 與 §8.7 **兩節都在** | `Select-String -Path AGENTS.md -Pattern "規則 4|§8.7|8\.7 macOS"` | ✅ |

---

## 4. 這台機器上**做不到**的事（要請使用者在 Mac 上做）

| 項目 | 為什麼 | 請使用者執行 |
|---|---|---|
| `test_mac_e2e.py` | 真 CGEventTap + AVAudioEngine | `PYTHONPATH=<pyobjc> python tests/test_mac_e2e.py` |
| `test_mac_devices.py` | `system_profiler` 輸出 | `python tests/test_mac_devices.py` |
| `test_mac_keys.py` | Carbon keycode 表 | `PYTHONPATH=<pyobjc> python tests/test_mac_keys.py` |
| mac 的「第一次按下有沒有音訊」 | main 的修法是 winmm 專屬 | 把 `tools/p1/measure_standby_audio.py` 的邏輯照抄成 CoreAudio 版再量 |
| 真實按住麥克風錄音鍵 → 文字出現 | 需要硬體 | `python3 app/mac_vibetalkie.py` 手動測 |

⚠️ **`mac_vibetalkie.py` 在 Windows 上「載入得動」不等於「跑得起來」。**
它一碰到 `Quartz` 就會 `ImportError`。所以 Phase E 的驗收**只保證介面一致
與狀態機正確**，原生行為要 Mac 實測。

---

## 5. 風險與對策

| 風險 | 對策 |
|---|---|
| `hotkey.py` 改名中心之後，某處仍在讀 `spec.vk` 而拿到 0 | Phase B Step 1 的 `test_hotkey_names.py` `[2]` 直接釘住 `vk` 的值；`Select-String -Pattern "\.vk\b"` 全 repo 掃一次 |
| `trigger.py` 偷渡平台相依 | `tests/test_mac_import.py` `[3]` 用 AST 掃 import |
| 兩份 `Status` 又漂移 | `test_status_contract.py` 同時驗兩個平台（Phase F1） |
| mac 端的原生行為沒人測過就上線 | Phase E 明確標示「只保證介面＋狀態機」，實機驗證列在 §4 |
| `_mods_down` 的大小寫不一致（`Ctrl` vs `ctrl`） | Phase B 統一成小寫，`test_hotkey_names.py` `[4]` 釘住 |
| 重構把「暖機待命＋前捲」弄壞 | 那段**完全不在本計畫的搬移範圍**（它是 winmm 專屬，留在 `ptt.py`）；`test_trigger.py` `[20]` 與 `test_mic_stream.py` 是守門員 |
| 一次改太多、壞了不知道是哪一步 | 每個 Phase 一個 commit，且每個 Phase 都有「跑測試」步驟 |

---

## 6. 指令備忘

```powershell
# 起點
git status --short ; git log --oneline -1

# merge 的衝突面
git merge origin/mac
git diff --name-only --diff-filter=U

# 每個 Phase 之後
python tests/test_trigger.py
python tests/test_status_contract.py

# 共用層有沒有偷渡平台相依
Select-String -Path app\core\trigger.py,app\core\hotkey.py -Pattern "ctypes|windll|Quartz|WM_|RAWKEYBOARD"

# 誰還在讀 spec.vk
Select-String -Path app\*.py,app\core\*.py,tests\*.py -Pattern "\.vk\b"

# 兩邊分支的差異（不動工作區）
git diff --stat 06d01ab origin/main
git diff --stat 06d01ab origin/mac
```

⚠️ 這台機器上 `git fetch`／`git push` 需要**放寬沙箱權限**才會成功
（`schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS` ——
那是環境問題，不是沒憑證）。**推送前必須逐次取得使用者同意。**
