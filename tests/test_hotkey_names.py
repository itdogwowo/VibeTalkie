#!/usr/bin/env python3
"""`hotkey.py` 的 canonical 名稱契約：名稱、側別、平台鍵碼三者要對得起來。

## 為什麼要測這個

`hotkey.py` 原本把按鍵存成 **Windows VK 碼**（`vk=0xA3`＝RightCtrl）。
macOS 沒有 VK 碼，CGEventTap 送的是 Carbon keycode —— 所以內部表示必須
改成**平台無關的名稱**，VK 只留在 Windows 平台層。

不這樣做的後果很具體：同一個 `HotkeySpec` 的 `vk` 欄位在兩個平台會裝
兩種完全不同的數字（F9 = `0x78` / `0x65`），而比對邏輯依賴 VK 家族
（`{0x11, 0xA2, 0xA3}` 是同一顆 Ctrl）→ 在 mac 上必然全錯。

這支測試釘住四件事：

  1. `HotkeySpec` 的內部表示是**名稱**（`"ctrl"`／`"f9"`），不是鍵碼
  2. 相等性只看（名稱、側別、修飾鍵）—— 與寫法無關
  3. `spec.vk` 是**推導出來的**（給 Windows 平台層用）
  4. 側別是三態：`None`＝兩側都算、`"left"`／`"right"`＝限定該側

第 4 點是實測踩過的 bug：`LeftCtrl` 曾經左右不分，按右 Ctrl 也會觸發。

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

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

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
    check("name 是 canonical 名稱", getattr(s, "name", None) == "ctrl", repr(getattr(s, "name", None)))
    check("side 是 'right'", s.side == "right", repr(s.side))
    check("side_name 帶側別前綴", getattr(s, "side_name", None) == "rightctrl",
          repr(getattr(s, "side_name", None)))
    f9 = hk.parse("F9")
    check("F9 → name='f9' side=None", (getattr(f9, "name", None), f9.side) == ("f9", None),
          f"{getattr(f9, 'name', None)}/{f9.side}")

    print("\n[2] spec.vk 是推導出來的（Windows 平台層用）")
    # ⚠️ 修飾鍵一律推導成**通用** VK（`0x11`）不是筆誤：Raw Input 送的就是
    #    通用 VK ＋ E0 旗標，不是 VK_RCONTROL(0xA3)。側別交給 `side`。
    #    專用 VK 由 `_vk_to_name_side()` 反查回名稱與側別，所以兩者都命中
    #    （見 [6] 的 `VK 形式（0xA3）`）。
    check("RightCtrl → 0x11（通用 VK，靠 side 分左右）", s.vk == 0x11, hex(s.vk))
    check("F9 → 0x78", f9.vk == 0x78, hex(f9.vk))
    check("LeftCtrl → 0x11（同樣是通用 VK）", hk.parse("LeftCtrl").vk == 0x11,
          hex(hk.parse("LeftCtrl").vk))
    check("組合鍵 Ctrl+Alt+R → 0x52", hk.parse("Ctrl+Alt+R").vk == 0x52,
          hex(hk.parse("Ctrl+Alt+R").vk))

    print("\n[2b] 專用 VK 反查得回名稱與側別（Windows 事件可能是專用 VK）")
    check("0xA2 → (ctrl, left)", hk._vk_to_name_side(0xA2) == ("ctrl", "left"),
          str(hk._vk_to_name_side(0xA2)))
    check("0xA3 → (ctrl, right)", hk._vk_to_name_side(0xA3) == ("ctrl", "right"),
          str(hk._vk_to_name_side(0xA3)))
    check("0x11 → (ctrl, None)（通用）", hk._vk_to_name_side(0x11) == ("ctrl", None),
          str(hk._vk_to_name_side(0x11)))
    check("不認得的鍵碼 → 空名稱（不猜）", hk._vk_to_name_side(0xFE) == ("", None),
          str(hk._vk_to_name_side(0xFE)))

    print("\n[3] 相等性只看（名稱、側別、修飾鍵）")
    check("大小寫不影響", hk.parse("f9") == hk.parse("F9"))
    check("修飾鍵順序不影響",
          hk.parse("Ctrl+Alt+R") == hk.parse("Alt+Ctrl+R"))
    check("左右不同就是不相等",
          hk.parse("LeftCtrl") != hk.parse("RightCtrl"))
    check("沒寫左右 ≠ 限定左側", hk.parse("Ctrl") != hk.parse("LeftCtrl"))

    print("\n[4] 修飾鍵用 canonical 小寫名稱（跨平台一致）")
    mods = hk.parse("Ctrl+Alt+R").modifiers
    check("Ctrl+Alt+R 的修飾鍵", mods == frozenset({"ctrl", "alt"}), str(sorted(mods)))
    check("MODIFIER_VKS 回傳小寫名稱",
          hk.MODIFIER_VKS[0x11] == "ctrl" and hk.MODIFIER_VKS[0xA3] == "ctrl",
          f"{hk.MODIFIER_VKS.get(0x11)}/{hk.MODIFIER_VKS.get(0xA3)}")

    print("\n[5] 側別解析：旗標只是「提示」，名稱優先")
    check("通用名稱 + e0 → right", hk.side_of("ctrl", None, e0=True) == "right")
    check("通用名稱 + 沒 e0 → left", hk.side_of("ctrl", None, e0=False) == "left")
    check("名稱已帶側別 → 名稱優先", hk.side_of("ctrl", "right", e0=False) == "right")
    check("leftctrl → left（名稱自己說）",
          hk._key_name_and_side("leftctrl") == ("ctrl", "left"),
          str(hk._key_name_and_side("leftctrl")))
    check("F9 沒有側別", hk.side_of("f9", None, e0=True) is None)

    print("\n[6] spec_hit 同時接受 VK 整數與 canonical 名稱")
    rc = hk.parse("RightCtrl")
    check("VK 形式（0x11 + e0）", hk.spec_hit(rc, 0x11, True))
    check("VK 形式（0xA3）", hk.spec_hit(rc, 0xA3, False))
    check("名稱形式", hk.spec_hit(rc, "rightctrl", False))
    check("名稱形式（左邊不命中）", not hk.spec_hit(rc, "ctrl", False))
    check("VK 形式（左邊不命中）", not hk.spec_hit(rc, 0xA2, False))
    check("F9 帶不帶 e0 都是 F9", hk.spec_hit(hk.parse("F9"), 0x78, True))

    print("\n[7] `_key_name_and_side()`：唯一的「說法 → canonical」入口")
    for raw, want in (("RightCtrl", ("ctrl", "right")),
                      ("Ctrl", ("ctrl", None)),
                      ("control", ("ctrl", None)),
                      ("F9", ("f9", None)),
                      ("Escape", ("esc", None)),
                      ("Numpad0", ("numpad0", None))):
        got = hk._key_name_and_side(raw.lower())
        check(f"{raw} → {want}", got == want, str(got))

    print("\n[8] 舊有的解析行為一個都不能變（test_trigger.py [1] 的同義重述）")
    check("require_e0 由 side 推導", hk.parse("RightCtrl").require_e0 is True
          and hk.parse("LeftCtrl").require_e0 is False)
    check("單獨一顆修飾鍵可以當主鍵",
          hk.parse("Ctrl").main_is_modifier is True)
    check("裝置原生鍵的判定",
          hk.parse("RightCtrl").is_native_device_key is True
          and hk.parse("F9").is_native_device_key is False)
    for bad in ("", "Banana", "Ctrl+Alt+R+F9", "Ctrl+Alt"):
        try:
            hk.parse(bad)
            check(f"應拒絕 {bad!r}", False, "竟然通過了")
        except hk.HotkeyError:
            check(f"應拒絕 {bad!r}", True)

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
