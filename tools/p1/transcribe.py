#!/usr/bin/env python3
"""P1 最小可用鏈路：**按住說話 → 文字**。

這是「能用模型」的最小驗證：
    錄音（即時音量表 + 自動停止）→ ASR → （可選）簡→繁轉換 → 印出文字

刻意**不含**文字注入與全域熱鍵 —— 那兩塊是 P2/P5 的事。
這樣可以先確認「模型到底能不能用」，而不被其他變數干擾。

用法:
    python tools/p1/transcribe.py --status              # 看各引擎可用狀態
    python tools/p1/transcribe.py                       # 錄一句 → 出字
    python tools/p1/transcribe.py --traditional         # 簡→繁後輸出
    python tools/p1/transcribe.py --file x.wav          # 吃現成檔案
    python tools/p1/transcribe.py --engine api          # 看 API 佔位引擎的錯誤訊息
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "p0"))

from speech_engine import (  # noqa: E402
    EngineNotConfigured,
    EngineUnavailable,
    build_engine,
    engine_status,
    has_opencc,
    to_traditional,
)

ROOT = Path(__file__).resolve().parents[2]


def setup_console() -> None:
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def cmd_status() -> int:
    print("引擎狀態：\n")
    for name, ok, why in engine_status():
        mark = "✅ 可用" if ok else "⬜ 不可用"
        print(f"  {mark}  {name:<14} {why}")
    print()
    print(f"OpenCC（簡→繁）：{'✅ 已安裝' if has_opencc() else '❌ 未安裝'}")
    if not has_opencc():
        print("  台灣使用者需要繁體輸出 → python tools/p1/fetch_wheels.py "
              "opencc-python-reimplemented")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1 最小可用鏈路：說話 → 文字")
    parser.add_argument("--engine", default="sherpa-onnx", help="sherpa-onnx / api")
    parser.add_argument("--file", default=None, help="改吃現成 WAV，不錄音")
    parser.add_argument("--device", type=int, default=1, help="音訊裝置索引")
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--traditional", action="store_true", help="輸出繁體（OpenCC s2t）")
    parser.add_argument("--language", default=None, help="zh / en / ja / ko / yue / auto")
    parser.add_argument("--status", action="store_true", help="列出引擎狀態後結束")
    parser.add_argument("--loop", action="store_true", help="連續錄，Ctrl+C 結束")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)
    setup_console()

    if args.status:
        return cmd_status()

    try:
        engine = build_engine(args.engine, threads=args.threads)
    except KeyError as exc:
        print(f"❌ {exc}")
        return 1

    ok, why = engine.is_available()
    if not ok:
        print(f"❌ 引擎 {args.engine} 不可用：{why}")
        return 1

    if engine.is_local:
        print(f"引擎：{engine.name}（本地）")
    else:
        print(f"引擎：{engine.name}（雲端，音訊會上傳）")
    if hasattr(engine, "warmup"):
        engine.warmup()

    if args.file:
        return run_once(engine, args, Path(args.file))

    print(f"錄音裝置 index={args.device}　（Ctrl+C 結束）")
    print("按 Enter 開始錄一句；說完停一下會自動停止。\n")
    while True:
        try:
            input("按 Enter 開始錄音…")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            code = record_and_transcribe(engine, args)
        except KeyboardInterrupt:
            print("\n（中斷）")
            break
        if code and not args.loop:
            return code
    return 0


def record_and_transcribe(engine, args) -> int:
    from recorder import capture_utterance

    try:
        res = capture_utterance(args.device, rate=args.rate)
    except Exception as exc:
        print(f"  ❌ 錄音失敗：{exc}")
        return 1

    if res["timed_out"] or res["duration_s"] < 0.2:
        print("  ⚠️ 沒偵測到說話。")
        return 0

    print(f"  錄到 {res['duration_s']:.2f}s，峰值 {res['peak']}/32767，辨識中…")
    return _emit(engine, res["pcm"], args.rate, args)


def run_once(engine, args, path: Path) -> int:
    samples, rate = _read(path)
    return _emit(engine, samples, rate, args)


def _read(path: Path):
    from speech_engine import read_wav
    return read_wav(path)


def _emit(engine, samples, rate: int, args) -> int:
    try:
        result = engine.transcribe(samples, rate, args.language)
    except (EngineNotConfigured, EngineUnavailable) as exc:
        print(f"  ❌ {exc}")
        return 1

    text = result.text
    out = to_traditional(text) if args.traditional else text

    print(f"\n  原文：{text or '（空）'}")
    if args.traditional and out != text:
        print(f"  繁體：{out}")
    if not text:
        print("  ⚠️ 空結果。實測：音量太低（約 -48 dBFS）時模型會完全聽不到。")

    rtf = f"{result.rtf:.3f}" if result.rtf else "n/a"
    print(f"  引擎 {result.engine}　{result.duration_s:.2f}s → "
          f"{result.latency_ms:.0f} ms（RTF {rtf}）")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
