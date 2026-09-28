#!/usr/bin/env python3
"""macOS 全域按鍵監聽（CGEventTap）。

## 這個模組取代什麼

Windows 版的 `keycode_logger.py`（762 行）用 **Raw Input + 訊息迴圈**。
macOS 沒有那套東西，對應的是 **CGEventTap**：

    Windows                          macOS
    CreateWindowExW + 訊息迴圈        CFRunLoop + CGEventTap callback
    RIDEV_INPUTSINK 攔截全系統         kCGSessionEventTap 攔截 session
    RAWKEYBOARD.VKey（VK 碼）         kCGKeyboardEventKeycode（Carbon keycode）
    Flags & RI_KEY_E0（分左右）        keycode 本身就分左右（59 vs 62）

## 與 Windows 版最大的設計差異：**不做裝置辨識**

Windows 版必須分辨「這顆鍵是不是目標裝置送的」（AGENTS.md §8.2 規則 2），
因為預設鍵 RightCtrl 在實體鍵盤上也有。

**macOS 的 CGEventTap 做不到** —— 這是實測結論，不是推測：

    欄位                     右 Ctrl（實體鍵盤）  錄音鍵（藍牙裝置）
    kCGKeyboardEventKeyboardType    40                 40
    kCGEventSourceUserData           0                  0
    kCGEventSourceStateID            1                  1
    kCGTabletEventDeviceID           0                  0
                                 ↑ 全部相同，無法區分

所以 mac 版的策略是**不做裝置篩選**，靠「錄音鍵可自由設定、選一個不衝突的鍵」
來達到同樣的效果。這與 `hotkey.py` 開頭寫的設計原則一致：
**「使用者要的是完全脫離硬體綁定」**。

## 依賴

需要 **pyobjc**（`Quartz`）。這是 mac 上唯一的額外依賴：

    pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa

## 權限

CGEventTap 需要「**輔助使用**」（Accessibility）權限，否則
`CGEventTapCreate` 會回 None。這是 macOS 的 TCC 保護，跟防毒無關。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Callable

import keys

# ---------------------------------------------------------------- 事件型別


@dataclass(frozen=True)
class KeyEvent:
    """一個**平台無關**的按鍵事件。

    這是整個移植的接縫：狀態機（ptt.py）只認這個結構，
    所以 Windows 的 Raw Input 與 macOS 的 CGEventTap 可以餵同一套邏輯。

    `key` 是 canonical 名稱（"rightctrl" / "f9" / "r"），不是鍵碼 ——
    鍵碼是平台專屬的，名稱才是共通語言。
    """

    key: str                     # canonical 名稱，小寫
    down: bool                   # True = 按下，False = 放開
    autorepeat: bool = False     # 按住產生的重複事件
    device: str | None = None    # 裝置標籤；macOS 恆為 None（無法取得）


# ---------------------------------------------------------------- CGEventTap

# CGEventFlags 位元（不從 pyobjc 拿，因為常數名稱在各版本不一致）
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_ALTERNATE = 0x00080000
FLAG_COMMAND = 0x00100000
FLAG_ALPHA_SHIFT = 0x00010000
FLAG_SECONDARY_FN = 0x00800000

EVENT_KEYDOWN = 10
EVENT_KEYUP = 11
EVENT_FLAGS_CHANGED = 12


class PermissionError_(RuntimeError):
    """缺少輔助使用權限 —— 訊息要能直接顯示給使用者。"""


def accessibility_hint() -> str:
    return (
        "需要「輔助使用」權限才能全域監聽按鍵。\n"
        "  系統設定 → 隱私權與安全性 → 輔助使用 → 把 Terminal（或本程式）打勾。\n"
        "  改完要**完全結束再重開**該程式（⌘Q），權限才會生效。"
    )


class MacKeyListener:
    """用 CGEventTap 監聽全域按鍵，把事件轉成 `KeyEvent`。

    用法：

        listener = MacKeyListener(on_event)
        listener.run()          # 阻塞，跑 CFRunLoop

    只監聽、不攔截（`kCGEventTapOptionListenOnly`）—— 絕不吃掉使用者的按鍵。
    這對應 AGENTS.md §8.3「導覽鍵不要抑制」。
    """

    def __init__(self, on_event: Callable[[KeyEvent], None],
                 debug: bool = False):
        self.on_event = on_event
        self.debug = debug
        self._Quartz = None
        self._tap = None
        self._source = None
        # 修飾鍵的按下狀態。**必須自己維護**：
        # FlagsChanged 事件的 flags 是「所有修飾鍵的聯集」，
        # 同一個 keycode 按下與放開都會送，只能靠 flags 的變化判斷。
        self._mod_state: dict[str, bool] = {}
        self._pressed: set[str] = set()      # 目前按住的非修飾鍵（判斷自動重複）
        self._stopping = False
        self.count = 0

    # -- 載入 pyobjc --
    def _ensure_quartz(self):
        if self._Quartz is not None:
            return self._Quartz
        try:
            import Quartz  # type: ignore
        except ImportError as exc:
            raise PermissionError_(
                "載入不到 Quartz（pyobjc）。\n"
                "  請安裝：pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa\n"
                f"  原始錯誤：{exc}"
            ) from exc
        self._Quartz = Quartz
        return Quartz

    # -- callback --
    def _callback(self, proxy, etype, event, refcon):
        """每一顆按鍵都會經過這裡。**絕不拋錯**（拋錯會讓 tap 失效）。"""
        try:
            Q = self._Quartz
            kc = int(Q.CGEventGetIntegerValueField(
                event, Q.kCGKeyboardEventKeycode))
            raw_flags = int(Q.CGEventGetFlags(event))
            name = keys.code_to_name(kc)

            if name is None:
                # 不認得的鍵（媒體鍵、廠商自訂鍵…）→ 安靜略過。
                # 不報錯是因為這是常態，不是異常。
                return event

            if etype == EVENT_FLAGS_CHANGED:
                ev = self._on_flags_changed(name, raw_flags)
            elif etype in (EVENT_KEYDOWN, EVENT_KEYUP):
                ev = self._on_key(name, etype == EVENT_KEYDOWN)
            else:
                return event

            if ev is not None:
                self.count += 1
                if self.debug:
                    self._log(ev, kc, raw_flags)
                self.on_event(ev)
        except Exception as exc:                    # noqa: BLE001
            # ⚠️ 這裡刻意吞掉所有例外：CGEventTap 的 callback 一旦拋錯，
            #    macOS 會把整個 tap 停用（kCGEventTapDisabledByTimeout），
            #    變成「按了完全沒反應」而且沒有任何訊息。
            #    寧可漏掉一個事件，也不要讓整個監聽死掉。
            if self.debug:
                print(f"  ⚠️ 監聽 callback 錯誤（已忽略）：{type(exc).__name__}: {exc}")
        return event

    def _on_flags_changed(self, name: str, raw_flags: int) -> KeyEvent | None:
        """修飾鍵事件 —— 靠 flags 判斷是按下還是放開。"""
        flag = keys.modifier_flag(name)
        if flag is None:
            return None                # 不是我們認得的修飾鍵
        now_on = bool(raw_flags & flag)
        was_on = self._mod_state.get(name, False)
        if now_on == was_on:
            return None                # 狀態沒變（例如另一側的修飾鍵動了）
        self._mod_state[name] = now_on
        return KeyEvent(key=name, down=now_on, device=None)

    def _on_key(self, name: str, down: bool) -> KeyEvent | None:
        """一般按鍵。自動重複要標記出來 —— 分派層要忽略它。"""
        if down:
            repeat = name in self._pressed
            self._pressed.add(name)
        else:
            repeat = False
            self._pressed.discard(name)
        return KeyEvent(key=name, down=down, autorepeat=repeat, device=None)

    def _log(self, ev: KeyEvent, kc: int, flags: int) -> None:
        state = "DOWN" if ev.down else "UP  "
        extra = " (repeat)" if ev.autorepeat else ""
        print(f"  · [{self.count:4d}] {ev.key:12s} {state} "
              f"keycode={kc:<4} flags=0x{flags:08X}{extra}")

    # -- 建立 tap --
    def _create_tap(self):
        Q = self._ensure_quartz()
        mask = (Q.CGEventMaskBit(EVENT_KEYDOWN)
                | Q.CGEventMaskBit(EVENT_KEYUP)
                | Q.CGEventMaskBit(EVENT_FLAGS_CHANGED))
        tap = Q.CGEventTapCreate(
            Q.kCGSessionEventTap,
            Q.kCGHeadInsertEventTap,
            Q.kCGEventTapOptionListenOnly,     # 只聽，不攔截
            mask, self._callback, None)
        if tap is None:
            raise PermissionError_(accessibility_hint())
        return tap

    def run(self, seconds: float | None = None,
            on_tick: Callable[[], None] | None = None) -> None:
        """跑事件迴圈。`seconds=None` 表示一直跑到 `stop()`。

        為什麼不阻塞在 `CFRunLoopRun()`：那是**無法中斷**的。
        用「跑一小段、檢查一次停止旗標」才能在收到訊號時乾淨收尾，
        也才能實作 `seconds` 逾時（測試需要）。

        `on_tick`：每一輪（約 0.1 秒）呼叫一次。給常駐程式用來把狀態
        推給 UI —— 沒有這個的話，UI 的狀態燈與音量表不會更新
        （因為按鍵事件只在按下時才發生，閒置時迴圈是空轉的）。
        """
        Q = self._ensure_quartz()
        self._tap = self._create_tap()
        self._source = Q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        run_loop = Q.CFRunLoopGetCurrent()
        Q.CFRunLoopAddSource(run_loop, self._source, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self._tap, True)

        deadline = None if seconds is None else time.time() + seconds
        while not self._stopping:
            if deadline is not None and time.time() >= deadline:
                break
            Q.CFRunLoopRunInMode(Q.kCFRunLoopDefaultMode, 0.1, False)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception:                      # noqa: BLE001
                    # ⚠️ 回呼出錯不可以讓監聽死掉 —— 寧可 UI 不更新，
                    #    也不要變成「按了完全沒反應」。
                    pass

    def stop(self) -> None:
        """要求事件迴圈結束（可從其他執行緒呼叫，或訊號處理器）。"""
        self._stopping = True

    # -- 權限檢查（給啟動流程用） --
    def check_permission(self) -> tuple[bool, str]:
        """能不能建立 tap。回傳 (可以, 原因/提示)。"""
        try:
            tap = self._create_tap()
        except PermissionError_ as exc:
            return False, str(exc)
        except Exception as exc:                    # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        # 建立成功就關掉，這裡只做檢查
        try:
            Q = self._Quartz
            Q.CFMachPortInvalidate(tap)
        except Exception:                           # noqa: BLE001
            pass
        return True, "ok"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="macOS 按鍵監聽（診斷用）")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    print("=" * 74)
    print("  macOS 按鍵監聽")
    print("=" * 74)

    seen: list[KeyEvent] = []
    listener = MacKeyListener(seen.append, debug=True)

    ok, why = listener.check_permission()
    if not ok:
        print(f"\n  ❌ {why}\n")
        raise SystemExit(1)
    print("\n  ✅ 權限正常，開始監聽（只聽不攔截）")
    print(f"  跑 {args.seconds:.0f} 秒 —— 請按幾個鍵試試"
          f"（例如 RightCtrl、F9、Space）\n" + "-" * 74)

    try:
        listener.run(seconds=args.seconds)
    except KeyboardInterrupt:
        print("\n  （中斷）")

    print("\n" + "=" * 74)
    print(f"  收到 {len(seen)} 個事件")
    if seen:
        from collections import Counter
        c = Counter(e.key for e in seen)
        for name, n in c.most_common():
            print(f"    {name:14s} {n:>3} 次")
    else:
        print("  ⚠️ 沒有事件。確認輔助使用權限已開，且監聽期間有按鍵。")
    print("=" * 74)
