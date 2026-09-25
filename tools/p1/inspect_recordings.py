#!/usr/bin/env python3
"""檢查錄音檔的音量與「有聲/靜音」結構。

用途：在跑 ASR 之前先確認錄音本身沒問題。實測踩過的坑：

  - 用「整段平均」當音量 → 前後靜音會嚴重低估，產生**假警報**
    （實測整段平均 -42 dBFS，但實際說話是 -37 dBFS）
  - 錄音裡大部分是靜音 → 模型容易在長靜音上出錯或截斷
  - 位準太低（約 -48 dBFS 以下）→ ASR 直接回傳**空字串**，不是回錯字

用法:
    python tools/p1/inspect_recordings.py                 # 看 artifacts/p1
    python tools/p1/inspect_recordings.py <目錄或檔案>
    python tools/p1/inspect_recordings.py --min-db -38
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
_CORE = Path(__file__).resolve().parents[2] / "app" / "core"
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "third_party"))

from recorder import active_level  # noqa: E402
from record_wav import setup_console  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def whole_level(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return float("-inf")
    s = struct.unpack(f"<{n}h", pcm[:n * 2])
    rms = (sum(v * v for v in s) / n) ** 0.5
    return 20.0 * math.log10(rms / 32768.0) if rms > 0 else float("-inf")


def structure(pcm: bytes, rate: int, buckets: int = 24) -> str:
    """把有聲/靜音畫成一條字串，一眼看出錄音的形狀。"""
    n = len(pcm) // 2
    if n == 0:
        return ""
    s = struct.unpack(f"<{n}h", pcm[:n * 2])
    win = max(1, int(rate * 0.05))
    frames = []
    for i in range(0, n - win + 1, win):
        seg = s[i:i + win]
        frames.append((sum(v * v for v in seg) / len(seg)) ** 0.5)
    if not frames:
        return ""
    srt = sorted(frames)
    thr = max(srt[len(srt) // 4] * 2.5, 25.0)
    out = []
    for b in range(buckets):
        lo = int(b * len(frames) / buckets)
        hi = max(lo + 1, int((b + 1) * len(frames) / buckets))
        out.append("#" if any(f > thr for f in frames[lo:hi]) else ".")
    return "".join(out)


def inspect_file(path: Path, min_db: float) -> dict:
    with wave.open(str(path), "rb") as wf:
        rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        pcm = wf.readframes(wf.getnframes())
        dur = wf.getnframes() / rate if rate else 0.0

    whole = whole_level(pcm)
    adb, ratio, asec = active_level(pcm, rate)
    peak = 0
    n = len(pcm) // 2
    if n:
        peak = max(abs(v) for v in struct.unpack(f"<{n}h", pcm[:n * 2]))

    problems = []
    if adb == float("-inf"):
        problems.append("完全偵測不到說話")
    elif adb < min_db:
        problems.append(f"說話位準 {adb:+.1f} dBFS 低於門檻 {min_db:+.0f}")
    if dur > 0 and ratio < 0.35:
        problems.append(f"只有 {ratio * 100:.0f}% 在說話（靜音過多）")
    if peak >= 32000:
        problems.append("峰值接近滿刻度，可能削波")

    return {"path": path, "rate": rate, "ch": ch, "width": width, "dur": dur,
            "whole": whole, "active": adb, "ratio": ratio, "asec": asec,
            "peak": peak, "structure": structure(pcm, rate), "problems": problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="檢查錄音的音量與結構")
    parser.add_argument("target", nargs="?", default=str(ROOT / "artifacts" / "p1"),
                        help="目錄或單一 WAV（預設 artifacts/p1）")
    parser.add_argument("--min-db", type=float, default=-42.0,
                        help="說話位準的可接受下限（預設 -42 dBFS；"
                             "實測 -38.4 dBFS 仍可完全正確辨識）")
    args = parser.parse_args(argv)
    setup_console()

    target = Path(args.target)
    files = sorted(target.glob("*.wav")) if target.is_dir() else [target]
    if not files:
        print(f"找不到 WAV：{target}")
        return 1

    print(f"{'檔案':<12}{'長度':>7}{'整段':>8}{'說話':>8}{'有聲':>7}{'峰值':>8}  結構（# 有聲 . 靜音）")
    print("-" * 92)
    bad = 0
    for f in files:
        try:
            r = inspect_file(f, args.min_db)
        except Exception as exc:
            print(f"{f.stem:<12} ❌ {exc}")
            bad += 1
            continue
        flag = "⚠️" if r["problems"] else "  "
        print(f"{f.stem:<12}{r['dur']:>6.1f}s{r['whole']:>7.1f}{r['active']:>8.1f}"
              f"{r['ratio'] * 100:>6.0f}%{r['peak']:>8} {flag} {r['structure']}")
        for p in r["problems"]:
            print(f"{'':<12}    ⚠️ {p}")
            bad += 1

    print("-" * 92)
    print(f"共 {len(files)} 個檔案，{bad} 個問題。")
    print(f"\n註：『整段』是整段平均（會被靜音拉低，僅供對照），"
          f"『說話』才是有聲段落的實際音量。")
    print(f"    位準低於 {args.min_db:+.0f} dBFS 時，ASR 可能回傳空字串而不是回錯字。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
