#!/usr/bin/env python3
"""錄音鍵的觸發行為測試：hold / toggle / double ＋修飾鍵組合。

## 為什麼要測這個

「按鍵觸發」是整個產品的入口 —— 它壞掉的時候症狀是「按了沒反應」，
而使用者完全無從判斷是「沒收到事件」還是「被邏輯吃掉」。

這三種模式的行為差異全在**狀態機**裡（什麼時候開始錄、什麼時候停），
那是用眼睛盯不住的地方：

  · hold    按住開始、放開停止 → 放開後若沒停，會一直錄到 max_s
  · toggle  按一下開始、再按一下停 → 若把「開始」也當成「停」，就永遠錄不了
  · double  連兩下才動作 → 單擊**不可以**有任何反應（否則跟 toggle 一樣）

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

import hotkey as hotkey_mod  # noqa: E402
import ptt as ptt_mod  # noqa: E402

failures: list[str] = []
VK = {"RCtrl": 0xA3, "LCtrl": 0xA2, "R": 0x52, "F9": 0x78, "A": 0x41,
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
        self.hotkey = hotkey
        self.trigger_mode = mode
        self.double_tap_ms = 400
        self.mic_stream = "per_press"
        self.idle_timeout_s = 7.0
        self.traditional = True
        self.remove_trailing_period = True
        self.mode = "auto"


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
    check("Ctrl+Alt+R → 主鍵 R、修飾 Ctrl+Alt",
          s.vk == 0x52 and s.modifiers == frozenset({"Ctrl", "Alt"}),
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
    print("\n[4] double：連兩下才動作，單擊不可有反應")
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
    check("逾時後配對狀態被清掉", d._last_tap_at == 0.0)

    # 快速連兩下 → 開始
    tap()
    tap()
    check("連兩下 → 開始錄音", d._capturing is True)

    # 再連兩下 → 停
    tap()
    tap()
    check("再連兩下 → 停止錄音", d._capturing is False)


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
