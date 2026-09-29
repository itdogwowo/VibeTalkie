#!/usr/bin/env python3
"""`config.apply_hotkeys_patch()` 的契約測試 —— 兩個平台共用的那一份驗證。

## 為什麼要單獨測它

它是 **Windows 的 `vibetalkie.py` 與 mac 的 `ui_server.py` 共用的唯一一份**
錄音鍵驗證。兩邊各寫一份會漂移，而漂移的症狀是
「同一筆設定在 Windows 存得進去、在 mac 被拒」—— 使用者完全無法理解。

所以它值得有自己的測試：驗的不是 HTTP，而是「什麼算合法、什麼算重複、
什麼必須被拒絕」。HTTP 那兩層只負責把錯誤訊息轉成 400。

執行：python tests/test_hotkeys_patch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import config as config_module  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def apply(raw):
    """套用一筆 patch，回傳 `(cfg, err)`。"""
    cfg = config_module.Config()
    return cfg, config_module.apply_hotkeys_patch(cfg, raw)


def main() -> int:
    print("=" * 68)
    print("錄音鍵 patch 驗證（兩個平台共用）")
    print("=" * 68)

    print("\n[1] 合法：一組、多組、配對、兩邊各自的行為")
    for raw, want in ((["RightCtrl"], ["RightCtrl"]),
                      (["RightCtrl", "F9"], ["RightCtrl", "F9"]),
                      (["F9,Esc"], ["F9,Esc"]),
                      (["F9,Esc@toggle"], ["F9,Esc@toggle"]),
                      (["Ctrl+Alt+R@double,Esc"], ["Ctrl+Alt+R@double,Esc"])):
        cfg, err = apply(raw)
        check(f"{raw} 可存", err is None and cfg.hotkeys == want,
              f"err={err} hotkeys={cfg.hotkeys}")

    print("\n[2] 同一顆開始鍵、**不同收尾方式**是合法的兩組（不可去重掉）")
    # ⚠️ 這一條抓的是真實的 bug：去重原本只比對「開始鍵的規格」，
    #    於是 `F9@double` 與 `F9,Esc@toggle` 被當成重複 —— 第二筆**靜默消失**。
    #    使用者的感受是「我設了兩組，存完只剩一組」，而且完全沒有錯誤訊息。
    #    實際踩到的情境：`["F9@double", "F9,Esc@toggle"]` 只留下前者。
    cfg, err = apply(["F9@double", "F9,Esc@toggle"])
    check("兩筆都在", err is None and cfg.hotkeys == ["F9@double", "F9,Esc@toggle"],
          f"err={err} hotkeys={cfg.hotkeys}")

    cfg, err = apply(["F9", "F9,Esc"])
    check("只有結束鍵不同 → 兩筆都在",
          err is None and cfg.hotkeys == ["F9", "F9,Esc"], str(cfg.hotkeys))

    cfg, err = apply(["F9@double", "F9@toggle"])
    check("只有開始行為不同 → 兩筆都在",
          err is None and cfg.hotkeys == ["F9@double", "F9@toggle"], str(cfg.hotkeys))

    cfg, err = apply(["F9,Esc", "F9,Space"])
    check("不同結束鍵 → 兩筆都在",
          err is None and cfg.hotkeys == ["F9,Esc", "F9,Space"], str(cfg.hotkeys))

    print("\n[3] 真正重複的寫法要去重（大小寫、分隔符、修飾鍵順序）")
    for raw in (["F9", "f9"], ["F9", "F9"], ["RightCtrl", "rightctrl"],
                ["Ctrl+Alt+R", "ctrl-alt-r"], ["Ctrl+Alt+R", "Alt+Ctrl+R"]):
        cfg, err = apply(raw)
        check(f"{raw} → 只留一組", err is None and len(cfg.hotkeys) == 1,
              f"err={err} hotkeys={cfg.hotkeys}")
    cfg, err = apply(["F9@double", "f9@double"])
    check("模式相同才算重複（F9@double 與 f9@double）",
          err is None and cfg.hotkeys == ["F9@double"], str(cfg.hotkeys))

    print("\n[3b] 停用與啟用**不是**重複（`~F9` 與 `F9,Esc` 是兩件事）")
    # ⚠️ 少了 `enabled` 這一項，這兩筆會被合併成「兩組都停用」——
    #    使用者明明啟用了一組，卻按什麼都沒反應，而畫面看起來都正常。
    cfg, err = apply(["~F9", "F9,Esc"])
    check("兩筆都在", err is None and cfg.hotkeys == ["~F9", "F9,Esc"],
          f"err={err} hotkeys={cfg.hotkeys}")
    check("啟用狀態也對", cfg.hotkey_enabled() == [False, True],
          str(cfg.hotkey_enabled()))
    cfg, err = apply(["~F9", "~f9"])
    check("兩筆都停用且同義 → 仍然去重", err is None and cfg.hotkeys == ["~F9"],
          str(cfg.hotkeys))

    print("\n[4] 必須拒絕（不可靜默接受）")
    for raw, why in ((["F9,Banana"], "結束鍵打錯"),
                     (["F9@dboule"], "行為打錯"),
                     (["Banana"], "開始鍵打錯"),
                     ([], "空清單"),
                     (["   ", ""], "只有空白"),
                     ("F9", "不是陣列")):
        cfg, err = apply(raw)
        check(f"拒絕 {why}", err is not None, f"竟然通過了：{cfg.hotkeys}")

    print("\n[5] 舊欄位 `hotkey` 只留開始鍵（不含模式與結束鍵）")
    cfg, err = apply(["F9,Esc@toggle"])
    check("F9,Esc@toggle → hotkey='F9'", cfg.hotkey == "F9", repr(cfg.hotkey))
    cfg, err = apply(["F9@double"])
    check("F9@double → hotkey='F9'", cfg.hotkey == "F9", repr(cfg.hotkey))
    cfg, err = apply(["Ctrl+Alt+R,Esc@toggle"])
    check("組合鍵的開始鍵要保留 `+`", cfg.hotkey == "Ctrl+Alt+R", repr(cfg.hotkey))

    print("\n[6] 舊設定檔（只有 hotkey、沒有 hotkeys）仍然可用")
    cfg = config_module.Config()
    cfg.hotkey = "F9"
    cfg.hotkeys = []
    check("effective_hotkeys() 退回舊欄位", cfg.effective_hotkeys() == ["F9"],
          str(cfg.effective_hotkeys()))

    print("\n[7] round-trip：存進去的東西要能原樣讀回來（`hotkey.bindings()`）")
    # ⚠️ 這兩層（HTTP 驗證 / daemon 解析）都各自做了一次去重，而它們
    #    **必須一致** —— 不一致的症狀是「UI 存得進去，重啟後少一組」，
    #    因為 daemon 是在**重啟之後**才用 `hotkey.bindings()` 解析的。
    import hotkey as hk
    for raw in (["F9@double", "F9,Esc@toggle"],
                ["F9", "F9,Esc", "F9@toggle"],
                ["~F9", "F9,Esc"],
                ["RightCtrl", "F9,Esc", "~F8"]):
        cfg, err = apply(raw)
        b, berr = hk.bindings(list(cfg.hotkeys))
        check(f"{raw} → entries 原樣寫回",
              err is None and berr is None and b.entries == raw,
              f"err={err} berr={berr} entries={b.entries}")

    print("\n[8] 同一顆開始鍵的兩組，要能被分別看待（`Binding` 的存在理由）")
    # ⚠️ 這一條抓的是真實的 bug：`HotkeyBindings` 原本用
    #    `self.specs.index(spec)` 查值，一旦同一顆鍵出現兩次，
    #    `.index()` **永遠回第一筆** → 第二筆的結束鍵、行為、啟用狀態
    #    全部讀成第一筆的值。症狀：`["~F9", "F9,Esc"]` 的 `entries`
    #    寫回去變成兩筆都停用（＝存一次設定就把第二組關掉）。
    b, err = hk.bindings(["~F9", "F9,Esc"])
    check("兩組都在", len(b.specs) == 2, str(len(b.specs)))
    check("啟用狀態各自獨立", b.enabled == (False, True), str(b.enabled))
    check("entries 只有第一筆帶 `~`", b.entries == ["~F9", "F9,Esc"], str(b.entries))
    check("active 只有啟用那一組",
          [hk.spec_text(s) for s in b.active] == ["F9"],
          str([hk.spec_text(s) for s in b.active]))

    b, _ = hk.bindings(["F9@double", "F9,Esc@toggle"])
    check("兩列的標籤要不一樣（UI 才看得出差別）",
          len(b.labels) == 2 and b.labels[0] != b.labels[1], str(b.labels))

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
