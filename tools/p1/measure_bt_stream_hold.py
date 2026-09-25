#!/usr/bin/env python3
"""量測「麥克風串流一直開著」vs「每次按下才開」對藍牙播放的差別。

## 為什麼要量這個（本專案先前只有推論，沒有數據）

`docs/hardware.md` §2.2 明文寫著「**不要**用保持串流常開來換低延遲」，
理由是「會讓耳機在整個程式執行期間都被踢掉」。但那個結論是**推理**來的：
既然開麥克風會踢掉耳機，那就別一直開著。**沒有人量過實際的形狀。**

而使用者的需求正好相反 —— 他問的是「能不能跳過失敗」。
所以要回答這個問題，必須先知道兩件事：

  1. 串流常開時，耳機是**只被踢一次**（在開串流那一刻），還是持續被踢？
  2. 串流常開時，耳機會不會掉到 **HFP**（單聲道、音質差）而不是消失？

## 這支工具做什麼

分成三段觀察同一個播放端點：

    階段 A：什麼都不做           → 基準線
    階段 B：麥克風串流開著 20 秒 → 觀察是否有重複轉變、最終狀態是什麼
    階段 C：關掉串流             → 觀察是否恢復

用法:
    python tools/p1/measure_bt_stream_hold.py
    python tools/p1/measure_bt_stream_hold.py --mic 0 --hold 20
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app" / "core"))
sys.path.insert(0, str(_ROOT / "tools" / "p1"))
sys.path.insert(0, str(_ROOT / "third_party"))

from record_wav import setup_console, list_devices  # noqa: E402
from recorder import Capture  # noqa: E402
from diagnose_bt_output import (  # noqa: E402
    REDACT, _state_name, enumerate_endpoints, parent_token, device_mac,
)


def bt_endpoints() -> dict[str, str]:
    """{顯示字串: 狀態} —— 只取藍牙音訊端點，用來畫時間軸。"""
    out = {}
    for ep in enumerate_endpoints():
        if ep.is_bt:
            out[f"{ep.direction[:3]} {ep.label_text()}"] = _state_name(ep.state)
    return out


def simulate(mic: int, presses: int, gap: float, poll: float) -> int:
    """模擬「連按 N 次」在兩種策略下各會造成幾次中斷。

    · **策略 1（現行）**：每次按下開串流、放開就關 → 每次都重新協商。
    · **策略 2（常開）**：整段對話期間串流不關（放開後仍開著）→ 只協商一次。

    這是把「使用者的實際體感」直接量化：同樣按 N 次，各會被斷幾次。
    """
    print("=" * 78)
    print("模擬：連按 %d 次，兩種策略各會中斷幾次" % presses)
    print("=" * 78)

    def count_transitions(action, label: str) -> int:
        events = 0
        prev = bt_endpoints()

        def poll_once() -> None:
            nonlocal prev, events
            cur = bt_endpoints()
            for k in set(cur) | set(prev):
                if prev.get(k, "（不存在）") != cur.get(k, "（不存在）"):
                    events += 1
            prev = cur

        print(f"\n--- {label} ---")
        action(poll_once)
        print(f"    端點轉變合計：{events} 次")
        return events

    # 策略 1：每次按下才開
    def strategy_per_press(poll_once) -> None:
        for i in range(presses):
            cap = Capture(mic, rate=16000, max_seconds=30)
            cap.__enter__()
            t_end = time.time() + 2.0          # 模擬按住 2 秒
            while time.time() < t_end:
                time.sleep(poll)
                poll_once()
            cap.__exit__(None, None, None)
            print(f"    第 {i + 1} 次：按下 2s → 放開")
            t_end = time.time() + gap
            while time.time() < t_end:
                time.sleep(poll)
                poll_once()

    # 策略 2：整段保持開啟
    def strategy_keep_open(poll_once) -> None:
        cap = Capture(mic, rate=16000, max_seconds=120)
        cap.__enter__()
        for i in range(presses):
            t_end = time.time() + 2.0          # 模擬按住 2 秒
            while time.time() < t_end:
                time.sleep(poll)
                poll_once()
            print(f"    第 {i + 1} 次：按下 2s → 放開（**串流不關**）")
            t_end = time.time() + gap
            while time.time() < t_end:
                time.sleep(poll)
                poll_once()
        cap.__exit__(None, None, None)

    n1 = count_transitions(strategy_per_press, "策略 1：每次按下才開（現行做法）")
    time.sleep(2.0)
    n2 = count_transitions(strategy_keep_open, "策略 2：整段常開（放開後不關）")

    print("\n" + "=" * 78)
    print("結論")
    print("=" * 78)
    print(f"  按 {presses} 次：策略 1 = {n1} 次中斷，策略 2 = {n2} 次中斷")
    print()
    print("  ⚠️ 代價（同樣是實測，見階段 B）：策略 2 期間耳機**一直是斷的**，")
    print("     所以那段時間你聽不到任何聲音（音樂、會議都一樣）。")
    print("     策略 1 至少在你沒錄音的時候聲音是好的。")
    return 0


def measure_idle_timeout(mic: int, presses: int, gap: float, idle: float, poll: float) -> int:
    """量「按幾次 → 閒置一段時間 → 再按」在**關閉時機不同**下的端點轉變次數。

    ## 為什麼要量這個

    §2.4 只量了兩個極端：每次都關（21 次轉變）、整段不關（2 次）。
    但中間路線（放開後等閒置逾時才關）**沒有數據** —— 而它有兩種可能的結果，
    差別就在「關掉那一下，值不值得」：

      · 若關掉後**再開**只多幾次轉變 → 中間路線划算（閒置時拿回聲音）
      · 若關掉後**再開**會像第一次一樣貴 → 中間路線要挑對逾時長度

    本函式把「使用者的實際節奏」直接量化：連按幾句 → 停下來 → 再按幾句。
    分別用不同的「關閉間隔」跑，看轉變次數怎麼變。
    """
    print("=" * 78)
    print(f"中間路線：連按 {presses} 次 → 閒置 {idle:g} 秒 → 再按 {presses} 次")
    print("=" * 78)

    def run(close_after: float, label: str) -> int:
        """close_after：放開後幾秒關掉串流。0 = 立刻關；很大 = 幾乎不關。"""
        events = 0
        prev = bt_endpoints()

        def poll_once() -> None:
            nonlocal prev, events
            cur = bt_endpoints()
            for k in set(cur) | set(prev):
                if prev.get(k, "（不存在）") != cur.get(k, "（不存在）"):
                    events += 1
            prev = cur

        print(f"\n--- {label}（放開後 {close_after:g} 秒關）---")
        cap = None
        next_close: float | None = None
        t_end = time.time() + (presses * 2 + gap) * 2 + idle + 5
        t0 = time.time()
        plan: list[tuple[str, float]] = []
        for i in range(presses):
            plan.append(("press", 2.0))
            plan.append(("gap", gap))
        plan.append(("idle", idle))
        for i in range(presses):
            plan.append(("press", 2.0))
            plan.append(("gap", gap))

        for kind, dur in plan:
            if kind == "press" and cap is None:
                cap = Capture(mic, rate=16000, max_seconds=180)
                cap.__enter__()
            stop_at = time.time() + dur
            while time.time() < stop_at and time.time() < t_end:
                time.sleep(poll)
                poll_once()
                if (cap is not None and next_close is not None
                        and kind == "gap" and time.time() >= next_close):
                    cap.__exit__(None, None, None)
                    cap = None
            if kind == "press":
                next_close = time.time() + close_after
                print(f"    t+{time.time() - t0:5.2f}s 按了 {dur:g}s")

        if cap is not None:
            cap.__exit__(None, None, None)
        # 收尾：讓端點穩定下來
        settle = time.time() + 4.0
        while time.time() < settle:
            time.sleep(poll)
            poll_once()
        print(f"    端點轉變合計：{events} 次")
        return events

    a = run(0.0, "A：放開就關（現行做法）")
    time.sleep(2.0)
    b = run(7.0, "B：放開後 7 秒關（短逾時）")
    time.sleep(2.0)
    c = run(1e9, "C：幾乎不關（整段常開）")

    print("\n" + "=" * 78)
    print("判讀")
    print("=" * 78)
    print(f"  A 放開就關            ：{a} 次轉變")
    print(f"  B 放開後 7 秒關       ：{b} 次轉變")
    print(f"  C 幾乎不關（常開）    ：{c} 次轉變")
    print()
    if b < a:
        print("  → 中間路線**有效**：延後關閉能明顯減少轉變次數。")
        print("     逾時長度要配合你講話的節奏（比句間停頓長一點）。")
    else:
        print("  → 中間路線在 7 秒逾時下**沒有幫助**（句間停頓比逾時長）。")
        print("     要用就得把逾時拉長，但那就接近常開了。")
    print(f"  ⚠️ 別忘了 C 的代價：那段時間耳機一直是斷的（見 §2.4）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量測串流常開 vs 每次開對藍牙播放的影響")
    parser.add_argument("--mic", type=int, default=None, help="錄音裝置索引（預設找藍牙）")
    parser.add_argument("--hold", type=float, default=20.0, help="串流開著幾秒")
    parser.add_argument("--poll", type=float, default=0.1)
    parser.add_argument("--simulate", action="store_true",
                        help="模擬連按 N 次，比較兩種策略的中斷次數")
    parser.add_argument("--idle-timeout", action="store_true",
                        help="量「連按→閒置→再按」在不同關閉時機下的轉變次數")
    parser.add_argument("--presses", type=int, default=3, help="模擬按幾次")
    parser.add_argument("--gap", type=float, default=3.0, help="兩次之間隔幾秒")
    parser.add_argument("--idle", type=float, default=12.0, help="中途閒置幾秒")
    args = parser.parse_args(argv)
    setup_console()

    devs = [(i, n.strip()) for i, n, *_ in list_devices()]
    mic = args.mic
    if mic is None:
        cands = [i for i, n in devs if "Hands" in n or "Headset" in n]
        if not cands:
            print("❌ 找不到藍牙錄音裝置（先確認耳機／麥克風已連線）")
            return 1
        mic = cands[0]
    name = next((n for i, n in devs if i == mic), f"device {mic}")

    if args.simulate:
        print(f"錄音裝置：[{mic}] {REDACT.name(name)}\n")
        return simulate(mic, args.presses, args.gap, args.poll)
    if args.idle_timeout:
        print(f"錄音裝置：[{mic}] {REDACT.name(name)}\n")
        return measure_idle_timeout(mic, args.presses, args.gap, args.idle, args.poll)

    print("=" * 78)
    print("串流常開 vs 每次開 —— 對藍牙播放端點的影響")
    print("=" * 78)
    print(f"\n錄音裝置：[{mic}] {REDACT.name(name)}")
    print("（本工具只讀端點狀態，不會寫入任何設定）\n")

    events: list[tuple[float, str, str, str]] = []
    t0 = time.time()
    prev = bt_endpoints()

    def poll(phase: str) -> None:
        nonlocal prev
        cur = bt_endpoints()
        for key in sorted(set(cur) | set(prev)):
            old = prev.get(key, "（不存在）")
            new = cur.get(key, "（不存在）")
            if old != new:
                events.append((time.time() - t0, phase, REDACT.scrub(key), f"{old} → {new}"))
        prev = cur

    # ── 階段 A：基準線 ───────────────────────────────────────────────────────
    print("階段 A：基準線（不開麥克風，5 秒）")
    a_end = time.time() + 5.0
    base_transitions = 0
    while time.time() < a_end:
        time.sleep(args.poll)
        before = len(events)
        poll("A 基準")
        base_transitions += len(events) - before
    print(f"  端點轉變次數：{base_transitions}")

    # ── 階段 B：串流常開 ─────────────────────────────────────────────────────
    print(f"\n階段 B：麥克風串流**一直開著** {args.hold:g} 秒")
    cap = Capture(mic, rate=16000, max_seconds=args.hold + 15)
    cap.__enter__()
    t_open = time.time() - t0
    held_transitions = 0
    b_start = time.time()
    try:
        while time.time() - b_start < args.hold:
            time.sleep(args.poll)
            before = len(events)
            poll("B 串流開著")
            held_transitions += len(events) - before
            elapsed = time.time() - b_start
            print(f"\r  已開 {elapsed:4.1f}s  期間轉變 {held_transitions} 次",
                  end="", flush=True)
    finally:
        cap.__exit__(None, None, None)
    t_close = time.time() - t0
    print()
    print(f"  開串流後發生的轉變：{held_transitions} 次")

    # ── 階段 C：關掉後觀察恢復 ───────────────────────────────────────────────
    print("\n階段 C：關掉串流後 5 秒")
    c_end = time.time() + 5.0
    close_transitions = 0
    while time.time() < c_end:
        time.sleep(args.poll)
        before = len(events)
        poll("C 已關閉")
        close_transitions += len(events) - before
    print(f"  關閉後發生的轉變：{close_transitions} 次")

    # ── 報告 ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("時間軸")
    print("=" * 78)
    if not events:
        print("  整段期間沒有任何端點轉變。")
    for rel, phase, key, change in events:
        print(f"  t+{rel:6.2f}s [{phase}] {key}\n              {change}")

    print("\n" + "=" * 78)
    print("判讀")
    print("=" * 78)
    print(f"  · 基準線轉變 {base_transitions} 次（應該接近 0）")
    print(f"  · 串流開著 {args.hold:g} 秒期間轉變 {held_transitions} 次")
    print(f"  · 關閉後轉變 {close_transitions} 次")
    print(f"  · 開串流 t+{t_open:.2f}s → 關串流 t+{t_close:.2f}s")
    print()
    if held_transitions <= 2:
        print("  → 串流開著期間**沒有持續**轉變：踢掉只發生在「開串流那一刻」。")
        print("     也就是說：常開＝**只斷一次**，但那次之後播放端點不會自己回來。")
    else:
        print(f"  → 串流開著期間有 {held_transitions} 次轉變：連線一直在重新協商。")
    print("\n  對照「每次按下才開」（見 docs/hardware.md §2.3）：")
    print("     每次按下 → 斷一次，放開 → 恢復一次。按 N 次就是 2N 次中斷。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
