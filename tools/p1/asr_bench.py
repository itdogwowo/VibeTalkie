#!/usr/bin/env python3
"""P1 — sherpa-onnx ASR 基準測試：準確率（CER）與延遲。

「中文句準確率 ≥ 95%」在計畫裡**沒有定義**，所以本工具把三種常見定義全部算出來，
不讓後續的人各說各話：

  1. 字元錯誤率 CER        = 編輯距離 / 參考字數        → 主指標（業界標準）
  2. 字元正確率            = 1 - CER                   → 對應「≥ 95%」的說法
  3. 完全相符句比例        = 整句一字不差的句子比例     → 最嚴格

⚠️ 簡繁問題（本專案必須面對）：
   SenseVoice 的訓練語料以**簡體中文**為主，輸出很可能是簡體，
   而台灣使用者的參考答案與期望是**繁體**。
   直接把簡體輸出拿去跟繁體參考比對，CER 會爆高，但那是**編碼問題不是辨識問題**。
   所以本工具同時回報三種比對基準：原樣、統一轉繁體、統一轉簡體。

用法:
    python tools/p1/asr_bench.py --smoke                  # 用模型內附的測試音檔驗證管線
    python tools/p1/asr_bench.py --transcribe <file.wav>  # 轉單一檔案
    python tools/p1/asr_bench.py --bench                  # 跑 20 句測試集（需先錄音）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "third_party"))

DEFAULT_MODEL_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
TESTSET = ROOT / "tools" / "p1" / "testset_zh.txt"
REC_DIR = ROOT / "artifacts" / "p1"

PUNCT = set("。，、！？：；「」『』（）《》〈〉…—～·．,.;:!?\"'()[]{}<>／/\\|*#@$%^&_+=~`-"
            "　 \t\n\r\u3000")


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


# ---------------------------------------------------------------- 文字正規化

def normalize(text: str) -> str:
    """去標點、去空白、全形轉半形、英文轉小寫。

    不做的話，光是「模型加了句號但參考沒有」就會被算成全錯，
    那衡量到的是標點習慣而不是辨識能力。
    """
    out = []
    for ch in text:
        if ch in PUNCT:
            continue
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:      # 全形 ASCII
            ch = chr(code - 0xFEE0)
        elif code == 0x3000:
            continue
        out.append(ch)
    return "".join(out).lower()


_OPENCC = {}


def convert_script(text: str, config: str) -> str:
    """用 OpenCC 轉換字形。config 例：'s2t'（簡→繁）、't2s'（繁→簡）。

    沒安裝 OpenCC 就原樣回傳（呼叫端會標示）。
    """
    if config not in _OPENCC:
        try:
            from opencc import OpenCC  # type: ignore
            _OPENCC[config] = OpenCC(config)
        except Exception:
            _OPENCC[config] = None
    cc = _OPENCC[config]
    return cc.convert(text) if cc else text


def has_opencc() -> bool:
    convert_script("测试", "s2t")
    return _OPENCC.get("s2t") is not None


# ---------------------------------------------------------------- 指標

def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距離（字元層級）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # 刪
                           cur[j - 1] + 1,       # 插
                           prev[j - 1] + (ca != cb)))  # 改
        prev = cur
    return prev[-1]


def score_pair(ref: str, hyp: str) -> dict:
    """在「原樣 / 統一繁 / 統一簡」三種基準下算 CER，並判斷是不是簡繁差異造成的。"""
    ref_n, hyp_n = normalize(ref), normalize(hyp)
    results = {"as-is": edit_distance(ref_n, hyp_n)}
    for cfg in ("s2t", "t2s"):
        results[cfg] = edit_distance(normalize(convert_script(ref_n, cfg)),
                                     normalize(convert_script(hyp_n, cfg)))
    best_key = min(results, key=lambda k: results[k])
    return {
        "ref_norm": ref_n,
        "hyp_norm": hyp_n,
        "dist_as_is": results["as-is"],
        "dist_s2t": results["s2t"],
        "dist_t2s": results["t2s"],
        "best_dist": results[best_key],
        "best_basis": best_key,
        "ref_len": len(ref_n),
    }


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# ---------------------------------------------------------------- ASR

def find_model_dir(name: str) -> Path | None:
    p = ROOT / "models" / name
    if p.is_dir():
        return p
    if (ROOT / "models").is_dir():
        hits = [d for d in (ROOT / "models").iterdir() if d.is_dir() and name in d.name]
        if hits:
            return hits[0]
    return None


def build_recognizer(model_dir: Path, threads: int, language: str, itn: bool):
    import sherpa_onnx  # type: ignore

    model = model_dir / "model.int8.onnx"
    if not model.exists():
        model = model_dir / "model.onnx"
    tokens = model_dir / "tokens.txt"
    if not model.exists() or not tokens.exists():
        raise FileNotFoundError(f"{model_dir} 內找不到 model*.onnx / tokens.txt")

    t0 = time.perf_counter()
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model),
        tokens=str(tokens),
        num_threads=threads,
        use_itn=itn,
        language=language,
        debug=False,
    )
    return rec, (time.perf_counter() - t0)


def read_wav(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        ch = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"只支援 16-bit WAV，{path.name} 是 {width * 8}-bit")
    import struct
    n = len(raw) // 2
    samples = struct.unpack(f"<{n}h", raw[:n * 2])
    if ch == 2:  # 立體聲轉單聲道
        samples = [(samples[i] + samples[i + 1]) / 2 for i in range(0, n - 1, 2)]
    return [s / 32768.0 for s in samples], rate


def transcribe(rec, path: Path) -> tuple[str, float, float]:
    samples, rate = read_wav(path)
    t0 = time.perf_counter()
    stream = rec.create_stream()
    stream.accept_waveform(rate, samples)
    rec.decode_stream(stream)
    elapsed = time.perf_counter() - t0
    return stream.result.text, elapsed, len(samples) / rate


# ---------------------------------------------------------------- 子指令

def cmd_smoke(rec, model_dir: Path) -> int:
    wavs = sorted((model_dir / "test_wavs").glob("*.wav"))
    if not wavs:
        print("模型內沒有 test_wavs/，改用 --transcribe。")
        return 1
    print("用模型內附的測試音檔驗證管線是否正常：\n")
    ok = 0
    for w in wavs:
        try:
            text, elapsed, dur = transcribe(rec, w)
        except Exception as exc:
            print(f"  ❌ {w.name}: {exc}")
            continue
        ok += 1
        print(f"  {w.stem:>4}  {dur:5.2f}s  解碼 {elapsed * 1000:6.0f} ms  "
              f"RTF {elapsed / dur if dur else 0:4.2f}  → {text}")
    print(f"\n{ok}/{len(wavs)} 個音檔成功解碼。")
    print("若上面出現合理的中文/英文，代表模型與 sherpa-onnx 管線正常。")
    return 0 if ok else 1


def cmd_transcribe(rec, path: Path) -> int:
    text, elapsed, dur = transcribe(rec, path)
    print(f"{path}")
    print(f"  長度 {dur:.2f}s，解碼 {elapsed * 1000:.0f} ms，RTF {elapsed / dur if dur else 0:.3f}")
    print(f"  結果：{text}")
    return 0


def load_testset() -> list[tuple[str, str]]:
    items = []
    for line in TESTSET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        idx, _, ref = line.partition("|")
        items.append((idx.strip(), ref.strip()))
    return items


def cmd_bench(rec, rec_dir: Path, model_load_s: float, as_json: Path | None) -> int:
    items = load_testset()
    print(f"測試集：{TESTSET.name}（{len(items)} 句）")
    print(f"錄音目錄：{rec_dir}\n")

    missing = [i for i, _ in items if not (rec_dir / f"utt{i}.wav").exists()]
    if missing:
        print(f"⚠️ 缺少 {len(missing)} 個錄音：{', '.join('utt' + m for m in missing[:8])}"
              f"{' …' if len(missing) > 8 else ''}")
        print("   先跑：python tools/p1/record_testset.py\n")
    if len(missing) == len(items):
        return 1

    rows = []
    print(f"{'#':<5}{'長度':>6}{'延遲':>8}{'RTF':>6}  {'CER':>7} {'最適基準':<8} 參考 → 辨識")
    print("-" * 100)
    tot_dist = tot_best = tot_ref = 0
    tot_time = tot_dur = 0.0
    exact = 0
    script_mismatch = 0

    for idx, ref in items:
        wav = rec_dir / f"utt{idx}.wav"
        if not wav.exists():
            continue
        hyp, elapsed, dur = transcribe(rec, wav)
        s = score_pair(ref, hyp)
        rows.append({"id": idx, "ref": ref, "hyp": hyp, "dur_s": round(dur, 3),
                     "latency_ms": round(elapsed * 1000, 1),
                     "rtf": round(elapsed / dur, 4) if dur else None,
                     "dist_as_is": s["dist_as_is"], "dist_s2t": s["dist_s2t"],
                     "dist_t2s": s["dist_t2s"], "ref_len": s["ref_len"],
                     "best_basis": s["best_basis"]})
        tot_dist += s["dist_as_is"]
        tot_best += s["best_dist"]
        tot_ref += s["ref_len"]
        tot_time += elapsed
        tot_dur += dur
        if s["dist_as_is"] == 0:
            exact += 1
        if s["dist_t2s"] < s["dist_as_is"]:
            script_mismatch += 1

        cer = s["dist_as_is"] / s["ref_len"] if s["ref_len"] else 0
        ref_s = ref if len(ref) <= 22 else ref[:21] + "…"
        hyp_s = hyp if len(hyp) <= 30 else hyp[:29] + "…"
        print(f"{idx:<5}{dur:>5.1f}s{elapsed * 1000:>7.0f}ms"
              f"{elapsed / dur if dur else 0:>6.2f}  {fmt_pct(cer):>7} "
              f"{s['best_basis']:<8} {ref_s} → {hyp_s}")

    n = len(rows)
    print("-" * 100)
    print(f"\n句子數：{n}")
    print(f"整體 CER（原樣比對）      ：{fmt_pct(tot_dist / tot_ref)}"
          f"   → 字元正確率 {fmt_pct(1 - tot_dist / tot_ref)}")
    print(f"整體 CER（最適字形基準）  ：{fmt_pct(tot_best / tot_ref)}"
          f"   → 字元正確率 {fmt_pct(1 - tot_best / tot_ref)}")
    print(f"完全相符句比例            ：{fmt_pct(exact / n)}  （{exact}/{n}）")
    print(f"平均句準確率 (1-CER)      ：{fmt_pct(1 - tot_dist / tot_ref)}")
    print(f"\n延遲：平均 {tot_time / n * 1000:.0f} ms／句，"
          f"RTF {tot_time / tot_dur:.3f}（<1 表示比即時快）")
    print(f"音訊總長 {tot_dur:.1f}s，解碼總計 {tot_time:.1f}s")
    print(f"模型載入時間：{model_load_s * 1000:.0f} ms")

    if not has_opencc():
        print("\n⚠️ 未安裝 OpenCC，無法區分「簡繁差異」與「真正的辨識錯誤」。")
    elif script_mismatch:
        print(f"\n⚠️ 有 {script_mismatch}/{n} 句在「統一轉簡體」後 CER 下降 "
              f"→ 這些是**簡繁字形差異**，不是辨識錯誤。")
        print("   產品若要在台灣使用，必須在 ASR 之後加上簡→繁轉換步驟。")

    print("\n判準（plan.md §12）：中文準確率 ≥ 95%、5 秒語音端到端 ≤ 1.5 秒。")
    print("注意：上述延遲只含**解碼**，不含錄音、VAD、文字注入。")

    if as_json:
        as_json.parent.mkdir(parents=True, exist_ok=True)
        as_json.write_text(json.dumps({
            "model_dir": str(rec.rec if False else ""),
            "sentences": n,
            "cer_as_is": tot_dist / tot_ref,
            "cer_best_script": tot_best / tot_ref,
            "exact_match_rate": exact / n,
            "mean_latency_ms": tot_time / n * 1000,
            "rtf": tot_time / tot_dur,
            "model_load_ms": model_load_s * 1000,
            "rows": rows,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已寫出 JSON：{as_json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1 sherpa-onnx ASR 基準測試")
    parser.add_argument("--model", default=DEFAULT_MODEL_DIR, help="models/ 下的模型目錄名")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--language", default="zh", help="zh / en / ja / ko / yue / auto")
    parser.add_argument("--no-itn", action="store_true", help="關閉逆文字正規化（數字轉換等）")
    parser.add_argument("--smoke", action="store_true", help="用模型內附音檔驗證管線")
    parser.add_argument("--transcribe", metavar="WAV", help="轉譯單一 WAV")
    parser.add_argument("--bench", action="store_true", help="跑 20 句測試集")
    parser.add_argument("--rec-dir", default=str(REC_DIR), help="測試集錄音目錄")
    parser.add_argument("--json", default="artifacts/p1/bench-result.json",
                        help="結果輸出的 JSON 路徑")
    args = parser.parse_args(argv)
    setup_console()

    model_dir = find_model_dir(args.model)
    if not model_dir:
        print(f"❌ 找不到模型：models/{args.model}")
        print("   先跑：python tools/p1/fetch_model.py --get " + args.model)
        return 1

    print(f"模型：{model_dir.name}")
    print(f"執行緒：{args.threads}　語言：{args.language}　ITN：{not args.no_itn}")
    rec, load_s = build_recognizer(model_dir, args.threads, args.language, not args.no_itn)
    print(f"模型載入：{load_s * 1000:.0f} ms\n")

    if args.transcribe:
        return cmd_transcribe(rec, Path(args.transcribe))
    if args.bench:
        return cmd_bench(rec, Path(args.rec_dir), load_s,
                         Path(args.json) if args.json else None)
    return cmd_smoke(rec, model_dir)


if __name__ == "__main__":
    raise SystemExit(main())
