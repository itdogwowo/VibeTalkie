#!/usr/bin/env python3
"""重現使用者的回報：session 模式「錄三段卻斷三次」。

## 為什麼要這支

我先前用 `measure_ptt_modes.py` 量到 session 模式 3 次按鍵只有 **1 次**中斷，
但使用者實際用卻是**三次錄音三次中斷** —— 兩者矛盾，代表我的量測漏掉了
真實路徑的某個環節。

最可能的嫌疑：`_ensure_capture()` 會比對
`getattr(self._cap, "device_id") == self.current_device()`，
**不相等就關掉重開**。而真實 app 的 `device_provider` 是
`resolve_device(cfg)[0]`（用**名稱**重新解析索引），不是固定值。

若索引在兩次錄音之間變動，串流就會被關掉重開 → 每按一次都重新協商
→ 使用者聽到的就是「每段錄音都斷一次」。

本工具把每一段錄音的「裝置索引 / 擷取物件身分」印出來，直接看它有沒有變。

用法:
    python tools/p1/measure_session_reuse.py
    python tools/p1/measure_session_reuse.py --presses 3 --hold 2 --gap 4
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
    def __init__(self, mic_stream: str, idle_timeout_s: float = 7.0):
        self.mic_stream = mic_stream
        self.idle_timeout_s = idle_timeout_s
        self.traditional = True
        self.remove_trailing_period = True
        self.mode = "auto"


class FakeEngine:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="session 模式是否真的重用串流")
    ap.add_argument("--mic", type=int, default=None)
    ap.add_argument("--mode", default="session",
                    choices=("per_press", "idle_timeout", "session"))
    ap.add_argument("--presses", type=int, default=3)
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--gap", type=float, default=4.0, help="兩次錄音之間隔幾秒")
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
    print("=" * 78)
    print(f"session 重用檢查（mode={args.mode}，按 {args.presses} 次，間隔 {args.gap:g}s）")
    print("=" * 78)
    print(f"\n錄音裝置：[{mic}] {REDACT.name(next((n for i, n in devs if i == mic), '?'))}\n")

    cfg = Settings(args.mode)
    # 用**會動態解析**的 device_provider，模仿真實 app（resolve_device 每次重查）
    resolve_calls: list[int] = []

    def provider() -> int:
        idx = next((i for i, n, *_ in list_devices()
                    if n.strip().lower().startswith("headset (ai_voice")), mic)
        resolve_calls.append(idx)
        return idx

    daemon = ptt_mod.PttDaemon(
        FakeEngine(), device_index=mic, dry_run=True,
        cfg_provider=lambda: cfg, device_provider=provider,
    )

    events: list[tuple[float, str]] = []
    prev = bt_states()
    t0 = time.time()
    stop = threading.Event()

    def watch() -> None:
        nonlocal prev
        while not stop.is_set():
            cur = bt_states()
            for k in sorted(set(cur) | set(prev)):
                old, new = prev.get(k, "（不存在）"), cur.get(k, "（不存在）")
                if old != new:
                    events.append((time.time() - t0, f"{REDACT.scrub(k)}  {old} → {new}"))
            prev = cur
            time.sleep(0.05)

    threading.Thread(target=watch, daemon=True).start()

    def idle_loop() -> None:
        while not stop.is_set():
            daemon._tick_idle(cfg.idle_timeout_s)
            time.sleep(0.02)

    threading.Thread(target=idle_loop, daemon=True).start()

    reuse: list[str] = []
    for n in range(1, args.presses + 1):
        before = daemon._cap
        t_press = time.time() - t0
        daemon._start_recording(f"第 {n} 段")
        after = daemon._cap
        same = before is not None and before is after
        reuse.append("沿用" if same else "新開")
        print(f"  第 {n} 段：裝置索引解析={resolve_calls[-1]}　"
              f"串流={'沿用同一個' if same else '**新開一個**'}")
        if not same and before is not None:
            print(f"         ⚠️ 上一個串流被關掉了（device_id 比對失敗？"
                  f"舊={getattr(before, 'device_id', '?')} 新索引={resolve_calls[-1]}）")
        time.sleep(args.hold)
        daemon._finish_recording()
        daemon._set_state("IDLE")
        if n < args.presses:
            time.sleep(args.gap)

    time.sleep(3.0)
    stop.set()
    time.sleep(0.2)
    daemon._close_capture()
    events.sort(key=lambda e: e[0])

    print("\n" + "=" * 78)
    print("端點轉變時間軸")
    print("=" * 78)
    for rel, text in events:
        print(f"  t+{rel:6.2f}s  {text}")

    print("\n" + "=" * 78)
    print("判讀")
    print("=" * 78)
    print(f"  串流處置：{reuse}")
    print(f"  裝置索引解析：{resolve_calls}")
    if all(r == "沿用" for r in reuse) and len(set(resolve_calls)) <= 1:
        print("  → 串流有正確沿用、索引也穩定。若你實際仍聽到每段都斷，")
        print("     代表真實 app 的路徑與這裡不同，要把 app 跑起來量。")
    else:
        print("  → **重現了！** 串流沒有被沿用（或索引在變），所以每段錄音都重新協商。")
        print("     這就是「錄三段斷三次」的原因。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
