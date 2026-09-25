#!/usr/bin/env python3
"""量測藍牙音訊的「首次取樣延遲」，以及閒置後連線是否會斷。

為什麼這很重要（產品級）：
    「按住說話」的 UX 假設「按下就開始錄」。但實測這支藍牙麥克風
    在**冷啟動**時要等一段時間才會有音訊：

        冷啟動第一次開串流   : 1.19s
        緊接著再開           : 0.67s
        閒置 2/5/10/20s 後再開: 0.04s  ← 連線沒有斷

    意思不是「每次都要等」，而是「**連線一旦建立就會一直保持**」。
    所以只要在程式啟動時暖機一次，之後每次按下都能立刻開始錄。

    若不做暖機，使用者**第一次**按住說話會拿到 0 bytes，
    開頭那句話就不見了 —— 實測真的發生過。

用法:
    python tools/p1/measure_audio_latency.py            # 預設裝置 index 1
    python tools/p1/measure_audio_latency.py --device 3
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2] / "app" / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "third_party"))

from recorder import Capture  # noqa: E402
from record_wav import setup_console  # noqa: E402


def time_to_first_audio(device: int, timeout: float = 8.0) -> float | None:
    """開串流後，等到真的收到第一個 byte 花了多久。"""
    cap = Capture(device, rate=16000, max_seconds=timeout + 2.0)
    cap.__enter__()
    t0 = time.monotonic()
    first = None
    try:
        while time.monotonic() - t0 < timeout:
            if len(cap.recorded()) > 0:
                first = time.monotonic() - t0
                break
            time.sleep(0.02)
    finally:
        cap.__exit__(None, None, None)
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量測藍牙音訊首次取樣延遲")
    parser.add_argument("--device", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--idle", default="2,5,10,20",
                        help="閒置測試的秒數，逗號分隔")
    args = parser.parse_args(argv)
    setup_console()

    def show(label: str, r: float | None) -> None:
        print(f"  {label:<22}{'逾時' if r is None else f'{r:.2f}s'}")

    print(f"裝置 index={args.device}\n")
    print("=== 冷啟動（第一次開串流）===")
    for i in range(2):
        show(f"第 {i + 1} 次", time_to_first_audio(args.device, args.timeout))

    print("\n=== 關閉後閒置 N 秒再開（看連線會不會斷）===")
    for idle in (float(x) for x in args.idle.split(",") if x.strip()):
        time.sleep(idle)
        show(f"閒置 {idle:g}s 後", time_to_first_audio(args.device, args.timeout))

    print("\n判讀：")
    print("  冷啟動明顯較慢、但閒置後仍然很快 → 連線建立後會保持。")
    print("  → 程式啟動時暖機一次即可，不必每次按下都等。")
    print("  若閒置後又變慢，代表連線會斷，就需要定期 keep-alive。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
