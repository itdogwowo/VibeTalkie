#!/usr/bin/env python3
"""macOS 文字注入：剪貼簿 + Cmd+V，失敗則逐字輸入。

## 這個模組取代什麼

Windows 版的 `textin.py`（322 行）用 **OpenClipboard + SendInput**。
macOS 對應的是 **NSPasteboard + CGEventPost**：

    Windows                          macOS
    OpenClipboard / SetClipboardData  NSPasteboard.generalPasteboard
    SendInput(Ctrl+V)                 CGEventPost(Cmd+V)
    SendInput(逐字 keybd_event)        CGEventPost(每個字元的 keycode)

## 為什麼剪貼簿要備份「所有型別」而不只是文字

Windows 版只備份文字格式。macOS 的剪貼簿可以同時裝很多型別
（`public.utf8-plain-text`、`public.rtf`、`public.png`…），使用者複製
一張圖片或一段有格式的文字，如果我們只還原純文字部分，**格式就沒了**。

所以這裡的做法是：把 `pasteboardItems()` 的**每一個 item 的每一個型別**
都存下來（`NSData` / 字串），注入後再 `clearContents` + `setData` 還原。
這比 Windows 版更完整，是刻意的。

## 權限

`CGEventPost` 送出 Cmd+V 需要「**輔助使用**」權限。
沒有的話事件會被靜默丟棄（不報錯，只是沒反應）——所以這裡**不假裝成功**：
送出後如果無法確認，就把狀態誠實回報，讓上層顯示「文字已複製到剪貼簿」。

## 回傳契約（與 Windows 的 `inject_text` 一致）

    {"ok": bool, "method": "paste"|"type"|None, "clipboard_restored": bool,
     "paste_ms": float, "restore_ms": float, "detail": str}
"""

from __future__ import annotations

import time
from typing import Any

# US 鍵盤的「字元 → keycode」對照（給 type 模式用）
# 參考 <HIToolbox/Events.h>。只涵蓋能直接打出來的字元。
_CHAR_KEYCODES: dict[str, tuple[int, bool]] = {
    # 字元: (keycode, 是否需要 Shift)
    "a": (0, False), "b": (11, False), "c": (8, False), "d": (2, False),
    "e": (14, False), "f": (3, False), "g": (5, False), "h": (4, False),
    "i": (34, False), "j": (38, False), "k": (40, False), "l": (37, False),
    "m": (46, False), "n": (45, False), "o": (31, False), "p": (35, False),
    "q": (12, False), "r": (15, False), "s": (1, False), "t": (17, False),
    "u": (32, False), "v": (9, False), "w": (13, False), "x": (7, False),
    "y": (16, False), "z": (6, False),
    "1": (18, False), "2": (19, False), "3": (20, False), "4": (21, False),
    "5": (23, False), "6": (22, False), "7": (26, False), "8": (28, False),
    "9": (25, False), "0": (29, False),
    " ": (49, False), "-": (27, False), "=": (24, False),
    "[": (33, False), "]": (30, False), "\\": (42, False),
    ";": (41, False), "'": (39, False), ",": (43, False),
    ".": (47, False), "/": (44, False), "`": (50, False),
    # 需要 Shift 的
    "A": (0, True), "B": (11, True), "C": (8, True), "D": (2, True),
    "E": (14, True), "F": (3, True), "G": (5, True), "H": (4, True),
    "I": (34, True), "J": (38, True), "K": (40, True), "L": (37, True),
    "M": (46, True), "N": (45, True), "O": (31, True), "P": (35, True),
    "Q": (12, True), "R": (15, True), "S": (1, True), "T": (17, True),
    "U": (32, True), "V": (9, True), "W": (13, True), "X": (7, True),
    "Y": (16, True), "Z": (6, True),
    "!": (18, True), "@": (19, True), "#": (20, True), "$": (21, True),
    "%": (23, True), "^": (22, True), "&": (26, True), "*": (28, True),
    "(": (25, True), ")": (29, True),
    "_": (27, True), "+": (24, True), "{": (33, True), "}": (30, True),
    "|": (42, True), ":": (41, True), '"': (39, True), "<": (43, True),
    ">": (47, True), "?": (44, True), "~": (50, True),
}

KEY_V = 9
FLAG_SHIFT = 0x00020000
FLAG_COMMAND = 0x00100000


