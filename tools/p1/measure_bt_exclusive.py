#!/usr/bin/env python3
"""決定性實驗：錄音串流開著的時候，播放端點**到底還能不能被打開**？

## 為什麼這個實驗重要

到目前為止我們知道：開麥克風串流 → 耳機的播放端點變 UNPLUGGED。
但 UNPLUGGED 有兩種可能，而這兩種的**產品意涵完全相反**：

  (甲) **Windows 強制獨占（hard constraint）**
       同一顆無線電上的 SCO（麥克風）與 A2DP（播放）在裝置層級就是互斥，
       誰來都打不開。→ 沒有軟體解法，只能換路徑。

  (乙) **Windows 的政策性驅逐（policy）**
       連結其實還能用，只是 Windows 主動把另一條踢掉。
       → 若能讓 Windows「不必踢」，就有繞過的空間。

## 怎麼分辨

在**麥克風串流開著**的情況下，對耳機的播放端點反覆呼叫 `waveOutOpen`：

  · 一直失敗（`NODRIVER` 之類）→ 甲：真的打不開
  · 打得開、而且能把緩衝區寫進去 → 乙：只是政策，那就有戲

⚠️ 注意本工具**不是**在測「聲音好不好聽」——只測「開不開得起來」。

用法:
    python tools/p1/measure_bt_exclusive.py
    python tools/p1/measure_bt_exclusive.py --mic 0 --out 0 --hold 10
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app" / "core"))
sys.path.insert(0, str(_ROOT / "tools" / "p1"))
sys.path.insert(0, str(_ROOT / "third_party"))

from record_wav import setup_console, list_devices  # noqa: E402
from recorder import Capture  # noqa: E402
from diagnose_bt_output import REDACT, enumerate_endpoints, _state_name  # noqa: E402

winmm = ctypes.WinDLL("winmm", use_last_error=True)

WHDR_DONE = 0x00000001
MMSYSERR = {0: "OK", 1: "ERROR", 2: "BADDEVICEID", 3: "NOTENABLED", 4: "ALLOCATED",
            5: "INVALHANDLE", 6: "NODRIVER", 7: "NOMEM", 8: "NOTSUPPORTED"}


class WAVEOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD),
    ]


class FMT(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class HDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_void_p),
                ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]


HWO = wintypes.HANDLE
winmm.waveOutGetNumDevs.restype = wintypes.UINT
winmm.waveOutGetDevCapsW.restype = wintypes.UINT
winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEOUTCAPS), wintypes.UINT]
winmm.waveOutOpen.argtypes = [ctypes.POINTER(HWO), wintypes.UINT, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
winmm.waveOutPrepareHeader.argtypes = [HWO, ctypes.POINTER(HDR), wintypes.UINT]
winmm.waveOutWrite.argtypes = [HWO, ctypes.POINTER(HDR), wintypes.UINT]
winmm.waveOutClose.argtypes = [HWO]


def list_out() -> list[tuple[int, str]]:
    out = []
    for i in range(winmm.waveOutGetNumDevs()):
        caps = WAVEOUTCAPS()
        if winmm.waveOutGetDevCapsW(ctypes.c_void_p(i), ctypes.byref(caps),
                                    ctypes.sizeof(WAVEOUTCAPS)) == 0:
            out.append((i, caps.szPname.strip()))
    return out


def try_render(dev: int, seconds: float = 0.3) -> tuple[int, float, bool]:
    """試著播一小段無聲資料，回傳 (錯誤碼, 實際耗時, 是否回報播完)。"""
    rate, ch, width = 44100, 2, 2
    n = int(rate * seconds) * ch * width
    data = bytes(n)                                  # 全 0 = 無聲，不吵人
    fmt = FMT(1, ch, rate, rate * ch * width, ch * width, width * 8, 0)
    hwo = HWO()
    rc = winmm.waveOutOpen(ctypes.byref(hwo), dev, ctypes.byref(fmt), None, None, 0)
    if rc != 0:
        return rc, 0.0, False
    buf = ctypes.create_string_buffer(data)
    hdr = HDR()
    hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
    hdr.dwBufferLength = len(data)
    winmm.waveOutPrepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(HDR))
    t0 = time.monotonic()
    rc2 = winmm.waveOutWrite(hwo, ctypes.byref(hdr), ctypes.sizeof(HDR))
    if rc2 != 0:
        winmm.waveOutClose(hwo)
        return rc2, 0.0, False
    deadline = t0 + seconds + 1.5
    while not (hdr.dwFlags & WHDR_DONE) and time.monotonic() < deadline:
        time.sleep(0.01)
    elapsed = time.monotonic() - t0
    done = bool(hdr.dwFlags & WHDR_DONE)
    winmm.waveOutClose(hwo)
    return 0, elapsed, done


def endpoint_state(label_needle: str) -> str:
    for ep in enumerate_endpoints():
        if label_needle in ep.label_text():
            return _state_name(ep.state)
    return "（找不到）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="測錄音期間播放端點是否還開得起來")
    ap.add_argument("--mic", type=int, default=None, help="錄音裝置索引（預設找藍牙）")
    ap.add_argument("--out", type=int, default=None, help="播放裝置索引（預設找藍牙耳機）")
    ap.add_argument("--hold", type=float, default=10.0, help="麥克風開著幾秒")
    ap.add_argument("--interval", type=float, default=0.4, help="每隔幾秒試一次")
    args = ap.parse_args(argv)
    setup_console()

    ins = [(i, n.strip()) for i, n, *_ in list_devices()]
    outs = list_out()
    mic = args.mic
    if mic is None:
        cands = [i for i, n in ins if "Hands" in n or "Headset" in n]
        if not cands:
            print("❌ 找不到藍牙錄音裝置")
            return 1
        mic = cands[0]
    outdev = args.out
    if outdev is None:
        # 播放端點優先挑不是錄音裝置的那一個（才測得出「另一台裝置」）
        cands = [i for i, n in outs if "Headset" in n or "Headphones" in n]
        cands = [i for i in cands if i != mic] or cands
        if not cands:
            print("❌ 找不到藍牙播放裝置")
            return 1
        outdev = cands[0]

    mic_name = next((n for i, n in ins if i == mic), f"device {mic}")
    out_name = next((n for i, n in outs if i == outdev), f"device {outdev}")
    print("=" * 78)
    print("決定性實驗：麥克風串流開著時，播放端點還開得起來嗎？")
    print("=" * 78)
    print(f"\n麥克風 [{mic}] {REDACT.name(mic_name)}")
    print(f"播放   [{outdev}] {REDACT.name(out_name)}\n")

    def probe(tag: str) -> tuple[bool, str]:
        rc, elapsed, done = try_render(outdev)
        if rc != 0:
            verdict = f"❌ 開不起來（{MMSYSERR.get(rc, f'rc={rc}')}）"
            ok = False
        elif done:
            verdict = f"✅ 開得起來且回報播完（{elapsed:.2f}s）"
            ok = True
        else:
            verdict = f"⚠️ 開得起來但緩衝區沒被消化（{elapsed:.2f}s）"
            ok = False
        print(f"  [{tag}] {verdict}")
        return ok, verdict

    print("── 階段 A：麥克風**關著**（對照組）")
    for _ in range(2):
        probe("麥克風關")
        time.sleep(args.interval)

    print(f"\n── 階段 B：麥克風**開著** {args.hold:g} 秒")
    cap = Capture(mic, rate=16000, max_seconds=args.hold + 10)
    cap.__enter__()
    results: list[bool] = []
    try:
        t_end = time.time() + args.hold
        while time.time() < t_end:
            ok, _ = probe("麥克風開")
            results.append(ok)
            time.sleep(args.interval)
    finally:
        cap.__exit__(None, None, None)

    print("\n── 階段 C：麥克風關掉後")
    time.sleep(1.0)
    for _ in range(3):
        probe("恢復後")
        time.sleep(args.interval)

    print("\n" + "=" * 78)
    print("判讀")
    print("=" * 78)
    n_ok = sum(results)
    print(f"  麥克風開著期間，播放端點開啟成功 {n_ok} / {len(results)} 次")
    print()
    if results and n_ok == 0:
        print("  → **(甲) Windows 強制獨占**：錄音串流開著時，播放端點根本打不開。")
        print("     這是裝置層級的互斥，不是政策問題 —— 軟體無法繞過。")
    elif results and n_ok == len(results):
        print("  → **(乙) 政策性驅逐**：播放端點**還開得起來**，")
        print("     Windows 只是把原本那條連結踢掉。這種情況下有繞過的空間。")
    else:
        print("  → **混合／不穩定**：有時開得起來、有時不行 —— 需要更多次取樣才能定論。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
