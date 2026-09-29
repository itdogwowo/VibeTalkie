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

## 一組不夠 —— 所以是**清單**

實測回饋：只想設「一個」錄音鍵是不夠的。實際會用到的情境至少有

  · 藍牙麥克風的 RightCtrl ＋ 鍵盤上的 F9（在家／外出、有無帶裝置）
  · 同一顆鍵的兩種觸發方式並存（按住說話 ＋ 雙擊）

所以真正的資料形狀是**清單**（`HotkeyBindings`）。`hotkey.py` 只負責
「字串 ⇄ 規格」與「這個事件命中哪幾組」，**不做觸發狀態機**
（那是 `ptt.py` 的事）。

## 設計取捨

· 修飾鍵用**集合**比對，不管順序（`Ctrl+Alt+R` ＝ `Alt+Ctrl+R`）。
· 主鍵只能是「一顆」（`Ctrl+Alt+R` 的主鍵是 `R`）。這不是限制，
  是按鍵事件一次只送一顆鍵 —— 組合鍵的語意本來就是「修飾鍵按住 + 主鍵」。
· 認不出來的字串**回報原因**，不靜默退回預設值 ——
  否則使用者會以為設定生效了，實際上還在用舊鍵。

## 側別（左／右）為什麼要三態

⚠️ 這裡是實測踩到的**真 bug**，不是理論問題：

原本只有 `require_e0` 一個布林值，於是 `LeftCtrl` 解析出來是 `0xA2`
且 `require_e0=False` —— 而比對時同一家族（`0x11/0xA2/0xA3`）一律視為相等，
所以設定 `LeftCtrl` **按右 Ctrl 也會觸發**。UI 上明明白白寫著「LeftCtrl」，
行為卻是左右不分，使用者只會覺得「這設定根本沒作用」。

改成三態之後：`None`＝左右都算（`Ctrl`）、`"left"`／`"right"`＝限定該側。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

# 修飾鍵的虛擬鍵碼（左／右都算同一個修飾鍵）。
#
# ⚠️ 值一律是**小寫 canonical 名稱**，與 `HotkeySpec.modifiers`、
#    `trigger.Event.mods`、mac 的 `keys.generic_modifier()` 同一套寫法。
#    先前用 `"Ctrl"` 這種大寫顯示名當值，於是「規格裡是 Ctrl、事件裡是 ctrl」
#    兩邊各轉一次 —— 那就是漂移的溫床（比對時得記得 `lower()`）。
MODIFIER_VKS: dict[int, str] = {
    0x10: "shift", 0xA0: "shift", 0xA1: "shift",
    0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
    0x12: "alt", 0xA4: "alt", 0xA5: "alt",
    0x5B: "win", 0x5C: "win",
}

# 修飾鍵的 canonical 名稱集合（跨平台共用的那一份）。
_MODIFIER_NAMES: frozenset[str] = frozenset({"ctrl", "shift", "alt", "win"})

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
    # 數字鍵盤（NumLock 開著時是 0x60–0x69；導覽鍵另有一組 VK，
    # 所以 UI 錄到的 Numpad0 不會跟主鍵盤的 0 混淆）
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69,
    "numpadmultiply": 0x6A, "numpadadd": 0x6B, "numpadsubtract": 0x6D,
    "numpaddecimal": 0x6E, "numpaddivide": 0x6F, "numpadenter": 0x0D,
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
    "numpad0": "Numpad0", "numpad1": "Numpad1", "numpad2": "Numpad2",
    "numpad3": "Numpad3", "numpad4": "Numpad4", "numpad5": "Numpad5",
    "numpad6": "Numpad6", "numpad7": "Numpad7", "numpad8": "Numpad8",
    "numpad9": "Numpad9", "numpadmultiply": "NumpadMultiply",
    "numpadadd": "NumpadAdd", "numpadsubtract": "NumpadSubtract",
    "numpaddecimal": "NumpadDecimal", "numpaddivide": "NumpadDivide",
}

# 名稱有沒有明寫「左／右」—— 用來決定側別。
# ⚠️ 不要用 `vk == 0xA2` 之類的判斷：`0xA2` 同時可能是「使用者寫了 leftctrl」
#    也可能是「UI 錄到 LeftCtrl 後存成 0xA2」。這裡只看字串，語意最清楚。
_RIGHT_NAMES = {"rightctrl", "rightshift", "rightalt"}
_LEFT_NAMES = {"leftctrl", "leftshift", "leftalt"}

# 別名 → canonical 名稱（**不是** VK）。三件事在這裡收斂成一件：
#   · `control`／`meta`／`escape`／`return` 這類同義寫法
#   · `lwin`／`rwin` 這種「左／右」寫在**後面**的鍵（`_RIGHT_NAMES` 認不到）
#   · mac 的 `cmd`／`option` 之後由 `keys.py` 的別名表接手
_ALIASES: dict[str, str] = {"control": "ctrl", "meta": "win", "return": "enter",
                            "escape": "esc", "lwin": "win", "rwin": "win"}

# 修飾鍵的側別寫法。⚠️ `ctrl`／`shift`／`alt` 不在此表 —— 它們是**三態**
# （不寫側別＝左右都算），側別由事件端的 `E0` 旗標或 keycode 決定。
_EXPLICIT_SIDE: dict[str, tuple[str, str]] = {
    "rightctrl": ("ctrl", "right"), "rightshift": ("shift", "right"),
    "rightalt": ("alt", "right"),
    "leftctrl": ("ctrl", "left"), "leftshift": ("shift", "left"),
    "leftalt": ("alt", "left"),
    # Win 只有 `lwin`／`rwin`，沒有 `win` 的左右別名
    "lwin": ("win", "left"), "rwin": ("win", "right"),
}


