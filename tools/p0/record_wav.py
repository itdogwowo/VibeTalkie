#!/usr/bin/env python3
"""P0 / T2 — 從「指定」藍牙麥克風錄音，並判定它真正的原生取樣率。

為什麼要單獨量取樣率？
    目標裝置走的是藍牙 HFP（Hands-Free Profile），頻寬受限：
        窄頻 CVSD → 8 kHz   ← 對 ASR 明顯不利
        寬頻 mSBC → 16 kHz  ← 可接受
    若只拿到 8 kHz，「中文準確率 ≥ 95%」的門檻可能不成立。
    這件事必須在 P0 定案，不能拖到 P5 才發現。

用法:
    python tools/p0/record_wav.py --list                 # 列舉輸入裝置
    python tools/p0/record_wav.py --probe --device 0     # 探測各取樣率是否被接受
    python tools/p0/record_wav.py --device 0 --seconds 5 # 錄 5 秒 (預設 16k/mono/16bit)
    python tools/p0/record_wav.py --device 0 --seconds 5 --rate 48000 --out artifacts/x.wav

僅使用標準函式庫（ctypes + winmm），不需要 pip install。
只支援 Windows。輸出 WAV 位於 artifacts/（已 gitignore），請勿 commit 真實錄音。
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import sys
import time
import wave
from ctypes import wintypes
from pathlib import Path

if sys.platform != "win32":
    sys.exit("本工具只支援 Windows（使用 winmm waveIn API）。")

winmm = ctypes.WinDLL("winmm", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WAVE_MAPPER = 0xFFFFFFFF
CALLBACK_NULL = 0x00000000
WHDR_DONE = 0x00000001
WAVE_FORMAT_PCM = 0x0001

MMSYSERR = {
    0: "MMSYSERR_NOERROR",
    1: "MMSYSERR_ERROR",
    2: "MMSYSERR_BADDEVICEID",
    3: "MMSYSERR_NOTENABLED",
    4: "MMSYSERR_ALLOCATED",
    5: "MMSYSERR_INVALHANDLE",
    6: "MMSYSERR_NODRIVER",
    7: "MMSYSERR_NOMEM",
    8: "MMSYSERR_NOTSUPPORTED",
    32: "WAVERR_BADFORMAT",
    33: "WAVERR_STILLPLAYING",
    34: "WAVERR_UNPREPARED",
    35: "WAVERR_SYNC",
}

# WAVEINCAPS.dwFormats 的位元 → 人話
FORMAT_BITS = [
    (0x00000001, "11.025 kHz, mono, 8-bit"),
    (0x00000002, "11.025 kHz, mono, 16-bit"),
    (0x00000004, "11.025 kHz, stereo, 8-bit"),
    (0x00000008, "11.025 kHz, stereo, 16-bit"),
    (0x00000010, "22.05 kHz, mono, 8-bit"),
    (0x00000020, "22.05 kHz, mono, 16-bit"),
    (0x00000040, "22.05 kHz, stereo, 8-bit"),
    (0x00000080, "22.05 kHz, stereo, 16-bit"),
    (0x00000100, "44.1 kHz, mono, 8-bit"),
    (0x00000200, "44.1 kHz, mono, 16-bit"),
    (0x00000400, "44.1 kHz, stereo, 8-bit"),
    (0x00000800, "44.1 kHz, stereo, 16-bit"),
    (0x00001000, "48 kHz, mono, 8-bit"),
    (0x00002000, "48 kHz, mono, 16-bit"),
    (0x00004000, "48 kHz, stereo, 8-bit"),
    (0x00008000, "48 kHz, stereo, 16-bit"),
]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


HWAVEIN = wintypes.HANDLE

winmm.waveInGetNumDevs.restype = wintypes.UINT
winmm.waveInGetDevCapsW.restype = wintypes.UINT
winmm.waveInGetDevCapsW.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEINCAPSW), wintypes.UINT]
winmm.waveInOpen.restype = wintypes.UINT
winmm.waveInOpen.argtypes = [ctypes.POINTER(HWAVEIN), wintypes.UINT,
                             ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD]
winmm.waveInPrepareHeader.restype = wintypes.UINT
winmm.waveInPrepareHeader.argtypes = [HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveInUnprepareHeader.restype = wintypes.UINT
winmm.waveInUnprepareHeader.argtypes = [HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveInAddBuffer.restype = wintypes.UINT
winmm.waveInAddBuffer.argtypes = [HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveInStart.restype = wintypes.UINT
winmm.waveInStart.argtypes = [HWAVEIN]
winmm.waveInStop.restype = wintypes.UINT
winmm.waveInStop.argtypes = [HWAVEIN]
winmm.waveInReset.restype = wintypes.UINT
winmm.waveInReset.argtypes = [HWAVEIN]
winmm.waveInClose.restype = wintypes.UINT
winmm.waveInClose.argtypes = [HWAVEIN]


def setup_console() -> None:
    try:
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def err_name(code: int) -> str:
    return MMSYSERR.get(code, f"MMSYSERR_0x{code:X}")


def list_devices() -> list[tuple[int, str, int, int, int]]:
    n = winmm.waveInGetNumDevs()
    out = []
    for i in range(n):
        caps = WAVEINCAPSW()
        rc = winmm.waveInGetDevCapsW(ctypes.c_void_p(i), ctypes.byref(caps),
                                     ctypes.sizeof(WAVEINCAPSW))
        if rc == 0:
            out.append((i, caps.szPname, caps.dwFormats, caps.wChannels, caps.wMid << 16 | caps.wPid))
    return out


def describe_formats(mask: int) -> list[str]:
    return [label for bit, label in FORMAT_BITS if mask & bit]


def make_format(rate: int, channels: int, bits: int) -> WAVEFORMATEX:
    fmt = WAVEFORMATEX()
    fmt.wFormatTag = WAVE_FORMAT_PCM
    fmt.nChannels = channels
    fmt.nSamplesPerSec = rate
    fmt.wBitsPerSample = bits
    fmt.nBlockAlign = channels * bits // 8
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
    fmt.cbSize = 0
    return fmt


def try_open(device_id: int, fmt: WAVEFORMATEX) -> tuple[int, int | None]:
    hwi = HWAVEIN()
    rc = winmm.waveInOpen(ctypes.byref(hwi), device_id, ctypes.byref(fmt), None, None,
                          CALLBACK_NULL)
    if rc == 0:
        winmm.waveInClose(hwi)
        return 0, None
    return rc, None


def cmd_list() -> int:
    devices = list_devices()
    if not devices:
        print("找不到任何 waveIn 輸入裝置。")
        return 1
    print(f"共 {len(devices)} 個 waveIn 輸入裝置\n")
    for idx, name, mask, ch, _pid in devices:
        print(f"[{idx}] {name}")
        print(f"      channels={ch}  dwFormats=0x{mask:08X}")
        for label in describe_formats(mask) or ["（驅動未回報標準格式）"]:
            print(f"        - {label}")
        print()
    print("提示：藍牙 HFP 裝置常只回報 8 kHz 或 16 kHz 單聲道。請用 --probe 實測。")
    return 0


PROBE_RATES = [8000, 16000, 22050, 32000, 44100, 48000]


def estimate_bandwidth(data: bytes, rate: int, bits: int) -> dict | None:
    """判定音訊底層的頻寬（8 kHz 窄頻 vs 16 kHz 寬頻）。

    為什麼不能只靠「驅動接受 16 kHz」？因為 Windows 藍牙音訊堆疊會**重採樣**：
    即使底層 HFP 連結是 8 kHz 窄頻，你要求 16 kHz 它也會照樣接受，
    再內插成 16 kHz —— 資料裡高於 4 kHz 的部分全是空的。

    ⚠️ 為什麼不能只看「高頻有能量」？
    極安靜的錄音（例如只有底噪）在高頻帶會充滿**量化雜訊**，
    看起來也有能量，會被誤判成寬頻。實測：純底噪錄音的 4–8k/0.3–3k 比值約 -20 dB，
    與正常語音同級 → 這個比值**單獨看沒有判別力**。

    正確的判別方式：**高頻帶會不會「跟著聲音變大」**。
      - 8 kHz 窄頻連結：4 kHz 以上永遠是雜訊底，大聲段落與安靜段落**一樣低**。
      - 16 kHz 寬頻連結：發『ㄙ』這類高頻音時，4–8 kHz 會**明顯上升**。
    所以本函式比較「最大聲段落」與「最安靜段落」在高頻帶的**絕對能量差**（delta）。

    回傳 None 表示沒有 numpy（可選依賴）。
    """
    try:
        import numpy as np
    except ImportError:
        return None
    if not data:
        return None

    dt = np.dtype(np.int16 if bits == 16 else np.uint8)
    usable = len(data) - (len(data) % dt.itemsize)
    samples = np.frombuffer(data[:usable], dtype=dt).astype(np.float64)
    if bits == 8:
        samples -= 128.0
    if samples.size < 4096:
        return None

    frame_len = max(1024, int(rate * 0.032))
    hop = frame_len // 2
    starts = list(range(0, samples.size - frame_len + 1, hop))
    if not starts:
        return None

    window = np.hanning(frame_len)
    specs = []
    frame_rms = []
    for s in starts:
        seg = samples[s:s + frame_len]
        specs.append(np.abs(np.fft.rfft(seg * window)) ** 2)
        # 位準一律用**時域 RMS** 計算。FFT 的絕對尺度取了決於窗長與窗函式，
        # 拿它當 dBFS 會嚴重高估（實測會把 -62 dBFS 的底噪算成 -9.5 dBFS）。
        frame_rms.append(float(np.sqrt(np.mean(seg ** 2))))
    specs = np.array(specs)
    frame_rms = np.array(frame_rms)
    freqs = np.fft.rfftfreq(frame_len, d=1.0 / rate)
    hi_band = (freqs >= 4000) & (freqs < min(rate / 2.0, 8000))

    total_power = specs.sum(axis=1)  # noqa: F841  保留供未來分析，不參與判定
    order = np.argsort(frame_rms)
    top_n = max(1, len(order) // 4)
    active_rows, quiet_rows = order[-top_n:], order[:top_n]

    full_scale = 32768.0 if bits == 16 else 128.0
    active_level_db = 20.0 * np.log10(max(float(frame_rms[active_rows].mean()), 1e-12) / full_scale)
    peak = float(np.abs(samples).max())

    def hf_band_db(rows) -> float:
        """高頻帶（4–8 kHz）在這些段落的能量位準。

        尺度是任意的（FFT 功率未正規化）—— 但因為只拿來做**同一份錄音內**
        大聲段落與安靜段落的相減，任意但一致的尺度不影響判讀。
        絕對音量一律由時域 RMS 負責。
        """
        if not hi_band.any():
            return float("-inf")
        p = float(specs[rows][:, hi_band].mean())
        return 10.0 * np.log10(p) if p > 0 else float("-inf")

    hf_active_db = hf_band_db(active_rows)
    hf_quiet_db = hf_band_db(quiet_rows)
    delta_db = (hf_active_db - hf_quiet_db
                if hf_active_db > float("-inf") and hf_quiet_db > float("-inf") else 0.0)

    base = {"nyquist_hz": rate / 2.0, "delta_db": delta_db,
            "hf_active_db": hf_active_db, "hf_quiet_db": hf_quiet_db,
            "active_level_db": active_level_db, "peak": peak,
            "frames": len(order)}

    if not hi_band.any():
        return {**base, "verdict": "narrowband",
                "note": "⚠️ 取樣率本身不到 4 kHz 以上，無法判定。"}

    if active_level_db < -45:
        return {**base, "verdict": "too-quiet",
                "note": (f"⚠️ 這段錄音幾乎是靜音（最大聲段落只有 {active_level_db:+.1f} dBFS）。"
                         "無法判定頻寬 —— 請**確定出聲**再錄一次。純底噪的頻譜會騙人。")}

    if delta_db >= 6.0 and active_level_db < -40:
        return {**base, "verdict": "low-level",
                "note": (f"⚠️ 高頻有反應，但整段訊號太小聲（{active_level_db:+.1f} dBFS）"
                         "→ 可能只是單一寬頻雜音，不足以判定。"
                         "請**靠近麥克風正常音量說話**並發『ㄙ～～』重測。")}

    if delta_db >= 6.0:
        return {**base, "verdict": "wideband",
                "note": (f"✅ 高頻帶佔比明顯跟著聲音上升（+{delta_db:.1f} dB），"
                         f"訊號位準足夠（{active_level_db:+.1f} dBFS）→ "
                         "底層是 **16 kHz 寬頻**，ASR 可用。")}
    if delta_db < 3.0:
        return {**base, "verdict": "no-hf-response",
                "note": ("⚠️ 高頻帶佔比幾乎不隨聲音變化 → 兩種可能："
                         "(a) 底層是 **8 kHz 窄頻**，或 "
                         "(b) 你的語音本來就沒有高頻（例如只發母音）。"
                         "請**拉長音發『ㄙ～～』**重測，才能區分這兩者。")}
    return {**base, "verdict": "indeterminate",
            "note": "🤔 介於中間。請拉長音發『ㄙ～～』再測一次。"}


def report_bandwidth(data: bytes, rate: int, bits: int) -> None:
    result = estimate_bandwidth(data, rate, bits)
    if result is None:
        print("  （未安裝 numpy 或音訊過短，跳過頻寬分析。）")
        return
    print(f"  訊號位準          : 主動段落 {result['active_level_db']:+.1f} dBFS，"
          f"峰值 {result['peak']:.0f} / 32767")
    print(f"  高頻帶(4–8k) 位準 : 大聲段落 {result['hf_active_db']:+.1f} dB，"
          f"安靜段落 {result['hf_quiet_db']:+.1f} dB（相對值，只看差值）")
    print(f"  → 高頻帶上升量    : {result['delta_db']:+.1f} dB"
          f"   （>= +6 代表高頻真的跟著聲音出現 → 寬頻；< +3 代表高頻沒反應）")
    print(f"  判定: {result['verdict']} — {result['note']}")


def cmd_probe(device_id: int, channels: int, bits: int) -> int:
    name = next((n for i, n, *_ in list_devices() if i == device_id), f"device {device_id}")
    print(f"裝置 [{device_id}] {name}")
    print(f"探測格式：{channels}ch / {bits}-bit\n")
    accepted = []
    for rate in PROBE_RATES:
        fmt = make_format(rate, channels, bits)
        rc, _ = try_open(device_id, fmt)
        if rc == 0:
            accepted.append(rate)
            print(f"  {rate:>6} Hz  ✅ 接受")
        else:
            print(f"  {rate:>6} Hz  ❌ 拒絕（{err_name(rc)}）")
    print()
    if not accepted:
        print("→ 沒有任何取樣率被接受。請確認裝置未被其他程式獨佔，並已開機。")
        return 1
    best = max(accepted)
    print(f"可接受的取樣率：{accepted}")
    print(f"最高可用：{best} Hz")
    print()
    print("⚠️ 重要：『驅動接受』不等於『真的能收到那個頻寬』。")
    print("   Windows 藍牙音訊堆疊會把 8 kHz 窄頻連結重採樣成你要求的任何取樣率，")
    print("   所以 48 kHz 也會被「接受」，但資料裡 4 kHz 以上全是空的。")
    print("   → 真正的頻寬只能靠實際錄音後的頻譜分析（本工具錄完會自動做）。")
    print(f"   請接著跑：python tools/p0/record_wav.py --device {device_id} --seconds 6 --rate {best}")
    return 0


def record(device_id: int, seconds: float, rate: int, channels: int, bits: int,
           out_path: Path) -> int:
    fmt = make_format(rate, channels, bits)
    hwi = HWAVEIN()
    rc = winmm.waveInOpen(ctypes.byref(hwi), device_id, ctypes.byref(fmt), None, None,
                          CALLBACK_NULL)
    if rc != 0:
        print(f"waveInOpen 失敗：{err_name(rc)}", file=sys.stderr)
        print("提示：先跑 --probe 看哪些取樣率可用。", file=sys.stderr)
        return 1

    n_bytes = int(fmt.nAvgBytesPerSec * seconds)
    buf = ctypes.create_string_buffer(n_bytes)
    hdr = WAVEHDR()
    hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
    hdr.dwBufferLength = n_bytes

    try:
        rc = winmm.waveInPrepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
        if rc != 0:
            print(f"waveInPrepareHeader 失敗：{err_name(rc)}", file=sys.stderr)
            return 1
        rc = winmm.waveInAddBuffer(hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
        if rc != 0:
            print(f"waveInAddBuffer 失敗：{err_name(rc)}", file=sys.stderr)
            return 1
        rc = winmm.waveInStart(hwi)
        if rc != 0:
            print(f"waveInStart 失敗：{err_name(rc)}", file=sys.stderr)
            return 1

        print(f"錄音中… {seconds:g} 秒。")
        print("請對麥克風做兩件事：先說一句中文，再拉長音發『ㄙ～～』（或噓聲）。")
        print("（『ㄙ』含大量高頻，是判斷底層是 8 kHz 還是 16 kHz 的關鍵。）")
        deadline = time.monotonic() + seconds + 5.0
        while not (hdr.dwFlags & WHDR_DONE):
            if time.monotonic() > deadline:
                print("（等待逾時，提前停止）", file=sys.stderr)
                break
            time.sleep(0.02)
        winmm.waveInStop(hwi)
        winmm.waveInReset(hwi)
        winmm.waveInUnprepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
    finally:
        winmm.waveInClose(hwi)

    got = int(hdr.dwBytesRecorded) or n_bytes
    # 對齊 block align，避免寫出半個 sample
    got -= got % fmt.nBlockAlign
    data = buf.raw[:got]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(bits // 8)
        wf.setframerate(rate)
        wf.writeframes(data)

    # 簡單的能量檢查：全 0 表示拿到的是靜音
    peak = 0
    rms = 0.0
    if data and bits == 16:
        count = len(data) // 2
        samples = struct.unpack(f"<{count}h", data[:count * 2])
        peak = max(abs(s) for s in samples)
        rms = (sum(s * s for s in samples) / count) ** 0.5
    elif data and bits == 8:
        count = len(data)
        centered = [b - 128 for b in data[:count]]
        peak = max(abs(c) for c in centered)
        rms = (sum(c * c for c in centered) / count) ** 0.5
        peak = int(peak * 32767 / 128)  # 正規化到 16-bit 尺度，方便與 16-bit 比較
        rms = rms * 32767 / 128

    print(f"\n已寫出：{out_path}")
    print(f"  格式   : {rate} Hz / {channels}ch / {bits}-bit")
    print(f"  長度   : {got / fmt.nAvgBytesPerSec:.2f} 秒（{got} bytes）")
    print(f"  峰值   : {peak} / 32767   RMS: {rms:.1f}")
    if peak == 0:
        print("  ⚠️ 全為靜音！可能錄到了錯誤的裝置，或該裝置需要切到 HFP 模式。")
    else:
        print("  ✅ 有訊號。請務必回放確認『是這支麥克風』的聲音。")
    report_bandwidth(data, rate, bits)
    print("\n隱私提醒：此檔為真實錄音，artifacts/ 已 gitignore，請勿 commit。")
    return 0


def cmd_analyze_wav(path: Path) -> int:
    """對既有的 WAV 重跑頻寬分析，不必重錄。"""
    if not path.exists():
        print(f"找不到檔案：{path}", file=sys.stderr)
        return 1
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        data = wf.readframes(wf.getnframes())
        frames = wf.getnframes()
    print(f"{path}")
    print(f"  格式: {rate} Hz / {channels}ch / {width * 8}-bit / {frames} frames "
          f"({frames / rate:.2f} 秒)")
    report_bandwidth(data, rate, width * 8)
    print("\n提示：判定要可信，錄音時必須**有說話**且**拉長音發『ㄙ』**。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0/T2：指定裝置錄音與取樣率探測")
    parser.add_argument("--list", action="store_true", help="列舉輸入裝置後結束")
    parser.add_argument("--analyze-wav", default=None, metavar="WAV",
                        help="對既有 WAV 重跑頻寬分析，不必重錄")
    parser.add_argument("--probe", action="store_true", help="探測各取樣率是否被接受")
    parser.add_argument("--device", type=int, default=None,
                        help="裝置索引（用 --list 查）")
    parser.add_argument("--seconds", type=float, default=5.0, help="錄音秒數（預設 5）")
    parser.add_argument("--rate", type=int, default=16000, help="取樣率（預設 16000）")
    parser.add_argument("--channels", type=int, default=1, help="聲道數（預設 1）")
    parser.add_argument("--bits", type=int, default=16, choices=(8, 16), help="位元深度")
    parser.add_argument("--out", default=None, help="輸出 WAV 路徑")
    args = parser.parse_args(argv)
    setup_console()

    if args.list:
        return cmd_list()

    if args.analyze_wav:
        return cmd_analyze_wav(Path(args.analyze_wav))

    if args.device is None:
        parser.error("請用 --device N 指定裝置（先跑 --list），或用 --list 檢視。")

    if args.probe:
        return cmd_probe(args.device, args.channels, args.bits)

    out = Path(args.out) if args.out else Path(
        f"artifacts/t2-dev{args.device}-{args.rate}hz.wav")
    return record(args.device, args.seconds, args.rate, args.channels, args.bits, out)


if __name__ == "__main__":
    raise SystemExit(main())
