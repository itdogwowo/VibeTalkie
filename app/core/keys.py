#!/usr/bin/env python3
"""可攜的按鍵名稱 ↔ 各平台鍵碼對應。

## 為什麼要這一層

`hotkey.py` 原本把「按鍵」直接存成 **Windows 虛擬鍵碼（VK）**：

    HotkeySpec(vk=0xA3, modifiers=frozenset(), require_e0=True)   # RightCtrl

這在只有 Windows 的時候沒問題，但**VK 碼是微軟專屬的**。
macOS 的 CGEventTap 送的是完全另一套 **Carbon keycode**：

    同一個「右 Ctrl」：  Windows VK = 0xA3      macOS keycode = 62

要在 mac 上跑，就必須有一個**平台無關的中介名稱**。本模組提供它：

    名稱（canonical）        Windows VK   macOS keycode
    "RightCtrl"              0xA3         62
    "F9"                     0x78         101
    "R"                      0x52         15

## 設計原則

1. **名稱是唯一的共通語言。** 平台層只負責「我的鍵碼是哪個名稱」與
   「哪個名稱是我的鍵碼」，不碰對方的表。
2. **不硬編一份新的名稱清單。** Windows 的名字沿用 `hotkey._NAME_TO_VK`，
   避免兩份清單各自漂移（那種 bug 症狀是「設定打得進去但按了沒反應」）。
3. **macOS 對照表自己維護。** Carbon keycode 是固定的事實（來自
   `<HIToolbox/Events.h>`），不是猜的。

## 平台差異的處理

`.command` / `.cmd` 這種平台專屬寫法，在這層做**別名**：

    macOS 上  "Cmd"  →  "Win"     （Command 鍵就是 mac 的 Super 鍵）
    macOS 上  "Option" → "Alt"

使用者打哪一種都認得，而輸出永遠是 canonical 名稱 —— 這樣
`config.toml` 在兩個平台之間可以互通（`Ctrl+Alt+R` 兩邊都成立）。
"""

from __future__ import annotations

import sys

# ---------------------------------------------------------------- 平台判定


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform == "win32"


# ---------------------------------------------------------------- Windows 表


def _windows_tables() -> tuple[dict[str, int], dict[int, str]]:
    """跟 `hotkey.py` 借用既有的 VK 表（避免兩份清單漂移）。

    這裡刻意**不自己抄一份**。`hotkey._NAME_TO_VK` 是那個模組的唯一真相，
    任何新增的按鍵名稱都應該改那裡，這裡自動跟上。

    ⚠️ 有快取：這個函式會在**每一個按鍵事件**被呼叫（`code_to_name`），
    沒有快取的話每個事件都重建一次反轉 dict。快取失效的時機是
    「`hotkey` 在執行期被改表」—— 那件事不會發生（表是模組層常數）。
    """
    global _WIN_CACHE
    if _WIN_CACHE is None:
        import hotkey as _hotkey            # 同目錄，呼叫端已把 app/core 放進 sys.path

        # _NAME_TO_VK 是模組私有的，但它是**資料**不是行為。
        # 與其複製一份（會漂移），不如直接引用並在這裡註明依賴。
        name_to_vk = dict(_hotkey._NAME_TO_VK)          # noqa: SLF001
        vk_to_name: dict[int, str] = {}
        for name, vk in name_to_vk.items():
            # 每個 VK 只留一個「最正式」的名稱：優先序見 _PREFERRED
            cur = vk_to_name.get(vk)
            if cur is None or _rank(name) < _rank(cur):
                vk_to_name[vk] = name
        _WIN_CACHE = (name_to_vk, vk_to_name)
    return _WIN_CACHE


_WIN_CACHE: tuple[dict[str, int], dict[int, str]] | None = None


# 同一個鍵碼有多個寫法時（`ctrl` / `control`、`rightctrl` / …），
# 挑哪一個當 canonical。數字越小越優先。
_PREFERRED = [
    "rightctrl", "leftctrl", "ctrl",
    "rightshift", "leftshift", "shift",
    "rightalt", "leftalt", "alt",
    "win", "cmd", "meta",
    "space", "enter", "tab", "esc", "backspace", "delete",
    "up", "down", "left", "right",
    "capslock", "backtick", "scrolllock", "pause", "printscreen", "numlock",
    "insert", "home", "end", "pageup", "pagedown", "apps",
    "semicolon", "quote", "comma", "period", "slash", "backslash",
    "minus", "equals", "leftbracket", "rightbracket",
]


