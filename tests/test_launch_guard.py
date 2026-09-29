#!/usr/bin/env python3
"""「已經有另一個實例在跑」的偵測測試。

## 為什麼要測這個（實測踩到，症狀是「設定一直被還原」）

原本 `pick_port()` 的行為是：

    for p in range(preferred, preferred + 20):
        if connect_ex(("127.0.0.1", p)) != 0:    # 「連不上」= 沒人用
            return p
    return preferred                              # ← 找不到就直接回 preferred

於是使用者再啟動一次時，第二個行程**靜默地**換一個 port 起來，他完全不知道。
後果不只是兩個視窗 —— 兩個行程**共用同一個 `config.toml`**、各自握一份記憶體，
而 `cfg.save()` 有 5 處（啟動、換麥克風、換模型、UI 儲存…）每次都是整個檔案重寫。
舊行程會把它記憶體裡的舊值蓋回去，使用者的感受是「我存了，過一陣子又變回去」。

實測證據（同一台機器，兩個行程都活著）：

    磁碟：model_dir = "sherpa-onnx-paraformer-…"、mic_stream = "session"
    舊行程記憶體：model_dir = ""（空）、mic_stream = "per_press"

## 這裡怎麼測

**真的開一個行程佔住 port，再啟動第二個看它有沒有拒絕。**
只檢查原始碼裡有沒有那行字是不夠的 —— 實際會踩到的就是「程式碼看起來對、
跑起來默默換 port」。

⚠️ 用**暫存的設定檔**，不碰使用者的 `config.toml`。
（`tests/test_config_api.py` 開頭記錄過這個坑：測試存檔存到真的設定檔，
把使用者的 `device_index` 與 `mic_name` 一起重設掉。）

執行：python tests/test_launch_guard.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def bind(port: int) -> socket.socket:
    """佔住一個 port（模擬「已經有一個實例在跑」）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def occupy_range(preferred: int, span: int) -> list[socket.socket]:
    """佔住 `preferred` 起算的整個範圍（給「池滿了」的測試用）。"""
    out = []
    for p in range(preferred, preferred + span):
        try:
            out.append(bind(p))
        except OSError:
            for s in out:
                s.close()
            raise
    return out


def close_all(socks: list[socket.socket]) -> None:
    for s in socks:
        try:
            s.close()
        except OSError:
            pass


def free_port(start: int = 8900) -> int:
    for p in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("找不到可用的測試 port")


