#!/usr/bin/env python3
"""P1 錄音輔助：即時音量表 + 說話完自動停止。

為什麼不直接用 P0 的 `record_wav.py`？
    固定秒數錄音會把大段靜音也錄進去。SenseVoice 這類模型遇到長靜音
    容易產生幻覺輸出，而且會拖長延遲。這裡改成：
      1. 先用前 0.5 秒估底噪，再據此定說話門檻（自適應，不寫死）
      2. 偵測到說話後，安靜超過 0.7 秒就自動停止
      3. **即時顯示音量表** —— 使用者回報「系統裡看不到輸入音量」，
         所以在終端機自己畫一個，才知道有沒有收到聲音

底層沿用 `app/core/record_wav.py` 已驗證過的 winmm waveIn 設定。
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # app/core
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "third_party"))

from record_wav import (  # noqa: E402
    CALLBACK_NULL,
    HWAVEIN,
    WAVEHDR,
    WHDR_DONE,
    WAVEFORMATEX,
    err_name,
    make_format,
    winmm,
)


def speech_thresh(noise_floor: float) -> float:
    """說話門檻：底噪的 2.5 倍，但有下限。

    下限取 25 是因為實測這支麥克風偏小聲：某次正常說話的整體 RMS 只有 33.5，
    門檻若訂在 60 就會完全偵測不到。
    """
    return max(noise_floor * 2.5, 25.0)


def active_level(pcm: bytes, rate: int, frame_s: float = 0.05) -> tuple[float, float, int]:
    """只算「有聲段落」的位準。回傳 (dBFS, 有聲時間佔比, 有聲秒數)。

    為什麼不能拿整段平均當音量？因為錄音前後都有靜音，
    整段平均會嚴重低估說話音量。實測實際案例：

        整段平均 -42.2 dBFS  →  看起來「太小聲」
        實際說話 -37.3 dBFS  →  其實還算正常

    用整段平均會產生**假警報**，叫使用者重錄根本不需要的東西。
    這裡用第 25 百分位估底噪，再用 `speech_thresh()` 取門檻。
    """
    import math

    n = len(pcm) // 2
    if n == 0:
        return float("-inf"), 0.0, 0
    s = struct.unpack(f"<{n}h", pcm[:n * 2])
    win = max(1, int(rate * frame_s))
    frames = []
    for i in range(0, n - win + 1, win):
        seg = s[i:i + win]
        frames.append((sum(v * v for v in seg) / len(seg)) ** 0.5)
    if not frames:
        return float("-inf"), 0.0, 0

    srt = sorted(frames)
    noise = srt[len(srt) // 4]
    thr = speech_thresh(noise)
    active = [i for i, v in enumerate(frames) if v > thr]
    if not active:
        return float("-inf"), 0.0, 0

    vals = [v for i in active for v in s[i * win:(i + 1) * win]]
    rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
    db = 20.0 * math.log10(rms / 32768.0) if rms > 0 else float("-inf")
    return db, len(active) / len(frames), len(active) * frame_s


def meter(rms: float, width: int = 30) -> str:
    """把 RMS 畫成條狀圖（滿刻度 32768）。"""
    import math
    if rms <= 0:
        return " " * width
    db = 20.0 * math.log10(rms / 32768.0)
    # 映射 -60..0 dBFS 到 0..width
    frac = max(0.0, min(1.0, (db + 60.0) / 60.0))
    filled = int(frac * width)
    return "█" * filled + "·" * (width - filled)


class Capture:
    """一次性的 waveIn 擷取（開啟 → 啟動 → 輪詢 → 關閉）。"""

    def __init__(self, device_id: int, rate: int = 16000, channels: int = 1,
                 bits: int = 16, max_seconds: float = 15.0):
        self.device_id = device_id
        self.fmt = make_format(rate, channels, bits)
        self.rate = rate
        self.max_bytes = int(self.fmt.nAvgBytesPerSec * max_seconds)
        self.buf = ctypes.create_string_buffer(self.max_bytes)
        self.hdr = WAVEHDR()
        self.hwi = HWAVEIN()
        self._open = False

    def __enter__(self) -> "Capture":
        rc = winmm.waveInOpen(ctypes.byref(self.hwi), self.device_id,
                              ctypes.byref(self.fmt), None, None, CALLBACK_NULL)
        if rc != 0:
            raise RuntimeError(f"waveInOpen 失敗：{err_name(rc)}")
        self._open = True
        self.hdr.lpData = ctypes.cast(self.buf, ctypes.c_void_p)
        self.hdr.dwBufferLength = self.max_bytes
        for fn, name in ((winmm.waveInPrepareHeader, "PrepareHeader"),
                         (winmm.waveInAddBuffer, "AddBuffer")):
            rc = fn(self.hwi, ctypes.byref(self.hdr), ctypes.sizeof(WAVEHDR))
            if rc != 0:
                raise RuntimeError(f"waveIn{name} 失敗：{err_name(rc)}")
        rc = winmm.waveInStart(self.hwi)
        if rc != 0:
            raise RuntimeError(f"waveInStart 失敗：{err_name(rc)}")
        return self

    def __exit__(self, *exc) -> None:
        if not self._open:
            return
        winmm.waveInStop(self.hwi)
        winmm.waveInReset(self.hwi)
        winmm.waveInUnprepareHeader(self.hwi, ctypes.byref(self.hdr), ctypes.sizeof(WAVEHDR))
        winmm.waveInClose(self.hwi)
        self._open = False

    def recorded(self) -> bytes:
        got = int(self.hdr.dwBytesRecorded)
        got -= got % self.fmt.nBlockAlign
        return self.buf.raw[:got]

    @property
    def done(self) -> bool:
        return bool(self.hdr.dwFlags & WHDR_DONE)


def capture_manual(device_id: int, rate: int = 16000, max_seconds: float = 120.0,
                   show_meter: bool = True) -> dict:
    """手動停止錄音：開始後一直錄，直到使用者按 Enter。

    為什麼要有這個模式？
        自動停止（靠 VAD 門檻）在實測中表現不好：8 段錄音有聲比例只有 9–33%，
        而且會过早停止（某句 19 個字只錄到 0.24 秒語音）。
        原廠工具的做法也是「按鍵開始 → 停止」的手動流程。
        手動停止把不可靠的門檻判斷從關鍵路徑上拿掉。

    音量表跑在背景執行緒，所以在等 Enter 的時候仍然看的到有沒有收到聲音。
    """
    import threading

    with Capture(device_id, rate=rate, max_seconds=max_seconds) as cap:
        stop = threading.Event()
        peak = 0

        def watch() -> None:
            nonlocal peak
            last = 0
            while not stop.is_set():
                data = cap.recorded()
                if len(data) > last:
                    new = data[last - (last % 2):]
                    last = len(data)
                    n = len(new) // 2
                    if n:
                        s = struct.unpack(f"<{n}h", new[:n * 2])
                        rms = (sum(v * v for v in s) / n) ** 0.5
                        peak = max(peak, max(abs(v) for v in s))
                        if show_meter:
                            print(f"\r  [{meter(rms)}] {rms:7.1f}", end="", flush=True)
                time.sleep(0.05)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            input("  🎤 錄音中… 說完按 Enter 停止")
        except (EOFError, KeyboardInterrupt):
            pass
        stop.set()
        watcher.join(timeout=0.5)
        if show_meter:
            print("\r" + " " * 50 + "\r", end="", flush=True)
        pcm = cap.recorded()

    return _summarize(pcm, rate, peak, timed_out=len(pcm) < rate // 5)


def _summarize(pcm: bytes, rate: int, peak: int, timed_out: bool) -> dict:
    import math

    n = len(pcm) // 2
    db = float("-inf")
    if n:
        s = struct.unpack(f"<{n}h", pcm[:n * 2])
        rms = (sum(v * v for v in s) / n) ** 0.5
        db = 20.0 * math.log10(rms / 32768.0) if rms > 0 else float("-inf")
    adb, aratio, asec = active_level(pcm, rate)
    return {
        "pcm": pcm,
        "duration_s": n / rate,
        "speech_db": db,          # 整段平均（會被靜音拉低，僅供對照）
        "active_db": adb,         # 有聲段落實際音量（要看這個）
        "active_ratio": aratio,
        "active_s": asec,
        "peak": peak,
        "timed_out": timed_out,
    }


def capture_utterance(device_id: int, rate: int = 16000, max_seconds: float = 15.0,
                      silence_stop_s: float = 0.7, min_speech_s: float = 0.35,
                      onset_timeout_s: float = 6.0, show_meter: bool = True) -> dict:
    """錄一句話：等到有人聲 → 安靜一段時間後自動停。

    回傳 dict：pcm、duration_s、speech_db、noise_db、peak、timed_out。
    """
    import math

    chunk = int(rate * 0.05) * 2          # 50 ms 的 byte 數（16-bit mono）
    noise_floor = None
    noise_samples: list[float] = []
    spoke = False
    last_voice = None
    speech_start = None
    peak = 0
    onset_deadline = None

    with Capture(device_id, rate=rate, max_seconds=max_seconds) as cap:
        t_start = time.monotonic()
        onset_deadline = t_start + onset_timeout_s
        last_len = 0

        while True:
            data = cap.recorded()
            if len(data) > last_len:
                new = data[last_len - (last_len % 2):]
                last_len = len(data)
                if new:
                    n = len(new) // 2
                    s = struct.unpack(f"<{n}h", new[:n * 2])
                    rms = (sum(v * v for v in s) / n) ** 0.5
                    peak = max(peak, max(abs(v) for v in s))
                    now = time.monotonic()

                    if noise_floor is None:
                        noise_samples.append(rms)
                        if now - t_start >= 0.5:
                            # 用中位數而不是平均：平均會被一聲咳嗽或碰撞拉高，
                            # 導致門檻訂太高、安靜的說話反而偵測不到。
                            srt = sorted(noise_samples)
                            med = srt[len(srt) // 2]
                            noise_floor = max(med, 3.0)
                            if show_meter:
                                print(f"\r  底噪 RMS {noise_floor:6.1f}"
                                      f"（門檻 {speech_thresh(noise_floor):6.1f}）",
                                      flush=True)
                    else:
                        thresh = speech_thresh(noise_floor)
                        if rms > thresh:
                            if not spoke:
                                spoke = True
                                speech_start = now
                            last_voice = now
                        if show_meter:
                            tag = "🎤" if rms > thresh else "  "
                            print(f"\r  {tag} [{meter(rms)}] {rms:7.1f}", end="", flush=True)

                    if spoke and last_voice and (now - last_voice) > silence_stop_s:
                        if (last_voice - speech_start) >= min_speech_s:
                            break
                    if not spoke and now > onset_deadline:
                        break

            if cap.done or time.monotonic() - t_start > max_seconds:
                break
            time.sleep(0.02)

        pcm = cap.recorded()

    if show_meter:
        print("\r" + " " * 60 + "\r", end="", flush=True)

    n = len(pcm) // 2
    speech_db = float("-inf")
    if n:
        s = struct.unpack(f"<{n}h", pcm[:n * 2])
        rms = (sum(v * v for v in s) / n) ** 0.5
        speech_db = 20.0 * math.log10(rms / 32768.0) if rms > 0 else float("-inf")

    return {
        "pcm": pcm,
        "duration_s": n / rate,
        "speech_db": speech_db,
        "noise_floor": noise_floor or 0.0,
        "peak": peak,
        "timed_out": not spoke,
    }