def _rank(name: str) -> int:
    try:
        return _PREFERRED.index(name)
    except ValueError:
        return len(_PREFERRED)          # 沒列到的排最後（f1、a、0…）


# ---------------------------------------------------------------- macOS 表

# Carbon keycode，來源：`<HIToolbox/Events.h>` 的 kVK_* 常數。
# ⚠️ 這是**事實資料**，不是猜的。專案規則要求實測 —— 這份表的來源是
#    Apple 的公開標頭檔，而「對不對」由 tests/test_keys.py 驗證。
# ⚠️ macOS **用 keycode 區分左右**（與 Windows 用 E0 旗標不同手段，但做得到）：
#     左 Ctrl = 59 (0x3B)   右 Ctrl = 62 (0x3E)
#    左 Shift = 56          右 Shift = 60
#    所以 `RightCtrl` 這種限定右側的設定在 mac 上**照樣成立**。
#    唯一要注意的差別是：鍵碼分得出左右，但 CGEventFlags 只有一個 Ctrl 位元。
#    因此判斷「是按下還是放開」時要用 flags（見 MACOS_MODIFIER_FLAGS），
#    不能只看 keycode。
_MACOS_KEYCODE_TO_NAME: dict[int, str] = {
    # 修飾鍵（左右 keycode 不同 —— 這是 mac 的優點，可以限定側別）
    0x37: "win",          # kVK_Command（左）
    0x36: "rightwin",     # kVK_RightCommand
    0x38: "shift",        # kVK_Shift（左）
    0x3C: "rightshift",   # kVK_RightShift
    0x3A: "alt",          # kVK_Option
    0x3D: "rightalt",     # kVK_RightOption
    0x3B: "ctrl",         # kVK_Control
    0x3E: "rightctrl",    # kVK_RightControl
    0x39: "capslock",     # kVK_CapsLock
    0x3F: "fn",           # kVK_Function
    # 符號與數字列
    0x32: "backtick", 0x12: "1", 0x13: "2", 0x14: "3", 0x15: "4",
    0x17: "5", 0x16: "6", 0x1A: "7", 0x1C: "8", 0x19: "9", 0x1D: "0",
    0x1B: "minus", 0x18: "equals", 0x33: "backspace",
    0x72: "insert",       # kVK_Help
    0x73: "home", 0x77: "end", 0x74: "pageup", 0x79: "pagedown",
    0x75: "delete",       # kVK_ForwardDelete
    0x30: "tab", 0x31: "space", 0x24: "enter", 0x35: "esc",
    # 字母
    0x00: "a", 0x0B: "b", 0x08: "c", 0x02: "d", 0x0E: "e", 0x03: "f",
    0x05: "g", 0x04: "h", 0x22: "i", 0x26: "j", 0x28: "k", 0x25: "l",
    0x2E: "m", 0x2D: "n", 0x1F: "o", 0x23: "p", 0x0C: "q", 0x0F: "r",
    0x01: "s", 0x11: "t", 0x20: "u", 0x09: "v", 0x0D: "w", 0x07: "x",
    0x10: "y", 0x06: "z",
    # 標點
    0x27: "quote", 0x2A: "backslash", 0x2C: "slash", 0x2B: "comma",
    0x2F: "period", 0x29: "semicolon", 0x21: "leftbracket",
    0x1E: "rightbracket",
    # 方向鍵
    0x7B: "left", 0x7C: "right", 0x7D: "down", 0x7E: "up",
    # 功能鍵
    0x7A: "f1", 0x78: "f2", 0x63: "f3", 0x76: "f4", 0x60: "f5",
    0x61: "f6", 0x62: "f7", 0x64: "f8", 0x65: "f9", 0x6D: "f10",
    0x67: "f11", 0x6F: "f12", 0x69: "f13", 0x6B: "f14", 0x71: "f15",
    0x6A: "f16", 0x40: "f17", 0x4F: "f18", 0x50: "f19", 0x5A: "f20",
    # 數字鍵盤
    0x52: "num0", 0x53: "num1", 0x54: "num2", 0x55: "num3", 0x56: "num4",
    0x57: "num5", 0x58: "num6", 0x59: "num7", 0x5B: "num8", 0x5C: "num9",
    0x41: "numperiod", 0x4B: "numslash", 0x43: "numstar",
    0x4E: "numminus", 0x45: "numplus", 0x4C: "numenter", 0x47: "numclear",
}

