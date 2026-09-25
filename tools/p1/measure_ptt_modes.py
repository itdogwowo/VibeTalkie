#!/usr/bin/env python3
"""用**真實的 `ptt.py`** 驅動三種模式，把端點時間軸與操作事件對齊。

## 為什麼要這支（模擬腳本不夠）

`measure_bt_stream_hold.py` 是用 `Capture` 直接寫的模擬，答得出「中斷幾次」，
但答不出使用者的實際體驗問題：

    「錄音停止後**一段時間**它會斷一斷」   ← 使用者回報

這句話指向的是「**關閉串流那一刻**」造成的第二次中斷。要知道使用者什麼時候
聽到那一聲，就必須把 **ASR 的處理時間**也算進去 —— 而只有真的跑 `PttDaemon`
才會有這一段。所以這支工具直接建構 `PttDaemon`（注入假引擎 + 假設定），
自己控制按下／放開，同時即時監看端點。

## 量到什麼

  · 放開 → 串流關閉（idle_timeout 模式）
  · 放開 → ASR 完成（session 模式可以用這個時間點來關，見 §2.4.3）
  · 放開 → 耳機恢復 ACTIVE

用法:
    python tools/p1/measure_ptt_modes.py --mode idle_timeout --timeout 7
    python tools/p1/measure_ptt_modes.py --mode session
    python tools/p1/measure_ptt_modes.py --compare      # 三種模式都跑一輪
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT / "app" / "core"))
sys.path.insert(0, str(_ROOT / "tools" / "p1"))
sys.path.insert(0, str(_ROOT / "third_party"))

from record_wav import setup_console, list_devices  # noqa: E402
from diagnose_bt_output import REDACT, enumerate_endpoints, _state_name  # noqa: E402
import ptt as ptt_mod  # noqa: E402


class Settings:
    """假的設定物件（`PttDaemon._live()` 只讀這幾個屬性）。"""

    def __init__(self, mic_stream: str, idle_timeout_s: float):
        self.mic_stream = mic_stream
        self.idle_timeout_s = idle_timeout_s
        self.traditional = True
        self.remove_trailing_period = True
        self.mode = "auto"


class FakeEngine:
    """假 ASR：故意花一點時間，才能量出「放開 → 辨識完成」有多久。"""

    name = "fake"
    asr_ms = 400

    def transcribe(self, samples, rate):
        time.sleep(self.asr_ms / 1000.0)

        class R:
            text = ""
        return R()


def bt_states() -> dict[str, str]:
    return {f"{ep.direction[:3]} {ep.label_text()}": _state_name(ep.state)
            for ep in enumerate_endpoints() if ep.is_bt}


class Timeline:
    """把端點轉變與操作事件寫在同一條時間軸上。"""

    def __init__(self):
        self.t0 = time.time()
        self.rows: list[tuple[float, str]] = []
        self._prev = bt_states()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def mark(self, text: str) -> float:
        rel = time.time() - self.t0
        self.rows.append((rel, f"◆ {text}"))
        return rel

    def _loop(self) -> None:
        while not self._stop.is_set():
            cur = bt_states()
            for k in sorted(set(cur) | set(self._prev)):
                old, new = self._prev.get(k, "（不存在）"), cur.get(k, "（不存在）")
                if old != new:
                    self.rows.append((time.time() - self.t0,
                                      f"  {REDACT.scrub(k)}  {old} → {new}"))
            self._prev = cur
            time.sleep(0.05)

    def stop(self) -> list[tuple[float, str]]:
        self._stop.set()
        time.sleep(0.15)
        return sorted(self.rows, key=lambda r: r[0])


def run_one(mic: int, mic_stream: str, timeout_s: float,
            hold: float, wait_after: float) -> list[tuple[float, str]]:
    cfg = Settings(mic_stream, timeout_s)
    daemon = ptt_mod.PttDaemon(
        FakeEngine(), device_index=mic, dry_run=True,
        cfg_provider=lambda: cfg, device_provider=lambda: mic,
        on_state=lambda s: None,
    )
    # 不跑真正的 run()：那會需要真的 Right Ctrl 與引擎。這裡只驅動錄音生命週期。
    tl = Timeline()
    tl.mark(f"開始（mode={mic_stream}, timeout={timeout_s:g}s）")

    tl.mark("按下")
    daemon._start_recording("test")
    time.sleep(hold)

    tl.mark("放開")
    t_release = time.time() - tl.t0

    # 驅動 _tick_idle：模擬 run() 的迴圈（每 20ms 檢查一次）。
    # ⚠️ 一定要在「放開」之後就開始輪詢，因為 _idle_at 是 _finish_recording()
    # 設定的 —— 第一次寫這支工具時漏了這一步，結果逾時永遠不會觸發。
    stop = threading.Event()
    closed_at: list[float] = []

    def idle_loop() -> None:
        while not stop.is_set():
            had = daemon._cap is not None
            daemon._tick_idle(timeout_s)
            if had and daemon._cap is None:
                closed_at.append(time.time() - tl.t0)
                tl.mark("串流關閉（idle 逾時觸發）")
            time.sleep(0.02)

    threading.Thread(target=idle_loop, daemon=True).start()

    tl.mark("開始辨識")
    daemon._finish_recording()          # 這裡會設定 _idle_at、並跑（假）ASR
    daemon._set_state("IDLE")           # 產品路徑由 run() 的插入流程負責收尾
    tl.mark("辨識完成 → IDLE")

    time.sleep(wait_after)
    stop.set()
    rows = tl.stop()
    daemon._close_capture()

    print("=" * 78)
    print(f"模式：{mic_stream}（逾時 {timeout_s:g}s）")
    print("=" * 78)
    for rel, text in rows:
        print(f"  t+{rel:6.2f}s  {text}")

    if closed_at:
        print(f"\n  放開 → 串流關閉 ：{(closed_at[0] - t_release) * 1000:.0f} ms"
              f"  ← 這就是你聽到的「停一下之後又斷一次」")
        print(f"  關閉後耳機才恢復（見上面時間軸，約 200–400ms）")
    elif mic_stream == "per_press":
        print("\n  放開 → 立刻關閉串流（per_press 模式，不等逾時）")
    else:
        print(f"\n  觀察期 {wait_after:g}s 內沒有關閉串流（逾時 {timeout_s:g}s 還沒到）")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用真實 ptt.py 驅動三種模式並量時間軸")
    ap.add_argument("--mic", type=int, default=None, help="錄音裝置索引（預設找藍牙）")
    ap.add_argument("--mode", default="idle_timeout",
                    choices=("per_press", "idle_timeout", "session"))
    ap.add_argument("--timeout", type=float, default=7.0, help="idle_timeout 秒數")
    ap.add_argument("--hold", type=float, default=2.0, help="按住幾秒")
    ap.add_argument("--wait-after", type=float, default=6.0, help="放開後再觀察幾秒")
    ap.add_argument("--compare", action="store_true", help="三種模式各跑一輪")
    args = ap.parse_args(argv)
    setup_console()

    devs = [(i, n.strip()) for i, n, *_ in list_devices()]
    mic = args.mic
    if mic is None:
        cands = [i for i, n in devs if "Hands" in n or "Headset" in n]
        if not cands:
            print("❌ 找不到藍牙錄音裝置")
            return 1
        mic = cands[0]
    print(f"錄音裝置：[{mic}] {REDACT.name(next((n for i, n in devs if i == mic), '?'))}\n")

    if args.compare:
        for mode, tmo in (("per_press", 7.0), ("idle_timeout", args.timeout), ("session", 7.0)):
            run_one(mic, mode, tmo, args.hold, args.wait_after)
            print()
            time.sleep(2.0)
        return 0

    run_one(mic, args.mode, args.timeout, args.hold, args.wait_after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