def _key_name_and_side(key: str) -> tuple[str, str | None]:
    """`"rightctrl"` → `("ctrl", "right")`；`"f9"` → `("f9", None)`。

    **這是「使用者的寫法 → canonical 名稱」的唯一入口。** 別名、側別前綴
    都在這裡處理完，其他模組（`keys.generic_modifier()`、`trigger.py`）
    都轉呼叫它 —— 三份實作的漂移症狀是「同一顆鍵在 mac 命中、在 Windows 不命中」。

    ⚠️ 側別只看**字串**，不要用 `vk == 0xA2` 之類的判斷：`0xA2` 同時可能是
    「使用者寫了 leftctrl」也可能是「UI 錄到 LeftCtrl 後存成 0xA2」，
    而 Raw Input 送的是**通用** VK ＋ E0 旗標，根本沒有 0xA2 這種東西。
    """
    k = _norm(key)
    if k in _EXPLICIT_SIDE:
        return _EXPLICIT_SIDE[k]
    return _ALIASES.get(k, k), None


# 修飾鍵名稱與 VK 表的值必須一致 —— 不一致就當場炸掉，不要等到
# 「按了沒反應」才發現（那是這個專案最常見的失敗症狀）。
assert set(MODIFIER_VKS.values()) == _MODIFIER_NAMES, \
    f"MODIFIER_VKS 與 _MODIFIER_NAMES 不一致：{set(MODIFIER_VKS.values())}"


def _name_label(name: str) -> str:
    """主鍵的顯示名稱（左右側的標註在 `HotkeySpec.label` 處理）。

    ⚠️ 這裡刻意**去掉** Left／Right 前綴：`rightctrl` 該顯示成
    「Ctrl（限定右側）」而不是「RightCtrl（限定右側）」—— 後者沒有錯，
    但讀起來像兩個限制。
    """
    return DISPLAY_NAMES.get(name) or name.upper()



@dataclass(frozen=True)
class HotkeySpec:
    """一組按鍵：canonical 名稱 + 側別 + 需要按住的修飾鍵。

    ## 為什麼內部表示是**名稱**而不是 VK 碼

    原本這裡是 `vk: int`（Windows 虛擬鍵碼）。只有 Windows 時沒問題，
    但 macOS 的 CGEventTap 送的是完全另一套 Carbon keycode：

        右 Ctrl：Windows VK = 0xA3      macOS keycode = 62
        F9     ：Windows VK = 0x78      macOS keycode = 101

    同一個 `HotkeySpec` 在兩個平台裝兩種數字，而 `spec_hit()` 的比對依賴
    VK 家族（`{0x11, 0xA2, 0xA3}` 是同一顆 Ctrl）→ 在 mac 上必然全錯。

    所以內部表示改成**平台無關的名稱**（`"ctrl"`／`"f9"`），
    平台鍵碼只由 `keys.py` 在平台層換算（見 `app/core/keys.py` 檔頭）。
    VK 仍然可用（`spec.vk`），但它是**推導出來的**，只有 Windows 平台層在讀。
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

        這是**平台層要比對的字串**：macOS 的 keycode 本身就分左右，
        所以 mac 不需要另外看旗標（見 `app/core/keys.py`）。
        """
        return f"{self.side}{self.name}" if self.side else self.name

    @property
    def vk(self) -> int:
        """Windows 虛擬鍵碼（**推導出來的**，只給 Windows 平台層用）。

        ⚠️ 修飾鍵一律指向**通用 VK**（`rightctrl` → `0x11`），側別交給 `side` ——
        因為 Raw Input 送的就是通用 VK ＋ `E0` 旗標，不是 `VK_RCONTROL`。
        這與 `_single_modifier()` 原本的行為一致（那裡是直接寫死 `0x11`）。

        專用 VK（`0xA2`／`0xA3`）由 `_vk_to_name_side()` 反查回名稱與側別，
        所以「事件帶專用 VK」與「事件帶通用 VK ＋ E0」在 `spec_hit()` 裡
        走同一條名稱比對 —— 兩者都命中，這正是原本家族比對在做的事。
        """
        return _NAME_TO_VK.get(self.name, 0)


    # 舊欄位的相容視圖（`require_e0` == 限定右側）。
    # 保留是因為 ptt.py 早期版本與外部工具可能讀它；值一律由 side 推導。
    @property
    def require_e0(self) -> bool:
        return self.side == "right"

    @property
    def main_is_modifier(self) -> bool:
        """主鍵本身是不是修飾鍵（例如單獨一顆 Ctrl 當熱鍵）。"""
        return self.name in _MODIFIER_NAMES

    @property
    def is_native_device_key(self) -> bool:
        """是不是「裝置原生」的那顆鍵（右 Ctrl，單獨一顆、沒有其他修飾鍵）。

        用途：判斷要不要限定裝置來源。使用者改成別的鍵就代表他想用
        鍵盤／別的來源，這時再要求裝置符合 filter 就等於「設定沒作用」。
        """
        return self.name == "ctrl" and self.side == "right" and not self.modifiers

    @property
    def label(self) -> str:
        mods = "+".join(DISPLAY_NAMES.get(m, m) for m in sorted(self.modifiers))
        main = _name_label(self.name)
        s = f"{mods}+{main}" if mods else main
        return f"{s}（限定右側）" if self.require_e0 else s


class HotkeyError(ValueError):
    """按鍵規格無法解析（附上原因，呼叫端要顯示給使用者）。"""


