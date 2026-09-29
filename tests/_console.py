#!/usr/bin/env python3
"""測試的輸出編碼：**每一支測試都應該呼叫它**。

## 為什麼要有這一支（實測踩到）

測試用 `✅` / `❌` / `↔` 印結果，而 Windows 的 Python 在輸出被**接管**時
（管線、`> out.txt`、CI）用的不是主控台代碼頁，而是**地區設定**（cp950）：

    UnicodeEncodeError: 'cp950' codec can't encode character '\\u2705'

於是「測試全綠」變成「12 支測試自己崩在印 ✅ 那一行」——
看起來像產品壞掉，其實是**測試的輸出**壞掉。實際踩到的情況：
用管線收集 22 支測試的結果，其中 12 支是這樣紅的，而且錯誤訊息指向
`check()`，完全看不出真正的原因。

AGENTS.md §8.6 對**進入點**已經有同一條規則（`SetConsoleOutputCP(65001)`
＋ `reconfigure(encoding="utf-8")`），但 22 支測試裡只有 6 支照做 ——
**同一條規則寫在 22 個地方就是漂移**，所以收斂成這一支。

## 怎麼用

    import _console

    _console.setup()

`tests/` 是腳本自己所在的目錄，一定在 `sys.path[0]`，所以直接 import 即可。
重複呼叫沒有副作用，在非 Windows 上只做 `reconfigure`。
"""

from __future__ import annotations

import sys

_done = False


def setup() -> None:
    """讓 stdout／stderr 一定編得出 `✅`（重複呼叫只做一次）。"""
    global _done
    if _done:
        return
    _done = True

    # 主控台：連代碼頁也一起換，否則 Interactive 視窗仍會是 cp950。
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)   # type: ignore[attr-defined]
    except Exception:                                      # noqa: BLE001
        pass                                               # 非 Windows／沒有主控台

    # 被管線或檔案接走時，`reconfigure` 才是真正生效的那一步。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                  # noqa: BLE001
            pass