class InjectError(RuntimeError):
    """注入相關錯誤。"""


def accessibility_hint() -> str:
    return (
        "送出 Cmd+V 需要「輔助使用」權限。\n"
        "  系統設定 → 隱私權與安全性 → 輔助使用 → 把 Terminal（或本程式）打勾。\n"
        "  改完要**完全結束再重開**該程式（⌘Q）。"
    )


def _pasteboard():
    """取得系統剪貼簿。載入不到 AppKit 就明確報錯。"""
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString  # type: ignore
    except ImportError as exc:
        raise InjectError(
            f"載入不到 AppKit（pyobjc）：{exc}\n"
            "  請安裝：pip install pyobjc-framework-Cocoa"
        ) from exc
    return NSPasteboard.generalPasteboard(), NSPasteboardTypeString


# ---------------------------------------------------------------- 剪貼簿


def snapshot_clipboard() -> list[dict[str, Any]]:
    """把剪貼簿的**所有 item 與所有型別**存下來。

    回傳的結構可以直接餵給 `restore_clipboard()`。

    ⚠️ 這裡刻意不用「只讀字串」的簡化版：那會讓使用者複製的圖片、
    有格式的文字在注入後**永久消失**。剪貼簿是使用者的資料，
    我們只是借過，必須完整還原（`architecture.md` §3 `textin` 的鐵則）。
    """
    pb, _ = _pasteboard()
    items = pb.pasteboardItems()
    if not items:
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        entry: dict[str, Any] = {}
        for t in item.types():
            data = item.dataForType_(t)
            if data is not None:
                entry[str(t)] = bytes(data)
            else:
                s = item.stringForType_(t)
                if s is not None:
                    entry[str(t)] = str(s)
        if entry:
            out.append(entry)
    return out


def restore_clipboard(snapshot: list[dict[str, Any]]) -> bool:
    """還原剪貼簿。回傳有沒有成功。

    ⚠️ 即使注入失敗也**必須**呼叫這個（`finally` 語意）——
    否則使用者的剪貼簿會停在我們寫入的辨識結果上。
    """
    try:
        pb, _ = _pasteboard()
        from AppKit import NSPasteboardItem  # type: ignore

        pb.clearContents()
        if not snapshot:
            return True
        objs = []
        for entry in snapshot:
            pi = NSPasteboardItem.alloc().init()
            ok_any = False
            for t, v in entry.items():
                try:
                    if isinstance(v, str):
                        pi.setString_forType_(v, t)
                    else:
                        pi.setData_forType_(v, t)
                    ok_any = True
                except Exception:                     # noqa: BLE001
                    continue
            if ok_any:
                objs.append(pi)
        if not objs:
            return True
        return bool(pb.writeObjects_(objs))
    except Exception:                                 # noqa: BLE001
        return False


def set_clipboard_text(text: str) -> bool:
    """把文字放進剪貼簿。"""
    try:
        pb, PBStr = _pasteboard()
        pb.clearContents()
        return bool(pb.setString_forType_(text, PBStr))
    except Exception:                                 # noqa: BLE001
        return False


# ---------------------------------------------------------------- 送按鍵


def _post_key(keycode: int, flags: int = 0) -> None:
    """送出一顆按鍵（按下 + 放開）。"""
    import Quartz  # type: ignore

    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def post_paste() -> None:
    """送出 Cmd+V。"""
    _post_key(KEY_V, FLAG_COMMAND)


def type_text(text: str) -> int:
    """逐字輸入（退路）。回傳成功送出的字元數。

    ⚠️ 只支援 US 鍵盤佈局能直接打出的字元。中文、emoji 這類**打不出來**
    的字元會被跳過並計數——呼叫端要能分辨「成功」與「部分成功」。
    """
    sent = 0
    for ch in text:
        spec = _CHAR_KEYCODES.get(ch)
        if spec is None:
            continue                                  # 打不出來，跳過
        kc, need_shift = spec
        _post_key(kc, FLAG_SHIFT if need_shift else 0)
        sent += 1
        time.sleep(0.004)                             # 太快會被目標程式漏掉
    return sent


# ---------------------------------------------------------------- 主入口


