#!/usr/bin/env python3
"""P1 — Push-to-talk 常駐程式：**按住錄音鍵 → 說話 → 放開 → 文字自動出現**。

這是「能不能取代原廠工具」的核心驗證。

偵測方式（重要設計決定）：
    只用 **Raw Input 一條通道**。因為 Raw Input 同時給了我們
      - 哪個裝置送的（`hDevice`，可解析出 interface path）
      - 哪個鍵（`VK_CONTROL` + `E0` 旗標 = Right Ctrl）
    `WH_KEYBOARD_LL` 只有在**要抑制按鍵**時才需要，而抑制是可選的品質改善
    （Ctrl 單獨按住不輸入任何字元）。不用 hook 也就不會拖慢全系統輸入。

    注意：hook 看不到裝置，這是它的先天限制；靠 hook 做裝置辨識是不可能的。

流程（`docs/architecture.md` 的狀態機）：
    IDLE ──按下錄音鍵──> RECORDING ──放開──> PROCESSING ──> INSERTING ──> IDLE

用法:
    python tools/p1/ptt.py --list-devices           # 確認要認哪個裝置
    python tools/p1/ptt.py --device-filter 00001124 # 只認藍牙裝置的 Right Ctrl
    python tools/p1/ptt.py --dry-run                # 只顯示事件，不注入文字
    python tools/p1/ptt.py --traditional            # 輸出繁體
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "p0"))

# 重用 P0 已經驗證過的 Raw Input 管線（結構定義、註冊、解析）
from keycode_logger import (  # noqa: E402
    RAWHID,
    RAWKEYBOARD,
    RAWINPUTDEVICE,
    RAWINPUTHEADER,
    RIDEV_INPUTSINK,
    RID_INPUT,
    RIM_TYPEHID,
    RIM_TYPEKEYBOARD,
    WM_INPUT,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    device_info,
    device_label,
    device_name,
    list_raw_input_devices,
    setup_console,
    user32,
    kernel32,
)

from recorder import Capture  # noqa: E402
from speech_engine import (  # noqa: E402
    EngineNotConfigured,
    EngineUnavailable,
    build_engine,
    has_opencc,
    pcm_to_samples,
    to_traditional,
)
from textin import inject_text  # noqa: E402

HWND_MESSAGE = wintypes.HWND(-3)
VK_CONTROL = 0x11
RI_KEY_E0 = 0x02          # 右側修飾鍵（Right Ctrl 的判別依據）


class PttDaemon:
    """按住錄音鍵說話，放開後把文字注入前景視窗。"""

    def __init__(self, engine, device_filter: str | None = None,
                 key_vk: int = VK_CONTROL, require_e0: bool = True,
                 traditional: bool = True, mode: str = "auto",
                 dry_run: bool = False, min_s: float = 0.2,
                 max_s: float = 60.0, device_index: int = 1, debug: bool = False):
        self.engine = engine
        self.device_filter = device_filter.lower() if device_filter else None
        self.key_vk = key_vk
        self.require_e0 = require_e0
        self.traditional = traditional
        self.mode = mode
        self.dry_run = dry_run
        self.min_s = min_s
        self.max_s = max_s
        self.device_index = device_index
        self.debug = debug

        self.state = "IDLE"
        self._cap: Capture | None = None
        self._rec_started = 0.0
        self._pressed = False
        self._lock = threading.Lock()
        self._wndproc_ref = None
        self._hwnd = None
        self.stats = {"presses": 0, "inserted": 0, "failed": 0, "empty": 0}
        self._msg_count = 0
        self._input_count = 0
        self._last_unknown_dev: dict[int, dict] = {}

    # ------------------------------------------------ 裝置判定
    def _is_target_device(self, hdevice) -> tuple[bool, str]:
        key = int(hdevice or 0)
        info = self._last_unknown_dev.get(key)
        if info is None:
            info = {"name": device_name(hdevice), **device_info(hdevice)}
            self._last_unknown_dev[key] = info
        name = info.get("name", "") or ""
        if not self.device_filter:
            return True, device_label(name)
        if self.device_filter in name.lower():
            return True, device_label(name)
        return False, device_label(name)

    # ------------------------------------------------ 錄音 / 辨識 / 注入
    def _start_recording(self, label: str) -> None:
        try:
            cap = Capture(self.device_index, rate=16000, max_seconds=self.max_s)
            cap.__enter__()
        except Exception as exc:
            print(f"  ❌ 無法開始錄音：{exc}")
            self.state = "IDLE"
            return
        self._cap = cap
        self._rec_started = time.monotonic()
        self.state = "RECORDING"
        print(f"  🔴 錄音中…（{label}）", flush=True)

    def _finish_recording(self) -> None:
        cap, self._cap = self._cap, None
        if cap is None:
            self.state = "IDLE"
            return
        duration = time.monotonic() - self._rec_started
        try:
            cap.__exit__(None, None, None)
            pcm = cap.recorded()
        except Exception as exc:
            print(f"  ❌ 停止錄音失敗：{exc}")
            self.state = "IDLE"
            return

        n = len(pcm) // 2
        secs = n / 16000
        print(f"  ⏹  停止（{duration:.1f}s，音訊 {secs:.2f}s）")
        if secs < self.min_s:
            print("  ⏭  太短，視為誤觸，不辨識")
            self.state = "IDLE"
            return

        self.state = "PROCESSING"
        t0 = time.perf_counter()
        try:
            result = self.engine.transcribe(pcm_to_samples(pcm), 16000)
        except (EngineNotConfigured, EngineUnavailable) as exc:
            print(f"  ❌ 辨識失敗：{exc}")
            self.stats["failed"] += 1
            self.state = "IDLE"
            return
        except Exception as exc:
            print(f"  ❌ 辨識發生未預期錯誤：{type(exc).__name__}: {exc}")
            self.stats["failed"] += 1
            self.state = "IDLE"
            return
        asr_ms = (time.perf_counter() - t0) * 1000

        text = (result.text or "").strip()
        if not text:
            print(f"  ⚠️ 沒有辨識出文字（{asr_ms:.0f} ms）")
            self.stats["empty"] += 1
            self.state = "IDLE"
            return

        out = to_traditional(text) if (self.traditional and has_opencc()) else text
        print(f"  📝 {out}   （辨識 {asr_ms:.0f} ms）")

        if self.dry_run:
            print("  （--dry-run：不注入）")
            self.state = "IDLE"
            return

        self.state = "INSERTING"
        t1 = time.perf_counter()
        res = inject_text(out, mode=self.mode, verbose=True)
        ins_ms = (time.perf_counter() - t1) * 1000
        if res["ok"]:
            self.stats["inserted"] += 1
            print(f"  ✅ 已注入（{res['method']}，{ins_ms:.0f} ms）"
                  f"{'' if res['clipboard_restored'] else ' ⚠️ 剪貼簿未還原'}")
        else:
            self.stats["failed"] += 1
            print(f"  ❌ 注入失敗：{res['detail']}")
        total = (time.perf_counter() - t0) * 1000
        print(f"  ⏱  端到端（辨識+注入）{total:.0f} ms\n")
        self.state = "IDLE"

    # ------------------------------------------------ Raw Input
    def _handle_key(self, hdevice, kb: RAWKEYBOARD) -> None:
        if int(kb.VKey) != self.key_vk:
            return
        target, label = self._is_target_device(hdevice)
        e0 = bool(int(kb.Flags) & RI_KEY_E0)
        down = kb.Message in (WM_KEYDOWN, WM_SYSKEYDOWN)

        # 被拒絕的時候要說得出原因，否則只能看到「按了沒反應」。
        # 實測踩過：送合成的 Ctrl 進來時 make=0、裝置無名、沒有 E0，
        # 結果整個事件被靜默丟棄，完全查不出為什麼。
        if self.require_e0 and not e0:
            if self.debug:
                print(f"  · 忽略非 E0 Ctrl（左 Ctrl／合成事件）flags=0x{int(kb.Flags):X} "
                      f"make={int(kb.MakeCode)} dev={label}")
            return
        if not target:
            if self.debug:
                print(f"  · 忽略 {label} 的 Ctrl（不是目標裝置）")
            return

        # 通過篩選的事件也要看得見 —— 否則「按了沒反應」時分不出是
        # 「沒收到事件」還是「收到了但被邏輯吃掉」。
        if self.debug:
            print(f"  · 接受 Ctrl {'DOWN' if down else 'UP  '} "
                  f"flags=0x{int(kb.Flags):X} make={int(kb.MakeCode)} dev={label} "
                  f"pressed={self._pressed}")

        with self._lock:
            if down and not self._pressed:
                self._pressed = True
                self.stats["presses"] += 1
                self._start_recording(label)
            elif not down and self._pressed:
                self._pressed = False
                self._finish_recording()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        self._msg_count += 1
        if msg == WM_INPUT:
            self._input_count += 1
            try:
                size = wintypes.UINT(0)
                hdr_size = ctypes.sizeof(RAWINPUTHEADER)
                user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), hdr_size)
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    got = user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                                                 hdr_size)
                    if got not in (0, 0xFFFFFFFF):
                        hdr = RAWINPUTHEADER.from_buffer(buf)
                        if hdr.dwType == RIM_TYPEKEYBOARD:
                            self._handle_key(hdr.hDevice,
                                             RAWKEYBOARD.from_buffer(buf, hdr_size))
            except Exception as exc:
                print(f"  ⚠️ 解析 raw input 失敗：{exc!r}", file=sys.stderr)
            return 0
        if msg == 0x0002:                       # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self):
        from keycode_logger import WNDPROC

        hinst = kernel32.GetModuleHandleW(None)
        class_name = "VibeTalkiePtt"

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):
                raise ctypes.WinError(err)
        hwnd = user32.CreateWindowExW(0, class_name, "ptt", 0, 0, 0, 0, 0,
                                      HWND_MESSAGE, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    def _register(self) -> None:
        # 只註冊鍵盤即可 —— 錄音鍵是標準鍵盤 collection 的 Right Ctrl
        devs = (RAWINPUTDEVICE * 1)()
        devs[0].usUsagePage = 0x01
        devs[0].usUsage = 0x06
        devs[0].dwFlags = RIDEV_INPUTSINK
        devs[0].hwndTarget = self._hwnd
        if not user32.RegisterRawInputDevices(devs, 1, ctypes.sizeof(RAWINPUTDEVICE)):
            raise ctypes.WinError(ctypes.get_last_error())

    def run(self, seconds: float | None = None) -> None:
        self._hwnd = self._create_window()
        self._register()
        if self.debug:
            print(f"  (debug) 視窗 hwnd={int(self._hwnd or 0)}，raw input 已註冊"
                  f"（usage 0x01/0x06，RIDEV_INPUTSINK）")
        print("準備完成。請把游標放到記事本，然後**按住裝置上的錄音鍵說話**，放開後文字會自動出現。")
        print("（Ctrl+C 結束）\n")
        msg = wintypes.MSG()
        deadline = time.monotonic() + seconds if seconds else None
        next_beat = time.monotonic() + 2.0
        try:
            while True:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0012:
                        return
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                if self.debug and time.monotonic() > next_beat:
                    next_beat = time.monotonic() + 2.0
                    print(f"  (debug) 收到訊息 {self._msg_count} 個，"
                          f"其中 WM_INPUT {self._input_count} 個", flush=True)
                if deadline and time.monotonic() > deadline:
                    break
                time.sleep(0.005)
        except KeyboardInterrupt:
            print("\n（使用者中斷）")
        finally:
            if self._cap is not None:
                try:
                    self._cap.__exit__(None, None, None)
                except Exception:
                    pass
                self._cap = None
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
            print(f"\n統計：按下 {self.stats['presses']} 次，"
                  f"注入成功 {self.stats['inserted']}，"
                  f"空結果 {self.stats['empty']}，失敗 {self.stats['failed']}")


def cmd_list(needle: str | None) -> int:
    for d in list_raw_input_devices():
        name = d.get("name", "")
        mark = ""
        if needle and needle.lower() in name.lower():
            mark = "  ← 符合"
        print(f"  {device_label(name):<14} {name}{mark}")
    print("\n提示：藍牙 HID 的 interface path 不含裝置名，用 --device-filter 00001124")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1 push-to-talk 常駐程式")
    parser.add_argument("--device-filter", default="00001124",
                        help="只認 interface path 含此字串的裝置（預設藍牙 HID UUID）")
    parser.add_argument("--any-device", action="store_true",
                        help="不限定裝置（任何鍵盤的 Right Ctrl 都觸發）")
    parser.add_argument("--key", default="ctrl", choices=("ctrl",),
                        help="觸發鍵（目前只支援 ctrl）")
    parser.add_argument("--no-e0", action="store_true",
                        help="不要求 E0（左右 Ctrl 都接受）")
    parser.add_argument("--engine", default="sherpa-onnx")
    parser.add_argument("--traditional", action="store_true", default=True)
    parser.add_argument("--simplified", dest="traditional", action="store_false",
                        help="保留模型原始的簡體輸出")
    parser.add_argument("--mode", default="auto", choices=("auto", "paste", "type"))
    parser.add_argument("--dry-run", action="store_true", help="只顯示，不注入文字")
    parser.add_argument("--device-index", type=int, default=1, help="音訊裝置索引")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="顯示被忽略的 Ctrl 事件與原因（排查『按了沒反應』用）")
    parser.add_argument("--seconds", type=float, default=None, help="執行 N 秒後結束（測試用）")
    args = parser.parse_args(argv)
    setup_console()

    if args.list_devices:
        return cmd_list(args.device_filter if not args.any_device else None)

    engine = build_engine(args.engine)
    ok, why = engine.is_available()
    if not ok:
        print(f"❌ 引擎不可用：{why}")
        return 1
    engine.warmup()
    print(f"引擎：{engine.name}（本地）　繁體輸出：{'是' if args.traditional else '否'}"
          f"　OpenCC：{'有' if has_opencc() else '無'}")

    daemon = PttDaemon(
        engine,
        device_filter=None if args.any_device else args.device_filter,
        require_e0=not args.no_e0,
        traditional=args.traditional,
        mode=args.mode,
        dry_run=args.dry_run,
        device_index=args.device_index,
        debug=args.debug,
    )
    daemon.run(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