def spec_from_vk(vk: int, require_e0: bool = False,
                 raw: str = "") -> HotkeySpec:
    """**舊介面**：用 Windows 鍵碼建一組規格（命令列 `--key-vk` 還在用）。

    `HotkeySpec` 的內部表示已經改成 canonical 名稱，所以建構子收的是
    `name=`／`side=`。但 `ptt.PttDaemon(key_vk=..., require_e0=...)` 是
    既有的公開參數，把它翻過來的工作放在這裡，而不是散在呼叫端。

    ⚠️ 認不得的鍵碼**不猜**（專案規則 2）—— 退回 `DEFAULT_SPEC`。
    """
    name, side = _vk_to_name_side(int(vk))
    if not name:
        return parse(DEFAULT_SPEC)
    if require_e0 and name in _MODIFIER_NAMES:
        side = "right"
    return HotkeySpec(name=name, modifiers=frozenset(), side=side, raw=raw)


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

    mod_names: list[str] = []
    main_name: str | None = None
    side: str | None = None

    for tok in tokens:
        key = _norm(tok)
        if key not in _NAME_TO_VK:
            raise HotkeyError(f"認不得的按鍵名稱：{tok!r}")
        name, tok_side = _key_name_and_side(key)
        if name in _MODIFIER_NAMES:
            mod_names.append(name)
            # 明確寫「RightCtrl」時要限定右側（裝置原生送的就是右 Ctrl）；
            # 「LeftCtrl」同理限定左側 —— 否則左右不分，設定形同虛設。
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

    # ⚠️ 主鍵若是修飾鍵家族的成員，上面已經 `continue` 掉了（它會變成
    #    `mod_names` 的一員），所以走到這裡的 `modifiers` 一定是「另外按住的」。
    return HotkeySpec(name=main_name, modifiers=frozenset(mod_names),
                      side=side, raw=raw)



# 預設值：裝置原生送出的就是右 Ctrl（見 docs/hardware.md §3.3）
DEFAULT_SPEC = "RightCtrl"

# 給 UI／文件的候選清單（使用者也可以直接錄製或打字）
COMMON_KEYS: list[str] = [
    "RightCtrl", "LeftCtrl", "F9", "F10", "F8", "ScrollLock", "Pause",
    "Ctrl+Alt+R", "Ctrl+Shift+Space", "Ctrl+Alt+Space", "Alt+`",
]

# --------------------------------------------------------------- 觸發方式
#
# ⚠️ 觸發方式**每一邊各自**一份 —— 開始有開始的行為、結束有結束的行為。
# 為什麼：實測回饋 ——
#   「開始和結束都有他自己的行為，下拉選單，而且開始和結束的行為會不一樣，
#     結束有鬆開行為」
# 所以「鬆開」是**結束那一邊**的選項（放開結束鍵才停），不是全域設定。
#
# 存法：`@` 標在那一邊的鍵名後面，或把兩個行為擠在結尾 ——
#   `F9@double`            開始鍵是「連按兩下開始」
#   `F9,Esc`               開始 F9、結束 Esc（兩邊都用預設行為）
#   `F9,Esc@toggle`        結束鍵是「再按一下才停」（`hold`＝鬆開才停 ← 預設）
#   `F9,Esc@double@toggle` 兩邊都指定（**本程式輸出的正式寫法**）
# `split_binding()` 兩種寫法都吃得下，輸出只採後者。
MODE_HOLD = "hold"          # 開始：按一下開始｜結束：**鬆開**才停
MODE_TOGGLE = "toggle"      # 開始：按一下開始｜結束：**再按一下**才停
MODE_DOUBLE = "double"      # 開始：連按兩下開始｜結束：連按兩下才停
MODE_VALUES: tuple[str, ...] = (MODE_HOLD, MODE_TOGGLE, MODE_DOUBLE)
MODE_SEP = "@"

# --------------------------------------------------------------- 啟用／禁用
#
# 為什麼要有：「我想再添加一個啟用禁用，讓多種組合能夠同時生效」——
# 多組本來就是「或」（任何一組命中都觸發），但使用者需要**暫時關掉某一組**
# 而不刪掉它（例如換了一支麥克風、或某顆鍵拿去給別的程式用）。
# 刪掉再重建很麻煩，而且要記得原本的設定。
#
# 存法：鍵名前面加 `~` —— `"~F9,Esc"` ＝ 這一組先停用。
# 為什麼用前綴而不是另開欄位（`["F9"]` + `enabled=[false]`）：
# 兩份清單會走音，而且使用者手改 TOML 時要同時改兩個地方。
# 為什麼不用刪除：停用的組合要**看得見**（知道自己有什麼），
# 靜默消失是最糟的結果。
DISABLED_PREFIX = "~"
MODE_LABELS: dict[str, str] = {
    MODE_HOLD: "按住說話",
    MODE_TOGGLE: "按一下切換",
    MODE_DOUBLE: "雙擊",
}
# 同一個模式在「開始」與「結束」兩邊的說法不一樣 —— UI 的兩個下拉各用一份。
START_MODE_LABELS: dict[str, str] = {
    MODE_HOLD: "按一下開始",
    MODE_TOGGLE: "按一下開始",
    MODE_DOUBLE: "連按兩下開始",
}
END_MODE_LABELS: dict[str, str] = {
    MODE_HOLD: "鬆開才停",
    MODE_TOGGLE: "再按一下停",
    MODE_DOUBLE: "連按兩下停",
}
START_MODE_ORDER: tuple[str, ...] = (MODE_HOLD, MODE_DOUBLE)
END_MODE_ORDER: tuple[str, ...] = (MODE_HOLD, MODE_TOGGLE, MODE_DOUBLE)


