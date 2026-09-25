#!/usr/bin/env python3
"""speech_engine 的單元測試。

為什麼要測？因為實際踩到一個只有**麥克風路徑**才會觸發的 bug：

    檔案路徑：WAV → read_wav() → float 樣本 → OK
    錄音路徑：waveIn → bytes ──────────────→ TypeError

    TypeError: accept_waveform(): incompatible function arguments

兩條路徑餵給引擎的型別不一樣，而檔案路徑的測試完全蓋不到。
所以這裡**兩條路徑都測**。

執行：python tools/p1/test_speech_engine.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from speech_engine import (  # noqa: E402
    ApiEngine,
    EngineNotConfigured,
    SenseVoiceEngine,
    build_engine,
    engine_status,
    has_opencc,
    pcm_to_samples,
    read_wav,
    to_traditional,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / SenseVoiceEngine.DEFAULT_DIR

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def test_pcm_conversion() -> None:
    print("\n[1] PCM bytes → float 樣本")
    pcm = struct.pack("<5h", 0, 32767, -32768, 16384, -16384)
    s = pcm_to_samples(pcm)
    check("長度正確", len(s) == 5, f"len={len(s)}")
    check("0 → 0.0", abs(s[0]) < 1e-9, str(s[0]))
    check("32767 → ~1.0", abs(s[1] - 32767 / 32768) < 1e-6, f"{s[1]:.6f}")
    check("-32768 → -1.0", abs(s[2] + 1.0) < 1e-9, str(s[2]))
    check("空輸入回空 list", pcm_to_samples(b"") == [])
    check("奇數長度不炸", len(pcm_to_samples(b"\x01\x02\x03")) == 1)


def test_api_placeholder() -> None:
    print("\n[2] API 佔位引擎")
    eng = ApiEngine()
    ok, why = eng.is_available()
    check("未設定時 is_available 為 False", not ok)
    check("原因有說明是佔位實作", "佔位" in why or "尚未" in why, why)
    try:
        eng.transcribe([0.0] * 16000, 16000)
        check("transcribe 應該要報錯", False, "竟然沒有拋錯")
    except EngineNotConfigured:
        check("transcribe 拋出 EngineNotConfigured（不是 TypeError）", True)
    except Exception as exc:
        check("transcribe 拋出 EngineNotConfigured", False, f"實際 {type(exc).__name__}")


def test_factory() -> None:
    print("\n[3] build_engine 選項過濾")
    # 這個 bug：--threads 被硬傳給所有引擎 → ApiEngine TypeError
    try:
        build_engine("api", threads=2, endpoint="https://x", api_key="k")
        check("多餘的 threads 不會讓 ApiEngine 爆掉", True)
    except TypeError as exc:
        check("多餘的 threads 不會讓 ApiEngine 爆掉", False, str(exc))
    try:
        build_engine("不存在的引擎")
        check("未知引擎要報 KeyError", False)
    except KeyError:
        check("未知引擎報 KeyError", True)


def test_status() -> None:
    print("\n[4] engine_status 不會因引擎不可用而崩潰")
    try:
        rows = engine_status()
        check("回傳兩個以上的引擎", len(rows) >= 2, f"{[r[0] for r in rows]}")
        check("每列都是三元組", all(len(r) == 3 for r in rows))
    except Exception as exc:
        check("engine_status 正常", False, f"{type(exc).__name__}: {exc}")


def test_opencc() -> None:
    print("\n[5] 簡→繁轉換")
    if not has_opencc():
        check("OpenCC 已安裝", False, "沒安裝 → 台灣使用者拿不到繁體輸出")
        return
    out = to_traditional("开饭时间早上9点至下午5点。")
    check("簡→繁結果正確", out == "開飯時間早上9點至下午5點。", out)
    check("繁體輸入不變", to_traditional("今天天氣很好") == "今天天氣很好")


def test_engine_both_paths() -> None:
    """關鍵測試：檔案路徑與 bytes 路徑都要能餵進引擎。"""
    print("\n[6] 引擎實際推論（檔案路徑 + PCM bytes 路徑）")
    eng = SenseVoiceEngine()
    ok, why = eng.is_available()
    if not ok:
        check("模型可用", False, why)
        return
    ref = MODEL_DIR / "test_wavs" / "zh.wav"
    if not ref.exists():
        check("測試音檔存在", False, str(ref))
        return

    from_file = eng.transcribe_file(ref)
    check("檔案路徑可推論", bool(from_file.text), f"「{from_file.text}」")

    samples, rate = read_wav(ref)
    from_mem = eng.transcribe(samples, rate)
    check("float 樣本路徑可推論", bool(from_mem.text), f"「{from_mem.text}」")

    with ref.open("rb") as fh:
        fh.seek(44)                      # 跳過標準 WAV 標頭
        raw = fh.read()
    from_bytes = eng.transcribe(pcm_to_samples(raw), rate)
    check("PCM bytes → 樣本 路徑可推論（就是壞掉的那條）",
          bool(from_bytes.text), f"「{from_bytes.text}」")

    check("兩條路徑結果一致", from_mem.text == from_bytes.text)
    check("耗時有量到", from_file.latency_ms > 0, f"{from_file.latency_ms:.0f} ms")


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

    print("=" * 70)
    print("speech_engine 單元測試")
    print("=" * 70)

    test_pcm_conversion()
    test_api_placeholder()
    test_factory()
    test_status()
    test_opencc()
    test_engine_both_paths()

    print("\n" + "=" * 70)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
