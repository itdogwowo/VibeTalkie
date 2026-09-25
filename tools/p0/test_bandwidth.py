#!/usr/bin/env python3
"""頻寬判定器的單元測試。

為什麼要測這個？因為這個判定**會決定專案的一項硬性門檻**
（「中文準確率 ≥ 95%」能不能成立）。判定器先前出過兩個真實的錯誤：

  1. 用「高頻有沒有能量」當判準 → 純底噪的量化雜訊也會被判成寬頻。
  2. 用未正規化的 FFT 能量當 dBFS → 把 -62 dBFS 的底噪算成 -9.5 dBFS。

這兩個錯誤都會讓「8 kHz 窄頻」被誤判成「16 kHz 寬頻」，
也就是**把不合格的硬體判成合格**。所以值得有測試把它釘住。

執行：python tools/p0/test_bandwidth.py
（只用標準函式庫 + numpy，不需要 pytest。）
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from record_wav import estimate_bandwidth, setup_console  # noqa: E402

try:
    import numpy as np
except ImportError:
    sys.exit("需要 numpy。")

RATE = 16000


def to_pcm16(signal: np.ndarray) -> bytes:
    clipped = np.clip(signal, -32768, 32767).astype(np.int16)
    return clipped.tobytes()


def lowpass(signal: np.ndarray, cutoff_hz: float) -> np.ndarray:
    """用 FFT 做磚牆式低通，模擬 8 kHz 窄頻連結（4 kHz 以上全空）。"""
    spec = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(signal.size, d=1.0 / RATE)
    spec[freqs > cutoff_hz] = 0
    return np.fft.irfft(spec, n=signal.size)


def silence(seconds: float, level: float = 3.0) -> np.ndarray:
    """極安靜的底噪（±3 LSB），模擬沒說話時的錄音。"""
    rng = np.random.default_rng(1234)
    return rng.normal(0, level, int(RATE * seconds))


def speech_like(seconds: float, hf_ratio: float) -> np.ndarray:
    """合成「像語音」的訊號：低頻為主 + 可控制的高頻成分。

    hf_ratio 代表 4–8 kHz 的能量佔比，用來模擬「發ㄙ音」時的高頻。
    """
    rng = np.random.default_rng(4321)
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    # 低頻：幾個共振峰般的正弦，帶振幅包絡（模擬音節）
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)
    low = sum(np.sin(2 * np.pi * f * t) for f in (140, 400, 900, 1800))
    low = low / 4.0 * envelope
    # 高頻：寬頻雜訊經高通，模擬「ㄙ」
    noise = rng.normal(0, 1.0, n)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, d=1.0 / RATE)
    spec[freqs < 4000] = 0
    hf = np.fft.irfft(spec, n=n)
    hf = hf / (np.abs(hf).max() or 1.0) * envelope
    return (low * 9000.0 + hf * 9000.0 * hf_ratio)


CASES = [
    # (名稱, 訊號, 允許的判定結果集合)
    ("純底噪（沒說話）", silence(2.0), {"too-quiet"}),
    ("寬頻：有高頻內容 + 音量足夠", speech_like(2.0, hf_ratio=0.8), {"wideband"}),
    ("8 kHz 窄頻：4 kHz 以上被切掉", lowpass(speech_like(2.0, hf_ratio=0.8), 3800),
     {"no-hf-response"}),
    ("極小聲（低於可信門檻）", speech_like(2.0, hf_ratio=0.8) * 0.0008, {"too-quiet"}),
]


def main() -> int:
    setup_console()
    failures = 0
    print(f"{'案例':<34} {'位準 dBFS':>10} {'delta dB':>9}  判定")
    print("-" * 78)
    for name, signal, allowed in CASES:
        result = estimate_bandwidth(to_pcm16(signal), RATE, 16)
        if result is None:
            print(f"{name:<34} 分析器回傳 None")
            failures += 1
            continue
        verdict = result["verdict"]
        ok = verdict in allowed
        mark = "✅" if ok else "❌"
        print(f"{name:<34} {result['active_level_db']:>+10.1f} {result['delta_db']:>+9.1f}  "
              f"{mark} {verdict}")
        if not ok:
            failures += 1
            print(f"      預期 {sorted(allowed)}，實際 {verdict}")
            print(f"      說明：{result['note']}")

    print()
    if failures:
        print(f"❌ {failures} 個案例失敗")
        return 1
    print(f"✅ 全部 {len(CASES)} 個案例通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