def split_mode(entry: str) -> tuple[str, str | None]:
    """把 `"F9@double"` 拆成 `("F9", "double")`。

    沒有 `@` 就回 `(entry, None)` —— None 代表「沒指定，用設定檔的全域預設」。
    認不得的模式名稱要丟 `HotkeyError`（呼叫端負責顯示），**不可靜默忽略** ——
    否則使用者寫了 `F9@dboule` 會以為自己設了雙擊，實際上還是按住說話。
    """
    text = (entry or "").strip()
    if MODE_SEP not in text:
        return text, None
    head, _, mode = text.rpartition(MODE_SEP)
    head, mode = head.strip(), mode.strip().lower()
    if not head:                       # 使用者只打了 "@double"
        raise HotkeyError(f"觸發方式前面沒有按鍵：{entry!r}")
    if mode not in MODE_VALUES:
        raise HotkeyError(f"認不得的觸發方式 {mode!r}"
                          f"（只能是 {'、'.join(MODE_VALUES)}）")
    return head, mode


def split_binding(entry: str) -> tuple[list[str], list[str | None], list[str], bool]:
    """把一筆設定拆成 `(鍵, 各邊行為, 結尾行為, 是否啟用)`。

    ## 支援的寫法（行為寫在**它修飾的那顆鍵後面**，或全部擠在結尾）

        F9                        開始 F9，行為用預設
        F9@double                 開始 F9、連按兩下開始
        F9,Esc                    開始 F9、結束 Esc（行為都用預設）
        F9@double,Esc             開始 double、結束預設
        F9,Esc@toggle             開始預設、**結束 toggle**（Esc 再按一下才停）
        F9,Esc@double@toggle      兩邊都指定（依序＝開始、結束）
        ~F9,Esc                   同上但**停用**（`~` 放在最前面）

    ⚠️ 回傳四個值，是因為「行為標在哪一顆鍵後面」是有意義的
    （見 `parse_binding`），而「啟用與否」跟鍵名、行為都無關。
    """
    raw = (entry or "").strip()
    enabled = True
    if raw.startswith(DISABLED_PREFIX):
        enabled = False
        raw = raw[len(DISABLED_PREFIX):].strip()
    keys: list[str] = []
    side_modes: list[str | None] = []      # 每一顆鍵「自己後面」的行為
    tail_modes: list[str] = []             # 逗號後面才出現的行為（`A,B@m1@m2`）
    # 以**逗號**切段（逗號是配對分隔符，只會有一個）：
    #   "F9"                 → 一段、沒有 @
    #   "F9,Esc"             → 兩段、都沒有 @
    #   "F9,Esc@toggle"      → 兩段，第二段帶一個行為
    #   "F9@double,Esc"      → 兩段，第一段帶行為
    #   "F9,Esc@double@toggle" → 兩段，第二段帶**兩個**行為（正式寫法）
    for n, seg in enumerate(s for s in raw.split(BINDING_SEP) if s.strip()):
        seg = seg.strip()
        key, sep, tail = seg.partition(MODE_SEP)
        if not sep:
            keys.append(seg)
            side_modes.append(None)
            continue
        found = [t.strip().lower() for t in tail.split(MODE_SEP) if t.strip()]
        if (not key.strip() or not found
                or any(m not in MODE_VALUES for m in found)):
            return [entry], [], [], True   # 看不懂 → 整串交回去讓 parse() 報錯
        keys.append(key.strip())
        side_modes.append(found[0] if n == 0 else None)
        tail_modes.extend(found if n > 0 else found[1:])
    if len(keys) > 2 or len(tail_modes) > 2:
        return [entry], [], [], True       # 超過兩顆鍵或兩個行為 → 看不懂
    return keys, side_modes, tail_modes, enabled


def parse_entry(entry: str, default_mode: str | None = None
                ) -> tuple[HotkeySpec, str]:
    """解析清單裡的一筆：`"F9@double"` → `(規格, "double")`。

    `default_mode` 是設定檔的全域 `trigger_mode`（沒寫 `@` 時的後備）。
    """
    keys, side, tail, enabled = split_binding(entry)
    if len(keys) > 1:
        raise HotkeyError(f"這是配對寫法（`開始,結束`），不能用單鍵解析：{entry!r}")
    mode = side[0] or (tail[0] if tail else None)
    return parse(keys[0]), (mode or default_mode or MODE_HOLD)


def entry_text(spec: "HotkeySpec", mode: str | None = None) -> str:
    """把 `(規格, 模式)` 寫回清單字串（`F9@double`、`RightCtrl`）。"""
    key = spec.raw or VK_TO_NAME.get(spec.vk, f"0x{spec.vk:02X}")
    if mode and mode != MODE_HOLD:
        return f"{key}{MODE_SEP}{mode}"
    return key


def spec_text(spec: "HotkeySpec") -> str:
    """單一組規格的字串寫法（給 UI 顯示與 `spec_text(parse(x)) == x` 用）。"""
    return spec.raw or VK_TO_NAME.get(spec.vk, f"0x{spec.vk:02X}")


def parse_or_default(spec: str | None) -> tuple[HotkeySpec, str | None]:
    """解析；失敗時退回預設值並回傳原因（呼叫端負責顯示）。"""
    try:
        return parse(spec or DEFAULT_SPEC), None
    except HotkeyError as exc:
        return parse(DEFAULT_SPEC), str(exc)


