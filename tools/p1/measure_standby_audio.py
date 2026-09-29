#!/usr/bin/env python3
"""實測：串流開著「待命」時，音訊會不會持續進來？（暖機待命假設的驗證）

## 為什麼要驗
新的設計是「串流開著待命 + 前捲」，前提是**待命期間驅動程式仍然在送資料**。
如果 BlueZ/Windows 的 SCO 在閒置時會停送（或整條連線被拆掉），
那待命就沒有意義，前捲也只會是空的。

## 做什麼
開一次麥克風，然後**不按任何鍵**、什麼都不做，每 0.5 秒記一次
「目前收到的位元組數」與「新增的音量」，連續 20 秒。
最後再看「中間隔很久之後還收不收得到」。

判讀：
  · 位元組數一直增加 → 待命有效，前捲有內容
  · 增加到某個值就停住 → 串流被驅動程式停了（要換策略）
  · 有增加但音量一直是 0 → 收到的是靜音（也可能是 SCO 沒真的建立）

執行：python tools/p1/measure_standby_audio.py [秒數]
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import struct  # noqa: E402

import config as config_module  # noqa: E402
from recorder import Capture  # noqa: E402


def rms_of(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return -1.0
    s = struct.unpack(f"<{n}h", pcm[:n * 2])
    return (sum(v * v for v in s) / n) ** 0.5


def main(argv: list[str]) -> int:
    seconds = float(argv[1]) if len(argv) > 1 else 20.0
    cfg = config_module.Config.load()
    order = cfg.effective_mic_order()
    print(f"設定的麥克風：{order or '（沒指定，用 index 1）'}")

    device = cfg.device_index
    cap = Capture(device, rate=16000, max_seconds=seconds + 5)
    cap.__enter__()
    print(f"已開啟 device {device}（max {seconds + 5:.0f}s 的緩衝區）")
    print("接下來 20 秒什麼都不做 —— 模擬「暖機待命」的閒置狀態。\n")

    t0 = time.monotonic()
    seen = 0
    stalls = 0
    while time.monotonic() - t0 < seconds:
        data = cap.recorded()
        grew = len(data) - seen
        if grew <= 0:
            stalls += 1
        else:
            new = data[seen:]
            rms = rms_of(new)
            print(f"  t={time.monotonic() - t0:5.1f}s  +{grew:6d} bytes"
                  f"  累計 {len(data):7d}  新增 RMS {rms:7.1f}")
        seen = len(data)
        time.sleep(0.5)

    print(f"\n靜置 {seconds:.0f} 秒結束：累計 {seen} bytes"
          f"（約 {seen / 32000:.1f} 秒的音訊）")
    print(f"沒有新資料的次數：{stalls}")
    if stalls > seconds * 0.5:
        print("→ ⚠️ 待命期間幾乎收不到資料：暖機待命的前提不成立")
    elif seen == 0:
        print("→ ⚠️ 完全沒收到音訊：SCO 還沒建立或裝置被占用")
    else:
        print("→ ✅ 待命期間持續有音訊 → 前捲拿得到內容，第一次按下就能錄")

    print("\n再等 5 秒，看閒置久了會不會斷（SCO 逾時的風險）…")
    time.sleep(5.0)
    after = len(cap.recorded())
    print(f"  5 秒後累計 {after} bytes（增加 {after - seen}）")
    cap.__exit__(None, None, None)
    print("已關閉串流。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