# macOS 的修飾鍵 → CGEventFlags 位元。用來判斷 FlagsChanged 是按下還是放開。
# ⚠️ 這個判斷**不能**靠 keycode 單獨完成 —— 同一個 keycode 按下與放開都送。
MACOS_MODIFIER_FLAGS: dict[str, int] = {
    "ctrl": 0x00040000,        # kCGEventFlagMaskControl
    "rightctrl": 0x00040000,
    "shift": 0x00020000,       # kCGEventFlagMaskShift
    "rightshift": 0x00020000,
    "alt": 0x00080000,         # kCGEventFlagMaskAlternate
    "rightalt": 0x00080000,
    "win": 0x00100000,         # kCGEventFlagMaskCommand
    "rightwin": 0x00100000,    # 右 Command 共用同一個 flag
    "capslock": 0x00010000,    # kCGEventFlagMaskAlphaShift
    "fn": 0x00800000,          # kCGEventFlagMaskSecondaryFn
}

# 平台別名：使用者在 mac 上很自然會打 "Cmd"／"Option"
_MACOS_ALIASES: dict[str, str] = {
    "cmd": "win", "command": "win", "meta": "win", "super": "win",
    "option": "alt", "opt": "alt",
    "control": "ctrl",
    "return": "enter", "escape": "esc",
    # 數字鍵盤的常見寫法
    "kp0": "num0", "kp1": "num1", "kp2": "num2", "kp3": "num3", "kp4": "num4",
    "kp5": "num5", "kp6": "num6", "kp7": "num7", "kp8": "num8", "kp9": "num9",
}

# 名稱 → keycode。
# ⚠️ 反轉時**順序很重要**：`0x36: "win"` 與 `0x37: "win"` 都是 "win"，
#    反轉會互相覆蓋。下面的迴圈用「後者勝」讓右鍵碼勝出，再由
#    `_GENERIC_TO_CODE` 明確指定通用寫法要指向哪一個 —— 不靠巧合。
_MACOS_NAME_TO_KEYCODE: dict[str, int] = {}
for _code, _name in sorted(_MACOS_KEYCODE_TO_NAME.items()):
    _MACOS_NAME_TO_KEYCODE[_name] = _code

# 通用寫法（不限定左右）指向**左邊**那個鍵碼（`ctrl` 由 0x3E 修正為 0x3B）
# 並補上 `leftctrl`／`leftshift`／`leftalt` —— 使用者會從 Windows 沿用這些寫法，
# 而 mac 上左右同碼，所以它們就是左邊那個鍵碼。
_GENERIC_TO_CODE: dict[str, int] = {
    "ctrl": 0x3B,      # kVK_Control（左）
    "shift": 0x38,     # kVK_Shift（左）
    "alt": 0x3A,       # kVK_Option（左）
    "win": 0x37,       # kVK_Command（左）
    "leftctrl": 0x3B, "leftshift": 0x38, "leftalt": 0x3A, "leftwin": 0x37,
    "rightctrl": 0x3E, "rightshift": 0x3C, "rightalt": 0x3D, "rightwin": 0x36,
}
_MACOS_NAME_TO_KEYCODE.update(_GENERIC_TO_CODE)

# 別名也要能查到鍵碼（Cmd → win 的鍵碼）
for _alias, _target in _MACOS_ALIASES.items():
    if _target in _MACOS_NAME_TO_KEYCODE:
        _MACOS_NAME_TO_KEYCODE.setdefault(_alias, _MACOS_NAME_TO_KEYCODE[_target])


# ---------------------------------------------------------------- 公開介面


class UnknownKey(KeyError):
    """這個名稱在本平台沒有對應的鍵碼。"""


