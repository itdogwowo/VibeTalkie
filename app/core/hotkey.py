#!/usr/bin/env python3
"""按鍵規格：把設定檔裡的字串解析成可比對的按鍵組合。

## 為什麼要獨立一個模組

原本錄音鍵是**寫死**的：`key_vk = VK_CONTROL` + `require_e0 = True`
（也就是「右 Ctrl」）。這在 P0 是對的 —— 因為裝置原生就送右 Ctrl。
但使用者要的是**完全脫離硬體綁定**：用鍵盤、用別的鍵、用組合鍵都要能動。

所以按鍵必須變成資料：

    "RightCtrl"          → 右 Ctrl（裝置原生，也是預設）
    "Ctrl+Alt+R"         → 組合鍵
    "F9"                 → 功能鍵
    "Ctrl+Shift+Space"   → 含修飾鍵的組合

## 設計取捨

· 修飾鍵用**集合**比對，不管順序（`Ctrl+Alt+R` ＝ `Alt+Ctrl+R`）。
· 主鍵只能是「一顆」（`Ctrl+Alt+R` 的主鍵是 `R`）。這不是限制，
  是按鍵事件一次只送一顆鍵 —— 組合鍵的語意本來就是「修飾鍵按住 + 主鍵」。
· 認不出來的字串**回報原因**，不靜默退回預設值 ——
  否則使用者會以為設定生效了，實際上還在用舊鍵。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

# 修飾鍵的虛擬鍵碼（左／右都算同一個修飾鍵）
MODIFIER_VKS: dict[int, str] = {
    0x10: "Shift", 0xA0: "Shift", 0xA1: "Shift",
    0x11: "Ctrl", 0xA2: "Ctrl", 0xA3: "Ctrl",
    0x12: "Alt", 0xA4: "Alt", 0xA5: "Alt",
    0x5B: "Win", 0x5C: "Win",
}

# 名稱 → 虛擬鍵碼。查表時一律轉小寫並去掉空白與 `-`／`_`。
_NAME_TO_VK: dict[str, int] = {
    # 修飾鍵
    "ctrl": 0x11, "control": 0x11, "rightctrl": 0xA3, "leftctrl": 0xA2,
    "shift": 0x10, "rightshift": 0xA1, "leftshift": 0xA0,
    "alt": 0x12, "rightalt": 0xA5, "leftalt": 0xA4,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "meta": 0x5B, "cmd": 0x5B,
    # 常見特殊鍵
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "backtick": 0xC0,
    # 常見的「拿來當熱鍵」的低衝突鍵
    "scrolllock": 0x91, "pause": 0x13, "printscreen": 0x2C,
    "numlock": 0x90, "apps": 0x5D,
    "semicolon": 0xBA, "quote": 0xDE, "comma": 0xBC, "period": 0xBE,
    "slash": 0xBF, "backslash": 0xDC, "minus": 0xBD, "equals": 0xBB,
    "leftbracket": 0xDB, "rightbracket": 0xDD,
    # 滑鼠側鍵（有些鍵盤／裝置會送這些 VK）
    "mouse4": 0x05, "mouse5": 0x06,
    # 注音／輸入法常見
    "hanja": 0x19, "hangul": 0x15,
}
# F1–F24
for _i in range(1, 25):
    _NAME_TO_VK[f"f{_i}"] = 0x6F + _i          # F1 = 0x70
# 字母 A–Z
for _c in "abcdefghijklmnopqrstuvwxyz":
    _NAME_TO_VK[_c] = ord(_c.upper())
# 數字 0–9（主鍵盤列）
for _d in "0123456789":
    _NAME_TO_VK[_d] = ord(_d)
# 符號（直接打符號也能認 —— 使用者會很自然地打 `Ctrl+`` ）
for _sym, _vk in (("`", 0xC0), ("-", 0xBD), ("=", 0xBB), ("[", 0xDB), ("]", 0xDD),
                  ("\\", 0xDC), (";", 0xBA), ("'", 0xDE), (",", 0xBC), (".", 0xBE),
                  ("/", 0xBF)):
    _NAME_TO_VK[_sym] = _vk

# 反向：VK → 顯示名稱（給 UI 與錯誤訊息用）
VK_TO_NAME: dict[int, str] = {}
for _name, _vk in _NAME_TO_VK.items():
    VK_TO_NAME.setdefault(_vk, _name)


def _norm(token: str) -> str:
    """把單一 token 正規化。

    ⚠️ 不要把 `-` 當分隔符一起吃掉 —— 它本身是合法的主鍵（`Ctrl+-`）。
    這是實際踩到的：先前把 `-` 和 `_` 都替換掉，導致 `Ctrl+-` 解析失敗，
    而且錯誤訊息（「認不得的按鍵名稱：'-'」）還指向被吃掉後的字串。
    所以這裡只去空白；`-`／`_` 的容錯在 `parse()` 裡針對「整串」處理。
    """
    return token.strip().lower().replace(" ", "")


def _split(spec: str) -> list[str]:
    """把規格切成 tokens，並容忍 ` ` 與 `-` 當分隔符。

    規則：`+` 一律是分隔符；但**單獨一個符號**的 token 要保留
    （例如 `Ctrl+-` 的那個 `-`）。
    """
    parts = [p for p in spec.replace("＋", "+").split("+") if p.strip()]
    if not parts:
        return []
    # 只有「整串」用 `-`／`_` 串起來時才當分隔符（例如 `ctrl-alt-r`）
    if all(("+" not in spec) for _ in (0,)) and ("-" in spec or "_" in spec):
        alt = [p for p in spec.replace("_", "-").split("-") if p.strip()]
        # 若切完每一段都是認得的鍵，就採用這種切法；否則保留原樣
        if len(alt) > 1 and all(_norm(p) in _NAME_TO_VK for p in alt):
            return alt
    return parts


# 顯示用的名稱（正規化後的鍵 → 人類看得懂的字）
DISPLAY_NAMES: dict[str, str] = {
    "rightctrl": "RightCtrl", "leftctrl": "LeftCtrl", "ctrl": "Ctrl", "control": "Ctrl",
    "rightshift": "RightShift", "leftshift": "LeftShift", "shift": "Shift",
    "rightalt": "RightAlt", "leftalt": "LeftAlt", "alt": "Alt",
    "win": "Win", "meta": "Win", "cmd": "Win",
    "space": "Space", "enter": "Enter", "return": "Enter", "tab": "Tab",
    "esc": "Esc", "escape": "Esc", "backspace": "Backspace", "delete": "Delete",
    "insert": "Insert", "home": "Home", "end": "End",
    "pageup": "PageUp", "pagedown": "PageDown",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "capslock": "CapsLock", "backtick": "Backtick", "scrolllock": "ScrollLock",
    "pause": "Pause", "printscreen": "PrintScreen", "numlock": "NumLock", "apps": "Apps",
    "semicolon": "Semicolon", "quote": "Quote", "comma": "Comma", "period": "Period",
    "slash": "Slash", "backslash": "Backslash", "minus": "Minus", "equals": "Equals",
    "leftbracket": "LeftBracket", "rightbracket": "RightBracket",
    "mouse4": "Mouse4", "mouse5": "Mouse5",
}


@dataclass(frozen=True)
class HotkeySpec:
    """一組按鍵：修飾鍵集合 + 一顆主鍵。"""

    vk: int                          # 主鍵的虛擬鍵碼
    modifiers: frozenset[str]        # 需要按住的修飾鍵，例如 {"Ctrl","Alt"}
    require_e0: bool = False         # 是否限定右側（裝置原生的右 Ctrl 需要）
    # ⚠️ `compare=False`：語意相同但寫法不同的字串（`Ctrl+Alt+R` 與
    #    `Alt+Ctrl+R`）應該被視為**同一組按鍵**。若把 raw 算進相等性，
    #    「順序不影響」就會失效 —— 實測被測試抓到。
    raw: str = dc_field(default="", compare=False)

    @property
    def label(self) -> str:
        mods = "+".join(sorted(self.modifiers))
        main = DISPLAY_NAMES.get(_norm(VK_TO_NAME.get(self.vk, ""))) \
            or VK_TO_NAME.get(self.vk, f"0x{self.vk:02X}").upper()
        s = f"{mods}+{main}" if mods else main
        return f"{s}（限定右側）" if self.require_e0 else s


class HotkeyError(ValueError):
    """按鍵規格無法解析（附上原因，呼叫端要顯示給使用者）。"""


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
    main_vk: int | None = None
    require_e0 = False

    for tok in tokens:
        key = _norm(tok)
        vk = _NAME_TO_VK.get(key)
        if vk is None:
            raise HotkeyError(f"認不得的按鍵名稱：{tok!r}")
        if vk in MODIFIER_VKS:
            modifiers.add(MODIFIER_VKS[vk])
            # 明確寫「RightCtrl」時要限定右側（裝置原生送的就是右 Ctrl）
            if key in ("rightctrl", "rightshift", "rightalt"):
                require_e0 = True
            continue
        if main_vk is not None:
            raise HotkeyError(f"只能有一顆主鍵，但看到 {tok!r} 與另一顆")
        main_vk = vk

    if main_vk is None:
        # 只有修飾鍵 → 允許（例如單獨一顆 Ctrl 當熱鍵，這正是裝置的行為）
        if len(modifiers) == 1:
            return _single_modifier(next(iter(modifiers)), require_e0, raw)
        raise HotkeyError("至少要有一顆主鍵（或單獨一顆 Ctrl／Shift／Alt）")

    return HotkeySpec(vk=main_vk, modifiers=frozenset(modifiers),
                      require_e0=require_e0, raw=raw)


def _single_modifier(name: str, require_e0: bool, raw: str) -> HotkeySpec:
    """「只有一顆修飾鍵」的熱鍵（例如單獨按 Ctrl）。"""
    vk = {"Ctrl": 0x11, "Shift": 0x10, "Alt": 0x12, "Win": 0x5B}[name]
    # 用 VK_CONTROL 當主鍵、不要求任何修飾鍵；RightCtrl 時限定 E0
    return HotkeySpec(vk=vk, modifiers=frozenset(), require_e0=require_e0, raw=raw)


# 預設值：裝置原生送出的就是右 Ctrl（見 docs/hardware.md §3.3）
DEFAULT_SPEC = "RightCtrl"

# 給 UI 的候選清單（使用者也可以自己打字）
COMMON_KEYS: list[str] = [
    "RightCtrl", "LeftCtrl", "F9", "F10", "F8", "ScrollLock", "Pause",
    "Ctrl+Alt+R", "Ctrl+Shift+Space", "Ctrl+Alt+Space", "Alt+`",
]


def parse_or_default(spec: str | None) -> tuple[HotkeySpec, str | None]:
    """解析；失敗時退回預設值並回傳原因（呼叫端負責顯示）。"""
    try:
        return parse(spec or DEFAULT_SPEC), None
    except HotkeyError as exc:
        return parse(DEFAULT_SPEC), str(exc)
