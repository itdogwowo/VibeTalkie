#!/usr/bin/env python3
"""被動監看藍牙音訊端點，邊用 VibeTalkie 邊看它到底斷了幾次。

## 為什麼要這支

使用者的實測回報與我的量測出現矛盾：

  · 我量到 `session` 模式連錄三段 → 端點只轉變 2 次（＝一次中斷，全程穩定）
  · 使用者實際用 `session` 模式連錄三段 → **聽到三次中斷**

差異可能是：真實 app 的路徑、耳機是否同時在播放、或是「中斷」的認定不同。
再怎麼推論都沒用 —— **要在真實使用情境下量。**

## 這支工具怎麼用

它**不開麥克風、不碰音訊**，只被動讀端點狀態。所以你可以：

  1. 先開這支工具（它會開始記錄）
  2. 照平常那樣用 VibeTalkie 錄幾段（戴著耳機、照你平常的方式）
  3. 回來按 Enter 結束，它會印出時間軸與統計

輸出會自動遮蔽裝置型號與藍牙位址（AGENTS.md §7），可以直接貼出來。

用法:
    python tools/p1/watch_bt_events.py
    python tools/p1/watch_bt_events.py --seconds 180     # 時間到自動停
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app" / "core"))
sys.path.insert(0, str(_ROOT / "tools" / "p1"))
sys.path.insert(0, str(_ROOT / "third_party"))

from record_wav import setup_console  # noqa: E402
from diagnose_bt_output import REDACT, enumerate_endpoints, _state_name  # noqa: E402


def snapshot() -> dict[str, str]:
    return {f"{ep.direction[:3]} {ep.label_text()}": _state_name(ep.state)
            for ep in enumerate_endpoints() if ep.is_bt}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="被動監看藍牙音訊端點（不開麥克風）")
    ap.add_argument("--seconds", type=float, default=None, help="記錄幾秒後自動停")
    ap.add_argument("--poll", type=float, default=0.05, help="取樣間隔（秒）")
    ap.add_argument("--quiet", action="store_true", help="只印摘要，不即時滾動")
    args = ap.parse_args(argv)
    setup_console()

    print("=" * 78)
    print("藍牙音訊端點監看（被動，不會開麥克風）")
    print("=" * 78)
    # 顯示目前 app 實際生效的模式 —— 這是判讀數據的關鍵前提。
    # 實測踩到：使用者以為自己測的是 session，其實一直是 per_press，
    # 白白比對了好幾輪。所以監看器自己把模式印出來，不靠記憶。
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8756/api/status", timeout=1.5) as r:
            st = json.loads(r.read().decode())
        print(f"  VibeTalkie 正在跑：mic_stream = {st.get('mic_stream')!r}　"
              f"mic_open = {st.get('mic_open')!r}　presses = {st.get('presses')}")
    except Exception:
        print("  ⚠️ 找不到執行中的 VibeTalkie（port 8756）—— 數據會沒有對照")

    print("\n現在請照平常那樣使用 VibeTalkie 錄幾段（耳機照平常戴著）。")
    if args.seconds:
        print(f"將記錄 {args.seconds:g} 秒後自動結束。\n")
    else:
        print("記錄中… 結束時按 Enter（或按 Ctrl+C）。\n")

    t0 = time.time()
    prev = snapshot()
    events: list[tuple[float, str, str, str]] = []
    n_samples = 0

    def poll() -> None:
        nonlocal prev, n_samples
        cur = snapshot()
        n_samples += 1
        t = time.time() - t0
        for k in sorted(set(cur) | set(prev)):
            old, new = prev.get(k, "（不存在）"), cur.get(k, "（不存在）")
            if old != new:
                events.append((t, REDACT.scrub(k), old, new))
                if not args.quiet:
                    print(f"  t+{t:6.2f}s  {REDACT.scrub(k)}\n"
                          f"                {old} → {new}", flush=True)
        prev = cur

    # 第一張快照當基準線
    if not args.quiet:
        print("  起始狀態：")
        for k, v in sorted(prev.items()):
            print(f"    [{v:>16}] {REDACT.scrub(k)}")
        print()

    # ⚠️ 實測踩到：`threading.Thread(target=lambda: (input(), stop.set()))`
    # 在使用者按 Ctrl+C 時會讓那個執行緒丟出未捕捉的例外，噴出
    # `Exception in thread Thread-1 (<lambda>)` 加一段 traceback ——
    # 使用者以為工具壞了。這裡把 input() 包在會吃例外的函式裡。
    stop = threading.Event()

    def wait_for_enter() -> None:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass                      # Ctrl+C / 管線結束都算「停止」，不要噴 traceback
        finally:
            stop.set()

    if not args.seconds:
        threading.Thread(target=wait_for_enter, daemon=True).start()

    try:
        if args.seconds:
            end = time.time() + args.seconds
            while time.time() < end:
                time.sleep(args.poll)
                poll()
        else:
            while not stop.is_set():
                time.sleep(args.poll)
                poll()
    except KeyboardInterrupt:
        print("\n（使用者中斷，仍會輸出已記錄的結果）")

    print("\n" + "=" * 78)
    print("結果")
    print("=" * 78)
    print(f"  記錄 {time.time() - t0:.1f} 秒，取樣 {n_samples} 次")
    if not events:
        print("  ✅ 期間沒有任何端點轉變。")
        return 0

    print(f"\n  端點轉變共 {len(events)} 次：")
    for t, k, old, new in events:
        print(f"    t+{t:6.2f}s  {k}   {old} → {new}")

    gone = [e for e in events if e[3] == "UNPLUGGED"]
    back = [e for e in events if e[2] == "UNPLUGGED" and e[3] == "ACTIVE"]
    print(f"\n  「變成 UNPLUGGED」：{len(gone)} 次")
    print(f"  「從 UNPLUGGED 恢復 ACTIVE」：{len(back)} 次")
    print(f"\n  → 你耳朵聽到的中斷次數，最接近「變成 UNPLUGGED」的次數"
          f"（每次消失都伴隨聲音斷掉）：**{len(gone)} 次**")

    # 判讀簽章：模式不同，「消失 → 恢復」的間隔形狀就不同。
    # 這是唯一能從數據反推模式的方法 —— 使用者的主觀感受分不出來。
    if len(gone) >= 2:
        gaps = []
        for g, b in zip(gone, back):
            if b[0] > g[0]:
                gaps.append((b[0] - g[0], g[0]))
        if gaps:
            vals = [x[0] for x in gaps]
            spread = max(vals) - min(vals)
            avg = sum(vals) / len(vals)
            if not back:
                print("\n  📌 判讀：消失後**完全沒有恢復** → 這是 **session** 模式的特徵。")
            elif spread > 4:
                print(f"\n  📌 判讀：間隔 {min(vals):.1f}–{max(vals):.1f} 秒、"
                      f"**變化很大**（差 {spread:.1f} 秒）")
                print("     → 不像固定逾時（逾時會很接近）。那個長度是"
                      "「你按住多久」→ **per_press** 模式。")
            else:
                print(f"\n  📌 判讀：間隔很接近固定值（平均 {avg:.1f} 秒）"
                      f" → 像是 **idle_timeout**（逾時 {avg:.1f} 秒左右）。")
                print("     也可能只是你每次按住時間剛好相近，請搭配狀態頁確認。")

    # 依裝置分開統計，才看得出是「哪一台」在反覆被踢
    per_dev: dict[str, int] = {}
    for _, k, _, new in events:
        if new == "UNPLUGGED":
            per_dev[k] = per_dev.get(k, 0) + 1
    if per_dev:
        print("\n  各端點被踢次數：")
        for k, n in sorted(per_dev.items(), key=lambda x: -x[1]):
            print(f"    {n:>3} 次  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
