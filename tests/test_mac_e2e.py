#!/usr/bin/env python3
"""端到端測試：合成按鍵事件 → 狀態機 → 辨識 → 注入。

## 這個測試為什麼存在

前面驗證過兩件事，但**沒有把它們串起來**：

  1. 按鍵監聽：注入合成的 F9，確認 CGEventTap 收得到
  2. 錄音→辨識→注入：直接呼叫 `_finish_recording()`，**跳過了按鍵那一段**

所以「按住按鈕 → 出字」這個真實迴路其實沒被驗證過。這個測試補上：
按鍵事件**也是合成注入的**，走完整的監聽 → 狀態機 → 注入。

## 怎麼做到不需要真的說話

`MacCapture` 被換成假的實作，它回傳一個**已知內容的 WAV**
（macOS 粵語 TTS 產生的「我哋而家去食飯，你想唔想一齊去？」）。
這樣整條鏈路的每一環都是真的，只有「麥克風硬體」被替換 ——
而那一段已經在 `mac_recorder.py` 的自我測試裡單獨驗證過（3.00 秒 / 16 kHz）。

## 風險

測試期間會**真的送出 F9 按鍵**（走 CGEventPost）與 **Cmd+V**。
F9 在絕大多數程式裡沒有作用，但為了安全：
  · 請在測試前把焦點放在不會被影響的地方（或讓它貼到空白文件）
  · 剪貼簿會被寫入再還原（會驗證還原結果）

執行：PYTHONPATH=<pyobjc 目錄> python3 tests/test_mac_e2e.py
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

# 測試用的已知音檔（粵語 TTS）。找不到就跳過，不要假裝通過。
CANDIDATE_WAVS = [
    Path("/tmp/vt-eval/audio/c1.wav"),
    ROOT / "artifacts" / "e2e-c1.wav",
]
EXPECT_KEYWORD = "食飯"          # 這句的關鍵詞；用來斷言辨識真的對了

failures: list[str] = []
HOTKEY = "F9"
HOTKEY_KEYCODE = 101             # macOS kVK_F9


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def skip(name: str, why: str) -> None:
    print(f"  ⏭  {name}  — {why}")


def find_wav() -> Path | None:
    for p in CANDIDATE_WAVS:
        if p.is_file():
            return p
    return None


def load_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, \
            f"測試音檔必須是 16 kHz mono，實際 {w.getframerate()} Hz {w.getnchannels()}ch"
        return w.readframes(w.getnframes())


def main() -> int:
    print("=" * 74)
    print("  macOS 端到端測試（合成按鍵 → 錄音 → 辨識 → 注入）")
    print("=" * 74)

    wav = find_wav()
    if wav is None:
        print("\n  ⚠️ 找不到測試音檔：")
        for p in CANDIDATE_WAVS:
            print(f"      {p}")
        print("\n  請先用 macOS 的粵語 TTS 產生一個：")
        print('      say -v Sinji -o /tmp/c1.aiff "我哋而家去食飯，你想唔想一齊去？"')
        print("      afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/c1.aiff "
              "/tmp/vt-eval/audio/c1.wav")
        return 2

    pcm = load_wav_pcm(wav)
    secs = len(pcm) / 2 / 16000
    print(f"\n  測試音檔：{wav}（{secs:.2f} 秒）")
    print(f"  錄音鍵：{HOTKEY}（會真的送出合成按鍵）")

    try:
        import Quartz
    except ImportError as exc:
        print(f"\n  ❌ 載入不到 Quartz：{exc}")
        print("  請用裝了 pyobjc 的 Python，並設好 PYTHONPATH。")
        return 2

    # ---- 載入常駐程式（不能走 main()，我們要自己控制生命週期）----
    spec = importlib.util.spec_from_file_location(
        "mvd", ROOT / "app" / "mac_vibetalkie.py")
    mvd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mvd)                                  # type: ignore

    import config as config_module
    import mac_keylistener as keylistener
    import mac_recorder as recorder
    import mac_textin as textin

    cfg = config_module.Config.load()
    cfg.hotkey = HOTKEY
    cfg.trigger_mode = "hold"
    cfg.traditional = True
    cfg.remove_trailing_period = True
    if not (ROOT / "models" / cfg.model_dir).is_dir():
        # 允許用環境變數指定模型（測試環境常見）
        import os
        alt = os.environ.get("VIBETALKIE_MODEL_DIR")
        if alt and Path(alt).is_dir():
            cfg.model_dir = alt
        else:
            print(f"\n  ❌ 找不到模型：{ROOT / 'models' / cfg.model_dir}")
            print("  請先跑：python3 tools/p1/fetch_model.py --get <模型名>")
            print("  或用 VIBETALKIE_MODEL_DIR=<絕對路徑> 指定。")
            return 2

    daemon = mvd.MacPttDaemon(cfg, dry_run=False, debug=False)

    # ---- 換掉麥克風：回傳已知音檔 ----
    class FakeCapture:
        """假麥克風。介面與 MacCapture 相同，但資料是固定的。"""

        def __init__(self, *a, **kw):
            self.error = None
            self._entered = False

        def __enter__(self):
            self._entered = True
            return self

        def __exit__(self, *exc):
            self._entered = False

        def recorded(self) -> bytes:
            return pcm

        def full(self) -> bool:
            return False

        @property
        def done(self) -> bool:
            return not self._entered

        def poll_level(self) -> float:
            return 0.5

    recorder_mac = recorder.MacCapture
    recorder.MacCapture = FakeCapture                    # type: ignore[misc]
    # 常駐程式是 `import mac_recorder as recorder` 後用 recorder.MacCapture，
    # 所以換掉模組屬性就夠了。

    # ---- 權限檢查 ----
    probe = keylistener.MacKeyListener(lambda e: None)
    ok, why = probe.check_permission()
    if not ok:
        print(f"\n  ❌ 無法監聽按鍵：{why}")
        recorder.MacCapture = recorder_mac                # type: ignore[misc]
        return 2
    print("  ✅ 按鍵監聽權限正常")

    # ---- 啟動常駐程式（背景執行緒）----
    daemon.ensure_engine()
    print("  ✅ ASR 模型就緒")

    # 讓 daemon._handle_key 能安全被事件呼叫：它需要 _mods_down
    daemon._mods_down = set()                            # noqa: SLF001

    listener = keylistener.MacKeyListener(daemon._handle_key, debug=False)  # noqa: SLF001
    # 用 daemon 自己的 run() 會阻塞並自己建 listener，所以這裡手動接線：
    # 這樣才能在同一支程式裡注入按鍵並觀察結果。
    events: list = []
    original_handle = daemon._handle_key                 # noqa: SLF001

    def spy(ev):
        events.append(ev)
        original_handle(ev)

    listener.on_event = spy

    thread = threading.Thread(target=listener.run, kwargs={"seconds": 14},
                              daemon=True)
    thread.start()
    time.sleep(0.8)          # 等 tap 真的掛上

    # ---- 注入合成按鍵：按下 → 停一下 → 放開 ----
    print(f"\n  注入合成按鍵：{HOTKEY} 按下 → 等 {secs:.1f}s → 放開")
    print("  （這會走真正的 CGEventTap 路徑）\n")

    clip_before = textin.snapshot_clipboard()

    def post(down: bool) -> None:
        ev = Quartz.CGEventCreateKeyboardEvent(None, HOTKEY_KEYCODE, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    post(True)                       # 按下 → 開始錄音
    time.sleep(0.4)
    state_while_held = daemon.state          # 先記下來 —— 之後會被改回 IDLE
    held_ok = state_while_held == "RECORDING"
    # 錄音「時長」由 _rec_started 決定；FakeCapture 永遠回傳完整音檔，
    # 所以這裡只需要讓狀態機確實在 RECORDING。
    time.sleep(0.3)
    post(False)                      # 放開 → 辨識 + 注入

    # 等辨識與注入完成
    deadline = time.time() + 12
    while time.time() < deadline and daemon.state != "IDLE":
        time.sleep(0.1)
    time.sleep(0.6)                  # 給剪貼簿還原一點時間
    listener.stop()
    thread.join(timeout=3)

    recorder.MacCapture = recorder_mac                    # type: ignore[misc]

    # ---- 斷言 ----
    print("\n" + "=" * 74)
    print("  結果")
    print("=" * 74 + "\n")

    check("合成的 F9 有被 CGEventTap 收到", len(events) > 0,
          f"收到 {len(events)} 個事件")
    check("狀態機進入過 RECORDING（按住時）", held_ok,
          f"按住時的狀態：{state_while_held}")

    s = daemon.stats
    check("有成功注入一次", s["inserted"] == 1, str(s))
    check("沒有失敗", s["failed"] == 0, f"failed={s['failed']}")

    # ⚠️ 這一條是整個測試**最重要**的一條。
    #
    # 沒有它，測試會在「ASR 吐出亂碼」時照樣通過 —— 因為 inserted=1、
    # 沒有例外、剪貼簿也還原了。那種「什麼都沒壞，只是字是錯的」
    # 正是最難察覺的失敗。所以直接斷言辨識內容。
    import speech_engine as _se
    res = daemon.engine.transcribe(_se.pcm_to_samples(pcm), 16000)
    got_text = _se.to_traditional(res.text)
    check(f"辨識結果包含關鍵詞「{EXPECT_KEYWORD}」", EXPECT_KEYWORD in got_text,
          f"實際得到：{got_text!r}")

    lat = daemon.last_latency or {}
    check("有量到延遲數字", bool(lat), str(lat))
    if lat:
        check("端到端延遲 < 1500 ms（plan.md 門檻）",
              lat.get("total_ms", 9999) < 1500, f"{lat.get('total_ms')} ms")

    clip_after = textin.snapshot_clipboard()
    check("剪貼簿完整還原", clip_after == clip_before,
          f"{len(clip_before)} → {len(clip_after)} 個 item")

    events_desc = ", ".join(f"{e.key}{'↓' if e.down else '↑'}" for e in events[:8])
    print(f"\n  辨識文字：{got_text}")
    print(f"  收到的事件序列：{events_desc}")
    print(f"  統計：{s}")
    print(f"  延遲：{lat}")

    print("\n" + "=" * 74)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過 —— 完整迴路（合成按鍵 → 錄音 → ASR → 注入）可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
