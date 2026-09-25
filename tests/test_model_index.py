#!/usr/bin/env python3
"""模型分類器的測試（用真實的 release 資產名稱，不需連網）。

為什麼要測這個：
    分類錯了有兩種下場，兩種都很糟 ——
      · 把不能用的標成可用 → 使用者下載 200 MB 後才發現載不起來
      · 把能用的標成不可用 → 使用者找不到他要的模型
    名稱來自實際的 GitHub release 清單（499 個資產）。

執行：python tests/test_model_index.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import model_index as mi  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# (資產名稱, 應可用, 應屬家族 or None, 說明)
CASES = [
    # --- 平台特化：一律不可用 ---
    ("sherpa-onnx-rk3588-5-seconds-paraformer-zh-2025-10-07.tar.bz2",
     False, None, "RK3588 板"),
    ("sherpa-onnx-rk3562-5-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
     False, None, "RK3562 板"),
    ("sherpa-onnx-qnn-5-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17-int8-linux-x64.tar.bz2",
     False, None, "Qualcomm NPU"),
    ("sherpa-onnx-ascend-310b-sense-voice.tar.bz2", False, None, "Ascend NPU"),

    # --- 非 ASR ---
    ("librknnrt-android.tar.bz2", False, None, "執行期函式庫不是模型"),
    ("spoken-language-identification-test-wavs.tar.bz2", False, None, "測試音檔"),
    ("sherpa-onnx-vad-silero.tar.bz2", False, None, "VAD"),
    ("sherpa-onnx-tts-zh-en.tar.bz2", False, None, "TTS"),
    ("sherpa-onnx-kws-zipformer.tar.bz2", False, None, "關鍵詞偵測"),

    # --- 串流：離線引擎不吃 ---
    ("sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20.tar.bz2",
     False, None, "串流模型"),

    # --- 複合名稱：必須判成比較 specific 的那個家族 ---
    ("sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17.tar.bz2",
     False, "funasr_nano",
     "同時含 sense-voice 與 funasr-nano → 必須判成後者（曾誤判成可用）"),
    ("sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03.tar.bz2",
     False, "transducer", "transducer 不是 zipformer_ctc"),

    # --- 我們支援且驗證過的家族 ---
    ("sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
     True, "sense_voice", "已實測可用"),
    ("sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
     True, "sense_voice", "同家族"),
    ("sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2",
     True, "paraformer", "已實作"),
    ("sherpa-onnx-paraformer-zh-int8-2025-10-07.tar.bz2",
     True, "paraformer", "同家族"),

    # --- 尚未實作的家族：要看得出家族，但標為不可用 ---
    ("sherpa-onnx-whisper-tiny.tar.bz2", False, "whisper", "未實作"),
    ("sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27.tar.bz2",
     False, "moonshine", "未實作"),
    ("sherpa-onnx-zipformer-ctc-small-zh-int8-2025-07-16.tar.bz2",
     False, "zipformer_ctc", "未實作"),
    ("sherpa-onnx-x-asr-zipformer-transducer-zh-en-int8-2026-06-03.tar.bz2",
     False, "transducer", "未實作"),
    ("sherpa-onnx-telespeech-ctc-int8-zh-2024-06-04.tar.bz2",
     False, "telespeech_ctc", "未實作"),
    ("sherpa-onnx-wenetspeech-wu-u2pp-conformer-ctc-zh-int8-2026-02-03.tar.bz2",
     False, "wenet_ctc", "未實作"),
    ("sherpa-onnx-dolphin-base-ctc-multi-lang-int8-2025-04-02.tar.bz2",
     False, "dolphin_ctc", "未實作"),
    ("sherpa-onnx-medasr-ctc-en-int8-2025-12-25.tar.bz2",
     False, "medasr_ctc", "未實作"),
    ("sherpa-onnx-tdnn-yesno.tar.bz2", False, "tdnn_ctc", "未實作（且不是 ASR）"),
]


def live_stats() -> int:
    """對真實的 release 清單跑分類，看整體分佈（需連網）。"""
    from collections import Counter
    print("=" * 74)
    print("真實清單統計（連線 GitHub）")
    print("=" * 74)
    try:
        cat = mi.catalog(force=True)
    except Exception as exc:
        print(f"❌ 取不到清單：{type(exc).__name__}: {exc}")
        return 1

    print(f"\n來源 {cat['source']}　資產總數 {cat['total']}　"
          f"標為可用 {cat['supported']}")
    if cat.get("error"):
        print(f"⚠️ {cat['error']}")

    ok = [m for m in cat["models"] if m["supported"]]
    print(f"\n--- 可用模型（{len(ok)}）---")
    for m in ok[:20]:
        print(f"  {m['size_mb']:>7.1f} MB  {m['family']:<12} "
              f"{m['langs']:<12} {m['name'][:60]}")

    reasons = Counter(m["reason"].split("（")[0] for m in cat["models"]
                      if not m["supported"])
    print(f"\n--- 排除原因（{sum(reasons.values())}）---")
    for k, v in reasons.most_common():
        print(f"  {v:>4}  {k}")

    fams = Counter(m["family"] for m in cat["models"]
                   if m["family"] and not m["supported"])
    print(f"\n--- 尚未支援的家族（可作為後續實作順序參考）---")
    for k, v in fams.most_common():
        print(f"  {v:>4}  {k}")
    print()
    return 0


def main() -> int:
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if "--live" in sys.argv:
        return live_stats()

    print("=" * 74)
    print("模型分類器測試")
    print("=" * 74)

    print("\n[1] 逐筆分類")
    for name, want_ok, want_family, why in CASES:
        c = mi.classify(name, 100_000_000)
        ok_match = c["supported"] == want_ok
        fam_match = (c["family"] == want_family) if want_family else (c["family"] is None)
        good = ok_match and fam_match
        if not good:
            failures.append(f"{name}（{why}）")
        detail = f"supported={c['supported']} family={c['family']}"
        if not good:
            detail += f"　期望 supported={want_ok} family={want_family}"
        print(f"  {'✅' if good else '❌'} {why:<22} {name[:52]:<54} {detail}")

    print("\n[2] 不支援的必須有原因可顯示給使用者")
    n_no_reason = 0
    for name, want_ok, _f, _w in CASES:
        c = mi.classify(name, 1000)
        if not c["supported"] and not c["reason"]:
            n_no_reason += 1
    check("每個不支援的都有 reason", n_no_reason == 0, f"{n_no_reason} 筆沒有")

    print("\n[3] 支援的家族不可超出白名單")
    bad = [m for m in mi.SUPPORTED_FAMILIES
           if m not in {f[1] for f in mi.FAMILIES}]
    check("SUPPORTED_FAMILIES 都在 FAMILIES 裡", not bad, str(bad))

    print("\n[4] 離線退路：不能連網時仍要生得出清單")
    orig = mi.fetch_remote
    try:
        mi.fetch_remote = lambda: (_ for _ in ()).throw(OSError("no network"))
        cat = mi.catalog(force=True)
        check("仍回得出清單", len(cat["models"]) > 0, f"{len(cat['models'])} 筆")
        check("來源標示為 builtin 或 stale-cache",
              cat["source"] in ("builtin", "stale-cache"), cat["source"])
        check("有帶錯誤原因", bool(cat.get("error")), str(cat.get("error"))[:60])
    finally:
        mi.fetch_remote = orig

    print("\n" + "=" * 74)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
