#!/usr/bin/env python3
"""macOS 平台層的測試：鍵名對照、按鍵監聽邏輯、熱鍵比對。

## 為什麼要測這個

mac 版有四個新模組，其中三個碰原生 API（CGEventTap / AVAudioEngine /
NSPasteboard）。原生 API 的部分需要真實權限與硬體，沒辦法在 CI 跑 ——
但**底下的決策邏輯可以**，而那正是最容易寫錯的部分：

  · 鍵名 ↔ keycode 對照錯了 → 症狀是「設定打得進去，按了沒反應」
  · 「限定右側」的處理錯了 → 症狀是「按左 Ctrl 也會錄音」
  · 修飾鍵集合比對錯了   → 症狀是「Ctrl+Alt+R 永遠不觸發」

這些都不會拋錯，只會「行為怪怪的」——所以用測試釘住。

## 怎麼測

不需要 pyobjc：`keys.py` 是純資料，`_spec_matches()` 是純邏輯。
`mac_keylistener` 的 callback 邏輯（修飾鍵狀態機、自動重複）也用
假事件驗證，不需真的建立 CGEventTap。

執行：python tests/test_mac_keys.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))

import hotkey as hotkey_mod      # noqa: E402
import keys as keys_mod          # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def skip(name: str, why: str) -> None:
    print(f"  ⏭  {name}  — {why}")


IS_MAC = keys_mod.is_macos()


# ---------------------------------------------------------------- 鍵名對照

def test_key_tables() -> None:
    print("\n▍鍵名 ↔ 鍵碼對照")

    # 反向一致性：每個 keycode 都要能反查回一個「查得到同一個 keycode」的名字。
    # ⚠️ 這一條抓的是「表裡有但反查不到」——那種 bug 的症狀是
    #    「UI 列得出來、使用者選了、按了沒反應」。
    if IS_MAC:
        pairs = list(keys_mod._MACOS_KEYCODE_TO_NAME.items())   # noqa: SLF001
    else:
        n2v, _ = keys_mod._windows_tables()                     # noqa: SLF001
        pairs = list(n2v.items())

    bad: list[str] = []
    for code, name in pairs:
        try:
            back = keys_mod.name_to_code(name)
        except keys_mod.UnknownKey:
            bad.append(f"{name}（keycode {code}）查不回鍵碼")
            continue
        if IS_MAC and back != code:
            bad.append(f"{name}: {code} → {back}")
    check("每個鍵碼都能反查回一致的名稱", not bad,
          f"{len(pairs)} 項" + (f"，{len(bad)} 項不一致：{bad[:3]}" if bad else ""))

    # 常用鍵必須都在（這些是 COMMON_KEYS 與預設值的成員）
    must = ["ctrl", "rightctrl", "shift", "alt", "win", "space", "enter",
            "esc", "tab", "f9", "f10", "f8", "up", "down", "left", "right"]
    missing = [n for n in must if n not in keys_mod.all_names()]
    check("常用鍵都在表裡", not missing, f"缺 {missing}" if missing else "")

    # 數字與字母
    for n in list("abcdefghijklmnopqrstuvwxyz") + list("0123456789"):
        try:
            keys_mod.name_to_code(n)
        except keys_mod.UnknownKey:
            missing.append(n)
    check("a-z 與 0-9 都可查", not any(
        n in missing for n in "abcdefghijklmnopqrstuvwxyz0123456789"))


def test_right_left_distinction() -> None:
    print("\n▍左右修飾鍵（AGENTS.md §8.2 規則 2 的 mac 對應）")

    if not IS_MAC:
        skip("左右鍵碼不同", "這個檢查針對 macOS 的 keycode 佈局")
        return

    # macOS 用**不同 keycode** 區分左右（Windows 用 E0 旗標）。
    # 若這兩個相同，就代表「按實體鍵盤的左 Ctrl 也會觸發錄音」——
    # 也就是 Windows 版花力氣擋掉的那個誤觸。
    lc = keys_mod.name_to_code("LeftCtrl")
    rc = keys_mod.name_to_code("RightCtrl")
    check("左 Ctrl 與右 Ctrl 是不同鍵碼", lc != rc, f"左={lc} 右={rc}")

    ls = keys_mod.name_to_code("LeftShift")
    rs = keys_mod.name_to_code("RightShift")
    check("左 Shift 與右 Shift 是不同鍵碼", ls != rs, f"左={ls} 右={rs}")


def test_modifier_flags() -> None:
    print("\n▍修飾鍵 flag（判斷 FlagsChanged 是按下還是放開）")

    if not IS_MAC:
        skip("修飾鍵 flag", "macOS 專屬")
        return

    for name in ("ctrl", "shift", "alt", "win"):
        f = keys_mod.modifier_flag(name)
        check(f"{name} 有 flag", f is not None and f > 0,
              f"0x{f:08X}" if f else "None")

    # Ctrl 與 Shift 不可以共用同一個位元 —— 共用的話就分不出按了哪一顆
    flags = {keys_mod.modifier_flag(n) for n in ("ctrl", "shift", "alt", "win")}
    check("四個修飾鍵的 flag 互不相同", len(flags) == 4, str(len(flags)))


def test_generic_modifier() -> None:
    print("\n▍修飾鍵通用化（spec 用 Ctrl，事件用 rightctrl）")

    cases = {"rightctrl": "ctrl", "leftctrl": "ctrl", "rightshift": "shift",
             "leftalt": "alt", "f9": "f9", "space": "space"}
    bad = {k: (keys_mod.generic_modifier(k), v) for k, v in cases.items()
           if keys_mod.generic_modifier(k) != v}
    check("左右寫法收斂成通用名", not bad, str(bad) if bad else "")

    for n in ("Ctrl", "Shift", "Alt", "Win"):
        check(f"is_modifier({n})", keys_mod.is_modifier(n))
    for n in ("F9", "Space", "R"):
        check(f"is_modifier({n}) 為 False", not keys_mod.is_modifier(n))


# ---------------------------------------------------------------- 熱鍵比對

class FakeDaemon:
    """借用常駐程式的 `_spec_matches`，但不需要真的載入引擎或原生模組。"""

    def __init__(self, hotkey: str):
        self._hotkey = hotkey
        self._mods_down: set[str] = set()

    def spec(self):
        return hotkey_mod.parse_or_default(self._hotkey)[0]


def _matches(hotkey: str, mods: set[str], key: str) -> bool:
    """把 spec_matches 的邏輯獨立測（不 import mac_vibetalkie，避免拉進 pyobjc）。"""
    spec = hotkey_mod.parse_or_default(hotkey)[0]
    name = (hotkey_mod.VK_TO_NAME.get(spec.vk) or "").lower()
    if spec.require_e0 and name in ("ctrl", "shift", "alt"):
        name = "right" + name
    if key != name:
        return False
    want = {m.lower() for m in spec.modifiers}
    have = {keys_mod.generic_modifier(k) for k in mods}
    if keys_mod.is_modifier(name):
        have.discard(keys_mod.generic_modifier(name))
    return want == have


def test_spec_matching() -> None:
    print("\n▍熱鍵比對（這是最容易寫錯的地方）")

    # 預設：單獨右 Ctrl
    check("RightCtrl 設定：按 rightctrl 要中", _matches("RightCtrl", set(), "rightctrl"))
    check("RightCtrl 設定：按 ctrl（左）**不該**中",
          not _matches("RightCtrl", set(), "ctrl"),
          "若中了代表「限定右側」失效 → 按實體鍵盤左 Ctrl 也會錄音")

    # 單獨修飾鍵的關鍵：_mods_down 裡有自己，不可以讓它擋掉比對
    check("RightCtrl 設定：自己按住的狀態下仍要中",
          _matches("RightCtrl", {"rightctrl"}, "rightctrl"),
          "修飾鍵主鍵要從『目前按住的修飾鍵』排除，否則永遠不吻合")

    # 功能鍵：完全脫離修飾鍵
    check("F9 設定：按 f9 要中", _matches("F9", set(), "f9"))
    check("F9 設定：按 rightctrl 不中", not _matches("F9", set(), "rightctrl"))
    check("F9 設定：按住 Ctrl 再按 f9 不中（規格沒要求修飾鍵）",
          not _matches("F9", {"ctrl"}, "f9"))

    # 組合鍵
    check("Ctrl+Alt+R：修飾鍵齊全才中",
          _matches("Ctrl+Alt+R", {"ctrl", "alt"}, "r"))
    check("Ctrl+Alt+R：順序不影響（alt 先按也中）",
          _matches("Ctrl+Alt+R", {"alt", "ctrl"}, "r"),
          "modifiers 是集合，不是序列")
    check("Ctrl+Alt+R：少一個修飾鍵不中",
          not _matches("Ctrl+Alt+R", {"ctrl"}, "r"))
    check("Ctrl+Alt+R：多一個修飾鍵不中",
          not _matches("Ctrl+Alt+R", {"ctrl", "alt", "shift"}, "r"))

    # 左 Ctrl 搭配組合鍵時，用通用名比對
    check("Ctrl+Alt+R：用左 Ctrl 也要中",
          _matches("Ctrl+Alt+R", {"ctrl", "alt"}, "r"))


# ---------------------------------------------------------------- 監聽邏輯

def test_listener_logic() -> None:
    print("\n▍按鍵監聽的事件邏輯（不需要真的 CGEventTap）")

    try:
        import mac_keylistener as mk
    except ImportError as exc:
        skip("監聽邏輯", f"載入不到模組：{exc}")
        return

    # 修飾鍵狀態機：同一個 keycode 按下與放開都送 FlagsChanged，
    # 只能靠 flags 的位元變化判斷 —— 這裡驗證那個判斷。
    L = mk.MacKeyListener(lambda e: None)
    ev = L._on_flags_changed("rightctrl", mk.FLAG_CONTROL)        # noqa: SLF001
    check("flags 升起 → down=True", ev is not None and ev.down, str(ev))

    ev = L._on_flags_changed("rightctrl", mk.FLAG_CONTROL)        # noqa: SLF001
    check("同樣的 flags 再送 → 不重複觸發", ev is None,
          "否則按住 Ctrl 會一直重複開始錄音")

    ev = L._on_flags_changed("rightctrl", 0)                      # noqa: SLF001
    check("flags 落下 → down=False", ev is not None and not ev.down, str(ev))

    ev = L._on_flags_changed("f9", 0)                             # noqa: SLF001
    check("非修飾鍵走 flags 路徑 → 忽略", ev is None)

    # 自動重複：按住不放會一直送 DOWN，必須標記出來讓上層忽略
    L2 = mk.MacKeyListener(lambda e: None)
    first = L2._on_key("f9", True)                                # noqa: SLF001
    second = L2._on_key("f9", True)                               # noqa: SLF001
    third = L2._on_key("f9", True)                                # noqa: SLF001
    up = L2._on_key("f9", False)                                  # noqa: SLF001
    check("第一次按下不是重複", first is not None and not first.autorepeat)
    check("之後的按下標記為重複", second.autorepeat and third.autorepeat)
    check("放開後重設（下次按下不算重複）",
          up is not None and not up.down
          and not L2._on_key("f9", True).autorepeat)              # noqa: SLF001

    # KeyEvent 是平台無關的接縫 —— device 在 mac 上恆為 None
    check("mac 事件的 device 為 None（不做裝置辨識）",
          first.device is None)


def test_keyevent_shape() -> None:
    print("\n▍KeyEvent 結構（跨平台接縫）")

    try:
        import mac_keylistener as mk
    except ImportError as exc:
        skip("KeyEvent", f"載入不到模組：{exc}")
        return

    ev = mk.KeyEvent(key="f9", down=True)
    check("必要欄位有預設值", ev.autorepeat is False and ev.device is None)
    check("是 frozen（不可變）", getattr(type(ev), "__dataclass_params__").frozen)


def main() -> int:
    print("=" * 74)
    print("macOS 平台層測試")
    print("=" * 74)
    print(f"  平台：{'macOS' if IS_MAC else sys.platform}"
          f"　Python {sys.version.split()[0]}")

    test_key_tables()
    test_right_left_distinction()
    test_modifier_flags()
    test_generic_modifier()
    test_spec_matching()
    test_listener_logic()
    test_keyevent_shape()

    print("\n" + "=" * 74)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
