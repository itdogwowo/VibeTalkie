#!/usr/bin/env python3
"""macOS 錄音（AVAudioEngine）→ 16 kHz / mono / PCM16 bytes。

## 這個模組取代什麼

Windows 版的 `record_wav.py`（517 行）用 **winmm waveIn**。
macOS 對應的是 **AVAudioEngine**：

    Windows                        macOS
    waveInOpen / waveInPrepare…    AVAudioEngine.inputNode
    WAVEHDR 緩衝區                 installTapOnBus 的回呼
    裝置原生格式（實測 16k）       裝置原生格式（實測 44.1k / 2ch）
    → 不需重採樣                   → **需要重採樣到 16 kHz**

⚠️ **這是一個關鍵差異**：`docs/hardware.md` §4.1 實測那支藍牙麥克風
直接給 16 kHz，所以 Windows 版不需要重採樣。但 mac 的內建麥克風是
44.1 kHz 立體聲，所以這裡必須做轉換 —— 用 `AVAudioConverter`（系統內建，
不是自己寫 resampler）。

## 介面契約（與 Windows 的 `recorder.Capture` 一致）

`ptt.py` 的狀態機只認這個介面，所以兩平台可以共用同一套邏輯：

    __enter__() / __exit__()    開／關串流
    recorded() -> bytes         到目前為止的 PCM16 mono bytes（**不含 WAV 標頭**）
    full() -> bool              緩衝區滿了沒
    done -> bool                可否安全停止
    poll_level() -> float       目前音量（0.0–1.0，給 UI 音量表）

⚠️ `recorded()` 必須回傳**單調成長**的完整資料，不能是「自上次以來的增量」——
`ptt.py` 靠 `_cap_seen` 自己算增量（見 `ptt.py` 的 `_finish_recording`）。
搞錯的話症狀是「第二次錄音內容重複」。
"""

from __future__ import annotations

import math
import struct
import sys
import threading
import time
from typing import Any

TARGET_RATE = 16000          # ASR 要的取樣率（與 Windows 版一致）
TARGET_CHANNELS = 1
TARGET_BITS = 16


class MacAudioError(RuntimeError):
    """錄音相關錯誤 —— 訊息要能直接顯示給使用者。"""


def microphone_hint() -> str:
    return (
        "需要「麥克風」權限才能錄音。\n"
        "  系統設定 → 隱私權與安全性 → 麥克風 → 把 Terminal（或本程式）打勾。\n"
        "  改完要**完全結束再重開**該程式（⌘Q），權限才會生效。"
    )


def _read_float32_mono(buf, frames: int, channels: int) -> list[float]:
    """把 AVAudioPCMBuffer 的 Float32 資料讀成單聲道 list[float]。

    ## 這裡的寫法是一連串實測結果，不要「簡化」它

    pyobjc 對 `floatChannelData()` 的繫結很反直覺：

        floatChannelData()      → tuple（長度 = 聲道數）
        tuple[i]                → objc.varlist（不是 ctypes 指標！）
        objc.varlist            → 有 .as_buffer() / .as_tuple()

    走錯路的代價（都實際踩過）：

      · 把 varlist 直接餵 ctypes
        → TypeError: 'objc.varlist' object cannot be interpreted as c_void_p
      · 用 `raw[0][:frames]` 切片（靠 Python 猜長度）
        → **segmentation fault**（越界讀取，Python 攔不到）
      · 用 AVAudioConverter 繞過這一切
        → 也 segfault（見 MacCapture.__enter__ 的說明）

    `as_buffer()` 是 pyobjc 提供的安全通道：它知道真實長度，
    回傳支援緩衝區協定的物件，所以在 Python 端不會越界。
    """
    import array

    ch = buf.floatChannelData()
    if ch is None:
        return []

    def channel(i: int) -> list[float]:
        # ⚠️ `as_buffer(count)` 的 count 是**樣本數**（每次呼叫都要給，
        #    不給會得到 "function missing required argument 'count'"）。
        #    回傳 memoryview，長度 = count × 4 bytes（float32）。
        mv = ch[i].as_buffer(frames)
        a = array.array("f")
        a.frombytes(bytes(mv[: frames * 4]))
        return a.tolist()

    mono = channel(0)
    if channels <= 1:
        return mono
    right = channel(1)
    n = min(len(mono), len(right))
    return [(mono[i] + right[i]) * 0.5 for i in range(n)]