# --------------------------------------------------------- 開始／結束配對
#
# 為什麼要允許「開始鍵 ≠ 結束鍵」：實測回饋 ——
#   「也可以接受按鍵不一樣做開始和結束的配對」
# 真實情境：開始用藍牙麥克風的 RightCtrl（手不用離開裝置），
# 結束用鍵盤的 Escape（單手結束、不必再去按裝置）。
# 用同一顆鍵當然也支援 —— 那就是 `end` 留空（收尾交給觸發方式決定）。
#
# 存法：`"F9,Escape"` ＝ 開始 F9、結束 Escape；`"F9"` ＝ 開始/結束都用 F9。
#
# ⚠️ 分隔符刻意用 `,` 而不是 `+`（第一版用了 `+`，是錯的）：
#    `+` 已經是**組合鍵**的分隔符，`F9+Escape` 到底是一個組合鍵還是
#    兩個按鍵的配對，**無法從字串判斷**（`Ctrl+F9` 是合法的組合鍵！）。
#    實測就是這樣踩到的：`Ctrl+Alt+R` 被切成「開始 Ctrl+Alt、結束 R」。
#    用 `,` 之後「配對用逗號、組合用加號」，兩者不會互相誤解，
#    而且和 AppleScript／macOS 的寫法一致（`key code 105, key code 53`）。
BINDING_SEP = ","


def parse_binding(entry: str, default_mode: str | None = None
                  ) -> tuple[HotkeySpec, str, HotkeySpec | None, str]:
    """解析一筆完整設定，回傳 `(開始, 開始行為, 結束或 None, 結束行為)`。

    兩邊的寫法各帶自己的行為（見檔頭的說明）：

        "F9@hold"               開始 F9（按一下開始）、沒有結束鍵
        "F9,Esc"                結束鍵 Esc、**鬆開才停**
        "F9@double,Esc@toggle"  連按兩下開始、再按一下 Esc 才停

    ⚠️ `@` 標在哪一邊只是寫法差別，`split_binding()` 會一視同仁地拆乾淨。
    """
    keys, side, tail, enabled = split_binding(entry)
    if not keys:
        raise HotkeyError("按鍵不能是空的")
    start = parse(keys[0])
    # 正規化：把兩種寫法收成同一個 `[開始行為, 結束行為]`。
    #   `A@m1,B@m2`（各自標）→ side=[m1, m2]、tail=[]
    #   `A,B@m1@m2`（擠結尾）→ side=[None, None]、tail=[m1, m2]
    #   `A@m,B`              → side=[m, None]、tail=[m]
    #   `A,B@m`              → side=[None, None]、tail=[m]
    if len(keys) == 1:
        only = side[0] or (tail[0] if tail else None)
        return start, (only or default_mode or MODE_HOLD), None, ""
    if tail and not side[0] and side[1] is None:
        # `A,B@m1@m2` 或 `A,B@m`：依序對應開始、結束
        start_mode = tail[0] if len(tail) > 1 else (default_mode or MODE_HOLD)
        end_mode = tail[1] if len(tail) > 1 else tail[0]
    else:
        # `A@m1,B@m2` / `A@m1,B` 混用：各自認自己的
        start_mode = side[0] or (tail[0] if tail else None) \
            or default_mode or MODE_HOLD
        end_mode = side[1] or (tail[-1] if tail else None) or MODE_HOLD
    return start, start_mode, parse(keys[1]), end_mode


def _split_pair(text: str) -> tuple[str, str | None]:
    """把 `"F9,Escape"` 切成 `("F9", "Escape")`；不是配對就回 `(原文, None)`。

    只認第一個逗號：前面是開始鍵、後面是結束鍵（各自都可以是組合鍵，
    例如 `Ctrl+Alt+R,Escape`）。只有一段就是沒有結束鍵。
    """
    head, sep, tail = text.partition(BINDING_SEP)
    if not sep or not head.strip() or not tail.strip():
        return text, None
    return head.strip(), tail.strip()


def binding_text(start: "HotkeySpec", start_mode: str | None = None,
                 end: "HotkeySpec | None" = None,
                 end_mode: str | None = None) -> str:
    """把 `(開始, 開始行為, 結束, 結束行為)` 寫回設定檔字串。

    ⚠️ 輸出規則（**行為一律放最後**，見 `split_binding`）：

        F9                       開始 F9（行為用預設）
        F9@double                開始用雙擊
        F9,Esc                   開始 F9、結束 Esc（行為都用預設）
        F9,Esc@toggle            同上，但結束鍵「再按一下」才停
        F9@double,Esc@toggle     兩邊都不是預設值

    `@` 只標**非預設值**的那一邊（預設是 `hold`），所以最常見的
    `F9`、`F9,Esc` 讀起來最乾淨。
    """
    text = spec_text(start)
    if end is not None:
        text += f"{BINDING_SEP}{spec_text(end)}"
    if start_mode and start_mode != MODE_HOLD:
        text += f"{MODE_SEP}{start_mode}"
    # 結束行為只有在「有結束鍵」且「不是預設」時才寫；
    # 若開始行為也沒寫，就只寫一個 `@`（`F9,Esc@hold`）—— 解析時它會落在
    # 「一個行為」那一支，語意是「結束鍵的行為」，正確。
    if end is not None and end_mode and end_mode != MODE_HOLD:
        text += f"{MODE_SEP}{end_mode}"
    return text