def _canonical(name: str) -> str:
    """把使用者寫法正規化成 canonical 名稱（含平台別名）。"""
    n = (name or "").strip().lower().replace(" ", "")
    if is_macos():
        n = _MACOS_ALIASES.get(n, n)
    return n


def name_to_code(name: str) -> int:
    """canonical 名稱 → 本平台鍵碼。不認得就丟 `UnknownKey`。"""
    n = _canonical(name)
    if is_macos():
        if n in _MACOS_NAME_TO_KEYCODE:
            return _MACOS_NAME_TO_KEYCODE[n]
        # 「不知道就不猜」是原則（專案規則 2），但這裡有一個**有依據**的
        # 退化：mac 的字母／數字鍵碼與主鍵盤列不同，開發者很容易誤以為
        # 可以沿用 Windows 的直覺。與其靜默給錯的鍵碼，不如明確報錯，
        # 並在訊息裡說清楚「這張表是查得到的」。
        raise UnknownKey(f"macOS 沒有對應鍵碼：{name!r}")
    name_to_vk, _ = _windows_tables()
    if n not in name_to_vk:
        raise UnknownKey(f"Windows 沒有對應鍵碼：{name!r}")
    return name_to_vk[n]


def code_to_name(code: int) -> str | None:
    """本平台鍵碼 → canonical 名稱。不認得回 None（呼叫端要能容忍）。"""
    if is_macos():
        return _MACOS_KEYCODE_TO_NAME.get(code)
    _, vk_to_name = _windows_tables()
    return vk_to_name.get(code)


def is_modifier(name: str) -> bool:
    """這個名稱是不是修飾鍵（Ctrl／Shift／Alt／Win，含左右寫法）。

    ⚠️ 這是 `hotkey._MODIFIER_NAMES` 的**薄包裝，不是第二份清單** ——
    「哪些名字算修飾鍵」只有一個真相來源（`app/core/hotkey.py`）。
    兩份清單的漂移症狀是「mac 認為它是修飾鍵、Windows 不認為」。
    """
    import hotkey as _hotkey
    name_only, _side = _hotkey._key_name_and_side(_canonical(name))   # noqa: SLF001
    return name_only in _hotkey._MODIFIER_NAMES                        # noqa: SLF001


def modifier_flag(name: str) -> int | None:
    """macOS 專用：修飾鍵對應的 CGEventFlags 位元（判斷按下／放開要用）。"""
    return MACOS_MODIFIER_FLAGS.get(_canonical(name))


def generic_modifier(name: str) -> str:
    """把左／右的寫法收斂成通用名：`rightctrl` → `ctrl`。

    規格與事件兩邊都比對**通用名**（`hotkey.HotkeySpec.modifiers` 是小寫
    通用名），側別則由事件的鍵名或 `E0` 旗標決定。

    ⚠️ 同樣是 `hotkey._key_name_and_side()` 的**薄包裝**。先前這裡自己寫了
    一份 `startswith("right")/("left")` 的剝皮邏輯 —— 那種第二份實作正是
    「同一顆鍵在 mac 命中、在 Windows 不命中」的成因（`lwin`／`rwin` 就
    不在那份邏輯的涵蓋範圍內，而 `hotkey` 的別名表認得它們）。
    """
    import hotkey as _hotkey
    name_only, _side = _hotkey._key_name_and_side(_canonical(name))   # noqa: SLF001
    return name_only


def all_names() -> list[str]:
    """本平台所有認得的 canonical 名稱（給 UI 候選清單與測試用）。"""
    if is_macos():
        return sorted(set(_MACOS_KEYCODE_TO_NAME.values()))
    name_to_vk, _ = _windows_tables()
    return sorted(name_to_vk)


if __name__ == "__main__":       # 手動檢查用
    print(f"平台：{'macOS' if is_macos() else 'Windows' if is_windows() else sys.platform}")
    print(f"認得的按鍵名稱：{len(all_names())} 個")
    for probe in ("RightCtrl", "F9", "R", "Space", "Ctrl+Alt+R"):
        try:
            print(f"  {probe:14s} → 主鍵 keycode {name_to_code(probe.split('+')[-1])}")
        except UnknownKey as exc:
            print(f"  {probe:14s} → {exc}")