def _resample_to_pcm16(samples: list[float], in_rate: float,
                       out_rate: int) -> bytes:
    """線性插值重採樣 → PCM16 bytes。

    ## 為什麼不用 AVAudioConverter

    實測結論（見 `MacCapture.__enter__` 的說明）：pyobjc 的
    AVAudioConverter 在這個組合下會 **segfault**。
    純 Python 的算術慢一點，但不會無聲無息地把行程殺掉。

    ## 品質說明（誠實揭露）

    這是**線性插值**，沒有抗鋸齒濾波。對語音辨識的影響：
    44.1 kHz → 16 kHz 是降採樣，理論上會有 aliasing。
    但語音能量主要集中在 4 kHz 以下，而 16 kHz 的 Nyquist 是 8 kHz，
    所以落在危險區的能量很少。實務上 ASR 對這種程度的失真不敏感。

    若日後發現辨識率受影響，正確的升級路徑是加一個簡單的 FIR 低通
    （而不是回頭用 AVAudioConverter）。
    """
    n_in = len(samples)
    if n_in == 0 or in_rate <= 0:
        return b""

    ratio = in_rate / float(out_rate)
    n_out = int(n_in / ratio)
    if n_out <= 0:
        return b""

    out = bytearray(n_out * 2)
    pack = struct.pack_into
    for i in range(n_out):
        pos = i * ratio
        i0 = int(pos)
        frac = pos - i0
        s0 = samples[i0] if i0 < n_in else 0.0
        s1 = samples[i0 + 1] if i0 + 1 < n_in else s0
        v = s0 + (s1 - s0) * frac
        # 夾在 int16 範圍內 —— 超出的話 pack_into 會拋錯
        iv = int(v * 32767.0)
        if iv > 32767:
            iv = 32767
        elif iv < -32768:
            iv = -32768
        pack(f"<h", out, i * 2, iv)
    return bytes(out)


_DEV_CACHE: list[dict] | None = None
_DEV_CACHE_AT = 0.0
_DEV_CACHE_TTL = 30.0        # 秒；裝置清單不會常常變


