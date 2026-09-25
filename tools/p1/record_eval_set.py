#!/usr/bin/env python3
"""為 ASR 評測錄製語料（一句一個 WAV，計時自動換句）。

## 為什麼要「計時自動」而不是「按 Enter 換句」

評測要比較多個模型，錄音品質若不穩定（每次間隔不同、長度不同），
比較就沒有意義。計時制讓每一句的節奏一致，而且使用者只要專心念。

## 為什麼不把語料 commit 進 repo

`AGENTS.md` §7：真實錄音檔與語料中的個資不進版控。
所以錄音全部留在 `artifacts/`（已 gitignore），只把**結論**寫進文件。

用法:
    python tools/p1/record_eval_set.py --list
    python tools/p1/record_eval_set.py --phrases artifacts/yue-eval/phrases.txt --seconds 4
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app" / "core"))
sys.path.insert(0, str(_ROOT / "third_party"))

from record_wav import setup_console, list_devices  # noqa: E402
from recorder import Capture  # noqa: E402


def load_phrases(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        text, _, note = line.partition("|")
        out.append((text.strip(), note.strip()))
    return out


def beep(freq: int = 880, ms: int = 120) -> None:
    """用 winsound 發短音（不寫檔案）。"""
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        print("\a", end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="錄製 ASR 評測語料")
    ap.add_argument("--phrases", default=str(_ROOT / "artifacts" / "yue-eval" / "phrases.txt"))
    ap.add_argument("--out", default=str(_ROOT / "artifacts" / "yue-eval"))
    ap.add_argument("--device", type=int, default=None, help="錄音裝置索引")
    ap.add_argument("--seconds", type=float, default=4.0, help="每句錄幾秒")
    ap.add_argument("--gap", type=float, default=2.5, help="句子之間隔幾秒")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--start", type=int, default=1, help="從第幾句開始（續錄用）")
    args = ap.parse_args(argv)
    setup_console()

    phrases = load_phrases(Path(args.phrases))
    if args.list:
        for i, (t, note) in enumerate(phrases, 1):
            print(f"  [{i:2d}] {t}" + (f"    （{note}）" if note else ""))
        print(f"\n共 {len(phrases)} 句")
        return 0

    devs = [(i, n.strip()) for i, n, *_ in list_devices()]
    dev = args.device
    if dev is None:
        cands = [i for i, n in devs if "Hands" in n or "Headset" in n]
        if not cands:
            print("❌ 找不到藍牙麥克風；請用 --device 指定")
            return 1
        dev = cands[0]
    print(f"錄音裝置：[{dev}] {next((n for i, n in devs if i == dev), '?')}")
    print(f"輸出目錄：{args.out}")
    print(f"\n每句：{args.seconds:g} 秒；句間隔：{args.gap:g} 秒")
    print("流程：看到句子 → 聽到**短嗶** → 開始念 → 再一聲嗶表示這句結束\n")
    input("準備好就按 Enter 開始（念的時候用平時講話的粵語）…")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []

    for i, (text, _note) in enumerate(phrases, 1):
        if i < args.start:
            continue
        print(f"\n{'─' * 70}")
        print(f"  [{i}/{len(phrases)}] 請念：")
        print(f"      {text}")
        print(f"{'─' * 70}")
        for n in (3, 2, 1):
            print(f"  {n}…", end="", flush=True)
            time.sleep(1.0)
        beep(1200, 100)                       # 開始嗶
        try:
            cap = Capture(dev, rate=16000, max_seconds=args.seconds + 2)
            with cap:
                t_end = time.time() + args.seconds
                while time.time() < t_end:
                    time.sleep(0.05)
                pcm = cap.recorded()
        except Exception as exc:
            print(f"\n  ❌ 錄音失敗：{exc}")
            continue
        beep(700, 140)                        # 結束嗶

        path = out_dir / f"yue{i:02d}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        # 順手記下這句的位準，太低就提醒重錄
        import struct
        n = len(pcm) // 2
        s = struct.unpack(f"<{n}h", pcm[:n * 2]) if n else []
        peak = max(abs(v) for v in s) if s else 0
        rms = (sum(v * v for v in s) / n) ** 0.5 if n else 0.0
        flag = "✅" if peak > 1500 else "⚠️ 太小聲（建議重錄）"
        print(f"  已存 {path.name}（{len(pcm) / 32000:.2f}s，峰值 {peak}）{flag}")
        made.append(path)
        if i < len(phrases):
            time.sleep(args.gap)

    print(f"\n{'=' * 70}")
    print(f"完成：{len(made)} 句 → {out_dir}")
    print("\n下一步：python tools/p1/eval_yue_models.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