def run_app(port: int, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """啟動真正的進入點 —— 但給它一個**假的家目錄**（見 main 的說明）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ROOT / "app" / "vibetalkie.py"),
         "--no-browser", "--port", str(port)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=env, cwd=str(ROOT))


def main() -> int:
    print("=" * 68)
    print("啟動防護：已經有實例在跑時要拒絕啟動")
    print("=" * 68)

    # ⚠️ **保護使用者的 `config.toml`。**
    #
    # 子行程是真的進入點，而 `main()` 開頭有 `cfg.save()`（無條件重寫整個
    # 檔案）。就算內容一樣，**mtime 也會變** —— 那會讓另一個正在跑的實例
    # 的 `_sync_config()` 判定「檔案被外部改過」而重讀。
    #
    # 所以這裡把「內容 ＋ mtime」一起備份，結束時原樣還原。
    real_cfg = ROOT / "config.toml"
    saved = real_cfg.read_bytes() if real_cfg.exists() else None
    saved_mtime = real_cfg.stat().st_mtime if real_cfg.exists() else None

    try:
        return _run()
    finally:
        if saved is not None:
            if real_cfg.read_bytes() != saved:
                real_cfg.write_bytes(saved)
            os.utime(real_cfg, (saved_mtime, saved_mtime))
            print("\n  （已還原 config.toml：內容與 mtime）")


def _run() -> int:
    print("\n[1] pick_port() 的行為（純邏輯）")
    import vibetalkie as vt

    p = free_port()
    check("沒人用的 port 直接回傳它", vt.pick_port(p) == p, str(p))

    holder = bind(p)
    try:
        check("port_in_use() 認得出被佔用", vt.port_in_use(p) is True, str(p))
        try:
            got = vt.pick_port(p)
            check("被佔用就丟 AlreadyRunning（**不要靜默讓位**）", False,
                  f"竟然回傳了 {got}（真實情境就是這樣漂到隔壁的）")
        except vt.AlreadyRunning as exc:
            check("被佔用就丟 AlreadyRunning（**不要靜默讓位**）", True, str(exc))
        # span > 1 是給「確定要讓位」的場合（目前沒有這種呼叫端）
        nxt = vt.pick_port(p, span=2)
        check("明確要求 span>1 時才讓位", nxt == p + 1, str(nxt))
    finally:
        holder.close()

    print("\n[2] 真的啟動第二個行程 → 要拒絕，而且訊息要可行動")
    # ⚠️ **只佔住「你想用的那一個 port」** —— 這才是真實情境。
    #
    #    第一版這裡佔滿了整個池（span=20），因為當時的實作是「往後找 20 個」。
    #    那個測試**照著實作設計**，所以通過了；但真實情境是舊實例只佔著
    #    8756 一號、8757 空著，於是 `pick_port` 讓位、第二個行程照樣起來 ——
    #    防護完全失效（實測在使用者機器上就是這樣，見 pick_port 的 docstring）。
    #
    #    **測試要照真實情境設計，不是照實作設計。**
    p_used = free_port(8950)
    holder = bind(p_used)
    try:
        t0 = time.time()
        r = run_app(p_used)
        took = time.time() - t0
        out = (r.stdout or "") + (r.stderr or "")
        check("有回非零結束碼", r.returncode != 0, f"returncode={r.returncode}")
        check("訊息說出「已經在執行」", "已經在執行" in out,
              out.strip().splitlines()[-1] if out.strip() else "（沒有輸出）")
        check("訊息說出後果（設定互相覆蓋）", "設定互相覆蓋" in out)
        check("訊息給出可行的下一步（怎麼關掉舊的／換 port）",
              "Ctrl+C" in out and "--port" in out)
        check("沒有留下 traceback", "Traceback (most recent call last)" not in out)
        check("**沒有載入模型**（防護在做事之前就先擋）",
              "辨識引擎：" not in out,
              "有看到「辨識引擎：」表示它先載入模型才發現重複")
        check("沒有偷偷換 port 起來", f":{p_used + 1}/" not in out,
              "有看到隔壁 port 的網址 = 靜默讓位又發生了")
        check("很快就結束（沒有真的跑起來）", took < 30, f"{took:.1f}s")
    finally:
        holder.close()

    print("\n[2b] 明確指定 --port 時，被佔用也要誠實報錯（不偷偷換）")
    p_exp = free_port(p_used + 5)
    holder_exp = bind(p_exp)
    try:
        r = run_app(p_exp)
        out = (r.stdout or "") + (r.stderr or "")
        check("指定的 port 被佔用 → 拒絕（不是換一個）",
              r.returncode != 0 and "已經在執行" in out,
              f"returncode={r.returncode}")
    finally:
        holder_exp.close()

    print("\n[2c] 整個池都被佔用 → 也要拒絕（span>1 的路徑）")
    p_pool = free_port(p_exp + 5)
    holders = occupy_range(p_pool, 3)
    try:
        try:
            got = vt.pick_port(p_pool, span=3)
            check("池滿了要丟 AlreadyRunning", False, f"竟然回傳了 {got}")
        except vt.AlreadyRunning as exc:
            check("池滿了要丟 AlreadyRunning", True, str(exc))
    finally:
        close_all(holders)

    print("\n[3] 沒人佔用時，同一個啟動指令要正常跑（對照組）")
    # ⚠️ 對照組是必要的：只測「會拒絕」的話，一個「永遠拒絕」的實作也會通過。
    #
    # ⚠️ port 配置也要小心：`free_port(N)` 是「從 N 往上找第一個空的」，
    #    所以 `free_port(8900)` 與 `free_port(8910)` 可能**回同一個**。
    #    一開始就是這樣寫錯，導致「被佔用的 port」與「給子行程的 port」
    #    差一號 → 子行程真的跑起來、測試等到逾時（實測踩到）。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        p3 = probe.getsockname()[1]          # 讓 OS 給一個確定的空 port
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app" / "vibetalkie.py"),
         "--no-browser", "--port", str(p3), "--seconds", "6"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    started = False
    deadline = time.time() + 30
    while time.time() < deadline:
        if vt.port_in_use(p3):
            started = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.4)
    check("沒人佔用時真的綁得上那個 port", started,
          "（若這裡失敗，表示上面的拒絕邏輯可能壞成『永遠拒絕』）")
    try:
        proc.wait(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()

    print("\n" + "=" * 68)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())