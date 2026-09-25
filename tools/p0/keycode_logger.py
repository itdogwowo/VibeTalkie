#!/usr/bin/env python3
"""P0 / T3 — 記錄藍牙裝置按鈕的「真實鍵碼」。

為什麼要三路並進？因為裝置按鈕可能落在三個不同的地方，而它們的可見度不同：

    1. 標準鍵盤 usage (HID Usage Page 0x01 / Usage 0x06)
       → Low-Level Keyboard Hook 與 Raw Input 都看得到，有 VK code。
    2. Consumer Control (HID Usage Page 0x0C)
       → 多媒體鍵。Low-Level Hook 不一定看得到，Raw Input 看得到。
    3. 廠商自訂 collection
       → 標準鍵盤 API 完全看不到，只有 Raw Input 的原始 HID 位元組看得到。

本工具同時掛上三種監聽，並把每個事件的來源標記出來，所以可以回答：
「這個按鈕到底送了什麼、哪一層看得到、熱鍵層該用哪個 API。」

用法:
    # 先看系統有哪些 raw input 裝置（含 HID collection 的 usage page/usage）
    python tools/p0/keycode_logger.py --list-devices

    # 監聽 30 秒，請在裝置上按幾次按鈕
    python tools/p0/keycode_logger.py --seconds 30

    # 輸出 JSONL 供後續分析（預設寫到 artifacts/，已 gitignore）
    python tools/p0/keycode_logger.py --seconds 30 --out artifacts/t3-keycodes.jsonl

僅使用標準函式庫（ctypes），不需要 pip install。
只支援 Windows。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

if sys.platform != "win32":
    sys.exit("本工具只支援 Windows（Raw Input / Low-Level Hook 皆為 Win32 API）。")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 常數

WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

RID_INPUT = 0x10000003
RIDI_PREPARSEDDATA = 0x20000005
RIDI_DEVICENAME = 0x20000007
RIDI_DEVICEINFO = 0x2000000B

RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIM_TYPEHID = 2

RIDEV_INPUTSINK = 0x00000100
RIDEV_DEVNOTIFY = 0x00002000

WH_KEYBOARD_LL = 13

HWND_MESSAGE = wintypes.HWND(-3)

# 要註冊的 top-level collections。逐一註冊才能分辨事件來自哪一種。
REGISTER_TARGETS = [
    ("Generic Desktop / Keyboard", 0x01, 0x06),
    ("Generic Desktop / Keypad", 0x01, 0x07),
    ("Generic Desktop / System Control", 0x01, 0x80),
    ("Generic Desktop / Consumer Control", 0x0C, 0x01),  # 註：page 0x0C 為 Consumer
]

VK_NAMES = {
    0x08: "VK_BACK", 0x09: "VK_TAB", 0x0D: "VK_RETURN", 0x10: "VK_SHIFT",
    0x11: "VK_CONTROL", 0x12: "VK_MENU/ALT", 0x13: "VK_PAUSE", 0x14: "VK_CAPITAL",
    0x1B: "VK_ESCAPE", 0x20: "VK_SPACE", 0x21: "VK_PRIOR", 0x22: "VK_NEXT",
    0x23: "VK_END", 0x24: "VK_HOME", 0x25: "VK_LEFT", 0x26: "VK_UP",
    0x27: "VK_RIGHT", 0x28: "VK_DOWN", 0x2C: "VK_SNAPSHOT", 0x2D: "VK_INSERT",
    0x2E: "VK_DELETE",
    0x5B: "VK_LWIN", 0x5C: "VK_RWIN", 0x5D: "VK_APPS",
    0xA0: "VK_LSHIFT", 0xA1: "VK_RSHIFT", 0xA2: "VK_LCONTROL", 0xA3: "VK_RCONTROL",
    0xA4: "VK_LMENU", 0xA5: "VK_RMENU",
    0xAD: "VK_VOLUME_MUTE", 0xAE: "VK_VOLUME_DOWN", 0xAF: "VK_VOLUME_UP",
    0xB0: "VK_MEDIA_NEXT_TRACK", 0xB1: "VK_MEDIA_PREV_TRACK", 0xB2: "VK_MEDIA_STOP",
    0xB3: "VK_MEDIA_PLAY_PAUSE",
    0x5F: "VK_SLEEP", 0xA6: "VK_BROWSER_BACK", 0xA7: "VK_BROWSER_FORWARD",
    0xA8: "VK_BROWSER_REFRESH", 0xA9: "VK_BROWSER_STOP", 0xAA: "VK_BROWSER_SEARCH",
    0xAB: "VK_BROWSER_FAVORITES", 0xAC: "VK_BROWSER_HOME",
    0xB4: "VK_LAUNCH_MAIL", 0xB5: "VK_LAUNCH_MEDIA_SELECT",
    0xB6: "VK_LAUNCH_APP1", 0xB7: "VK_LAUNCH_APP2",
}
for _i in range(0x70, 0x88):
    VK_NAMES[_i] = f"VK_F{_i - 0x6F}"
for _i in range(0x30, 0x3A):
    VK_NAMES[_i] = chr(_i)
for _i in range(0x41, 0x5B):
    VK_NAMES[_i] = chr(_i)


def vk_name(vk: int) -> str:
    return VK_NAMES.get(vk, f"VK_0x{vk:02X}")


def setup_console() -> None:
    """Windows 主控台預設不是 UTF-8，中文輸出會變亂碼。強制切到 UTF-8。"""
    try:
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------- 結構

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [
        ("hDevice", wintypes.HANDLE),
        ("dwType", wintypes.DWORD),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    ]


class RAWHID(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", wintypes.DWORD),
        ("dwCount", wintypes.DWORD),
    ]


class RID_DEVICE_INFO_MOUSE(ctypes.Structure):
    _fields_ = [("dwId", wintypes.DWORD), ("dwNumberOfButtons", wintypes.DWORD),
                ("dwSampleRate", wintypes.DWORD), ("fHasHorizontalWheel", wintypes.BOOL)]


class RID_DEVICE_INFO_KEYBOARD(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSubType", wintypes.DWORD),
                ("dwKeyboardMode", wintypes.DWORD), ("dwNumberOfFunctionKeys", wintypes.DWORD),
                ("dwNumberOfIndicators", wintypes.DWORD), ("dwNumberOfKeysTotal", wintypes.DWORD)]


class RID_DEVICE_INFO_HID(ctypes.Structure):
    _fields_ = [("dwVendorId", wintypes.DWORD), ("dwProductId", wintypes.DWORD),
                ("dwVersionNumber", wintypes.DWORD), ("usUsagePage", wintypes.USHORT),
                ("usUsage", wintypes.USHORT)]


class RID_DEVICE_INFO_UNION(ctypes.Union):
    _fields_ = [("mouse", RID_DEVICE_INFO_MOUSE), ("keyboard", RID_DEVICE_INFO_KEYBOARD),
                ("hid", RID_DEVICE_INFO_HID)]


class RID_DEVICE_INFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwType", wintypes.DWORD),
                ("u", RID_DEVICE_INFO_UNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.restype = ctypes.c_longlong
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputDeviceList.restype = wintypes.UINT
user32.GetRawInputDeviceList.argtypes = [ctypes.POINTER(RAWINPUTDEVICELIST),
                                         ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                          ctypes.POINTER(wintypes.UINT)]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT,
                                           wintypes.UINT]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]

# 這些函式回傳 64-bit handle/pointer；不宣告 restype 會被 ctypes 截成 32-bit。
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_longlong
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


# ---------------------------------------------------------------- 裝置查詢

def device_name(hdevice) -> str:
    """取得裝置的 device interface path（含 VID/PID/usage，同時看出是哪個 collection）。"""
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(hdevice, RIDI_DEVICENAME, None, ctypes.byref(size))
    if size.value == 0:
        return ""
    buf = ctypes.create_unicode_buffer(size.value)
    got = user32.GetRawInputDeviceInfoW(hdevice, RIDI_DEVICENAME, buf, ctypes.byref(size))
    return buf.value if got not in (0, 0xFFFFFFFF) else ""


def device_info(hdevice) -> dict:
    """取得 top-level collection 的 usage page / usage 與 VID/PID。"""
    info = RID_DEVICE_INFO()
    info.cbSize = ctypes.sizeof(RID_DEVICE_INFO)
    size = wintypes.UINT(ctypes.sizeof(RID_DEVICE_INFO))
    got = user32.GetRawInputDeviceInfoW(hdevice, RIDI_DEVICEINFO, ctypes.byref(info),
                                       ctypes.byref(size))
    if got in (0, 0xFFFFFFFF):
        return {}
    out: dict = {"raw_type": int(info.dwType)}
    if info.dwType == RIM_TYPEKEYBOARD:
        out.update(kind="keyboard", keyboard_mode=int(info.u.keyboard.dwKeyboardMode),
                   keys_total=int(info.u.keyboard.dwNumberOfKeysTotal))
    elif info.dwType == RIM_TYPEMOUSE:
        out.update(kind="mouse", buttons=int(info.u.mouse.dwNumberOfButtons))
    elif info.dwType == RIM_TYPEHID:
        out.update(kind="hid", vid=int(info.u.hid.dwVendorId), pid=int(info.u.hid.dwProductId),
                   usage_page=int(info.u.hid.usUsagePage), usage=int(info.u.hid.usUsage))
    return out


def list_raw_input_devices() -> list[dict]:
    count = wintypes.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    if count.value == 0:
        return []
    arr = (RAWINPUTDEVICELIST * count.value)()
    got = user32.GetRawInputDeviceList(arr, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    result = []
    for i in range(got):
        d = arr[i]
        info = device_info(d.hDevice)
        result.append({
            "handle": int(d.hDevice or 0),
            "type": int(d.dwType),
            "name": device_name(d.hDevice),
            **info,
        })
    return result


def matches_target(entry: dict, needle: str) -> bool:
    """比對裝置。

    注意：藍牙 HID 裝置的 interface path 裡**不含裝置名稱**，只有 HID UUID，
    所以用品牌名（例：AI_VOICE）過濾會抓不到。此時請改用藍牙 HID UUID：
        --filter 00001124
    """
    n = needle.lower().strip()
    if not n:
        return False
    name = (entry.get("name") or "").lower()
    return n in name or n in (entry.get("kind") or "").lower()


BT_HID_UUID_HINT = "00001124"


def is_bluetooth_hid(entry: dict) -> bool:
    return BT_HID_UUID_HINT in (entry.get("name") or "").lower()


# ---------------------------------------------------------------- 主體

class KeycodeLogger:
    def __init__(self, out_path: Path | None, quiet_hook: bool = True):
        self.out_path = out_path
        self.records: list[dict] = []
        self.quiet_hook = quiet_hook
        self._dev_cache: dict[int, dict] = {}
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._hook_ref = HOOKPROC(self._ll_hook)
        self._hook_handle = None
        self._hwnd = None
        self._count = 0
        self._listeners: dict = {}

    # -- 裝置資訊快取
    def _dev(self, hdevice) -> dict:
        key = int(hdevice or 0)
        if key not in self._dev_cache:
            self._dev_cache[key] = {"name": device_name(hdevice), **device_info(hdevice)}
        return self._dev_cache[key]

    # -- 輸出
    def emit(self, source: str, **fields) -> None:
        self._count += 1
        rec = {
            "seq": self._count,
            "t": round(time.time(), 3),
            "iso": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "source": source,
            **fields,
        }
        self.records.append(rec)
        print(self._format(rec), flush=True)

    @staticmethod
    def _format(rec: dict) -> str:
        src = rec["source"]
        if src == "hook":
            state = "DOWN" if rec.get("is_down") else "UP  "
            return (f"[hook ] {state} vk={rec.get('vk')} ({rec.get('vk_name')}) "
                    f"scan={rec.get('scan')} flags=0x{rec.get('flags', 0):X} "
                    f"injected={rec.get('injected')}")
        if src == "rawkb":
            return (f"[rawkb] {rec.get('message_name')} vk={rec.get('vk')} "
                    f"({rec.get('vk_name')}) make={rec.get('make_code')} "
                    f"flags=0x{rec.get('flags', 0):X} | dev_usage_page={rec.get('dev_usage_page')} "
                    f"dev_usage={rec.get('dev_usage')}")
        if src == "rawhid":
            return (f"[rawhid] usage_page={rec.get('dev_usage_page')} usage={rec.get('dev_usage')} "
                    f"vid={rec.get('vid')} pid={rec.get('pid')} "
                    f"report_id={rec.get('report_id')} len={rec.get('size_hid')} "
                    f"count={rec.get('count')} data={rec.get('data_hex')}")
        return f"[{src}] {json.dumps(rec, ensure_ascii=False)}"

    # -- 訊息處理
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                self._handle_raw_input(lparam)
            except Exception as exc:  # 常駐工具不可因單一事件崩潰
                print(f"[warn] raw input 解析失敗: {exc!r}", file=sys.stderr)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw_input(self, lparam) -> None:
        size = wintypes.UINT(0)
        hdr_size = ctypes.sizeof(RAWINPUTHEADER)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), hdr_size)
        if size.value == 0:
            return
        buf = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), hdr_size)
        if got in (0, 0xFFFFFFFF):
            return

        hdr = RAWINPUTHEADER.from_buffer(buf)
        # 直接以 handle 查（比 wParam 可靠）
        info = self._dev(hdr.hDevice)

        if hdr.dwType == RIM_TYPEKEYBOARD:
            kb = RAWKEYBOARD.from_buffer(buf, hdr_size)
            if kb.VKey in (0xFF, 0):  # 0xFF = 假鍵（用於回報未知），0 = 無
                return
            msg_name = {WM_KEYDOWN: "WM_KEYDOWN", WM_KEYUP: "WM_KEYUP",
                        WM_SYSKEYDOWN: "WM_SYSKEYDOWN", WM_SYSKEYUP: "WM_SYSKEYUP"}.get(
                kb.Message, f"0x{kb.Message:04X}")
            self.emit("rawkb",
                      vk=int(kb.VKey), vk_name=vk_name(int(kb.VKey)),
                      make_code=int(kb.MakeCode), flags=int(kb.Flags),
                      message=int(kb.Message), message_name=msg_name,
                      dev_name=info.get("name", ""),
                      dev_usage_page=info.get("usage_page"),
                      dev_usage=info.get("usage"))
            if info.get("name"):
                self._listeners[int(hdr.hDevice or 0)] = info
        elif hdr.dwType == RIM_TYPEHID:
            hid = RAWHID.from_buffer(buf, hdr_size)
            data_off = hdr_size + ctypes.sizeof(RAWHID)
            raw = bytes(buf[data_off:data_off + int(hid.dwSizeHid)])
            report_id = raw[0] if raw else None
            self.emit("rawhid",
                      vid=info.get("vid"), pid=info.get("pid"),
                      dev_usage_page=info.get("usage_page"), dev_usage=info.get("usage"),
                      size_hid=int(hid.dwSizeHid), count=int(hid.dwCount),
                      report_id=report_id, data_hex=raw.hex(" "),
                      dev_name=info.get("name", ""))

    def _ll_hook(self, ncode, wparam, lparam):
        if ncode == 0:
            try:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                is_down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                injected = bool(kb.flags & 0x10)
                vk = int(kb.vkCode)
                if not (self.quiet_hook and injected and vk == 0):
                    self.emit("hook", vk=vk, vk_name=vk_name(vk), scan=int(kb.scanCode),
                              flags=int(kb.flags), is_down=is_down, injected=injected)
            except Exception as exc:
                print(f"[warn] hook 解析失敗: {exc!r}", file=sys.stderr)
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    # -- 生命週期
    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        class_name = "VibeTalkieP0KeycodeLogger"

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        wndclass = WNDCLASSW()
        wndclass.lpfnWndProc = self._wndproc_ref
        wndclass.hInstance = hinst
        wndclass.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):  # 1410 = class already exists
                raise ctypes.WinError(err)
        hwnd = user32.CreateWindowExW(0, class_name, "p0", 0, 0, 0, 0, 0,
                                     HWND_MESSAGE, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    def _register_raw_input(self):
        devs = (RAWINPUTDEVICE * len(REGISTER_TARGETS))()
        for i, (_label, page, usage) in enumerate(REGISTER_TARGETS):
            devs[i].usUsagePage = page
            devs[i].usUsage = usage
            devs[i].dwFlags = RIDEV_INPUTSINK
            devs[i].hwndTarget = self._hwnd
        if not user32.RegisterRawInputDevices(devs, len(REGISTER_TARGETS),
                                             ctypes.sizeof(RAWINPUTDEVICE)):
            raise ctypes.WinError(ctypes.get_last_error())

    def run(self, seconds: float, device_filter: str | None = None) -> None:
        self._hwnd = self._create_window()
        self._register_raw_input()
        self._hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_ref, None, 0)
        if not self._hook_handle:
            print(f"[warn] Low-Level Hook 掛載失敗（仍會用 Raw Input 監聽）: "
                  f"{ctypes.WinError(ctypes.get_last_error())}", file=sys.stderr)

        print(f"已開始監聽（{seconds:g} 秒）。")
        print("請在藍牙裝置上『按一下、放開』，重複 3 次。")
        if device_filter:
            print(f"（注意：事件未依裝置過濾，全部都會列出；用 name 欄位比對 '{device_filter}'）")
        print("-" * 78)

        msg = wintypes.MSG()
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0012:  # WM_QUIT
                        return
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.004)
        except KeyboardInterrupt:
            print("\n（使用者中斷）")
        finally:
            self._teardown()

    def _teardown(self):
        if self._hook_handle:
            user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self.out_path:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            with self.out_path.open("w", encoding="utf-8") as fh:
                for rec in self.records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"\n已寫出 {len(self.records)} 筆事件 → {self.out_path}")


# ---------------------------------------------------------------- CLI

def cmd_list_devices(needle: str | None) -> int:
    devices = list_raw_input_devices()
    print(f"共 {len(devices)} 個 raw input 裝置\n")
    hits = 0
    bt_hits = 0
    for d in devices:
        name = d.get("name", "")
        bt = is_bluetooth_hid(d)
        if bt:
            bt_hits += 1
        if d.get("kind") == "hid":
            line = (f"type={d.get('raw_type')} kind=hid      "
                    f"page={d.get('usage_page')} usage={d.get('usage')} "
                    f"vid=0x{(d.get('vid') or 0):04X} pid=0x{(d.get('pid') or 0):04X}")
        elif d.get("kind") == "keyboard":
            line = (f"type={d.get('raw_type')} kind=keyboard "
                    f"keys={d.get('keys_total')}")
        else:
            line = f"type={d.get('raw_type')} kind={d.get('kind', '?')}"
        if bt:
            line += "   [藍牙 HID]"
        print(line)
        print(f"      {name}")
        if needle and matches_target(d, needle):
            hits += 1
            print("      ^^^ 符合關鍵字")
        print()

    if bt_hits:
        print(f"註：上面標 [藍牙 HID] 的 {bt_hits} 個裝置來自藍牙（HID UUID {BT_HID_UUID_HINT}）。")
        print("    藍牙 HID 的 interface path **不含裝置名稱**，所以用品牌名過濾抓不到，")
        print(f"    要列出來請用：--filter {BT_HID_UUID_HINT}")
        print("    collection 的判讀：usage page 0x01 usage 0x06 = 鍵盤；")
        print("                       usage page 0x0C usage 0x01 = Consumer Control（多媒體鍵）")
        print()
    if needle:
        print(f"符合 '{needle}' 的裝置數：{hits}")
        if hits == 0:
            print("→ 沒找到。藍牙裝置請改用 --filter 00001124；")
            print("  若仍為 0，請確認裝置已配對並開機。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P0/T3：記錄藍牙裝置按鈕的真實鍵碼（Raw Input + Low-Level Hook）")
    parser.add_argument("--list-devices", action="store_true",
                        help="列出 raw input 裝置（含 HID collection 的 usage）後結束")
    parser.add_argument("--filter", default=None,
                        help="在 --list-devices 時標出符合此關鍵字的裝置（例：AI_VOICE）")
    parser.add_argument("--seconds", type=float, default=30.0,
                        help="監聽秒數（預設 30）")
    parser.add_argument("--out", default="artifacts/t3-keycodes.jsonl",
                        help="JSONL 輸出位置（預設 artifacts/t3-keycodes.jsonl）")
    parser.add_argument("--no-out", action="store_true", help="不寫檔，只印到畫面")
    parser.add_argument("--show-injected", action="store_true",
                        help="也顯示程式注入的按鍵事件（預設隱藏，避免雜訊）")
    args = parser.parse_args(argv)
    setup_console()

    if args.list_devices:
        return cmd_list_devices(args.filter)

    out = None if args.no_out else Path(args.out)
    logger = KeycodeLogger(out_path=out, quiet_hook=not args.show_injected)
    logger.run(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