# --------------------------------------------------------------- 多鍵綁定
# 同一顆鍵可能有**多種寫法**，事件比對時要視為相等。
#
# 為什麼需要：**兩種來源用的鍵碼不一樣。** 實測（docs/hardware.md §3.3）：
#   · Raw Input 回報**通用** VK_CONTROL（0x11），靠 E0 旗標分左右
#   · Low-Level Hook 回報**專用** VK_RCONTROL（0xA3）
#   · macOS 回報 Carbon keycode + `rightctrl` 這種帶側別的名稱
# 所以設定檔寫 `RightCtrl` 時，事件可能帶 0x11（+E0）、0xA3、
# 或 macOS 的 keycode 62。不做這層對應，單獨一顆 RightCtrl 就永遠觸發不了
# （實際踩到）。
#
# ⚠️ **改成名稱中心之後，這張表只剩「VK → 名稱」一個用途**：把 Windows
#    的鍵碼換算成 canonical 名稱，之後的比對全部走名稱。名稱層沒有
#    「家族」這種東西 —— `rightctrl` 與 `ctrl` 的關係由 `side` 表達。
_VK_TO_NAME_SIDE: dict[int, tuple[str, str | None]] = {}
for _n, _v in _NAME_TO_VK.items():
    # 同一個 VK 可能有多個寫法（`ctrl`／`control`）—— 留第一個就好，
    # 因為 `_key_name_and_side()` 保證同義寫法收斂成同一個 canonical 名稱。
    _VK_TO_NAME_SIDE.setdefault(_v, _key_name_and_side(_n))


def _vk_to_name_side(vk: int) -> tuple[str, str | None]:
    """Windows 鍵碼 → `(canonical 名稱, 名稱自帶的側別)`。

    不認得的鍵碼回 `("", None)` —— **不要猜**（專案規則 2）。
    呼叫端要能容忍空名稱並據此忽略事件。
    """
    return _VK_TO_NAME_SIDE.get(int(vk), ("", None))


def vk_matches(a: int, b: int) -> bool:
    """兩個 VK 是不是同一顆鍵（含左右通用／專用的對應）。

    ⚠️ 保留給外部呼叫端（舊工具、診斷腳本）。**內部比對請用 `spec_hit()`**，
    它走 canonical 名稱，兩個平台同一條路。
    """
    if a == b:
        return True
    na, sa = _vk_to_name_side(a)
    nb, sb = _vk_to_name_side(b)
    if not na or not nb:
        return False
    # 同一顆鍵：名稱相同，且側別不衝突（任一邊沒寫側別＝通用，算命中）
    return na == nb and (sa is None or sb is None or sa == sb)


def side_of(name: str, by_name_side: str | None = None,
            e0: bool = False) -> str | None:
    """這顆鍵是左邊還是右邊的那一顆？（`None`＝左右無意義）

    判準（實測）：
      · **名稱已經帶側別**（mac 的 keycode、或已解析過的名稱）→ **名稱優先**
      · **通用修飾鍵名稱 ＋ E0 旗標**（Windows Raw Input）→ 由旗標決定
      · 不是修飾鍵家族的鍵 → None（左右無意義）

    ⚠️ 名稱優先不是任意選擇：Windows 的 Raw Input 送通用 VK ＋ E0，
    mac 送的是**不同 keycode**（名稱已含側別）。兩者同時存在時，
    名稱是更精確的那一個。
    """
    if by_name_side:
        return by_name_side
    if _norm(name) in _MODIFIER_NAMES:
        return "right" if e0 else "left"
    return None


def event_side(vk: int, e0: bool) -> str | None:
    """**Windows 入口**：VK ＋ E0 旗標 → 側別（保留給舊呼叫端與測試）。"""
    name, by_name = _vk_to_name_side(int(vk))
    return side_of(name, by_name, e0)


def spec_hit(spec: HotkeySpec, key, e0: bool = False) -> bool:
    """這顆鍵是不是 `spec` 的主鍵（含左右側判定）。

    `key` 可以是 **VK 整數**（Windows 呼叫端）或 **canonical 名稱字串**
    （mac 呼叫端、以及抽出來的 `app/core/trigger.py`）—— 兩條路最後都走
    同一段名稱比對，這正是「兩個平台共用一顆引擎」的接縫。

    ⚠️ 這是**主鍵**的比對，不含修飾鍵條件（那個要用 `mods_ok`）。
    """
    if isinstance(key, str):
        name, by_name = _key_name_and_side(key)
    else:
        name, by_name = _vk_to_name_side(int(key))
    if not name or name != spec.name:
        return False
    if spec.side is not None:
        got = side_of(name, by_name, e0)
        if got is not None and got != spec.side:
            return False
    return True


def mods_ok(spec: HotkeySpec, mods_down: set[str] | frozenset[str]) -> bool:
    """修飾鍵是否**剛好**符合（不多也不少）。

    `mods_down` 是「目前按住的修飾鍵集合」，**小寫 canonical 名稱**
    （`{"ctrl","alt"}`）—— 不分左右，左右由 `spec.side` 與事件的鍵名判定。

    ⚠️ 主鍵本身就是修飾鍵時（例如單獨一顆 Ctrl 當錄音鍵），它自己一定
    會出現在 `mods_down` 裡 —— 那不是「需要另外按住的修飾鍵」，要先扣掉。
    """
    # 容忍大寫寫法（`"Ctrl"`）—— 舊呼叫端與手寫的測試可能還在用。
    have = {str(m).lower() for m in mods_down}
    if spec.main_is_modifier:
        have.discard(spec.name)
    return have == set(spec.modifiers)