def _system_profiler_devices() -> list[dict]:
    """用 `system_profiler SPAudioDataType` 列出音訊裝置。

    ## 為什麼用這個而不是 CoreAudio API

    試過 pyobjc 的 `CoreAudio` 綁定，但它的簽章在不同版本間不一致，
    而且**錯誤訊息完全指不到真正的問題**：

        ValueError: argument 4 must be None or objc.NULL
        TypeError: depythonifying struct, got no sequence

    前者是位址要 tuple 不要 ctypes 結構，後者相反 —— 而且 `None` 也不行。
    這種「綁定不穩定」的東西不該放在啟動路徑上。

    `system_profiler` 是 macOS 內建工具，輸出穩定、格式好解析，
    而且**只需要列舉**（不開串流、不改系統設定）。

    代價是慢（約 1–2 秒），所以有 30 秒快取 —— 設定頁不會一直重打。
    """
    import subprocess

    try:
        out = subprocess.run(["system_profiler", "SPAudioDataType"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:                                   # noqa: BLE001
        return []

    devs: list[dict] = []
    cur: dict | None = None
    in_devices = False

    def flush() -> None:
        """把目前累積的裝置收進清單 —— **只收有輸入通道的**。

        ⚠️ `system_profiler` 的清單**同時包含輸出裝置**
        （喇叭、HDMI…）。第一版沒有過濾，結果某些虛擬裝置
        也出現在麥克風清單裡。
        """
        nonlocal cur
        if cur is not None and (cur.get("_in_channels") or 0) > 0:
            devs.append(cur)
        cur = None

    for raw in out.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped == "Devices:":
            in_devices = True
            continue
        if not in_devices:
            continue

        # 裝置名稱：縮排最淺（8 空白）且以冒號結尾
        if indent <= 8 and stripped.endswith(":") and ":" not in stripped[:-1]:
            flush()
            cur = {"name": stripped[:-1].strip(), "_in_channels": 0,
                   "_rate": 0, "_default": False}
            continue
        if cur is None:
            continue

        key, _, val = stripped.partition(":")
        key, val = key.strip(), val.strip()
        kl = key.lower()
        if kl == "input channels":
            try:
                cur["_in_channels"] = int(val)
            except ValueError:
                pass
        elif kl in ("current samplerate", "current sample rate", "sample rate"):
            # ⚠️ 實際的鍵是 **`Current SampleRate`**（沒有空格），
            #    不是 `Current Sample Rate`。第一版認錯鍵名 → 所有裝置
            #    的取樣率都是 0。
            try:
                cur["_rate"] = int(float(val.split()[0]))
            except (ValueError, IndexError):
                pass
        elif kl == "default input device":
            cur["_default"] = val.lower().startswith("yes")
        elif kl == "transport":
            # ⚠️ 用來過濾**虛擬裝置**（見 list_input_devices 的說明）。
            #    實際踩到：某些虛擬音訊裝置的 `Input Channels` 是真的
            #    （雙向的虛擬裝置），所以「有沒有輸入通道」**無法**
            #    區分它跟真麥克風。
            cur["_transport"] = val

    flush()

    # 只有一個裝置時，它就預設（system_profiler 不一定會標）
    if len(devs) == 1:
        devs[0]["_default"] = True

    for i, d in enumerate(devs):
        d["index"] = i
        d["channels"] = d.pop("_in_channels", 0)
        d["rate"] = d.pop("_rate", 0)
        d["is_default"] = d.pop("_default", False)
        d["transport"] = d.pop("_transport", "")
    return devs


def list_input_devices(force: bool = False,
                       include_virtual: bool = False) -> list[dict]:
    """列出音訊輸入裝置（有快取）。

    ## ⚠️ 與 Windows 的關鍵差異

    macOS 的 AVAudioEngine **只能用「系統預設輸入裝置」**，
    程式無法指定要用哪一支（那由系統設定決定）。

    所以這份清單的用途是**顯示**（讓使用者知道有哪些、預設是哪一支），
    不是「選了就會生效」。設定頁的「麥克風優先順序」在 mac 上
    不會改變實際使用的裝置 —— 這一點必須誠實反映，不能假裝有作用。

    ## 為什麼預設排除虛擬裝置

    實測發現某些虛擬音訊裝置在 `system_profiler` 裡
    也有 `Input Channels`（雙向的虛擬裝置），
    所以它會混進麥克風清單裡 —— 但它不可能是使用者要拿來錄音的裝置。

    判準用 `Transport: Virtual`（不是「有沒有輸入通道」）。

    `include_virtual=True` 可以把它們也列出來（除錯用）。
    """
    global _DEV_CACHE, _DEV_CACHE_AT

    import time
    now = time.time()
    if not force and _DEV_CACHE is not None and (now - _DEV_CACHE_AT) < _DEV_CACHE_TTL:
        devs = _DEV_CACHE
    else:
        devs = _system_profiler_devices()

        # 補上「目前實際使用」的那一支（AVAudioEngine 的預設輸入）
        try:
            import AVFoundation  # type: ignore
            engine = AVFoundation.AVAudioEngine.alloc().init()
            node = engine.inputNode()
            fmt = node.inputFormatForBus_(0) if node else None
            cur_rate = int(fmt.sampleRate()) if fmt else 0
            cur_ch = int(fmt.channelCount()) if fmt else 0
            if not devs:
                devs = [{"name": "系統預設輸入裝置", "channels": cur_ch,
                         "rate": cur_rate, "is_default": True, "index": 0,
                         "transport": ""}]
            else:
                # AVAudioEngine 的實際取樣率最準，補到預設裝置上
                for d in devs:
                    if d.get("is_default"):
                        d["rate"] = cur_rate or d.get("rate", 0)
                        break
        except Exception:                               # noqa: BLE001
            pass

        _DEV_CACHE, _DEV_CACHE_AT = devs, now

    if include_virtual:
        return list(devs)
    return [d for d in devs if (d.get("transport") or "").lower() != "virtual"]


class MacCapture:
    """錄音串流。介面與 Windows 的 `recorder.Capture` 一致。

    音量與資料都在 tap 的回呼（背景執行緒）裡累積，所以 `recorded()`
    可以在主執行緒安全呼叫 —— 用鎖保護。
    """

    _LEVEL_WINDOW = 2048          # 音量計算的視窗（樣本數）

    def __init__(self, device_id: int = 0, rate: int = TARGET_RATE,
                 channels: int = TARGET_CHANNELS, bits: int = TARGET_BITS,
                 max_seconds: float = 120.0, device_name: str = ""):
        if rate != TARGET_RATE:
            # 現階段只支援 16 kHz —— 與 Windows 版一致，且 ASR 就要這個。
            # 不靜默接受再偷偷轉兩次（那會讓取樣率錯誤極難查）。
            raise MacAudioError(f"目前只支援 {TARGET_RATE} Hz，收到 {rate}")
        self.device_id = device_id
        self.device_name = device_name or "系統預設輸入裝置"
        self.rate = rate
        self.channels = channels
        self.bits = bits
        self.max_bytes = int(rate * channels * bits // 8 * max_seconds)

        self._buf = bytearray()
        self._lock = threading.Lock()
        self._open = False
        self._level = 0.0
        self._engine: Any = None
        self._out_format: Any = None
        self._in_format: Any = None
        self._tap_block: Any = None     # 必須保留參照；被 GC 回收會 segfault
        self._err: str | None = None
        self.started_at: float | None = None

    # ---------------------------------------------------------------- 開關

    def __enter__(self) -> "MacCapture":
        try:
            import AVFoundation  # type: ignore
        except ImportError as exc:
            raise MacAudioError(
                f"載入不到 AVFoundation（pyobjc）：{exc}\n"
                "  請安裝：pip install pyobjc-framework-AVFoundation"
            ) from exc

        engine = AVFoundation.AVAudioEngine.alloc().init()
        node = engine.inputNode()
        if node is None:
            raise MacAudioError("拿不到輸入節點。\n" + microphone_hint())

        in_fmt = node.inputFormatForBus_(0)
        if in_fmt is None or in_fmt.channelCount() == 0:
            raise MacAudioError(
                "輸入裝置沒有可用的格式（可能沒有麥克風，或權限未開）。\n"
                + microphone_hint())

        self._in_format = in_fmt
        # 輸出的目標：16 kHz / mono / int16。轉換**自己做**（見 _resample）。
        self._in_rate = float(in_fmt.sampleRate())
        self._in_channels = int(in_fmt.channelCount())

        # ⚠️ 三個做法都試過了，這裡是實測結論 —— 不要重蹈覆轍：
        #
        #   (a) tap 直接指定 16 kHz Int16（以為 AVAudioEngine 會自己轉）
        #       → "Failed to create tap due to format mismatch"
        #       AVAudioEngine 不接受與硬體不同的格式。
        #
        #   (b) tap 用原生格式 + 回呼裡用 AVAudioConverter 轉
        #       → **segmentation fault**。已用最小測試隔離確認：
        #         不含 converter 時回呼正常被呼叫 19 次、收 83790 frames；
        #         加上它就崩。pyobjc 對 AVAudioConverter 的 block 繫結
        #         在這個組合下不可靠。
        #
        #   (c) tap 用原生格式 + **自己做重採樣**（目前採用）
        #       → 沒有原生物件、沒有 block、沒有指標算術，
        #         只有純 Python 的算術。慢一點，但不會無聲無息地崩掉。
        #
        # 為什麼可以接受純 Python：錄音只在按住的幾秒內發生
        # （44.1 kHz × 3 秒 ≈ 13 萬個樣本），不是持續的即時處理。
        def on_buffer(buf, when) -> None:
            self._handle_buffer(buf)

        node.installTapOnBus_bufferSize_format_block_(
            0, 2048, in_fmt, on_buffer)
        # ⚠️ 一定要抓住 block 的參照 —— 否則會被 GC 回收，
        #    而原生層還握著它的指標 → 又是 segfault。
        self._tap_block = on_buffer

        try:
            engine.prepare()
            ok, err = engine.startAndReturnError_(None)
        except Exception as exc:                       # noqa: BLE001
            raise MacAudioError(f"啟動錄音引擎失敗：{exc}\n"
                                + microphone_hint()) from exc
        if not ok:
            raise MacAudioError(f"啟動錄音引擎失敗：{err}\n" + microphone_hint())

        self._engine = engine
        self._open = True
        self.started_at = time.monotonic()
        return self

    def __exit__(self, *exc) -> None:
        self._open = False
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.inputNode().removeTapOnBus_(0)
            except Exception:                          # noqa: BLE001
                pass
            try:
                engine.stop()
            except Exception:                          # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 回呼

    def _handle_buffer(self, buf) -> None:
        """Tap 回呼（背景執行緒）：原生格式 → 16 kHz mono PCM16。

        ⚠️ 這裡**絕不能拋錯** —— 在 AVAudioEngine 的回呼裡拋錯會讓引擎
        靜默停止（症狀：錄音長度 0，卻沒有任何錯誤訊息）。
        所以全部包在 try/except，失敗只記在 `self._err`。
        """
        try:
            frames = int(buf.frameLength())
            if frames <= 0:
                return
            mono = _read_float32_mono(buf, frames, self._in_channels)
            if not mono:
                return
            data = _resample_to_pcm16(mono, self._in_rate, self.rate)
            if not data:
                return
            with self._lock:
                self._buf.extend(data)
                self._level = self._compute_level(data)
        except Exception as exc:                       # noqa: BLE001
            if self._err is None:
                self._err = f"{type(exc).__name__}: {exc}"

    @classmethod
    def _compute_level(cls, data: bytes) -> float:
        """RMS → 0.0–1.0。給 UI 的音量表用。"""
        n = min(len(data) // 2, cls._LEVEL_WINDOW)
        if n <= 0:
            return 0.0
        s = struct.unpack(f"<{n}h", data[:n * 2])
        rms = math.sqrt(sum(v * v for v in s) / n) / 32768.0
        return min(1.0, rms * 3.0)      # 放大一點比較好看

    # ---------------------------------------------------------------- 查詢

    def recorded(self) -> bytes:
        """到目前為止**完整**的 PCM16 mono bytes（不含 WAV 標頭）。

        ⚠️ 回傳的是累積資料，不是增量 —— 增量由 `ptt.py` 的 `_cap_seen` 負責。
        """
        with self._lock:
            return bytes(self._buf)

    def full(self) -> bool:
        with self._lock:
            return len(self._buf) >= self.max_bytes

    @property
    def done(self) -> bool:
        """可不可以安全停止（已關閉，或緩衝區滿）。"""
        return (not self._open) or self.full()

    def poll_level(self) -> float:
        with self._lock:
            return self._level

    @property
    def error(self) -> str | None:
        """串流期間發生的錯誤（若有的話）。呼叫端應該要檢查。"""
        return self._err


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="macOS 錄音測試")
    ap.add_argument("--list", action="store_true", help="列出輸入裝置")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--out", default="/tmp/vt-mac-test/mac-rec.wav")
    args = ap.parse_args()

    if args.list:
        print("=" * 74)
        print("  macOS 音訊輸入裝置")
        print("=" * 74)
        for d in list_input_devices():
            print(f"\n  [{d['index']}] {d['name']}")
            for k, v in d.items():
                if k not in ("index", "name"):
                    print(f"        {k:12s} = {v}")
        raise SystemExit(0)

    print("=" * 74)
    print(f"  錄音測試：{args.seconds:.0f} 秒 → {args.out}")
    print("=" * 74)
    try:
        with MacCapture(max_seconds=30) as cap:
            print(f"  ✅ 已開始（{cap.device_name}）—— 請說話…")
            t0 = time.time()
            while time.time() - t0 < args.seconds:
                lv = cap.poll_level()
                bars = "█" * int(lv * 40)
                print(f"\r  音量 {lv:5.3f} {bars:<40}", end="", flush=True)
                time.sleep(0.1)
            print()
            pcm = cap.recorded()
            err = cap.error
    except MacAudioError as exc:
        print(f"\n  ❌ {exc}\n")
        raise SystemExit(1)

    n = len(pcm) // 2
    secs = n / TARGET_RATE
    print(f"\n  收到 {len(pcm)} bytes = {n} 樣本 = {secs:.2f} 秒")
    if err:
        print(f"  ⚠️ 串流期間有錯誤：{err}")
    if n:
        import wave
        with wave.open(args.out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TARGET_RATE)
            w.writeframes(pcm)
        print(f"  已寫出 {args.out}（可用 afplay 聽聽看）")
    else:
        print("  ⚠️ 完全沒收到音訊 —— 檢查麥克風權限，或裝置是否被靜音。")
