#!/usr/bin/env python3
"""文字注入（Windows）：剪貼簿貼上，失敗則退回逐字輸入。

流程（依 `docs/architecture.md` 的 `textin` 契約）：

    1. 備份剪貼簿現有內容
    2. 把要注入的文字放進剪貼簿
    3. 送 Ctrl+V
    4. **還原剪貼簿**（即使第 3 步失敗也要還原）

為什麼要「還原」？因為使用者的剪貼簿裡可能是他等一下要用的東西，
被我們覆蓋掉是很糟的使用體驗。這是 v1 驗收標準第 3 條。

⚠️ 與錄音鍵的競態：
    錄音鍵本身就是 **Right Ctrl**。若在 Ctrl 還按著的時候送 Ctrl+V，
    目標程式會收到「Ctrl(+Ctrl)+V」。多數程式仍會貼上，但為了正確性，
    注入前會先補送一次 Ctrl 的 key-up。

只用標準函式庫（ctypes）。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_V = 0x56
VK_RCONTROL = 0xA3
VK_LCONTROL = 0xA2


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _open_clipboard(retries: int = 10, delay: float = 0.03) -> bool:
    """剪貼簿是全域資源，別的程式可能正握著 → 重試。"""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def get_clipboard_text() -> str | None:
    """讀剪貼簿文字。沒有文字（例如只有圖片）時回 None。"""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, buf, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        # 成功時所有權移交系統，不要自己 free
        return True
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- 鍵盤送出

def _key_event(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = flags
    return inp


def _send(events: list[INPUT]) -> int:
    if not events:
        return 0
    arr = (INPUT * len(events))(*events)
    return user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def release_modifiers() -> None:
    """先放掉 Ctrl/Shift/Alt。

    錄音鍵就是 Right Ctrl；若在它還按著時送 Ctrl+V，
    目標程式會收到多餘的修飾鍵狀態。這裡先補送 key-up，
    對已經放開的鍵再送一次 key-up 是無害的。
    """
    _send([_key_event(vk=VK_RCONTROL, flags=KEYEVENTF_KEYUP),
           _key_event(vk=VK_LCONTROL, flags=KEYEVENTF_KEYUP)])


def send_ctrl_v() -> bool:
    events = [_key_event(vk=VK_CONTROL),
              _key_event(vk=VK_V),
              _key_event(vk=VK_V, flags=KEYEVENTF_KEYUP),
              _key_event(vk=VK_CONTROL, flags=KEYEVENTF_KEYUP)]
    return _send(events) == len(events)


def type_unicode(text: str) -> int:
    """逐字輸入任意 Unicode（含中文）。回傳送出的字元數。

    用 KEYEVENTF_UNICODE 而不是對應的 VK：中文沒有 VK，
    而且這樣不受使用者鍵盤配置影響。
    超出 BMP 的字元要先轉成 UTF-16 代理對。
    """
    units = text.encode("utf-16-le")
    n = len(units) // 2
    if n == 0:
        return 0
    events: list[INPUT] = []
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i:i + 2], "little")
        events.append(_key_event(scan=code, flags=KEYEVENTF_UNICODE))
        events.append(_key_event(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    _send(events)
    return n


# ---------------------------------------------------------------- 對外

def inject_text(text: str, mode: str = "auto", restore_delay: float = 0.25,
                verbose: bool = True) -> dict:
    """把文字注入目前的前景視窗。

    mode:
        "paste"  只用剪貼簿貼上
        "type"   只用逐字輸入
        "auto"   先試貼上，失敗改用逐字輸入

    回傳 dict：ok、method、clipboard_restored、paste_ms、restore_ms、detail

    ⚠️ 延遲的誤解：
        `restore_delay` 預設 0.25 秒，是「送完 Ctrl+V 之後等目標程式讀走
        剪貼簿，才還原」的等待。**使用者感覺不到這段** ——
        文字在送出的當下就已經貼上去了。
        所以「反應速度」要看 `paste_ms`，不是整個函式的耗時。
    """
    def log(msg: str) -> None:
        if verbose:
            print(msg)

    if not text:
        return {"ok": False, "method": None, "clipboard_restored": True,
                "paste_ms": 0.0, "restore_ms": 0.0, "detail": "文字為空，未注入"}

    if mode == "type":
        release_modifiers()
        time.sleep(0.02)
        t0 = time.perf_counter()
        n = type_unicode(text)
        return {"ok": n > 0, "method": "type", "clipboard_restored": True,
                "paste_ms": (time.perf_counter() - t0) * 1000, "restore_ms": 0.0,
                "detail": f"逐字輸入 {n} 個字元"}

    backup = get_clipboard_text()
    had_backup = backup is not None
    if not set_clipboard_text(text):
        if mode == "paste":
            return {"ok": False, "method": "paste", "clipboard_restored": True,
                    "paste_ms": 0.0, "restore_ms": 0.0,
                    "detail": "寫入剪貼簿失敗（可能被其他程式獨占）"}
        log("  ⚠️ 剪貼簿寫入失敗，改用逐字輸入")
        return inject_text(text, mode="type", verbose=verbose)

    release_modifiers()
    time.sleep(0.02)
    t_paste = time.perf_counter()
    ok = send_ctrl_v()
    paste_ms = (time.perf_counter() - t_paste) * 1000
    # 給目標程式時間讀走剪貼簿，再還原（這段使用者感覺不到）
    time.sleep(restore_delay)

    t_restore = time.perf_counter()
    restored = True
    if had_backup:
        restored = set_clipboard_text(backup)   # type: ignore[arg-type]
        if not restored:
            log("  ⚠️ 剪貼簿還原失敗")
    else:
        # 原本沒有文字（例如是圖片或空）→ 清掉我們放的內容
        if _open_clipboard():
            user32.EmptyClipboard()
            user32.CloseClipboard()
    restore_ms = (time.perf_counter() - t_restore) * 1000

    if not ok and mode == "auto":
        log("  ⚠️ 貼上失敗，改用逐字輸入")
        fallback = inject_text(text, mode="type", verbose=verbose)
        return {"ok": fallback["ok"], "method": "type",
                "clipboard_restored": restored,
                "paste_ms": paste_ms, "restore_ms": restore_ms,
                "detail": f"貼上失敗後逐字輸入：{fallback['detail']}"}

    return {"ok": ok, "method": "paste", "clipboard_restored": restored,
            "paste_ms": paste_ms, "restore_ms": restore_ms,
            "detail": "剪貼簿貼上" + ("（剪貼簿已還原）" if had_backup else "（原本無文字）")}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="文字注入測試")
    parser.add_argument("text", nargs="?", default="測試注入 OK 123",
                        help="要注入的文字")
    parser.add_argument("--mode", default="auto", choices=("auto", "paste", "type"))
    parser.add_argument("--clipboard-test", action="store_true",
                        help="只測剪貼簿來回，不注入")
    args = parser.parse_args(argv)

    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.clipboard_test:
        # 自己的測試工具也必須遵守「備份→還原」，否則就是雙重標準。
        original = get_clipboard_text()
        shown = (original[:60] + "…") if original and len(original) > 60 else original
        print(f"目前剪貼簿（截斷顯示）：{shown!r}")
        marker = "VibeTalkie 剪貼簿測試 ✓"
        print(f"寫入：{set_clipboard_text(marker)}")
        got = get_clipboard_text()
        ok = got == marker
        print(f"讀回：{got!r}  → {'✅ 一致' if ok else '❌ 不一致'}")
        if original is not None:
            back = set_clipboard_text(original)
            print(f"還原原本內容：{'✅' if back else '❌ 失敗'}")
            ok = ok and back
        return 0 if ok else 1

    print(f"3 秒後注入「{args.text}」到前景視窗，請先把游標放到記事本…")
    for i in (3, 2, 1):
        print(f"  {i}…", end="", flush=True)
        time.sleep(1)
    print()
    result = inject_text(args.text, mode=args.mode)
    print(f"結果：{result}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
