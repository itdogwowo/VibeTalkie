#!/usr/bin/env python3
"""P1 — 錄製 20 句 ASR 測試集。

流程：顯示句子 → 按 Enter 開始 → 即時音量表 → 說完自動停 → 立即回報音量並可重錄。

為什麼要「立即回報音量」？
    實測發現裝置的收音位準偏低，而位準太低時 ASR 會回傳空字串（不是回錯字，是**什麼都沒有**）。
    所以在錄的當下就要知道夠不夠大聲，而不是等跑完 20 句才發現全部失敗。

用法:
    python tools/p1/record_testset.py                 # 從頭錄，跳過已存在的
    python tools/p1/record_testset.py --only 003,007  # 只重錄特定句子
    python tools/p1/record_testset.py --redo-all      # 全部重錄
    python tools/p1/record_testset.py --device 1
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))

from recorder import active_level, capture_utterance  # noqa: E402
from record_wav import list_devices, setup_console  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TESTSET = Path(__file__).resolve().parent / "testset_zh.txt"
REC_DIR = ROOT / "artifacts" / "p1"

# 「說話位準」的可接受下限。
# 這個數字是實測調出來的，不是猜的：
#   -38.4 dBFS → 辨識完全正確（utt003 / utt005 兩句）
#   -39.9 dBFS → 空結果（utt008）
#   -29.9 dBFS → 反而辨識錯誤（utt007，峰值 29483 疑似削波）
# 所以「音量」不是唯一因素，訂在 -42 只抓真正太小聲的，避免像先前那樣
# 用整段平均產生一堆假警報。錄音長度與有聲比例影響更大。
WARN_DB = -42.0


def load_testset() -> list[tuple[str, str]]:
    items = []
    for line in TESTSET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        idx, _, ref = line.partition("|")
        items.append((idx.strip(), ref.strip()))
    return items


def save_wav(path: Path, pcm: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="錄製 P1 測試集")
    parser.add_argument("--device", type=int, default=1, help="音訊裝置索引（預設 1）")
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--only", default=None, help="只錄這些編號，逗號分隔（例：003,007）")
    parser.add_argument("--redo-all", action="store_true", help="已存在也重錄")
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args(argv)
    setup_console()

    if args.list_devices:
        for i, name, mask, ch, _ in list_devices():
            print(f"  [{i}] {name}  channels={ch}  formats=0x{mask:08X}")
        return 0

    items = load_testset()
    if args.only:
        want = {s.strip().zfill(3) for s in args.only.split(",") if s.strip()}
        items = [(i, r) for i, r in items if i in want]
        if not items:
            print(f"沒有符合的編號：{args.only}")
            return 1

    todo = []
    for idx, ref in items:
        path = REC_DIR / f"utt{idx}.wav"
        if path.exists() and not args.redo_all:
            print(f"  跳過 utt{idx}（已存在，--redo-all 可重錄）")
            continue
        todo.append((idx, ref))

    if not todo:
        print("\n全部都已錄好。要重錄用 --redo-all 或 --only。")
        return 0

    print(f"\n準備錄 {len(todo)} 句到 {REC_DIR}")
    print("裝置預設為 index 1（USB/藍牙麥克風）；用 --list-devices 確認。")
    print("\n操作方式：")
    print("  Enter     開始錄這一句（說完停一下會自動停止）")
    print("  r + Enter 重錄這一句")
    print("  s + Enter 跳過這一句")
    print("  q + Enter 結束（已錄的會保留）")
    print("\n⚠️ 請正常音量、靠近麥克風說話。位準低於 "
          f"{WARN_DB:.0f} dBFS 會被警告。\n")
    input("準備好按 Enter 開始…")

    results = []
    i = 0
    while i < len(todo):
        idx, ref = todo[i]
        path = REC_DIR / f"utt{idx}.wav"
        print("\n" + "=" * 70)
        print(f"[{i + 1}/{len(todo)}] utt{idx}")
        print(f"  請說：{ref}")
        cmd = input("  Enter 開始錄音 / r 重錄 / s 跳過 / q 結束：").strip().lower()

        if cmd == "q":
            break
        if cmd == "s":
            i += 1
            continue
        if cmd == "r" and path.exists():
            path.unlink()

        try:
            res = capture_utterance(args.device, rate=args.rate)
        except Exception as exc:
            print(f"  ❌ 錄音失敗：{exc}")
            print("     請確認裝置索引（--list-devices）與裝置是否已連線。")
            return 1

        if res["timed_out"] or res["duration_s"] < 0.2:
            print("  ⚠️ 沒有偵測到說話（逾時）。請確認麥克風並重試。")
            continue

        save_wav(path, res["pcm"], args.rate)
        adb, aratio, asec = active_level(res["pcm"], args.rate)
        status = "✅" if adb >= WARN_DB else "⚠️ 太小聲"
        print(f"  {status} 長度 {res['duration_s']:.2f}s，"
              f"說話 {asec:.1f}s（佔 {aratio * 100:.0f}%），"
              f"說話位準 {adb:+.1f} dBFS，峰值 {res['peak']}/32767")
        if adb < WARN_DB:
            print("     說話音量偏低。實測位準太低時 ASR 會回傳空字串。")
            print("     建議靠近麥克風、正常音量重錄（按 r）。")
        if res["duration_s"] > 8:
            print(f"     ⚠️ 這段有 {res['duration_s'] - asec:.1f}s 是靜音"
                  f"（只有 {aratio * 100:.0f}% 在說話）→ 建議重錄，避免模型在長靜音上出錯。")

        results.append((idx, res["duration_s"], adb, res["peak"], aratio))
        i += 1

    print("\n" + "=" * 70)
    if results:
        print(f"本次錄了 {len(results)} 句：")
        for idx, dur, db, peak, ratio in results:
            flag = "✅" if db >= WARN_DB else "⚠️"
            print(f"  {flag} utt{idx}  {dur:5.2f}s  說話位準 {db:+6.1f} dBFS  "
                  f"有聲 {ratio * 100:3.0f}%  peak {peak}")
        weak = [r for r in results if r[2] < WARN_DB]
        if weak:
            print(f"\n⚠️ 有 {len(weak)} 句說話位準偏低（{', '.join('utt' + r[0] for r in weak)}）")
            print("   建議重錄：python tools/p1/record_testset.py --only "
                  + ",".join(r[0] for r in weak))
    print(f"\n錄音位置：{REC_DIR}")
    print("接著跑：python tools/p1/asr_bench.py --bench")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
