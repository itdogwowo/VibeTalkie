#!/usr/bin/env python3
"""測試用：合成按鍵事件，用來驗證 ptt.py 的偵測鏈路（不需實體裝置）。

⚠️ **已知限制：合成的按鍵不會在 Raw Input 產生 key-DOWN。**
   實測結果（`ptt.py --debug`）：

       注入 SendInput(wVk=VK_RCONTROL, wScan=0x1D, KEYEVENTF_EXTENDEDKEY)
       然後按住 3 秒再放開
       → Raw Input 只收到 **1 個事件**：`flags=0x3 make=29`（= UP + E0）

   keydown 完全沒出現。所以這個工具**無法**驗證
   「按下 → 錄音 → 放開 → 辨識 → 注入」的完整迴路。

   它能驗證的是：
     - 視窗有收到 WM_INPUT
     - flags / make code 的解析正確（E0 有正確帶出來）
     - 裝置與 E0 的篩選邏輯會正確接受或拒絕

   完整迴路**只能靠實體裝置的錄音鍵**驗證（T3 已證明真裝置會送乾淨的
   DOWN+UP 配對：make=29、flags=0x02/0x03）。

用法:
    python tools/p1/inject_test_key.py right 3.0        # 注入 Right Ctrl 按住 3 秒
    python tools/p1/inject_test_key.py right 3.0 5      # 延遲 5 秒後才注入
    python tools/p1/inject_test_key.py left 1.0         # 左 Ctrl（應被 E0 篩選擋掉）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2] / "app" / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "third_party"))

from textin import KEYEVENTF_KEYUP, _key_event, _send  # noqa: E402

VK_RCONTROL = 0xA3
VK_LCONTROL = 0xA2
KEYEVENTF_EXTENDEDKEY = 0x0001
SCAN_CTRL = 0x1D


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    which = argv[0] if argv else "right"
    hold = float(argv[1]) if len(argv) > 1 else 2.0
    delay = float(argv[2]) if len(argv) > 2 else 0.0

    if which == "right":
        vk, flags = VK_RCONTROL, KEYEVENTF_EXTENDEDKEY
    else:
        vk, flags = VK_LCONTROL, 0

    if delay > 0:
        print(f"延遲 {delay}s 後注入…", flush=True)
        time.sleep(delay)

    label = "Right" if which == "right" else "Left"
    print(f"注入 {label} Ctrl 按住 {hold}s…", flush=True)
    n = _send([_key_event(vk=vk, scan=SCAN_CTRL, flags=flags)])
    print(f"  keydown 送出 {n} 個事件", flush=True)
    time.sleep(hold)
    n = _send([_key_event(vk=vk, scan=SCAN_CTRL, flags=flags | KEYEVENTF_KEYUP)])
    print(f"  keyup 送出 {n} 個事件", flush=True)
    print("提醒：合成事件在 Raw Input 只會出現 UP，無法驗證完整迴路。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
