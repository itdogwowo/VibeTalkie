#!/usr/bin/env python3
"""多模型粵語 ASR 對照評測（同音檔、逐句並排比較）。

## 為什麼要這支

「最新最快最準確」不能靠型號或日期猜。這支工具對**同一批音檔**
跑多個模型，把輸出並排，並量測每次辨識的耗時，讓差異看得見。

## 評分方式的取捨（重要）

本工具**不算 CER（字元錯誤率）**，因為那需要標準答案，而：
  · 官方測試集沒有附逐字稿
  · 自己寫標準答案會有「我寫的本身就不道地」的偏差

所以改成兩種互補的方式：
  1. **客觀**：速度（RTF＝辨識秒數 ÷ 音訊秒數）、模型載入時間、記憶體
  2. **主觀但可信**：並排輸出，由**粵語母語者**判斷哪個最對

## 用法

    python tools/p1/eval_yue_models.py --test-dir <模型>/test_wavs
    python tools/p1/eval_yue_models.py --wav artifacts/yue-eval/yue01.wav
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT / "app" / "core"))
sys.path.insert(0, str(_ROOT / "third_party"))

from record_wav import setup_console  # noqa: E402
from speech_engine import SenseVoiceEngine, read_wav  # noqa: E402

MODELS_DIR = _ROOT / "models"

# 候選模型（依大小排序）。名字對應 models/<name>。
CANDIDATES = [
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",   # 目前使用中
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",   # 粵語微調（int8）
    "sherpa-onnx-paraformer-trilingual-zh-cantonese-en",         # 粵語專門（三語）
]


def collect_wavs(args) -> list[Path]:
    if args.wav:
        return [Path(args.wav)]
    d = Path(args.test_dir)
    if not d.is_dir():
        return []
    return sorted(d.glob("yue*.wav"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="多模型粵語 ASR 對照評測")
    ap.add_argument("--test-dir", default=None, help="測試音檔目錄（預設用 2025 模型的 test_wavs）")
    ap.add_argument("--wav", default=None, help="只測單一檔案")
    ap.add_argument("--models", nargs="*", default=None, help="只測指定模型")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--language", default="auto", help="sense_voice 的 language 參數")
    args = ap.parse_args(argv)
    setup_console()

    if args.test_dir is None:
        args.test_dir = str(MODELS_DIR /
                            "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09" /
                            "test_wavs")
    wavs = collect_wavs(args)
    if not wavs:
        print(f"❌ 找不到測試音檔：{args.test_dir}")
        return 1

    names = args.models or CANDIDATES
    engines = []
    print("=" * 100)
    print("載入模型")
    print("=" * 100)
    for name in names:
        d = MODELS_DIR / name
        if not d.is_dir():
            print(f"  ⏭  跳過 {name}（尚未下載）")
            continue
        t0 = time.perf_counter()
        eng = SenseVoiceEngine(model_dir=d, threads=args.threads)
        ok, why = eng.is_available()
        if not ok:
            print(f"  ❌ {name}：{why}")
            continue
        load_t0 = time.perf_counter()
        try:
            eng.warmup()
        except Exception as exc:
            print(f"  ❌ {name} 載入失敗：{exc}")
            continue
        load_ms = (time.perf_counter() - load_t0) * 1000
        sz = sum(f.stat().st_size for f in d.rglob("*.onnx")) / 1e6
        print(f"  ✅ {name}")
        print(f"     家族={eng.resolved_kind()}　模型檔 {sz:.0f} MB　"
              f"載入 {load_ms:.0f} ms")
        engines.append((name, eng))

    if not engines:
        print("\n❌ 沒有可用的模型。先下載："
              "\n   python tools/p1/fetch_model.py --get <模型名>")
        return 1

    print("\n" + "=" * 100)
    print(f"逐句對照（{len(wavs)} 個音檔 × {len(engines)} 個模型）")
    print("=" * 100)

    stats: dict[str, list[float]] = {n: [] for n, _ in engines}
    for wav in wavs:
        samples, rate = read_wav(wav)
        secs = len(samples) / rate if rate else 0
        print(f"\n{'─' * 100}")
        print(f"📁 {wav.name}　（{secs:.2f} 秒 / {rate} Hz）")
        print(f"{'─' * 100}")
        for name, eng in engines:
            try:
                r = eng.transcribe(samples, rate, language=args.language)
                text = (r.text or "").strip() or "（空）"
                stats[name].append(r.rtf if r.rtf else 0.0)
                print(f"  {name.split('sherpa-onnx-')[-1][:42]:<44} "
                      f"{r.latency_ms or 0:6.0f} ms  RTF={r.rtf or 0:.3f}")
                print(f"      → {text}")
            except Exception as exc:
                print(f"  {name:<44} ❌ {type(exc).__name__}: {exc}")

    print("\n" + "=" * 100)
    print("速度總結（RTF：越小越快；1.0 = 辨識耗時等於音訊長度）")
    print("=" * 100)
    for name, _ in engines:
        vals = [v for v in stats[name] if v]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        print(f"  {name}")
        print(f"     平均 RTF = {avg:.3f}　最快 {min(vals):.3f}　最慢 {max(vals):.3f}　"
              f"取樣 {len(vals)} 句")
    print("\n⚠️ 準確率請由**你**判斷：上面每一句都並排列出各模型的輸出。")
    print("   把最正確的那個記下來 —— 這比任何自動指標可信，因為標準答案在你腦裡。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
