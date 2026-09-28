#!/usr/bin/env python3
"""診斷：連續錄音時，SCO 連結是否每次都在重建（導致前幾次 0 bytes）？

## 背景（使用者實測的症狀）

```
🔴 錄音中…（BT-HID/Col01）
⏹  停止（0.8s，音訊 0.00s）   ← 0 bytes
⏹  停止（1.6s，音訊 0.00s）   ← 0 bytes
⏹  停止（2.3s，音訊 0.00s）   ← 0 bytes
⏹  停止（0.7s，音訊 0.66s）   ← 終於有音訊
⏹  停止（2.3s，音訊 2.31s）   ← 正常
```

前三次全 0 bytes、第四次才通 —— 這正是 `ptt.py` 註解寫的
「SCO 音訊連線要等串流真的開始才建立」。

**要確定的是：串流有沒有在兩次錄音之間保持開啟？**
  · 保持 → SCO 只需建立一次，之後就該一直正常（前面 0 bytes 不合理）
  · 沒保持 → 每次按下都重建 SCO，每次都要碰運氣（符合症狀）

## 這支工具做什麼

用真實 `PttDaemon` + 真實裝置，模擬「連續按 N 次」，逐次記錄：
  · 這次是**沿用**既有串流還是**新開**一個（＝有沒有重建 SCO）
  · 這次實際收到幾個 byte
  · 累計建立了幾個串流

用法:
    python tools/p1/diagnose_zero_audio.py
    python tools/p1/diagnose_zero_audio.py --presses 5 --hold 1.5 --gap 3
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

import config as config_module  # noqa: E402
import ptt as ptt_mod  # noqa: E402
import recorder as recorder_mod  # noqa: E402
from record_wav import setup_console, list_devices  # noqa: E402

setup_console()


class FakeEngine:
    def __init__(self, model_dir):
        self.model_dir = Path(model_dir)

    def reload(self, p):
        pass

    def transcribe(self, samples, rate):
        class R:
            text = "x"
        return R()


class CountingCapture(recorder_mod.Capture):
    """真的 Capture，但記錄「自己被建立了」——用來判斷串流有沒有被沿用。"""

    created: list["CountingCapture"] = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        CountingCapture.created.append(self)
        self.bytes_at_open = 0

    @property
    def total_bytes(self) -> int:
        return len(self.recorded())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="診斷藍牙錄音拿到 0 bytes")
    ap.add_argument("--presses", type=int, default=5)
    ap.add_argument("--hold", type=float, default=1.5, help="每次按住幾秒")
    ap.add_argument("--gap", type=float, default=3.0, help="兩次之間隔幾秒")
    args = ap.parse_args(argv)

    cfg = config_module.Config.load()
    print("=" * 78)
    print("藍牙錄音 0-bytes 診斷")
    print("=" * 78)
    print(f"  mic_stream   = {cfg.mic_stream!r}（idle_timeout_s={cfg.idle_timeout_s}）")
    print(f"  mic_name     = {cfg.mic_name!r}")
    print(f"  device_index = {cfg.device_index}")

    devs = [(i, n.strip()) for i, n, *_ in list_devices()]
    print("\n  目前的 waveIn 裝置：")
    for i, n in devs:
        mark = "  ← 設定指定的" if n == cfg.mic_name else ""
        print(f"    [{i}] {n}{mark}")
    target_here = any(n == cfg.mic_name for _i, n in devs)
    if not target_here:
        print("\n  ⚠️ 設定指定的麥克風**不在清單裡** → 會退回 device_index")

    ptt_mod.Capture = CountingCapture

    d = ptt_mod.PttDaemon(
        FakeEngine(cfg.model_dir),
        device_index=cfg.device_index,
        dry_run=False,
        cfg_provider=lambda: cfg,
        device_provider=lambda: cfg.device_index,
        config_path=config_module.CONFIG_PATH,
        config_loader=config_module.Config.load,
    )

    print("\n" + "=" * 78)
    print("暖機（建立 SCO 連結）")
    print("=" * 78)
    d.warmup_audio(timeout=6.0)
    print(f"  暖機後：建立了 {len(CountingCapture.created)} 個串流，"
          f"{'串流保持開啟 ✅' if d._cap is not None else '串流已關閉'}")

    # ⚠️ 一定要驅動主迴圈的 tick —— 這支工具只呼叫 _start_recording /
    #    _finish_recording，不會經過 run() 的迴圈。少了這段，`_tick_buffer()`
    #    永遠不會被執行，也就測不到「緩衝區回收」這個修正。
    #    （第一次寫這支工具時就是漏了，結果量不到修正的效果。）
    import threading

    stop_ticks = threading.Event()

    def tick_loop() -> None:
        while not stop_ticks.is_set():
            try:
                d._sync_config()
                d._tick_model()
                d._tick_idle(d._live()["idle_timeout_s"])
                d._tick_device()
                d._tick_buffer()
            except Exception as exc:
                print(f"  ⚠️ tick 發生例外：{type(exc).__name__}: {exc}")
            time.sleep(0.02)

    ticker = threading.Thread(target=tick_loop, daemon=True)
    ticker.start()
    print("  （已啟動背景 tick 迴圈，模擬真實 app）")

    print("\n" + "=" * 78)
    print(f"連續按 {args.presses} 次（按住 {args.hold:g}s、間隔 {args.gap:g}s）")
    print("=" * 78)
    print(f"  {'次':>3} | {'沿用/新開':<8} | {'本次 bytes':>10} | {'秒數':>6} | 判定")
    rows: list[tuple[str, int]] = []
    for n in range(1, args.presses + 1):
        before = len(CountingCapture.created)
        d._start_recording(f"第 {n} 次")
        cap = d._cap
        mark = len(cap.recorded()) if cap is not None else 0
        time.sleep(args.hold)
        got = (len(cap.recorded()) - mark) if cap is not None else 0
        d._finish_recording()
        n_new = len(CountingCapture.created) - before
        reuse = "新開" if n_new else "沿用"
        verdict = "⚠️ 0 bytes" if got == 0 else "✅ 有音訊"
        rows.append((reuse, got))
        print(f"  {n:>3} | {reuse:<8} | {got:>10} | {got / 32000:>6.2f} | {verdict}")
        if n < args.presses:
            time.sleep(args.gap)

    stop_ticks.set()
    time.sleep(0.1)
    d._close_capture("診斷結束")

    print("\n" + "=" * 78)
    print("結果")
    print("=" * 78)
    total = len(CountingCapture.created)
    fresh = sum(1 for r, _ in rows if r == "新開")
    zeros = sum(1 for _r, g in rows if g == 0)
    print(f"  總共建立 {total} 個串流；{fresh} 次是「按下去才新開」")
    print(f"  0 bytes 的次數：{zeros} / {len(rows)}")
    print()
    if zeros == 0:
        print("  ✅ **沒有一次拿到 0 bytes** → 緩衝區回收有效")
        if total > 1:
            print(f"     （建立了 {total} 個串流；第一次換手通常是「依設定切到目標"
                  f"裝置」，屬正常行為 —— 重點是 0 bytes 消失了）")
    else:
        print(f"  ❌ 仍有 {zeros} 次拿到 0 bytes")
        if total <= 1:
            print("     串流只有一個、卻還是 0 bytes → 不是重建問題，另有原因")
        else:
            print(f"     建立了 {total} 個串流，每次重建都要重新協商")
            if cfg.mic_stream == "session":
                print("     ⚠️ 模式是 session 卻還是重開 → 這是 bug，請回報")
            else:
                print(f"     ℹ️ 模式是 {cfg.mic_stream!r}，本來就會重開；"
                      f"改成 session 可以只協商一次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
