#!/usr/bin/env python3
"""錄音鍵的觸發行為測試：hold / toggle / double ＋修飾鍵組合 ＋**多組**錄音鍵。

## 為什麼要測這個

「按鍵觸發」是整個產品的入口 —— 它壞掉的時候症狀是「按了沒反應」，
而使用者完全無從判斷是「沒收到事件」還是「被邏輯吃掉」。

這三種模式的行為差異全在**狀態機**裡（什麼時候開始錄、什麼時候停），
那是用眼睛盯不住的地方：

  · hold    按住開始、放開停止 → 放開後若沒停，會一直錄到 max_s
  · toggle  按一下開始、再按一下停 → 若把「開始」也當成「停」，就永遠錄不了
  · double  連兩下才動作 → 單擊**不可以**有任何反應（否則跟 toggle 一樣）

## 多組（[7]–[12]）為什麼也要測

使用者的實測回饋是「應該要能錄製、而且不該只有一個」，接著是
「雙擊只是開啟了錄音，怎樣結束？」。多組＋多模式最容易出的錯是**語意漂移**：

  · 加了第二組之後，第一組反而失效（篩選條件被覆蓋）
  · 裝置政策被第一組綁死 → 鍵盤上的 F9 被「不是目標裝置」擋掉
  · `LeftCtrl` 這種寫法左右不分（真的踩過：按右 Ctrl 也會觸發）
  · 模式變成全域 → 「麥克風按住說話 ＋ 鍵盤雙擊」做不到
  · 兩顆雙擊鍵的「等待第二下」互相配對
  · 按住不放的自動重複被當成連點 → 反而開始錄音

## 怎麼測

不碰真的鍵盤 —— 直接餵 `_handle_key()` 假造的 Raw Input 事件
（`RAWKEYBOARD` 結構 + hDevice）。這樣可以精確控制時序，包括雙擊的時間窗。

執行：python tests/test_trigger.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

import hotkey as hotkey_mod  # noqa: E402
import ptt as ptt_mod  # noqa: E402

failures: list[str] = []
VK = {"RCtrl": 0xA3, "LCtrl": 0xA2, "R": 0x52, "F8": 0x77, "F9": 0x78, "Esc": 0x1B, "A": 0x41,
      "LAlt": 0xA4, "LShift": 0xA0}
RI_KEY_E0 = 0x02
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


class FakeKB:
    """假的 RAWKEYBOARD（只需要 VKey / Flags / Message 三個欄位）。"""

    def __init__(self, vk: int, down: bool, e0: bool = False):
        self.VKey = vk
        self.Flags = RI_KEY_E0 if e0 else 0
        self.Message = WM_KEYDOWN if down else WM_KEYUP
        self.MakeCode = 0


class Cfg:
    def __init__(self, hotkey: str = "RightCtrl", mode: str = "hold"):
        # 舊欄位（單一）與新欄位（清單）並存 —— 跟真的 Config 一樣。
        # 測試要能同時覆蓋「舊設定檔只有 hotkey」與「新設定檔有 hotkeys」。
        self.hotkey = hotkey
        self.hotkeys = [hotkey] if hotkey else []
        # ⚠️ 觸發方式是**每一組自己**的（`F9@double`）；`trigger_mode` 是
        # 清單裡沒寫 `@模式` 時的後備值，所以測試可以單獨用 mode= 指定。
        self.trigger_mode = mode
        self.double_tap_ms = 400
        self.mic_stream = "per_press"
        self.idle_timeout_s = 7.0
        self.traditional = True
        self.remove_trailing_period = True
        self.mode = "auto"

    def effective_hotkeys(self) -> list:
        return list(self.hotkeys or ([self.hotkey] if self.hotkey else []))


class FakeCapture:
    def __init__(self, *a, **kw):
        self.device_id = kw.get("device_id", a[0] if a else 0)
        self.opened = self.closed = False
        self.data = bytearray()

    def __enter__(self):
        self.opened = True
        return self

    def __exit__(self, *e):
        self.closed = True

    def recorded(self):
        return bytes(self.data)

    @property
    def done(self):
        return False

    @property
    def full(self):
        return False

    def recycle(self):
        return True


class FakeEngine:
    def __init__(self, d="/tmp"):
        self.model_dir = Path(d)

    def transcribe(self, samples, rate):
        class R:
            text = "x"
        return R()


def make(mode: str, spec: str = "RightCtrl", filter_: str | None = None,
         device_name: str = "BT-HID/Col01"):
    cfg = Cfg(spec, mode)
    d = ptt_mod.PttDaemon(
        FakeEngine(), device_index=0, dry_run=True,
        device_filter=filter_, cfg_provider=lambda: cfg, device_provider=lambda: 0,
    )
    d._device_name_for_test = device_name
    return d, cfg


def press(d, vk: int, down: bool, e0: bool = False) -> None:
    d._handle_key(1, FakeKB(vk, down, e0))


def test_spec_parsing() -> None:
    print("\n[1] 按鍵設定解析")
    check("RightCtrl → VK_CONTROL + 限定右側",
          hotkey_mod.parse("RightCtrl").vk == 0x11
          and hotkey_mod.parse("RightCtrl").require_e0)
    check("F9 → 0x78", hotkey_mod.parse("F9").vk == 0x78)
    s = hotkey_mod.parse("Ctrl+Alt+R")
    # ⚠️ 修飾鍵是**小寫 canonical 名稱**（`ctrl`／`alt`），不是顯示名。
    #    理由：規格、事件、mac 的 `_mods_down` 三邊共用同一套寫法，
    #    比對時不必再各自 `lower()` 一次（那正是漂移的來源）。
    #    見 `tests/test_hotkey_names.py` [4]。
    check("Ctrl+Alt+R → 主鍵 R、修飾 ctrl+alt",
          s.vk == 0x52 and s.modifiers == frozenset({"ctrl", "alt"}),
          f"{s.vk:#x} {sorted(s.modifiers)}")
    check("順序不影響（Alt+Ctrl+R 相同）",
          hotkey_mod.parse("Alt+Ctrl+R") == hotkey_mod.parse("Ctrl+Alt+R"))
    for bad in ("", "Banana", "Ctrl+Alt+R+F9", "Ctrl+Alt"):
        try:
            hotkey_mod.parse(bad)
            check(f"應拒絕 {bad!r}", False, "竟然通過了")
        except hotkey_mod.HotkeyError:
            check(f"應拒絕 {bad!r}", True)


def test_hold() -> None:
    print("\n[2] hold：按住開始、放開停止（預設，裝置原生行為）")
    # device_filter=None → 不測裝置篩選（那是 [6] 的重點）
    d, _ = make("hold", filter_=None)
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    press(d, VK["RCtrl"], True, e0=True)
    check("按下 → 開始錄音", d._capturing is True)
    press(d, VK["RCtrl"], False, e0=True)
    check("放開 → 停止錄音", d._capturing is False)

    print("  （左 Ctrl 不該觸發 —— 設定限定右側）")
    d._capturing = False
    press(d, VK["LCtrl"], True, e0=False)
    check("左 Ctrl 不觸發", d._capturing is False)


def test_toggle() -> None:
    print("\n[3] toggle：按一下開始、再按一下停")
    d, _ = make("toggle", filter_=None)
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    # 一次「點擊」＝按下（觸發）+ 放開（toggle 模式下放開不做事）
    press(d, VK["RCtrl"], True, e0=True)
    check("第一下**按下** → 開始", d._capturing is True)
    press(d, VK["RCtrl"], False, e0=True)
    check("第一下放開 → 仍在錄（放開不可翻轉，否則等於沒錄到）",
          d._capturing is True)

    press(d, VK["RCtrl"], True, e0=True)
    check("第二下按下 → 停止", d._capturing is False)
    press(d, VK["RCtrl"], False, e0=True)
    check("第二下放開 → 仍是停", d._capturing is False)

    press(d, VK["RCtrl"], True, e0=True)
    check("第三下按下 → 又開始", d._capturing is True)


def test_double() -> None:
    print("\n[4] double：連兩下才動作，單擊不可有反應（全域 trigger_mode）")
    d, _ = make("double", filter_=None)
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    def tap() -> None:
        press(d, VK["RCtrl"], True, e0=True)
        press(d, VK["RCtrl"], False, e0=True)

    # 單擊
    tap()
    check("單擊 → **不動作**（這是 double 的重點）", d._capturing is False)

    # 等超過配對視窗
    time.sleep(d._double_window + 0.05)
    d.tick_trigger()
    check("逾時後配對狀態被清掉", not any(d._tap_at.values()), str(d._tap_at))

    # 快速連兩下 → 開始
    tap()
    tap()
    check("連兩下 → 開始錄音", d._capturing is True)

    # 再連兩下 → 停
    tap()
    tap()
    check("再連兩下 → 停止錄音", d._capturing is False)


def test_per_key_modes() -> None:
    print("\n[11] 每一組自己的觸發方式：混用 hold + toggle + double")
    d, cfg = make("hold", filter_=None)
    # 三顆鍵、三種模式同時存在 —— 這是實測回饋的核心需求
    # （「麥克風按住說話 ＋ 鍵盤 F9 雙擊」）。
    cfg.hotkeys = ["RightCtrl@hold", "F9@double", "F8@toggle"]
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    check("三組的模式都解析正確",
          d._bindings().modes == ("hold", "double", "toggle"),
          str(d._bindings().modes))
    check("標籤看得出兩邊的行為（UI 要顯示）",
          d._bindings().labels == ["Ctrl（限定右側）·按一下開始",
                                   "F9·連按兩下開始", "F8·按一下開始"],
          str(d._bindings().labels))

    print("  （F8 = toggle：按一下開始，放開不停）")
    press(d, VK["F8"], True)
    check("按下 F8 → 開始", d._capturing is True)
    press(d, VK["F8"], False)
    check("放開 F8 → 仍在錄（toggle 不可被放開中止）", d._capturing is True)
    press(d, VK["F8"], True)
    check("再按一下 F8 → 停", d._capturing is False)
    press(d, VK["F8"], False)

    print("  （F9 = double：單擊不動作）")
    press(d, VK["F9"], True)
    press(d, VK["F9"], False)
    check("F9 單擊 → 不動作", d._capturing is False)
    press(d, VK["F9"], True)
    press(d, VK["F9"], False)
    check("F9 連兩下 → 開始", d._capturing is True)
    # ⚠️ 停頓必須超過配對視窗：連續四下的第二對會跟前一對配成新的雙擊，
    # 那是正確行為（使用者真的是在連按），只是測不到「第二次雙擊」。
    time.sleep(d._double_window + 0.05)
    d.tick_trigger()
    press(d, VK["F9"], True)
    press(d, VK["F9"], False)
    press(d, VK["F9"], True)
    press(d, VK["F9"], False)
    check("F9 再連兩下 → 停（**同一個手勢收尾**，與開始對稱）",
          d._capturing is False)

    print("  （RightCtrl = hold：裝置原生行為不受其他兩組影響）")
    press(d, VK["RCtrl"], True, e0=True)
    check("按住 RightCtrl → 開始", d._capturing is True)
    press(d, VK["RCtrl"], False, e0=True)
    check("放開 → 停（hold 就是 hold）", d._capturing is False)

    print("  （兩顆 double 鍵的「等待第二下」要各自獨立）")
    d2, cfg2 = make("hold", filter_=None)
    cfg2.hotkeys = ["F9@double", "F8@double"]
    d2._start_recording = lambda label: setattr(d2, "_capturing", True)
    d2._finish_recording = lambda: setattr(d2, "_capturing", False)
    press(d2, VK["F9"], True)          # A 一下
    press(d2, VK["F9"], False)
    press(d2, VK["F8"], True)          # B 一下（不可跟 A 配成雙擊！）
    press(d2, VK["F8"], False)
    check("F9 一下 + F8 一下 ≠ 雙擊（狀態要分開記）", d2._capturing is False)


def test_modifier_repeat_ignored() -> None:
    print("\n[12] 按住不放的自動重複不可以被當成連點")
    d, cfg = make("hold", filter_=None)
    cfg.hotkeys = ["F9@double"]
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    # Windows 在按住時會一直送 keydown（自動重複）。若把每一次都算成
    # 「點一下」，按住不放反而會被判成雙擊 → **開始錄音**。實測會踩到。
    for _ in range(6):
        press(d, VK["F9"], True)
    check("連送 6 次 keydown（未放手）→ 不算連點", d._capturing is False)
    press(d, VK["F9"], False)
    check("放開後才結束這一下", d._capturing is False)
    press(d, VK["F9"], True)
    press(d, VK["F9"], False)
    check("真正的第二下才觸發", d._capturing is True)


def test_modifier_combo() -> None:
    print("\n[5] 組合鍵：Ctrl+Alt+R 必須兩個修飾鍵都按住")
    d, _ = make("hold", spec="Ctrl+Alt+R")
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    d._capturing = False
    press(d, VK["R"], True)                       # 沒按修飾鍵
    check("只按 R → 不觸發", d._capturing is False)

    press(d, VK["LCtrl"], True)
    press(d, VK["LAlt"], True)
    press(d, VK["R"], True)
    check("Ctrl+Alt 按住後按 R → 觸發", d._capturing is True)
    press(d, VK["R"], False)
    check("放開 R → 停止（hold）", d._capturing is False)

    press(d, VK["LCtrl"], False)
    press(d, VK["LAlt"], False)
    check("修飾鍵狀態清乾淨", d._mods_down == set(), str(sorted(d._mods_down)))

    print("  （多按一個修飾鍵不該觸發 —— 避免搶到別的快捷鍵）")
    d._capturing = False
    press(d, VK["LCtrl"], True)
    press(d, VK["LAlt"], True)
    press(d, VK["LShift"], True)                  # 多餘的
    press(d, VK["R"], True)
    check("Ctrl+Alt+Shift+R → 不觸發", d._capturing is False)


def test_any_device_when_custom_key() -> None:
    print("\n[6] 改成非裝置原生鍵時，不該再要求裝置符合 filter")
    d, _ = make("hold", spec="F9", filter_="00001124")
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda label=None: setattr(d, "_capturing", False)

    check("自訂鍵 → 允許任何裝置", d._allow_any_device() is True)
    d._is_target_device = lambda h: (False, "USB鍵盤")   # 非目標裝置
    press(d, VK["F9"], True)
    check("來自普通鍵盤的 F9 也能觸發（脫離硬體綁定）", d._capturing is True)

    print("  （裝置原生鍵仍要限定裝置）")
    d2, _ = make("hold", spec="RightCtrl", filter_="00001124")
    check("裝置原生鍵 → 仍限定裝置", d2._allow_any_device() is False)


def test_multi_key() -> None:
    print("\n[7] 多組錄音鍵：任何一組命中都要能錄音")
    d, cfg = make("hold")
    cfg.hotkey = "RightCtrl"
    cfg.hotkeys = ["RightCtrl", "F9"]        # ← 兩組：裝置原生 + 鍵盤
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    press(d, VK["RCtrl"], True, e0=True)
    check("第一組（RightCtrl）觸發", d._capturing is True)
    press(d, VK["RCtrl"], False, e0=True)
    check("放開 → 停", d._capturing is False)

    press(d, VK["F9"], True)
    check("第二組（F9）也觸發 —— 這才是「多組」的重點", d._capturing is True)
    press(d, VK["F9"], False)
    check("放開 → 停", d._capturing is False)

    print("  （沒有設定的鍵當然不能觸發）")
    press(d, VK["A"], True)
    check("沒設定的 A 不觸發", d._capturing is False)

    print("  （清單變空 → 退回舊的單一 hotkey 欄位，不可變成「完全沒反應」）")
    cfg.hotkeys = []
    check("effective_hotkeys() 退回 hotkey", cfg.effective_hotkeys() == ["RightCtrl"])
    press(d, VK["RCtrl"], True, e0=True)
    check("舊設定檔仍然可用", d._capturing is True)


def test_side_specific() -> None:
    print("\n[8] 左右側：LeftCtrl 不可以被右 Ctrl 觸發（實測踩過的 bug）")
    lc = hotkey_mod.parse("LeftCtrl")
    check("LeftCtrl 限定左側", lc.side == "left" and not lc.require_e0)
    check("左 Ctrl 事件命中", hotkey_mod.spec_hit(lc, 0xA2, False))
    check("通用 VK + 無 E0 命中", hotkey_mod.spec_hit(lc, 0x11, False))
    check("右 Ctrl 事件（0xA3）不命中", not hotkey_mod.spec_hit(lc, 0xA3, False))
    check("通用 VK + E0 不命中", not hotkey_mod.spec_hit(lc, 0x11, True))

    rc = hotkey_mod.parse("RightCtrl")
    check("RightCtrl 限定右側", rc.side == "right" and rc.require_e0)
    check("右 Ctrl 命中", hotkey_mod.spec_hit(rc, 0x11, True))
    check("左 Ctrl 不命中", not hotkey_mod.spec_hit(rc, 0xA2, False))

    plain = hotkey_mod.parse("Ctrl")
    check("沒寫左右的 Ctrl → 兩側都算",
          hotkey_mod.spec_hit(plain, 0x11, True) and hotkey_mod.spec_hit(plain, 0x11, False))

    print("  （實際跑一次：設定 LeftCtrl，按右 Ctrl 不該開始錄音）")
    d, _ = make("hold", spec="LeftCtrl")
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)
    press(d, VK["RCtrl"], True, e0=True)
    check("右 Ctrl 不觸發", d._capturing is False)
    press(d, VK["LCtrl"], True, e0=False)
    check("左 Ctrl 觸發", d._capturing is True)

    print("  （非修飾鍵不該被 E0 影響 —— F9 帶不帶 E0 都是 F9）")
    f9 = hotkey_mod.parse("F9")
    check("F9 + E0 仍命中", hotkey_mod.spec_hit(f9, 0x78, True))


def test_multi_bindings_parser() -> None:
    print("\n[9] 清單解析：typo 不可以把好的那幾組一起吃掉")
    b, err = hotkey_mod.bindings(["RightCtrl", "F9"])
    check("兩組都成立", len(b) == 2 and not err, str(err))
    check("label 用「或」串起來", " 或 " in b.label, b.label)

    b, err = hotkey_mod.bindings(["RightCtrl", "Banana", "F9"])
    check("打錯的那個被指出來", err is not None and "Banana" in err, str(err))
    check("認得的兩組仍然生效", len(b) == 2, str(b.labels))

    b, err = hotkey_mod.bindings(["Banana"])
    check("全部不認得 → 退回預設並說明", err is not None and len(b) == 1, str(err))

    b, err = hotkey_mod.bindings([])
    check("空清單要說得出原因（不可靜默）", err is not None, str(err))

    b, _ = hotkey_mod.bindings(["F9", "f9", "F9"])
    check("重複的寫法只留一組", len(b) == 1, str(b.labels))

    b, _ = hotkey_mod.bindings(["RightCtrl", "F9"])
    check("有任一組不是裝置原生鍵 → 放行所有裝置", b.accepts_any_device is True)
    b, _ = hotkey_mod.bindings(["RightCtrl"])
    check("只有裝置原生鍵 → 仍限定裝置", b.accepts_any_device is False)

    print("  （多工：任一組命中即可）")
    b, _ = hotkey_mod.bindings(["Ctrl+Alt+R", "F9"])
    check("F9 命中", b.any_match(0x78, False, set()))
    check("Ctrl+Alt+R 命中", b.any_match(0x52, False, {"Ctrl", "Alt"}))
    check("少了修飾鍵不命中", not b.any_match(0x52, False, {"Ctrl"}))
    check("多按一顆修飾鍵不命中", not b.any_match(0x52, False, {"Ctrl", "Alt", "Shift"}))

    check("matched() 回報命中的是哪些", [s.raw for s in b.matched(0x78, False, set())] == ["F9"])


def test_multi_key_device_policy() -> None:
    print("\n[10] 多組時的裝置政策：只要有一組脫離硬體，鍵盤就該能用")
    d, cfg = make("hold", spec="RightCtrl", filter_="00001124")
    cfg.hotkeys = ["RightCtrl", "F9"]
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)
    d._is_target_device = lambda h: (False, "USB鍵盤")     # 不是目標裝置

    check("有 F9 在裡面 → 允許任何裝置", d._allow_any_device() is True)
    press(d, VK["F9"], True)
    check("普通鍵盤的 F9 能觸發", d._capturing is True)
    check("裝置原生鍵仍然可用（多組是「或」，不是取代）",
          d._bindings().labels[0].startswith("Ctrl（限定右側）"),
          str(d._bindings().labels))


def test_auto_retry_first_empty() -> None:
    print("\n[13] 第一次沒收到音訊 → 同一口氣自動重錄（不是叫使用者再按一次）")
    d, _ = make("hold", filter_=None)

    class Cap:
        """假的擷取：前 N 次「沒有音訊」，之後才給資料。"""

        def __init__(self, empty_times, nbytes=3200):
            self.empty_times = empty_times
            self.nbytes = nbytes
            self.opens = 0
            self.device_id = 0
            self.max_bytes = 16000 * 2 * 60
            self.rate = 16000

        def __enter__(self):
            self.opens += 1
            return self

        def __exit__(self, *e):
            return False

        def recorded(self):
            return b"" if self.opens <= self.empty_times else b"\x00" * self.nbytes

        def recycle(self):
            return True

    caps = []

    def ensure():
        # ⚠️ 2026-09 之後錄音的資料流改了：音訊是由**主迴圈的
        # `_tick_capture()`** 餵進 `_rec_pcm`（暖機待命 + 前捲），
        # 不是放開時才去讀 `recorded()`。測試沒有主迴圈，所以在「開串流」
        # 這個時間點把音訊直接餵進去 —— 語意與真實流程一致。
        c = Cap(empty_times=1)
        caps.append(c)
        c.__enter__()
        # 真的 `_ensure_capture()` 會把串流登記成待命串流；這裡照做，
        # 否則 `_finish_recording` 會因為 `_cap is None` 直接 return。
        d._cap = c
        d._cap_seen = len(c.recorded())
        d._preroll_bytes = 0
        d._preroll = b""
        d._rec_pcm = bytearray(b"\x00" * c.nbytes if c.opens > c.empty_times else b"")
        return c

    # ⚠️ 只換掉「開串流」與「辨識之後的收尾」，其餘照真的路徑跑 ——
    # 全換掉就測不到「_cap 有沒有被正確接回來」這個最容易壞的地方。
    orig_start, orig_finish = d._start_recording, d._finish_recording
    d._ensure_capture = ensure
    d._start_recording = lambda label: orig_start(label)
    d._finish_recording = lambda: (orig_finish(), d._set_state("IDLE"))[1]

    press(d, VK["RCtrl"], True, e0=True)          # 按下 → 開始錄音（開串流 #1）
    check("按下 → 開了一次串流", len(caps) == 1, str(len(caps)))
    press(d, VK["RCtrl"], False, e0=True)         # 放開 → 0 bytes → 自動重錄
    check("第一次收到 0 bytes → 自動重開串流", len(caps) == 2, str(len(caps)))
    check("重錄有記錄（不是靜默）", d.stats.get("retried") == 1,
          str(d.stats.get("retried")))
    check("第一次不算「失敗」（救回來了）", d.stats["failed"] == 0,
          str(d.stats["failed"]))
    check("串流被接回來（還有東西在錄）", d._cap is not None,
          str(type(d._cap).__name__))

    print("  （重試只做一次：第二次又空就要放棄，否則會無窮迴圈）")
    d2, _ = make("hold", filter_=None)
    caps2 = []

    def ensure2():
        c = Cap(empty_times=99)
        caps2.append(c)
        c.__enter__()
        d2._cap = c                     # 照真的 `_ensure_capture()` 登記待命串流
        d2._cap_seen = len(c.recorded())
        d2._preroll_bytes = 0
        d2._preroll = b""
        d2._rec_pcm = bytearray()
        return c

    orig_start2, orig_finish2 = d2._start_recording, d2._finish_recording
    d2._ensure_capture = ensure2
    d2._start_recording = lambda label: orig_start2(label)
    d2._finish_recording = lambda: (orig_finish2(), d2._set_state("IDLE"))[1]

    press(d2, VK["RCtrl"], True, e0=True)
    press(d2, VK["RCtrl"], False, e0=True)
    check("一次按下最多重試一次（不會無窮迴圈）", len(caps2) == 2, str(len(caps2)))
    check("不是第三次重試，而是等下一次按下", d2._retrying is True and
          d2._retry_armed is False, f"retrying={d2._retrying} armed={d2._retry_armed}")
    check("成功救回的那一次記在 retried", d2.stats["retried"] == 1,
          str(d2.stats["retried"]))

    print("  （重試之後還是空 → 才算真的失敗，而且不會再重試第三次）")
    # ⚠️ 這裡**直接呼叫** `_finish_recording()` 而不是再餵一次 keyup：
    # 第二次放開時 `_cap` 已經是空的，keyup 那一條路會直接 return
    # （真實情況下使用者的放開是「一次」，重試的收尾是 daemon 自己的狀態轉移）。
    press(d2, VK["RCtrl"], True, e0=True)     # 真的再按一次 → 重新武裝
    d2._finish_recording()                    # 第一輪：空 → 自動重試
    check("再按一次會重新武裝（重試機會是「每次按下」一次）",
          len(caps2) == 4, str(len(caps2)))
    d2._finish_recording()                    # 重試那一輪也是空 → 放棄
    check("重試後仍空白 → failed 加一", d2.stats["failed"] == 1,
          str(d2.stats["failed"]))
    check("沒有第三次重試", len(caps2) == 4, str(len(caps2)))
    check("最後回到 IDLE", d2.state == "IDLE", d2.state)

    print("  （放開之後才空 → 不重錄：那會錄到使用者放開之後的環境音）")
    d3, _ = make("hold", filter_=None)
    caps3 = []

    def ensure3():
        c = Cap(empty_times=0, nbytes=0)      # 永遠 0 bytes
        caps3.append(c)
        c.__enter__()
        d3._cap = c
        d3._cap_seen = 0
        d3._preroll_bytes = 0
        d3._preroll = b""
        d3._rec_pcm = bytearray()
        return c

    orig_start3, orig_finish3 = d3._start_recording, d3._finish_recording
    d3._ensure_capture = ensure3
    d3._start_recording = lambda label: orig_start3(label)
    d3._finish_recording = lambda: (orig_finish3(), d3._set_state("IDLE"))[1]
    press(d3, VK["RCtrl"], True, e0=True)
    # ⚠️ 模擬「放開的那一刻已經知道收不到音訊」。
    #    這裡要清的是**引擎**的 pressed（`_key_held` 是它的衍生值：
    #    `press(down)` 會設下 `pressed=True`，而 `_on_trigger_finish()`
    #    在呼叫 `_finish_recording()` 之前會用 `pressed` 覆蓋 `_key_held` ——
    #    所以只改 `_key_held` 沒有用，會在收尾時被蓋回去）。
    #    實際情境：使用者按著講完、放開 → 引擎在 keyup 時就知道他放開了。
    d3.trigger.pressed = False
    press(d3, VK["RCtrl"], False, e0=True)
    check("使用者已放開 → 不重錄", len(caps3) == 1, str(len(caps3)))


def test_explicit_end_key() -> None:
    print("\n[14] 開始／結束可以是**不同顆鍵**（配對：F9,Escape）")
    d, cfg = make("hold", filter_=None)
    cfg.hotkeys = ["F9,Esc"]                 # 開始 F9、結束 Esc
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    b = d._bindings()
    check("標籤說出兩邊的行為", "按 Esc 鬆開才停" in b.label, b.label)
    check("end_of(F9) 是 Esc",
          hotkey_mod.spec_text(b.end_of(hotkey_mod.parse("F9"))) == "Esc",
          hotkey_mod.spec_text(b.end_of(hotkey_mod.parse("F9"))))

    press(d, VK["F9"], True)
    check("按 F9（開始鍵）→ 開始錄音", d._capturing is True)
    press(d, VK["F9"], False)
    check("**放開 F9 不會結束**（結束由 Esc 負責）", d._capturing is True)
    press(d, VK["Esc"], True)
    check("Esc 按下時還沒停（預設行為＝鬆開才停）", d._capturing is True)
    press(d, VK["Esc"], False)
    check("鬆開 Esc → 停止錄音", d._capturing is False)

    print("  （模式與結束鍵並存時，**結束鍵優先**）")
    d2, cfg2 = make("hold", filter_=None)
    cfg2.hotkeys = ["F9@double,Esc"]        # 連按兩下開始 + 明確結束鍵 Esc
    d2._start_recording = lambda label: setattr(d2, "_capturing", True)
    d2._finish_recording = lambda: setattr(d2, "_capturing", False)
    tap = lambda: (press(d2, VK["F9"], True), press(d2, VK["F9"], False))
    tap()
    check("單擊不開始（double 模式）", d2._capturing is False)
    tap()
    check("連兩下 → 開始", d2._capturing is True)
    press(d2, VK["Esc"], True); press(d2, VK["Esc"], False)
    check("結束鍵仍然有效（不會被 double 模式蓋掉）", d2._capturing is False)

    print("  （結束鍵自己的行為：鬆開才停 vs 再按一下停）")
    d4, cfg4 = make("hold", filter_=None)
    cfg4.hotkeys = ["F9,Esc"]          # 鬆開才停
    d4._start_recording = lambda label: setattr(d4, "_capturing", True)
    d4._finish_recording = lambda: setattr(d4, "_capturing", False)
    press(d4, VK["F9"], True); press(d4, VK["F9"], False)
    press(d4, VK["Esc"], True)              # 按下（還沒鬆開）
    check("Esc 按下時還沒停（行為＝鬆開才停）", d4._capturing is True)
    press(d4, VK["Esc"], False)             # 鬆開
    check("鬆開 Esc → 停", d4._capturing is False)

    d5, cfg5 = make("hold", filter_=None)
    cfg5.hotkeys = ["F9,Esc@toggle"]        # 再按一下停
    d5._start_recording = lambda label: setattr(d5, "_capturing", True)
    d5._finish_recording = lambda: setattr(d5, "_capturing", False)
    press(d5, VK["F9"], True); press(d5, VK["F9"], False)
    press(d5, VK["Esc"], True)
    check("Esc 一按下就停（行為＝再按一下停）", d5._capturing is False)
    press(d5, VK["Esc"], False)
    check("鬆開不會再停一次（狀態已清）", d5._capturing is False)

    print("  （沒設定結束鍵時，維持原本的自動收尾）")
    d3, cfg3 = make("hold", filter_=None)
    cfg3.hotkeys = ["F9"]
    d3._start_recording = lambda label: setattr(d3, "_capturing", True)
    d3._finish_recording = lambda: setattr(d3, "_capturing", False)
    check("end_of 是 None（＝自動）",
          d3._bindings().end_of(hotkey_mod.parse("F9")) is None)
    press(d3, VK["F9"], True)
    press(d3, VK["F9"], False)
    check("hold：放開就結束", d3._capturing is False)
    check("閒置時按別的鍵不會亂結束", d3._end_owner(0x77, False) is None)


def test_binding_pair_parsing() -> None:
    print("\n[15] 配對語法：`,` 是配對、`+` 是組合鍵、行為放最後")
    cases = [
        # 字串, 開始鍵, 開始行為, 結束鍵, 結束行為
        ("F9", "F9", "hold", None, ""),
        ("F9@double", "F9", "double", None, ""),
        ("F9,F8", "F9", "hold", "F8", "hold"),
        ("Ctrl+Alt+R", "Ctrl+Alt+R", "hold", None, ""),
        ("Ctrl+Alt+R,Esc", "Ctrl+Alt+R", "hold", "Esc", "hold"),
        ("F9,Esc@toggle", "F9", "hold", "Esc", "toggle"),
        ("F9,Esc@hold@toggle", "F9", "hold", "Esc", "toggle"),   # 兩個都寫（第一個也可能是預設）
        # 舊寫法（`@` 貼在自己那一邊）也要吃得下，因為使用者很自然會這樣寫
        ("F9@double,Esc", "F9", "double", "Esc", "hold"),
        ("F9@double,Esc@toggle", "F9", "double", "Esc", "toggle"),
    ]
    for text, sk, sm, ek, em in cases:
        s, s_mode, e, e_mode = hotkey_mod.parse_binding(text)
        got = (hotkey_mod.spec_text(s), s_mode,
               hotkey_mod.spec_text(e) if e else None, e_mode)
        check(f"{text}", got == (sk, sm, ek, em), str(got))

    print("  （最容易搞錯的一條：`Ctrl+Alt+R` 不可以被切成配對）")
    s, _, e, _ = hotkey_mod.parse_binding("Ctrl+Alt+R")
    check("整串就是一個組合鍵", e is None and hotkey_mod.spec_text(s) == "Ctrl+Alt+R")

    print("  （round-trip：寫回去要一模一樣，否則存一次就變形一次）")
    for text in ("F9", "F9@double", "F9,F8", "F9,Esc@toggle",
                 "Ctrl+Alt+R,Esc@toggle"):
        back = hotkey_mod.binding_text(*hotkey_mod.parse_binding(text))
        check(f"{text} round-trip", back == text, back)

    print("  （舊寫法會收斂成正式寫法 —— 兩者語意相同，這是刻意的）")
    check("F9@double,Esc@toggle → F9,Esc@double@toggle",
          hotkey_mod.binding_text(
              *hotkey_mod.parse_binding("F9@double,Esc@toggle"))
          == "F9,Esc@double@toggle")
    check("F9,Esc@hold → F9,Esc（hold 是預設，不寫）",
          hotkey_mod.binding_text(*hotkey_mod.parse_binding("F9,Esc@hold"))
          == "F9,Esc")

    print("  （壞掉的字串要拒絕，而且說得出原因）")
    for bad in ("F9,Banana", "F9,Esc@dboule"):
        try:
            hotkey_mod.parse_binding(bad)
            check(f"應拒絕 {bad}", False, "竟然通過了")
        except hotkey_mod.HotkeyError as exc:
            check(f"應拒絕 {bad}", True, str(exc))


def test_per_side_modes() -> None:
    print("\n[16] 開始與結束**各有自己的行為**（下拉選單的資料來源）")
    b, err = hotkey_mod.bindings(["RightCtrl", "F9@double,Esc@toggle", "F8,Esc"])
    check("三組都解析成功", len(b) == 3 and err is None, str(err))
    f9 = hotkey_mod.parse("F9")
    check("F9：開始＝double", b.mode_of(f9) == "double", b.mode_of(f9))
    check("F9：結束＝toggle", b.end_mode_of(f9) == "toggle", b.end_mode_of(f9))
    f8 = hotkey_mod.parse("F8")
    check("F8：開始＝預設 hold", b.mode_of(f8) == "hold", b.mode_of(f8))
    check("F8：結束＝預設 hold（＝鬆開才停）", b.end_mode_of(f8) == "hold",
          b.end_mode_of(f8))
    rc = hotkey_mod.parse("RightCtrl")
    check("沒有結束鍵 → 結束行為是空字串（UI 的下拉要 disable）",
          b.end_mode_of(rc) == "", repr(b.end_mode_of(rc)))
    check("標籤同時說出兩邊",
          b.labels[1] == "F9·連按兩下開始、按 Esc 再按一下停", b.labels[1])
    check("entries 可以寫回設定檔",
          b.entries == ["RightCtrl", "F9,Esc@double@toggle", "F8,Esc"],
          str(b.entries))

    print("  （每一組的兩邊行為互不影響）")
    for spec, sm, em in ((hotkey_mod.parse("F9"), "double", "toggle"),
                         (hotkey_mod.parse("F8"), "hold", "hold")):
        check(f"{hotkey_mod.spec_text(spec)} 的行為",
              (b.mode_of(spec), b.end_mode_of(spec)) == (sm, em),
              f"{b.mode_of(spec)}/{b.end_mode_of(spec)}")


def test_combos_at_the_same_time() -> None:
    print("\n[17] 多種組合**同時生效**（不是一次只能選一個）")
    d, cfg = make("hold", filter_=None)
    # 三組不同性質的組合一起開：裝置原生鍵、鍵盤單鍵配對、鍵盤組合鍵
    cfg.hotkeys = ["RightCtrl", "F9,Esc", "Ctrl+Alt+R@double"]
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)
    b = d._bindings()
    check("三組都在生效中", len(b.active) == 3, str(len(b.active)))

    print("  （逐一驗證：每一組都能用，而且互不干擾）")
    press(d, VK["RCtrl"], True, e0=True)
    check("① RightCtrl（按住）可用", d._capturing is True)
    press(d, VK["RCtrl"], False, e0=True)

    press(d, VK["F9"], True); press(d, VK["F9"], False)
    check("② F9 可用", d._capturing is True)
    press(d, VK["Esc"], True); press(d, VK["Esc"], False)
    check("② F9 的結束鍵 Esc 可用", d._capturing is False)

    press(d, VK["LCtrl"], True); press(d, VK["LAlt"], True)
    press(d, VK["R"], True); press(d, VK["R"], False)
    check("③ Ctrl+Alt+R 單擊不動作（它是連按兩下）", d._capturing is False)
    press(d, VK["R"], True); press(d, VK["R"], False)
    check("③ Ctrl+Alt+R 連兩下可用", d._capturing is True)
    press(d, VK["LCtrl"], False); press(d, VK["LAlt"], False)


def test_enable_disable() -> None:
    print("\n[18] 啟用／停用：停用的組合**完全不理**，但設定要留著")
    d, cfg = make("hold", filter_=None)
    cfg.hotkeys = ["RightCtrl", "~F9,Esc", "~F8"]
    d._start_recording = lambda label: setattr(d, "_capturing", True)
    d._finish_recording = lambda: setattr(d, "_capturing", False)

    b = d._bindings()
    check("三組都在清單裡（停用的沒有被刪掉）", len(b.specs) == 3, str(len(b.specs)))
    check("只有啟用的會比對", len(b.active) == 1, str(len(b.active)))
    check("啟用的是 RightCtrl",
          [hotkey_mod.spec_text(s) for s in b.active] == ["RightCtrl"],
          str([hotkey_mod.spec_text(s) for s in b.active]))
    check("標籤看得出停用", "（停用）" in b.label, b.label)
    check("entries 寫回去仍然帶 `~`（不會存一次就消失）",
          b.entries == ["RightCtrl", "~F9,Esc", "~F8"], str(b.entries))

    press(d, VK["F9"], True)
    check("停用的 F9 不觸發", d._capturing is False)
    press(d, VK["F9"], False)
    press(d, VK["F8"], True)
    check("停用的 F8 不觸發", d._capturing is False)
    press(d, VK["F8"], False)
    press(d, VK["RCtrl"], True, e0=True)
    check("啟用的 RightCtrl 照常可用", d._capturing is True)
    press(d, VK["RCtrl"], False, e0=True)

    print("  （停用那一組的結束鍵也不該有反應）")
    d2, cfg2 = make("hold", filter_=None)
    cfg2.hotkeys = ["~F9,Esc", "F8"]        # F9 那組整組停用（含它的 Esc）
    d2._start_recording = lambda label: setattr(d2, "_capturing", True)
    d2._finish_recording = lambda: setattr(d2, "_capturing", False)
    press(d2, VK["F8"], True)
    check("F8 開始", d2._capturing is True)
    press(d2, VK["Esc"], True); press(d2, VK["Esc"], False)
    check("停用那一組的 Esc 不會停掉 F8 的錄音", d2._capturing is True)
    press(d2, VK["F8"], False)
    check("放開 F8 才停", d2._capturing is False)

    print("  （全部停用要**明講**，不能只讓使用者覺得「按了沒反應」）")
    b3, err3 = hotkey_mod.bindings(["~RightCtrl", "~F9"])
    check("全部停用會回報原因", err3 is not None and "停用" in str(err3), str(err3))
    check("而且沒有任何一組生效中", len(b3.active) == 0, str(len(b3.active)))


def test_key_test_mode() -> None:
    print("\n[19] 「測試」模式：只回報收到什麼，**絕不錄音**")
    d, cfg = make("hold", filter_=None)
    cfg.hotkeys = ["RightCtrl", "F9,Esc"]
    started = []
    d._start_recording = lambda label: started.append(label)
    d._finish_recording = lambda: None

    st = d.start_key_test(5.0, "F9")
    check("測試可以啟動", st.get("ok") is True, str(st))
    check("狀態回報 running", d.test_state()["running"] is True)

    press(d, VK["F9"], True)
    press(d, VK["Esc"], True)
    press(d, VK["A"], True)
    check("測試期間**完全沒有開始錄音**", started == [], str(started))

    hits = d.test_state()["hits"]
    check("收到三個事件（含不相關的鍵）", len(hits) == 3, str(len(hits)))
    check("F9 被認成開始鍵", "開始鍵" in hits[0]["roles"][0], str(hits[0]["roles"]))
    check("Esc 被認成結束鍵", "結束鍵" in hits[1]["roles"][0], str(hits[1]["roles"]))
    check("沒設定的鍵也回報「不相關」",
          "不相關的鍵" in hits[2]["roles"][0], str(hits[2]["roles"]))
    press(d, VK["F9"], False)
    press(d, VK["Esc"], False)
    press(d, VK["A"], False)

    d.cancel_key_test()
    check("可以取消", d.test_state()["running"] is False)

    print("  （測試模式過期後就恢復正常）")
    # ⚠️ `start_key_test` 會把秒數抬到 3 秒下限（避免「還沒按就結束」），
    # 所以這裡直接改時間戳來模擬過期 —— 測的是「過期後行為要恢復」。
    d.start_key_test(3.0, "")
    d._test_until = time.monotonic() - 0.1
    check("逾時自動結束", d.test_state()["running"] is False)
    press(d, VK["F9"], True)
    check("恢復正常後 F9 會開始錄音", len(started) == 1, str(started))
    press(d, VK["F9"], False)

    print("  （UI 送太短的秒數會被抬到 3 秒，避免「還沒按就結束」）")
    d.start_key_test(0.5, "")
    check("下限 3 秒", d.test_state()["remaining"] > 2.5,
          str(d.test_state()["remaining"]))
    d.cancel_key_test()


def test_warm_standby_preroll() -> None:
    print("\n[20] 暖機待命 + 前捲：第一次按下就要有音（不能再靠重試）")
    d, _ = make("hold", filter_=None)

    class Cap:
        """可控的假擷取：`feed()` 模擬「驅動程式又送來一段音訊」。"""

        def __init__(self):
            self.device_id = 0
            self.rate = 16000
            self.max_bytes = 16000 * 2 * 60
            self._data = bytearray()
            self.recycles = 0

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def recorded(self):
            return bytes(self._data)

        def feed(self, nbytes):
            self._data.extend(b"\x00" * nbytes)

        def recycle(self):
            self.recycles += 1
            self._data = bytearray()
            return True

    cap = Cap()
    opened = []
    d._ensure_capture = lambda: (opened.append(1), setattr(d, "_cap", cap), cap)[2]
    d._cap = cap
    d._cap_seen = 0
    d._preroll_bytes = int(d._rate * d._PREROLL_S) * 2      # 0.25 秒
    d._preroll = b""
    d._rec_pcm = bytearray()

    print("  （串流開著、主迴圈餵音訊，但還沒按下 → 進前捲，不當成錄音）")
    for _ in range(10):
        cap.feed(1600)                       # 每次 50ms
        d._tick_capture()
    check("待命期間的音訊進了前捲", len(d._preroll) == d._preroll_bytes,
          f"{len(d._preroll)}/{d._preroll_bytes} bytes")
    check("待命期間**沒有**累積成錄音", len(d._rec_pcm) == 0, str(len(d._rec_pcm)))

    print("  （按下 → 錄音緩衝先裝入前捲，再把之後的音訊接上）")
    d._start_recording("-")
    check("按下時就拿到前捲（第一次按下不必等 SCO）",
          len(d._rec_pcm) == d._preroll_bytes, str(len(d._rec_pcm)))
    check("前捲用完就清掉（不會重複塞）", d._preroll == b"")

    cap.feed(32000)                          # 之後的 1 秒
    d._tick_capture()
    check("錄音中的新音訊接在前捲後面",
          len(d._rec_pcm) == d._preroll_bytes + 32000, str(len(d._rec_pcm)))

    print("  （放開後回到待命：緩衝清空、前捲重新累積）")
    d._finish_recording()
    check("放開後錄音緩衝清空", len(d._rec_pcm) == 0, str(len(d._rec_pcm)))
    check("串流還在（暖機待命沒有被關掉）", d._cap is cap)
    cap.feed(1600)
    d._tick_capture()
    check("待命又開始累積前捲", len(d._preroll) == 1600, str(len(d._preroll)))

    print("  （緩衝區回收後，索引要歸零 —— 否則會誤判一大段是新音訊）")
    cap.feed(cap.max_bytes)                  # 灌到超過回收門檻
    d._tick_capture()
    check("緩衝區滿一半 → 有回收", cap.recycles >= 1, str(cap.recycles))
    check("回收後索引歸零", d._cap_seen == 0, str(d._cap_seen))
    cap.feed(800)
    d._tick_capture()
    check("回收後只算真正的新音訊（不是整段緩衝區）",
          len(d._preroll) <= d._preroll_bytes, str(len(d._preroll)))


def main() -> int:
    print("=" * 74)
    print("錄音鍵觸發行為測試")
    print("=" * 74)
    ptt_mod.Capture = FakeCapture
    test_spec_parsing()
    test_hold()
    test_toggle()
    test_double()
    test_modifier_combo()
    test_any_device_when_custom_key()
    test_multi_key()
    test_side_specific()
    test_multi_bindings_parser()
    test_multi_key_device_policy()
    test_per_key_modes()
    test_modifier_repeat_ignored()
    test_auto_retry_first_empty()
    test_explicit_end_key()
    test_binding_pair_parsing()
    test_per_side_modes()
    test_combos_at_the_same_time()
    test_enable_disable()
    test_key_test_mode()
    test_warm_standby_preroll()

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
