#!/usr/bin/env python3
"""量測「藍牙錄音期間，播放到底還送不送得出去」。

## ⚠️ 先講清楚：這支工具裡的角色

本工具**自己扮成一個播放程式**（用 `waveOut*`），因為要回答的問題是：
「錄音期間，**別的**播放程式（播放器、會議軟體）會遇到什麼？」
產品程式本身**完全不用 `waveOut*`** —— `app/` 底下只有 `waveIn*`。
所以這裡的 `waveOutOpen` 是**探針**，不是產品路徑。

## 為什麼不能用端點狀態代替

`measure_bt_profile_switch.py` 與 `diagnose_bt_output.py` 讀的是 registry 的
`DeviceState`。但**端點變 UNPLUGGED ≠ 播放一定失敗** —— 已開啟的 waveOut
串流在裝置消失後可能仍然照常接受緩衝區、照常回報播完，聲音卻不知道去了哪裡。

這支工具因此不看端點狀態，改成**每 0.1 秒真的開一次 waveOut 播 0.4 秒**，
記錄每次的成功／失敗與耗時，再跟「錄音中／已放開」的時間軸對齊。

## 實測得到的結論（見 docs/hardware.md §2.3）

  · 錄音期間：`waveOutOpen` **照樣回報成功**，緩衝區也照常以正常速率被消耗
    → **Windows 不會告訴應用程式「聲音送不出去」**，播放器只會安靜地播進黑洞。
  · 放開瞬間：`waveOutOpen` 直接失敗（實測 `MMSYSERR_NODRIVER`），
    約 0.6 秒後才恢復 → 這就是使用者感受到的「鬆手時被影響」。

用法:
    python tools/p1/measure_bt_playback.py            # 預設測 0 號裝置對 0 號
    python tools/p1/measure_bt_playback.py --play 0 --record 2   # 藍牙播放 + USB 錄音
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
import wave
from ctypes import wintypes
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app" / "core"))
sys.path.insert(0, str(_ROOT / "third_party"))

from record_wav import setup_console  # noqa: E402
from recorder import Capture  # noqa: E402

winmm = ctypes.WinDLL("winmm", use_last_error=True)

WHDR_DONE = 0x00000001
MMSYSERR = {0: "OK", 1: "ERROR", 2: "BADDEVICEID", 3: "NOTENABLED", 4: "ALLOCATED",
            5: "INVALHANDLE", 6: "NODRIVER", 7: "NOMEM", 8: "NOTSUPPORTED"}


class WAVEOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
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


def make_test_tone(path: Path, seconds: float = 6.0, rate: int = 44100) -> None:
    """合成測試音（不是任何人的錄音 —— 不涉隱私，可安心產生）。

    每 2 秒換一次頻率，方便用耳朵分辨「哪一段被切掉」。
    """
    import math
    import struct
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for i in range(int(rate * seconds)):
        t = i / rate
        f = 440.0 * (1 + (int(t / 2.0) % 4))
        env = 0.5 * (1 - math.cos(2 * math.pi * min(1.0, (t % 2.0) / 2.0)))
        v = int(18000 * env * math.sin(2 * math.pi * f * t))
        frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def play_once(path: Path, dev: int, seconds: float = 0.4) -> tuple[int, float, bool]:
    """播放一小段，回傳 (錯誤碼, 實際耗時, 是否真的播完)。"""
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        n = int(rate * seconds) * ch * width
        data = w.readframes(w.getnframes())[:n]
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量測藍牙錄音期間的播放行為")
    parser.add_argument("--play", type=int, default=0, help="waveOut 裝置索引")
    parser.add_argument("--record", type=int, default=0, help="waveIn 裝置索引")
    parser.add_argument("--record-at", type=float, default=3.0, help="第幾秒開始錄音")
    parser.add_argument("--hold", type=float, default=12.0, help="錄幾秒")
    parser.add_argument("--total", type=float, default=20.0, help="總共觀察幾秒")
    parser.add_argument("--interval", type=float, default=0.1, help="兩次播放嘗試的間隔")
    parser.add_argument("--list", action="store_true", help="列出 waveOut 裝置")
    args = parser.parse_args(argv)
    setup_console()

    if args.list:
        from record_wav import list_devices
        print("waveOut 輸出裝置：")
        for i, n in list_out():
            print(f"  [{i}] {n}")
        print("\nwaveIn 輸入裝置：")
        for i, n, *_ in list_devices():
            print(f"  [{i}] {n.strip()}")
        return 0

    from record_wav import list_devices
    out_name = next((n for i, n in list_out() if i == args.play), f"device {args.play}")
    in_name = next((n for i, n, *_ in list_devices() if i == args.record),
                   f"device {args.record}")
    tone = _ROOT / "artifacts" / "bt-playback-testtone.wav"
    if not tone.exists():
        print("合成測試音…")
        make_test_tone(tone)

    print(f"播放 [{args.play}] {out_name}")
    print(f"錄音 [{args.record}] {in_name}")
    print(f"第 {args.record_at:g} 秒開始錄音 {args.hold:g} 秒；"
          f"每 {args.interval:g} 秒試播 0.4 秒\n")

    state = {"phase": "（尚未錄音）"}
    stop = threading.Event()

    def recorder() -> None:
        time.sleep(args.record_at)
        cap = Capture(args.record, rate=16000, max_seconds=args.hold + 20)
        cap.__enter__()
        state["phase"] = "🔴 錄音中"
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.hold and not stop.is_set():
            time.sleep(0.05)
        cap.__exit__(None, None, None)
        state["phase"] = "⚪ 已放開"

    threading.Thread(target=recorder, daemon=True).start()

    t0 = time.monotonic()
    rows: list[tuple[float, str, str]] = []
    while time.monotonic() - t0 < args.total:
        phase = state["phase"]
        rc, elapsed, done = play_once(tone, args.play)
        t = time.monotonic() - t0
        if rc != 0:
            verdict = f"❌ 開不起來（{MMSYSERR.get(rc, f'rc={rc}')}）"
        elif done:
            verdict = f"✅ 回報播完（{elapsed:.2f}s）"
        else:
            verdict = f"⚠️ 逾時／送不出去（{elapsed:.2f}s）"
        rows.append((t, phase, verdict))
        print(f"  t+{t:5.2f}s {phase:<12} {verdict}")
        time.sleep(args.interval)
    stop.set()

    bad = [r for r in rows if "✅" not in r[2]]
    print("\n" + "=" * 70)
    print(f"共嘗試 {len(rows)} 次，其中異常 {len(bad)} 次"
          f"（錄音中異常 {len([r for r in bad if '錄音中' in r[1]])} 次）")
    if bad:
        print("\n異常的時間點：")
        for t, phase, verdict in bad:
            print(f"  t+{t:5.2f}s {phase} {verdict}")
    print("\n判讀：")
    print("  · 錄音**期間**回報成功但端點已 UNPLUGGED → 播放器無法察覺聲音送不出去。")
    print("  · 放開**瞬間**出現開不起來（NODRIVER 之類）→ 這就是「鬆手被斷一下」的來源，")
    print("    也是播放器必須自行重試／重新綁定裝置的那一段。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
