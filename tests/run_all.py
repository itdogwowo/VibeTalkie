#!/usr/bin/env python3
"""一鍵跑完 `tests/` 底下所有測試（**單一來源**）。

## 為什麼要有這支

1. **`AGENTS.md` §9 的清單會漂移。** 實際發生過：清單列 20 支、`tests/`
   裡有 22 支，新寫的 `test_launch_guard.py` 與 `test_mac_keys.py` 沒進去 ——
   而「文件少列一支」的症狀是「那支測試從此沒人跑」。改成掃目錄就不會再漏。
2. **輸出編碼。** 每一支測試自己呼叫 `tests/_console.py`，但**執行者**也要
   把 `PYTHONUTF8=1` 傳給子行程 —— 否則輸出被管線接走時，Windows 會用
   cp950 編碼，測試會崩在印 `✅` 那一行（實測：22 支裡有 12 支是這樣紅的）。
3. **一致的口徑。** 一次跑完、統一的 PASS/FAIL 與耗時，失敗的自動附尾巴。

## 用法

    python tests/run_all.py                 # 全部（跳過需要 macOS 的）
    python tests/run_all.py --only trigger  # 只跑檔名含 "trigger" 的
    python tests/run_all.py --list          # 只列出會被跑哪些
    python tests/run_all.py --all           # 連需要 macOS 的也跑（會失敗）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

import _console  # noqa: E402  # 測試輸出一律 UTF-8（見 tests/_console.py）

# 需要真的 macOS（pyobjc／system_profiler）—— 在其他平台上跑一定失敗，
# 而「一定失敗的測試」會讓整份結果看起來是紅的，所以預設跳過並講明原因。
MAC_ONLY = {
    "test_mac_e2e.py": "需要 pyobjc 與真的 Quartz／AVAudioEngine",
    "test_mac_devices.py": "需要 system_profiler（macOS 內建）",
}


def discover() -> list[Path]:
    return sorted(p for p in HERE.glob("test_*.py"))


def main() -> int:
    _console.setup()

    ap = argparse.ArgumentParser(description="跑完 tests/ 底下所有測試")
    ap.add_argument("--only", default="", help="只跑檔名含這個字串的")
    ap.add_argument("--list", action="store_true", help="只列出會被跑哪些")
    ap.add_argument("--all", action="store_true", help="連需要 macOS 的也跑")
    ap.add_argument("--timeout", type=float, default=300.0, help="單支逾時秒數")
    args = ap.parse_args()

    is_mac = sys.platform == "darwin"
    files = [p for p in discover() if args.only in p.name]

    todo: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for p in files:
        if p.name in MAC_ONLY and not (is_mac or args.all):
            skipped.append((p, MAC_ONLY[p.name]))
            continue
        todo.append(p)

    print("=" * 74)
    print(f"VibeTalkie 測試總覽（{sys.platform}／Python "
          f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}）")
    print("=" * 74)

    if args.list:
        for p in todo:
            print(f"  會跑    {p.name}")
        for p, why in skipped:
            print(f"  跳過    {p.name}  — {why}")
        return 0

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"          # ⚠️ 沒有這個，管線下會用 cp950 編碼
    env["PYTHONIOENCODING"] = "utf-8"

    failed: list[str] = []
    for p in todo:
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, str(p)], cwd=str(ROOT), env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=args.timeout)
            code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            code = 124
            out = f"（超過 {args.timeout:.0f} 秒，已終止）"
        took = time.time() - t0

        mark = "✅" if code == 0 else "❌"
        print(f"  {mark} {p.name:<28} {took:6.1f}s" + ("" if code == 0 else f"  (exit {code})"))
        if code != 0:
            failed.append(p.name)
            tail = [ln for ln in out.splitlines() if ln.strip()][-25:]
            print("     " + "\n     ".join(tail))

    if skipped:
        print(f"\n  （跳過 {len(skipped)} 支需要 macOS 的："
              + "、".join(p.name for p, _ in skipped) + "）")

    print("\n" + "=" * 74)
    if failed:
        print(f"❌ {len(failed)}/{len(todo)} 支失敗：")
        for name in failed:
            print(f"   · {name}")
        return 1
    print(f"✅ 全部通過（{len(todo)} 支）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