class HotkeyBindings:
    """**多組**錄音鍵，每一組各自帶**觸發方式**。

    為什麼是清單而不是單一組：實測回饋 —— 只設一個鍵不夠用。
    典型情境是「藍牙麥克風的 RightCtrl」與「鍵盤上的 F9」並存，
    或多顆裝置輪流用。使用者不該為了換鍵而進設定頁改一次、再改回來。

    為什麼模式要跟著每一組：同一句話的回饋是「無法錄製雙擊」——
    真實情境是「藍牙麥克風按住說話 ＋ 鍵盤 F9 雙擊」。
    全域一個模式做不到這件事，所以模式是**每一組自己的屬性**。
    """

    def __init__(self, specs: list[HotkeySpec] | tuple[HotkeySpec, ...],
                 modes: list[str] | tuple[str, ...] | None = None,
                 ends: list[HotkeySpec | None] | tuple[HotkeySpec | None, ...] | None = None,
                 end_modes: list[str] | tuple[str, ...] | None = None,
                 enabled: list[bool] | tuple[bool, ...] | None = None):
        self.specs: tuple[HotkeySpec, ...] = tuple(specs)
        if modes is None:
            self.modes: tuple[str, ...] = (MODE_HOLD,) * len(self.specs)
        else:
            self.modes = tuple(modes)
        # 每一組的**結束鍵**（None＝沒有，收尾交給「開始行為」）
        if ends is None:
            self.ends: tuple[HotkeySpec | None, ...] = (None,) * len(self.specs)
        else:
            self.ends = tuple(ends)
        # 每一組的**結束行為**：hold＝鬆開才停、toggle＝再按一下停、double＝連按兩下停
        if end_modes is None:
            self.end_modes: tuple[str, ...] = ("",) * len(self.specs)
        else:
            self.end_modes = tuple(end_modes)
        # 每一組的**啟用狀態**（停用的組合保留在設定裡，只是不比對）
        if enabled is None:
            self.enabled: tuple[bool, ...] = (True,) * len(self.specs)
        else:
            self.enabled = tuple(enabled)
        for name, seq in (("開始行為", self.modes), ("結束鍵", self.ends),
                          ("結束行為", self.end_modes), ("啟用狀態", self.enabled)):
            if len(seq) != len(self.specs):
                raise ValueError(f"規格 {len(self.specs)} 組但{name} {len(seq)} 個")

    @property
    def active(self) -> tuple[HotkeySpec, ...]:
        """**啟用中**的規格（比對與觸發都只看這些）。

        ⚠️ 停用的組合仍然留在清單裡（UI 要顯示、設定檔要看得到），
        但事件比對時完全不理它 —— 這是「暫時關掉某一組」的實作方式。
        """
        return tuple(s for s, on in zip(self.specs, self.enabled) if on)

    def is_enabled(self, spec: HotkeySpec) -> bool:
        return bool(self._at(self.enabled, spec))

    def quads(self) -> list[tuple[HotkeySpec, str, HotkeySpec | None, str]]:
        """**啟用中**的 `(開始, 開始行為, 結束, 結束行為)`。"""
        return [(s, m, e, em) for s, m, e, em in
                zip(self.specs, self.modes, self.ends, self.end_modes) if self.is_enabled(s)]

    def __len__(self) -> int:
        return len(self.specs)

    def __iter__(self):
        return iter(self.specs)

    def __bool__(self) -> bool:
        return bool(self.specs)

    def _at(self, seq, spec):
        try:
            return seq[self.specs.index(spec)]
        except (ValueError, IndexError):
            return None

    def mode_of(self, spec: HotkeySpec) -> str:
        """這一組的**開始行為**（找不到就退回按一下開始，絕不回 None）。"""
        return self._at(self.modes, spec) or MODE_HOLD

    def end_of(self, spec: HotkeySpec) -> HotkeySpec | None:
        """這一組的**結束鍵**（None＝沒有指定，用 start 的 hold 收尾）。"""
        return self._at(self.ends, spec)

    def end_mode_of(self, spec: HotkeySpec) -> str:
        """這一組的**結束行為**（沒有結束鍵時回空字串＝不適用）。

        hold＝鬆開才停（預設）、toggle＝再按一下停、double＝連按兩下停。
        """
        if self.end_of(spec) is None:
            return ""
        return self._at(self.end_modes, spec) or MODE_HOLD

    def start_of(self, end_spec: HotkeySpec) -> HotkeySpec | None:
        """反查：這顆鍵是不是某一組的結束鍵？回傳那一組的開始鍵。"""
        for s, end in zip(self.specs, self.ends):
            if end is not None and end == end_spec:
                return s
        return None

    def pairs(self) -> list[tuple[HotkeySpec, str]]:
        return list(zip(self.specs, self.modes))

    def quads(self) -> list[tuple[HotkeySpec, str, HotkeySpec | None, str]]:
        """**全部**的 `(開始, 開始行為, 結束, 結束行為)`（含停用的）。

        ⚠️ 停用的也要回傳 —— `entries` 靠它把整份清單寫回設定檔，
        少回一筆就等於「存一次設定就刪掉一個組合」。
        要比對／觸發請用 `active`。
        """
        return list(zip(self.specs, self.modes, self.ends, self.end_modes))

    @property
    def entries(self) -> list[str]:
        """寫回設定檔用的字串清單（停用的帶 `~` 前綴）。

        `["RightCtrl", "~F9,Esc@toggle"]`
        """
        return [(binding_text(s, m, e, em) if self.is_enabled(s)
                 else DISABLED_PREFIX + binding_text(s, m, e, em))
                for s, m, e, em in self.quads()]

    @property
    def active_label(self) -> str:
        """**啟用中**的組合，用「或」串起來（給提示訊息用）。"""
        on = [self.label_of(s) for s in self.active]
        off = [self.label_of(s).replace("（停用）", "", 1)
               for s in self.specs if not self.is_enabled(s)]
        text = " 或 ".join(on) if on else "（沒有啟用中的錄音鍵）"
        if off:
            text += f"　（停用：{'、'.join(off)}）"
        return text

    def label_of(self, spec: HotkeySpec) -> str:
        """單一組的顯示標籤（含兩邊的行為與啟用狀態）。"""
        start = START_MODE_LABELS.get(self.mode_of(spec), self.mode_of(spec))
        end = self.end_of(spec)
        if end is None:
            text = f"{spec.label}·{start}"
        else:
            tail = f"按 {spec_text(end)} {END_MODE_LABELS.get(self.end_mode_of(spec), '')}"
            text = f"{spec.label}·{start}、{tail}"
        return text if self.is_enabled(spec) else f"（停用）{text}"

    @property
    def labels(self) -> list[str]:
        return [self.label_of(s) for s in self.specs]

    @property
    def label(self) -> str:
        """人類看得懂的一行（多組時用「或」串起來）。"""
        return " 或 ".join(self.labels)

    @property
    def accepts_any_device(self) -> bool:
        """是否允許事件來自任何裝置。

        只要**有任一組**不是裝置原生鍵，就放行 —— 否則「用鍵盤錄音」
        會被裝置篩選擋掉（那正是使用者改成 F9 的目的）。
        """
        return any(not s.is_native_device_key for s in self.specs)

    def matched(self, vk: int, e0: bool, mods_down: set[str] | frozenset[str]) -> list[HotkeySpec]:
        """這次事件命中了哪幾組？**只看啟用中的**（停用的完全不理）。

        回傳清單（可能多組，例如把同一顆鍵設定了兩次不同寫法）——
        呼叫端只需要知道「有沒有中」與「要用哪個標籤顯示」。
        """
        out: list[HotkeySpec] = []
        for spec in self.active:
            if spec_hit(spec, vk, e0) and mods_ok(spec, mods_down):
                out.append(spec)
        return out

    def any_match(self, vk: int, e0: bool, mods_down: set[str] | frozenset[str]) -> bool:
        return any(spec_hit(s, vk, e0) and mods_ok(s, mods_down) for s in self.active)