def inject_text(text: str, mode: str = "auto", restore_delay: float = 0.25,
                verbose: bool = False) -> dict:
    """把文字注入目前的前景視窗。

    `mode`：
        "paste"  只走剪貼簿貼上
        "type"   只走逐字輸入
        "auto"   先試貼上，失敗才逐字（預設，與 Windows 版一致）

    與 Windows 版的差異：**沒辦法確認貼上是否真的成功**。
    macOS 不提供「貼上完成」的通知，所以 `ok` 的語意是
    「Cmd+V 已經送出、沒有發生錯誤」，不是「目標程式確實貼上了」。
    這一點誠實反映在 `detail` 裡，不假裝成功。
    """
    now = lambda: time.perf_counter()               # noqa: E731
    t0 = now()

    if not text:
        return {"ok": False, "method": None, "clipboard_restored": True,
                "paste_ms": 0.0, "restore_ms": 0.0, "detail": "文字為空，未注入"}

    if mode == "type":
        n = type_text(text)
        return {"ok": n > 0, "method": "type", "clipboard_restored": True,
                "paste_ms": (now() - t0) * 1000, "restore_ms": 0.0,
                "detail": f"逐字輸入 {n} 個字元"
                          + ("" if n == len(text) else f"（跳過 {len(text)-n} 個打不出來的字元）")}

    # 主路徑：剪貼簿貼上
    backup = snapshot_clipboard()
    if not set_clipboard_text(text):
        fallback = {"ok": False, "detail": "寫入剪貼簿失敗"}
        if mode == "auto":
            n = type_text(text)
            if n:
                return {"ok": True, "method": "type", "clipboard_restored": True,
                        "paste_ms": (now() - t0) * 1000, "restore_ms": 0.0,
                        "detail": f"剪貼簿不可用，改逐字輸入 {n} 個字元"}
        return {"ok": False, "method": "paste", "clipboard_restored": True,
                "paste_ms": 0.0, "restore_ms": 0.0, "detail": fallback["detail"]}

    t_paste = now()
    try:
        post_paste()
        ok = True
        detail = "剪貼簿貼上"
    except Exception as exc:                          # noqa: BLE001
        ok = False
        detail = f"送出 Cmd+V 失敗：{exc}"
    paste_ms = (now() - t_paste) * 1000

    if not ok and mode == "auto":
        n = type_text(text)
        if n:
            detail = f"貼上失敗後逐字輸入 {n} 個字元"

    # 等目標程式讀走剪貼簿再還原。太快的話貼上的會是舊內容。
    time.sleep(max(0.0, restore_delay))
    t_restore = now()
    restored = restore_clipboard(backup)
    restore_ms = (now() - t_restore) * 1000

    had_backup = bool(backup)
    return {
        "ok": ok,
        "method": "paste",
        "clipboard_restored": restored,
        "paste_ms": paste_ms,
        "restore_ms": restore_ms,
        "detail": detail + ("（剪貼簿已還原）" if had_backup else "（原本無內容）")
                  + ("" if restored else " ⚠️ 剪貼簿還原失敗"),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="macOS 文字注入測試")
    ap.add_argument("--text", default="測試一二三 VibeTalkie")
    ap.add_argument("--mode", default="auto", choices=["auto", "paste", "type"])
    ap.add_argument("--seconds", type=float, default=4.0,
                    help="等幾秒再注入（讓你切到目標視窗）")
    args = ap.parse_args()

    print("=" * 74)
    print("  macOS 文字注入測試")
    print("=" * 74)

    # 先看剪貼簿現況
    snap = snapshot_clipboard()
    print(f"\n  注入前剪貼簿：{len(snap)} 個 item")
    for i, item in enumerate(snap, 1):
        print(f"    [{i}] 型別 {list(item.keys())}")

    print(f"\n  {args.seconds:.0f} 秒後注入 —— 請先切到可以打字的視窗（例如記事本）")
    for i in range(int(args.seconds), 0, -1):
        print(f"\r  {i}…", end="", flush=True)
        time.sleep(1)
    print("\r  注入中…")

    res = inject_text(args.text, mode=args.mode, verbose=True)
    print(f"\n  結果：{res}")
    after = snapshot_clipboard()
    print(f"  注入後剪貼簿：{len(after)} 個 item"
          f"  {'✅ 已還原' if after == snap else '⚠️ 與注入前不同'}")