def bindings(specs: list[str] | tuple[str, ...] | str | None,
             fallback: str = DEFAULT_SPEC,
             default_mode: str = MODE_HOLD) -> tuple[HotkeyBindings, str | None]:
    """把設定裡的字串清單解析成 `HotkeyBindings`。

    每一筆可以帶觸發方式：`"F9@double"`、`"Ctrl+Alt+R@toggle"`；
    沒寫就用 `default_mode`（設定檔的全域 `trigger_mode`）。

    回傳 `(bindings, error)`：
      · 全部解析成功 → error 為 None
      · 有認不得的 → 保留**認得的部分**，error 說明是哪一個壞掉
      · 一個都不認得（或清單是空的）→ 退回 `fallback`，error 說明原因

    ⚠️ 為什麼是「保留認得的部分」而不是整批丟掉：使用者加了三個鍵、
    其中一個打錯字，正確的兩顆應該繼續能用。但**絕不靜默** ——
    error 會一路傳到 UI 顯示。
    """
    raw_list: list[str]
    if specs is None:
        raw_list = []
    elif isinstance(specs, str):
        raw_list = [specs]
    else:
        raw_list = [str(s).strip() for s in specs if str(s or "").strip()]

    good: list[HotkeySpec] = []
    modes: list[str] = []
    ends: list[HotkeySpec | None] = []
    end_modes: list[str] = []
    enabled: list[bool] = []
    bad: list[str] = []
    for raw in raw_list:
        try:
            spec, mode, end, end_mode = parse_binding(raw, default_mode)
        except HotkeyError as exc:
            bad.append(f"{raw}（{exc}）")
            continue
        # ⚠️ 去重要比**解析後的規格**，不是原始字串 —— `F9` 與 `f9`、
        # `Ctrl+Alt+R` 與 `ctrl-alt-r` 都是同一組（`raw` 不參與相等性比較，
        # 見 `HotkeySpec`）。先前比字串，重複的寫法會塞進兩組，
        # 結果是「同一個鍵被觸發兩次」的隱患。
        if spec in good:
            continue
        good.append(spec)
        modes.append(mode)
        ends.append(end)
        end_modes.append(end_mode)
        enabled.append(_is_enabled_text(raw))

    err = None
    if bad:
        err = "無法解析：" + "；".join(bad)
    if not good:
        fb = parse(fallback)
        if not raw_list:
            # 空清單也要說得出原因 —— 呼叫端就是靠這個字串告訴使用者
            # 「你的設定沒有生效」，靜默退回預設值是最糟的結果。
            return (HotkeyBindings([fb], [default_mode], [None], [""], [True]),
                    f"沒有設定任何錄音鍵 —— 暫時沿用 {fb.label}")
        if err:
            err += f" —— 暫時沿用 {fb.label}"
        return HotkeyBindings([fb], [default_mode], [None], [""], [True]), err
    # ⚠️ 全部組合都停用時要**明講**。不然使用者只會看到「按了完全沒反應」，
    # 而畫面上每一列看起來都很正常（只是開關是關的）。
    if good and not any(enabled):
        err = (err + "；" if err else "") + "所有錄音鍵都被停用了 —— 按什麼都不會錄音"
    return HotkeyBindings(good, modes, ends, end_modes, enabled), err


def _is_enabled_text(entry: str) -> bool:
    """這一筆字串是不是啟用中（`~` 前綴＝停用）。"""
    return not (entry or "").strip().startswith(DISABLED_PREFIX)


def bindings_or_default(specs: list[str] | tuple[str, ...] | str | None,
                        default_mode: str = MODE_HOLD
                        ) -> tuple[HotkeyBindings, str | None]:
    """`bindings()` 的別名，語意與 `parse_or_default()` 對齊。"""
    return bindings(specs, default_mode=default_mode)
