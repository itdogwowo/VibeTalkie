#!/usr/bin/env python3
"""P1 — Push-to-talk 常駐程式：**按住錄音鍵 → 說話 → 放開 → 文字自動出現**。

這是「能不能取代原廠工具」的核心驗證。

偵測方式（重要設計決定）：
    只用 **Raw Input 一條通道**。因為 Raw Input 同時給了我們
      - 哪個裝置送的（`hDevice`，可解析出 interface path）
      - 哪個鍵（`VK_CONTROL` + `E0` 旗標 = Right Ctrl）
    `WH_KEYBOARD_LL` 只有在**要抑制按鍵**時才需要，而抑制是可選的品質改善
    （Ctrl 單獨按住不輸入任何字元）。不用 hook 也就不會拖慢全系統輸入。

    注意：hook 看不到裝置，這是它的先天限制；靠 hook 做裝置辨識是不可能的。

流程（`docs/architecture.md` 的狀態機）：
    IDLE ──按下錄音鍵──> RECORDING ──放開──> PROCESSING ──> INSERTING ──> IDLE

用法:
    python app/core/ptt.py --list-devices           # 確認要認哪個裝置
    python app/core/ptt.py --device-filter 00001124 # 只認藍牙裝置的 Right Ctrl
    python app/core/ptt.py --dry-run                # 只顯示事件，不注入文字
    python app/core/ptt.py --traditional            # 輸出繁體
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent          # app/core
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "third_party"))

# 重用 P0 已經驗證過的 Raw Input 管線（結構定義、註冊、解析）
import hotkey  # noqa: E402
from keycode_logger import (  # noqa: E402
    RAWKEYBOARD,
    RAWINPUTDEVICE,
    RAWINPUTHEADER,
    RIDEV_INPUTSINK,
    RID_INPUT,
    RIM_TYPEHID,
    RIM_TYPEKEYBOARD,
    WM_INPUT,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    device_info,
    device_label,
    device_name,
    list_raw_input_devices,
    setup_console,
    user32,
    kernel32,
)

from recorder import Capture  # noqa: E402
from speech_engine import (  # noqa: E402
    EngineNotConfigured,
    EngineUnavailable,
    build_engine,
    has_opencc,
    pcm_to_samples,
    strip_trailing_period,
    to_traditional,
)
from textin import inject_text  # noqa: E402

HWND_MESSAGE = wintypes.HWND(-3)
VK_CONTROL = 0x11
RI_KEY_E0 = 0x02          # 右側修飾鍵（Right Ctrl 的判別依據）

# ⚠️ 「同一家族但 VK 不同的鍵要視為相等」與左右側判定，現在都住在
# `hotkey.py`（`vk_matches()` / `spec_hit()` / `event_side()`）。
# 原本這裡自己有一份 `_VK_FAMILIES`，但支援**多顆錄音鍵**之後需要
# 「這顆鍵命中了哪幾組、每一組的修飾鍵條件是否都成立」的完整語意，
# 兩份實作一定會漂移 —— 所以單一真相來源放在 hotkey.py。


class PttDaemon:
    """按住錄音鍵說話，放開後把文字注入前景視窗。"""

    # 裝置列舉的節流間隔（秒）。`waveInGetDevCaps` 是逐台呼叫的，
    # 沒必要每個迴圈（5ms）都問一次。
    _DEVICE_POLL_S = 2.0

    def __init__(self, engine, device_filter: str | None = None,
                 key_vk: int = VK_CONTROL, require_e0: bool = True,
                 traditional: bool = True, mode: str = "auto",
                 dry_run: bool = False, min_s: float = 0.2,
                 max_s: float = 60.0, device_index: int = 1, debug: bool = False,
                 on_state=None, on_result=None, device_provider=None,
                 remove_period: bool = True, cfg_provider=None,
                 config_path: Path | None = None, config_loader=None,
                 target_online_probe=None):
        self.engine = engine
        self.device_filter = device_filter.lower() if device_filter else None
        self.key_vk = key_vk
        self.require_e0 = require_e0
        # 可自訂的錄音鍵（由設定檔決定；見 app/core/hotkey.py）。
        # ⚠️ 是**多組**：使用者可以同時綁藍牙麥克風的 RightCtrl 與鍵盤的 F9。
        # `key_vk` / `require_e0` 保留給純終端機模式，沒有 cfg_provider 時才用。
        self._hotkey_bindings = None
        self._hotkey_raw: tuple[str, ...] | None = None
        self._hotkey_error: str | None = None
        # 舊名稱（單一規格）—— 只為相容外部讀取，值是「第一組」。
        self._hotkey_spec = None
        # 純終端機模式（沒有 cfg_provider）用的單鍵設定：
        # 把建構參數轉成「一組」綁定，這樣後面的比對邏輯只有一條路徑。
        self._fallback_spec = hotkey.HotkeySpec(
            vk=key_vk, modifiers=frozenset(),
            side=("right" if require_e0 else None), raw="")
        self._fallback_binding = hotkey.HotkeyBindings([self._fallback_spec])
        # 目前按住的修飾鍵（自己維護 —— Raw Input 只給單一事件，
        # 不給「現在 Ctrl 有沒有按住」）
        self._mods_down: set[str] = set()
        # 觸發狀態機（hold / toggle / double，**每一組按鍵各自**）
        self._capturing = False
        # 「等待第二下」的時間戳，**以規格為 key**（每組雙擊鍵各自獨立）
        self._tap_at: dict = {}
        # 目前按下的主鍵（VK 集合）—— 用來吃掉作業系統的**自動重複**。
        # ⚠️ 沒有這一層的話：按住一顆 double 鍵不放，OS 每 ~30ms 送一次
        # keydown，會被當成連續點擊 → 反而觸發錄音。
        self._main_down: set[int] = set()
        self._double_window = 0.4           # 秒；double 模式的兩下間隔上限
        self.traditional = traditional
        self.remove_period = remove_period
        self.mode = mode
        self.dry_run = dry_run
        self.min_s = min_s
        self.max_s = max_s
        self.device_index = device_index
        # 每次錄音時重新解析裝置，讓「切換麥克風」不必重啟程式。
        # 傳 None 就用固定的 device_index（純終端機模式）。
        self.device_provider = device_provider
        self.cfg_provider = cfg_provider
        # 設定檔路徑 + 上次看到的 mtime：用來偵測「使用者手改檔案」並即時套用
        self.config_path = config_path
        self.config_loader = config_loader    # 由呼叫端注入，避免 ptt 直接依賴 config 模組
        self._cfg_mtime: float | None = None
        # 「設定指定的麥克風現在在不在？」由呼叫端注入（見 _tick_device）
        self.target_online_probe = target_online_probe
        self._target_seen_online: bool | None = None
        self._device_checked_at = 0.0
        # 執行期事實（給診斷用）——回答「這個行程到底有沒有在跑 tick」。
        # 實際踩到：狀態頁顯示 target_online=None，但程式碼明明是對的，
        # 查不出是「程式碼沒被執行」還是「舊行程」。把自己數到的次數報出來，
        # 一眼就能分辨。
        self._tick_counts: dict[str, int] = {}
        self.debug = debug
        # 給 UI 用的回呼（可選）。不傳就只是純終端機模式。
        self.on_state = on_state
        self.on_result = on_result

        self._set_state("IDLE")
        self.level = 0.0            # 錄音中的即時音量（0..1），UI 顯示用
        self.last_latency = None    # 最近一次的延遲分解
        self._cap: Capture | None = None
        # 麥克風串流常駐時的「這一段已經播到哪裡了」——每次讀取都往後切
        self._cap_seen = 0
        # 錄音累積緩衝：**不放開、不在這裡做逐段裁剪**，改成累積 bytearray。
        # 因為現在有兩條路徑（暖機待命 / 錄音中）共用同一次讀取（見 _tick_capture），
        # 在那裡切片比事後拼回來簡單得多。
        self._rec_pcm = bytearray()
        # 暖機待命的「前捲」：待命期間最近 _PREROLL_S 秒的音訊
        self._preroll = b""
        self._preroll_bytes = 0
        # 串流剛開的時間點（暖機寬限用，見 _WARM_GRACE_S）
        self._warm_deadline = 0.0
        self._rate = 16000
        self._mic_device: int | None = None   # 目前串流開在哪個裝置（UI 顯示用）
        self._idle_at: float | None = None   # 上次放開的時間（idle_timeout 用）
        self._rec_started = 0.0
        self._pressed = False
        # 主鍵現在是不是按著（**跨「開始錄音 → 放開」兩個時間點**都要看得到，
        # 因為自動重錄是在「放開的瞬間」才決定的，見 _retry_empty_capture）
        self._key_held = False
        # ⚠️ 命名有點反直覺，但這是最不容易搞錯的寫法：
        #   `_retry_armed`＝這一次錄音**還沒**用掉自動重錄的機會
        #   `_retrying`  ＝這一次錄音**正在**自動重錄中（第二次之後不再重試）
        self._retry_armed = False
        self._retrying = False
        # 結束鍵的狀態（見 _handle_key）：
        #   `_end_armed`  ＝結束鍵按下了（行為是「鬆開才停」）→ keyup 收尾
        #   `_end_pressed`＝結束鍵剛觸發過（其他行為，已經在 down 收尾）
        # 兩者都是為了讓 **keyup 不會重複收尾**（放開不該再停一次）。
        self._end_armed = False
        self._end_pressed = False
        # 「測試這個組合」模式（見 start_key_test）：時間戳 + 收到的按鍵
        self._test_until = 0.0
        self._test_which = ""
        self._test_hits: list[dict] = []
        self._lock = threading.Lock()
        self._wndproc_ref = None
        self._hwnd = None
        self.stats = {"presses": 0, "inserted": 0, "failed": 0, "empty": 0,
                      "retried": 0}
        self._msg_count = 0
        self._input_count = 0
        self._last_unknown_dev: dict[int, dict] = {}

    # ------------------------------------------------ 裝置判定
    def current_device(self) -> int:
        """現在要用的錄音裝置索引。

        每次錄音都重新問一次 —— 使用者在設定介面換麥克風之後**不必重啟**。
        設定檔變更、裝置重新連線（藍牙斷了又回來）都會在這裡反映出來。
        """
        if self.device_provider is not None:
            try:
                idx = self.device_provider()
                if isinstance(idx, int) and idx >= 0:
                    self.device_index = idx
            except Exception as exc:
                if self.debug:
                    print(f"  · 解析錄音裝置失敗，沿用 index {self.device_index}：{exc}")
        return self.device_index

    def _is_target_device(self, hdevice) -> tuple[bool, str]:
        key = int(hdevice or 0)
        info = self._last_unknown_dev.get(key)
        if info is None:
            info = {"name": device_name(hdevice), **device_info(hdevice)}
            self._last_unknown_dev[key] = info
        name = info.get("name", "") or ""
        if not self.device_filter:
            return True, device_label(name)
        if self.device_filter in name.lower():
            return True, device_label(name)
        return False, device_label(name)

    def _live(self) -> dict:
        """即時讀取設定。

        有 `cfg_provider` 就每次重新問 —— 這樣在 UI 改「繁體輸出／移除句號／
        注入方式」之後**不必重啟**。沒有 provider（純終端機模式）就用建構時的值。

        ⚠️ 純終端機模式的錄音鍵是**空清單**：那時按鍵來自建構參數
        （`key_vk` / `require_e0`，見 `_bindings()`），不是設定檔的字串。
        """
        fallback = {"traditional": self.traditional,
                    "remove_period": self.remove_period,
                    "mode": self.mode,
                    "mic_stream": "per_press",
                    "idle_timeout_s": 7.0,
                    "hotkeys": [],
                    "trigger_mode": "hold",
                    "double_tap_ms": 400}
        if self.cfg_provider is None:
            return fallback
        try:
            c = self.cfg_provider()
            # 錄音鍵有兩個欄位（新清單 hotkeys / 舊字串 hotkey）。
            # 判定邏輯放在 Config.effective_hotkeys()，這裡不重寫一份 ——
            # 沒有它就退回舊字串，這樣純終端機模式與舊設定檔都不會壞。
            if hasattr(c, "effective_hotkeys"):
                keys = list(c.effective_hotkeys())
            else:
                one = getattr(c, "hotkey", None)
                keys = [one] if one else []
            return {"traditional": bool(getattr(c, "traditional", True)),
                    "remove_period": bool(getattr(c, "remove_trailing_period", True)),
                    "mode": getattr(c, "mode", "auto") or "auto",
                    "mic_stream": getattr(c, "mic_stream", "per_press") or "per_press",
                    "idle_timeout_s": float(getattr(c, "idle_timeout_s", 7.0) or 7.0),
                    "hotkeys": keys,
                    "trigger_mode": getattr(c, "trigger_mode", "hold") or "hold",
                    "double_tap_ms": int(getattr(c, "double_tap_ms", 400) or 400)}
        except Exception:
            return fallback

    # ------------------------------------------------ 麥克風串流生命週期
    # 為什麼要有三種模式：藍牙的 A2DP（播放）與 SCO（麥克風）在同一顆無線電上
    # 互斥，開麥克風會把耳機踢掉（docs/hardware.md §2.2–2.5，已實測）。
    # 開串流的**時機**是我們唯一能控制的事，所以做成可選：
    #
    #   per_press   每次按下才開、放開就關   → 播放最不受影響，但每按一次都斷
    #   session     整段常開到程式結束       → 只斷一次，但期間完全沒聲音
    #   idle_timeout 放開後等 N 秒才關       → 連講好幾句不會被斷，停下來聲音就回來
    def _close_capture(self, why: str = "") -> None:
        """關掉麥克風串流（並清掉常駐狀態）。任何路徑都安全。

        `why` 只影響除錯輸出。**為什麼要記原因**：實測踩到「串流被反覆關閉又
        重開」卻查不出是誰關的 —— 三個呼叫點（模式切換、裝置變更、idle 逾時、
        程式結束）長得都一樣。有了原因，日誌就能直接指出兇手。
        """
        cap, self._cap = self._cap, None
        self._cap_seen = 0
        self._mic_device = None
        self._preroll = b""
        if cap is not None:
            if self.debug:
                print(f"  (debug) 關閉麥克風串流（{why or '未標明'}）", flush=True)
            try:
                cap.__exit__(None, None, None)
            except Exception as exc:
                if self.debug:
                    print(f"  · 關閉麥克風串流失敗：{exc}")

    def _ensure_capture(self) -> Capture:
        """取得擷取物件，並讓它進入**暖機待命**狀態。

        ## 三種模式的差別（實測回饋修正後）

        | 模式 | 串流何時開 | 待命時 |
        |---|---|---|
        | `per_press` | **一直開著**（待命） | 暖機待命 + 前捲，按下立刻有音 |
        | `session` | 一直開著 | 同上 |
        | `idle_timeout` | 一直開到逾時 | 逾時後關閉，下次按下才開 |

        ⚠️ **`per_press` 的語意改過了**（2026-09）。原本是「按下才開、放開就關」，
        但實測顯示那等於**每次按下都重新協商 SCO**，第一次幾乎一定拿到 0 bytes，
        使用者只看到「第一段經常無法成功」。
        現在改成待命（麥克風一直開），代價是 Windows 會一直顯示麥克風使用中 ——
        這件事已經寫進 UI 的選項說明，讓使用者自己取捨。
        """
        mode = self._live()["mic_stream"]
        want = self.current_device()
        if self._cap is not None:
            have = getattr(self._cap, "device_id", None)
            if have == want:
                return self._cap
            # ⚠️ 這裡是「每按一次就重開串流」的頭號嫌疑：裝置索引一變就重開。
            # 除錯輸出要能讓我們分辨是「索引真的變了」還是「比對邏輯有問題」。
            if self.debug:
                print(f"  (debug) 串流 device_id={have} 與目前解析到的 {want} 不符"
                      f" → 關掉重開", flush=True)
            self._close_capture("裝置索引變更")

        cap = Capture(want, rate=self._rate, max_seconds=self.max_s)
        cap.__enter__()
        self._mic_device = want
        self._cap = cap
        self._cap_seen = len(cap.recorded())
        self._preroll_bytes = int(self._rate * self._PREROLL_S) * 2  # 16-bit mono
        # ⚠️ **前捲一定要清掉。** 前捲是「舊裝置」的音訊；換裝置之後它已經
        # 沒有意義（而且開頭會混進另一支麥克風的聲音）。
        # 實測症狀：藍牙麥克風省電休眠後回來、裝置從 1 換到 0，
        # 下一次錄音只有 `音訊 0.25s`（＝舊裝置的前捲），之後一個 byte 都沒有。
        self._preroll = b""
        # 串流剛開（或剛換裝置）→ 給它一點時間真的開始送音訊（見 _finish_recording）
        self._warm_deadline = time.monotonic() + self._WARM_GRACE_S
        if self.debug:
            print(f"  (debug) 開啟麥克風串流（mode={mode}, device={want}）"
                  f" → 暖機待命", flush=True)
        return cap

    def _tick_idle(self, timeout_s: float) -> None:
        """`idle_timeout` 模式：放開後閒置超過 N 秒就關掉串流，讓聲音回來。

        ⚠️ **必須先檢查模式。** 實測踩到的 bug：這個函式原本只檢查
        「有沒有開串流、是不是 IDLE、過了多久」，**沒有檢查模式** ——
        於是 `session` 模式（明明叫「開著不關」）放開 `idle_timeout_s`
        秒之後也會被關掉。

        症狀很誤導：`session` 看起來「有時候有生效、有時候沒有」，
        因為它只在「按下 → 放開 → 5 秒內」這段時間才真的常駐。
        使用者早先測到的「session 零中斷」是真的 —— 那時他連續操作，
        每次都還沒超過閒置時間。**一次停止交談就會破功。**
        """
        if self._live()["mic_stream"] != "idle_timeout":
            return
        if self._idle_at is None or self._cap is None:
            return
        if self.state != "IDLE":            # 錄音／辨識中都別關
            return
        if time.monotonic() - self._idle_at >= timeout_s:
            self._close_capture(f"閒置逾時 {timeout_s:g}s")
            self._idle_at = None

    # 常駐串流的緩衝區回收門檻：累積到 `max_s` 的這個比例就回收。
    # 為什麼不能等它「滿」才回收 —— 滿了之後 `waveIn` 已經沒有 header 可寫，
    # **擷取就停了**，那時回收也救不回中間那段空窗。
    _BUFFER_RECYCLE_AT = 0.5

    # 暖機待命的「前捲」長度（秒）。
    #
    # 為什麼要留前捲：藍牙的 SCO 音訊連線是**串流開始時**才協商，實測要
    # 0.1–0.3 秒才有第一個 byte。就算串流開著，這段時間按下的音也會掉。
    # 留一小段前捲，等於「把按下前 0.25 秒的聲音也一起送進辨識」——
    # 使用者感覺到的就是「第一次按就錄到了」。
    #
    # ⚠️ 不要留太長：前捲會出現在錄音開頭。0.25 秒的雜訊（或上一個字尾）
    # 對 ASR 無害，但 2 秒的環境音就會變成幻覺的來源。
    _PREROLL_S = 0.25

    # 串流剛開（或剛換裝置）後的「暖機寬限」：這段時間內如果完全沒收到音訊，
    # 不要急著說「太短」，直接**自動重試一次**（重開串流往往就好了）。
    #
    # 實測情境：藍牙麥克風省電休眠後回來、裝置索引從 1 換到 0，
    # 下一次錄音的 `音訊 0.25s`（＝前捲）→ 被當成「太短」而沒有辨識。
    _WARM_GRACE_S = 1.5

    def _tick_capture(self) -> None:
        """串流的心跳：**讀新音訊 → 暖機待命（含前捲）或累積錄音 → 必要時回收**。

        ## 為什麼要「暖機待命」（實測回饋：「第一段錄音經常無法成功」）

        原本 `per_press` 是「按下才開串流、放開就關」，暖機跑完就關掉 ——
        於是**每次按下都要重新協商一次 SCO**，第一次幾乎一定來不及
        （log 裡就是 `音訊 0.00s`，靠自動重錄才救回來）。

        現在改成：串流一直開著（`_ensure_capture` 在 IDLE 時也會開），
        待命期間把音訊餵進「前捲」環形緩衝區，按下時連前捲一起交給辨識。
        這樣第一次按下就不再是特例。

        ⚠️ 代價要講清楚（UI 的 `per_press` 說明已改）：麥克風會一直處於
        開啟狀態（Windows 會顯示「使用中」）。不想要的人可以選別的取捨 ——
        但「第一次一定失敗」不是合理的預設。

        ⚠️ 回收（`recycle`）只在 IDLE 做：錄音中回收會丟掉正在錄的音。
        """
        cap = self._cap
        if cap is None:
            return
        try:
            rec = cap.recorded()
        except Exception as exc:
            if self.debug:
                print(f"  · 讀取擷取緩衝區失敗：{exc}")
            return
        # ⚠️ 用**舊的**索引判斷要不要回收：那才是「上一輪讀完之後緩衝區裡
        # 累積了多少」。更新後才判斷的話，數值會被這一輪的新音訊左右。
        was = self._cap_seen
        if len(rec) < was:
            # 緩衝區被回收過（`dwBytesRecorded` 歸零）→ 從頭開始算。
            # 沒有這一行的話「回收次數 × 緩衝區大小」會被誤判成新音訊。
            was = 0
        new = rec[was:]
        self._cap_seen = len(rec)

        # ⚠️ 回收判斷要**在「沒有新音訊就 return」之前** ——
        # 緩衝區滿的時候往往正是「沒有新音訊」的那一輪（擷取已經停了），
        # 先 return 就永遠不會回收，也就永遠不會恢復。
        if self.state == "IDLE":
            limit = getattr(cap, "max_bytes", 0)
            if limit and len(rec) >= limit * self._BUFFER_RECYCLE_AT:
                ok = False
                try:
                    ok = bool(cap.recycle())
                except Exception as exc:
                    if self.debug:
                        print(f"  · 緩衝區回收失敗：{exc}")
                if ok:
                    self._cap_seen = 0
                else:
                    # 回收失敗 → 只能關掉重開。留著的話緩衝區會填滿、
                    # 擷取停止，之後每一段都拿到空音訊（比重新協商更糟）。
                    self._close_capture("緩衝區回收失敗")
                    return

        if not new:
            return

        if self.state == "IDLE":
            keep = int(self._preroll_bytes)
            self._preroll = (self._preroll + new)[-keep:] if keep else b""
        else:
            self._rec_pcm.extend(new)
            # 保險：萬一沒收到放開事件，不要讓緩衝區無限長大
            cap_bytes = int(self._rate * self.max_s) * 2
            if len(self._rec_pcm) > cap_bytes:
                del self._rec_pcm[:-cap_bytes]

    def _tick_buffer(self) -> None:
        """舊介面（`run()` 的 tick 名稱）：轉呼叫 `_tick_capture()`。

        保留是因為它已經出現在 `_tick_counts` 的診斷輸出裡 ——
        狀態頁的「這個行程有沒有在跑」就是靠那些計數。
        """
        self._tick_capture()

    def _sync_config(self) -> None:
        """偵測 `config.toml` 被改動就重新載入（讓手改檔案也能生效）。

        ## 為什麼需要這個

        實際踩到：使用者在 UI 把 `mic_stream` 改成 `session`，卻聽到跟
        `idle_timeout` 一樣的結果 —— 因為他改的是**檔案**，而 app 只在啟動時
        讀一次設定。**執行中的行程完全不知道檔案變了。**

        更糟的是後續的「儲存設定」：UI 把表單上的值寫回檔案，
        就把他手改的 session 覆蓋掉了 —— 使用者看到的是「我的設定被吃掉了」。

        ## 為什麼要就地更新（不能整個換掉）

        `cfg_provider` 與 UI 都握著**同一個物件**。若改成 `self.cfg = 新物件`，
        provider 還指著舊的 → 兩邊不同步（UI 顯示 A、實際跑 B）。
        所以這裡逐欄位覆蓋，物件身分不變。

        ⚠️ 只在 mtime 真的變了才重讀，而且**忽略解析錯誤** ——
        使用者可能正存到一半，讀到壞檔不該讓程式崩掉。

        載入器由呼叫端注入（`config_loader`），這樣 `ptt.py` 不必直接
        import `config` 模組 —— 否則就得賭 `app/` 剛好在 sys.path 上。
        """
        if self.config_path is None or self.config_loader is None:
            return
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        if self.cfg_provider is None:
            return
        try:
            fresh = self.config_loader(self.config_path)
        except Exception as exc:
            if self.debug:
                print(f"  · 設定檔重讀失敗（忽略）：{exc}")
            return
        target = self.cfg_provider()
        if target is None:
            return
        changed = []
        # ⚠️ 這份清單就是「手改檔案會不會生效」的完整定義。
        # 實測踩到：漏了 `model_dir` → 使用者把 config.toml 改成粵語專門模型，
        # `ptt.py` 這邊（模式、裝置）都跟著變了，**但模型沒有** ——
        # 執行中的 app 一直用啟動時載入的舊模型，使用者聽到的卻是舊模型的效果，
        # 於是誤判「換模型沒用」。**只要改了設定檔就是改了，全部都要同步。**
        for name in ("traditional", "remove_trailing_period", "mode",
                     "mic_stream", "idle_timeout_s", "device_index", "mic_name",
                     "mic_names", "hotkeys", "hotkey", "trigger_mode",
                     "double_tap_ms", "engine", "model_dir", "language", "threads"):
            if not hasattr(fresh, name):
                continue
            new = getattr(fresh, name)
            if getattr(target, name, None) != new:
                setattr(target, name, new)
                changed.append(f"{name}={new}")
        if changed and self.debug:
            print(f"  · 設定檔已重新載入：{', '.join(changed)}")

    def config_saved(self, path: Path | None = None) -> None:
        """告訴 daemon「這個檔案是**我們自己**剛寫的，別當成外部修改」。

        ## 為什麼需要這個（實測踩到，症狀是模型反覆重新載入）

        「UI 儲存設定」與「偵測外部修改」原本會互相打架：

          1. 使用者在 UI 選了新模型 → handler 改記憶體、`cfg.save()` 寫檔案
          2. 主迴圈的 `_sync_config()` 看到 mtime 變了 → 從**磁碟**重讀
          3. 若磁碟上的值與記憶體不同（例如檔案寫入與 POST 的競態，
             或使用者其實沒按儲存），它會**把記憶體的值蓋回舊的**
          4. `_tick_model()` 看到不一致 → 重新載入**舊模型**
          5. 下一次 UI 儲存 → 回到步驟 1

        結果是日誌出現模型反覆切換、每次都要重建引擎（數百毫秒），
        而且使用者聽到的是「不知道現在跑哪個模型」。

        ## 做法

        應用程式自己存檔後呼叫這個方法，把 mtime 記下來 —— 下次
        `_sync_config()` 看到同一個 mtime 就會直接略過，不會自己覆蓋自己。
        **真正的外部編輯（mtime 又變了）仍然會被偵測到**，功能不受影響。
        """
        p = path or self.config_path
        if p is None:
            return
        try:
            self._cfg_mtime = Path(p).stat().st_mtime
        except OSError:
            pass

    def _tick_model(self) -> None:
        """設定檔換了模型就重新載入引擎（只在待命時做）。

        ## 為什麼需要這個

        `SpeechEngine._ensure_loaded()` **只在第一次載入** —— 它不會因為
        `model_dir` 被改就換模型。所以光把設定同步過來是不夠的，
        必須有人主動呼叫 `engine.reload()`。

        實際踩到的症狀：使用者把 `config.toml` 改成粵語專門模型，
        設定檔同步了（模式、裝置都變了），**但模型還是舊的** ——
        他聽到的是舊模型的效果，於是誤判「換模型沒用、尾音字還是掉」。
        這種「一半生效」的狀態最難查，因為畫面上看不出來。

        ⚠️ **只在 IDLE 時換**：推論中把 recognizer 換掉會讓正在跑的辨識
        拿到半個狀態（`speech_engine.reload()` 的 docstring 有寫）。
        """
        if self.engine is None or self.cfg_provider is None:
            return
        if self.state != "IDLE":
            return
        try:
            want = str(getattr(self.cfg_provider(), "model_dir", "") or "")
        except Exception:
            return
        if not want:
            return
        cur = getattr(self.engine, "model_dir", None)
        cur_name = Path(cur).name if cur else ""
        if cur_name == want or want == getattr(self, "_model_pending", None):
            return
        # 避免模型不存在時每 5ms 重試一次、把錯誤洗掉
        self._model_pending = want
        base = Path(cur).parent if cur else Path("models")
        target = base / want
        try:
            self.engine.reload(target)
            print(f"  · 已切換模型：{want}", flush=True)
        except Exception as exc:
            print(f"  ⚠️ 切換模型失敗（沿用目前模型）：{exc}", flush=True)

    def _tick_device(self) -> None:
        """目標麥克風回來時，**自動**跟上（不必等下一次按下）。

        ## 為什麼需要這個

        藍牙麥克風閒置久了會進**省電休眠**、從系統消失（實測 `AI_VOICE_MAX`
        會變 UNPLUGGED）。裝置離線期間，`resolve_device()` 會退回設定檔的
        `device_index`（通常是別的麥克風）—— 這本身是對的，總比完全不能錄好。

        但先前的行為是**被動**的：裝置回來之後，要等使用者**下一次按下**
        才會重算索引並換回去。使用者感覺到的就是「它醒了，但程式還在用錯的
        麥克風」，而且完全沒有提示。

        ## 做什麼

        每 `_DEVICE_POLL_S` 秒問一次「目標麥克風在不在」：

          · 不在 → 記下來（`_target_seen_online=False`），什麼都不做，
            讓現有的退路繼續運作
          · 從不在變成在（**醒過來**）→ 關掉目前（退路的）串流，
            下次錄音會用正確的裝置重開

        ⚠️ 只在**待命**時動作：錄音或辨識中換裝置會切掉正在錄的音。
        ⚠️ 裝置列舉不是免費的（`waveInGetDevCaps` 逐台呼叫），
           所以有節流，不是每個迴圈都問。
        """
        if self.target_online_probe is None:
            return
        now = time.monotonic()
        if now - self._device_checked_at < self._DEVICE_POLL_S:
            return
        self._device_checked_at = now

        try:
            online = bool(self.target_online_probe())
        except Exception:
            return

        was = self._target_seen_online
        self._target_seen_online = online
        if online and was is False:
            # 目標麥克風回來了。
            #
            # ⚠️ **不要在這裡主動關掉串流。** 第一版就是那樣寫的，結果每次
            #    「睡著 → 醒來」都主動多斷一次（關串流 → 藍牙重新協商）。
            #    使用者的需求是「它上線時照我設定的順序就好」，不是「一醒來
            #    就立刻換手」。
            #
            # 正確做法是把索引更新好，讓**下一次按下**自然用對的裝置：
            # `_ensure_capture()` 會發現目前的串流不是目標裝置而換掉它 ——
            # 那時機是使用者主動操作，中斷是合理的、也是預期的。
            if self.state == "IDLE":
                self.device_index = self.current_device()
            print(f"  · 目標麥克風已重新上線 → 下次錄音會用它"
                  f"（device {self.device_index}）", flush=True)
        elif not online and was is True:
            print("  · 目標麥克風離線（省電休眠？）—— 先沿用目前的裝置",
                  flush=True)

    # ------------------------------------------------ 錄音 / 辨識 / 注入
    def _set_state(self, state: str) -> None:
        self.state = state
        if self.on_state:
            try:
                self.on_state(state)
            except Exception:
                pass

    def poll_level(self) -> float:
        """錄音中即時音量（0..1），給 UI 的音量表用。"""
        cap = self._cap
        if cap is None:
            return 0.0
        data = cap.recorded()
        n = len(data) // 2
        if n < 160:                      # 少於 10ms 就不算
            return self.level
        import struct as _s
        tail = data[-min(len(data), 16000):]        # 只看最近 0.5 秒
        m = len(tail) // 2
        s = _s.unpack(f"<{m}h", tail[:m * 2])
        rms = (sum(v * v for v in s) / m) ** 0.5
        # 對應到 0..1（-60 dBFS 以下當 0）
        import math as _m
        db = 20.0 * _m.log10(rms / 32768.0) if rms > 0 else -99.0
        self.level = max(0.0, min(1.0, (db + 60.0) / 60.0))
        return self.level

    def _start_recording(self, label: str) -> None:
        try:
            self._ensure_capture()      # 待命中就直接沿用（暖機過）
        except Exception as exc:
            print(f"  ❌ 無法開始錄音：{exc}")
            self._close_capture("開始錄音失敗")
            self._set_state("IDLE")
            return
        # ⚠️ 錄音緩衝＝**前捲**（待命期間最近 0.25 秒）+ 之後的新音訊。
        # 前捲是為了蓋掉 SCO 剛建立時那一小段空窗（實測 0.1–0.3 秒），
        # 沒有它「第一次按下」就會掉開頭的字。
        self._rec_pcm = bytearray(self._preroll)
        self._preroll = b""
        self._key_held = True                # 主鍵還按著（自動重錄要用）
        self._idle_at = None                 # 有動作了，取消閒置計時
        self._rec_started = time.monotonic()
        self._set_state("RECORDING")
        print(f"  🔴 錄音中…（{label}）", flush=True)

    def _retry_empty_capture(self, pressed: bool) -> bool:
        """同一口氣重開串流重錄（回傳 True 表示已經重新開始錄音）。

        用在「按著講了，卻一個 byte 都沒收到」的情況 —— 藍牙的 SCO 音訊連線
        是**串流開始時**才建立，第一次常常來不及。重開一次就會好。

        為什麼是「同一口氣」而不是「請使用者再按一次」：
            實測回饋是「經常第一段錄音無法錄製」。請他再按一次，等於
            第一句話已經白講了（而且他不會知道要重講）。
            重開串流只要零點幾秒，使用者還按著的那一段就救得回來。

        `pressed` 由呼叫端（`_finish_recording`，在清掉 `_key_held` 之前）
        算好傳進來 —— 使用者已經放開就不該重錄，那會錄到他放開之後的環境音。

        ## 兩個旗標（名字很像，但缺一不可）

        | 旗標 | 意思 | 誰設 | 誰清 |
        |---|---|---|---|
        | `_retry_armed` | 這一次錄音**還沒**用掉重錄機會 | `_on_main_down`（按下時） | 這裡（用掉時） |
        | `_retrying` | **正在**重錄中（第二次之後不再重試） | 這裡 | `_on_main_down`（下次按下） |

        ⚠️ 為什麼不能只用一個：`_start_recording`（重錄時也會呼叫）不能被當成
        「使用者又按了一次」而重新武裝 —— 否則裝置真的收不到音訊時，
        「重錄 → 又空 → 重錄」會變成無窮迴圈，統計數字還會一直往上加。
        """
        if self._retrying or not self._retry_armed or not pressed:
            return False                # 已經重試過，或使用者早就放開了
        self._retrying = True           # 第二次之後不再重試
        self._retry_armed = False       # 機會用掉了（只有再按一次才會武裝回來）
        print("  ↻  重新開啟麥克風串流（暖機待命還是來不及建立藍牙音訊連線）…",
              flush=True)
        try:
            self._close_capture("SCO 未建立，重開")
            self._start_recording("自動重試")
        except Exception as exc:
            print(f"  ❌ 自動重試失敗（{exc}）—— 請再按一次錄音鍵")
            self._set_state("IDLE")
            return False
        return True

    def _finish_recording(self) -> None:
        """放開（或被結束鍵收尾）：把這次錄到的音訊交給辨識。

        ⚠️ 串流**不再關閉**（除非 idle_timeout 逾時）—— 暖機待命是
        「第一次按下就能錄到」的前提（見 `_ensure_capture`）。
        """
        cap, self._cap = self._cap, None
        if cap is None:
            self._set_state("IDLE")
            return
        # ⚠️ 必須在清掉 `_key_held` **之前**算好 —— 自動重錄要靠它判斷
        # 「使用者是不是還按著」（見 `_retry_empty_capture`）。
        pressed = self._pressed or self._key_held
        self._pressed = False
        self._key_held = False
        duration = time.monotonic() - self._rec_started
        pcm = bytes(self._rec_pcm)
        self._rec_pcm = bytearray()
        # 放回串流：它是暖機待命的那一個，不能消失。
        # 舊 bug 的教訓：這裡若因為 early return 而漏掉，session 模式會永久失效。
        self._cap = cap
        self._cap_seen = len(cap.recorded())
        self._idle_at = time.monotonic()

        n = len(pcm) // 2
        secs = n / 16000
        print(f"  ⏹  停止（{duration:.1f}s，音訊 {secs:.2f}s）")
        if n == 0:
            # 實測：啟動後第一次按下常常收到 0 bytes。
            # 藍牙 HFP 的 SCO 音訊連線要等串流真的開始才建立，
            # 在那之前 waveIn 拿不到任何資料。第二次之後就正常。
            #
            # ⚠️ 但「請再按一次」是**把問題丟給使用者**（實測回饋：「經常第一段
            # 錄音無法錄製」）。而且他按第二次時，第一句早就講完了。
            # 所以這裡**同一口氣自動重錄**：重新開一次串流（＝重新協商 SCO），
            # 使用者還按著的那一段就不會白講。
            #
            # 統計口徑：第一次的空白**不算失敗**（救回來了，記在 `retried`）；
            # 重試之後還是空白才算真的失敗。
            if self._retry_armed:
                self.stats["retried"] = self.stats.get("retried", 0) + 1
            else:
                self.stats["failed"] += 1
            if self._retry_empty_capture(pressed):
                return
            print("  ⚠️ 完全沒收到音訊 —— 藍牙音訊連線可能還沒建立好")
            print("     （暖機時若也逾時，請確認裝置已連線、未被原廠工具獨占；"
                  "或用 --device-index 確認裝置索引）")
            self._set_state("IDLE")
            return
        if secs < self.min_s:
            # ⚠️ 兩種情況要分開看（實測踩到）：
            #   · 串流剛開（或剛換裝置）就按下 → 那個「太短」不是誤觸，
            #     是**新串流還沒開始送音訊**。重開一次通常就好，所以先重試。
            #   · 串流早就暖好了還太短 → 真的按太快，維持原本的「視為誤觸」。
            if (self._retry_armed and pressed
                    and time.monotonic() < self._warm_deadline):
                print(f"  ⏭  只收到 {secs:.2f}s（串流剛開／裝置剛換）"
                      f"→ 自動重試一次")
                self.stats["retried"] = self.stats.get("retried", 0) + 1
                if self._retry_empty_capture(pressed):
                    return
            print(f"  ⏭  太短（{secs:.2f}s < {self.min_s}s），視為誤觸，不辨識")
            self._set_state("IDLE")
            return

        self._set_state("PROCESSING")
        t0 = time.perf_counter()
        try:
            result = self.engine.transcribe(pcm_to_samples(pcm), 16000)
        except (EngineNotConfigured, EngineUnavailable) as exc:
            print(f"  ❌ 辨識失敗：{exc}")
            self.stats["failed"] += 1
            self._set_state("IDLE")
            return
        except Exception as exc:
            print(f"  ❌ 辨識發生未預期錯誤：{type(exc).__name__}: {exc}")
            self.stats["failed"] += 1
            self._set_state("IDLE")
            return
        asr_ms = (time.perf_counter() - t0) * 1000

        text = (result.text or "").strip()
        if not text:
            print(f"  ⚠️ 沒有辨識出文字（{asr_ms:.0f} ms）")
            self.stats["empty"] += 1
            self._set_state("IDLE")
            return

        live = self._live()
        out = to_traditional(text) if (live["traditional"] and has_opencc()) else text
        if live["remove_period"]:
            out = strip_trailing_period(out)
            if not out:
                # 整句只有句號（模型聽到雜音時會這樣）→ 當成空結果，不要注入
                print("  ⚠️ 移除結尾句號後沒有內容，不注入")
                self.stats["empty"] += 1
                self._set_state("IDLE")
                return
        print(f"  📝 {out}   （辨識 {asr_ms:.0f} ms）")

        if self.dry_run:
            print("  （--dry-run：不注入）")
            self._set_state("IDLE")
            return

        self._set_state("INSERTING")
        res = inject_text(out, mode=live["mode"], verbose=True)
        if res["ok"]:
            self.stats["inserted"] += 1
            print(f"  ✅ 已注入（{res['method']}）"
                  f"{'' if res['clipboard_restored'] else ' ⚠️ 剪貼簿未還原'}")
        else:
            self.stats["failed"] += 1
            print(f"  ❌ 注入失敗：{res['detail']}")

        # 分開回報：使用者感覺到的是「放開 → 文字出現」，
        # 也就是辨識 + 送出貼上；剪貼簿還原發生在之後，看不到。
        perceived = asr_ms + res.get("paste_ms", 0.0)
        self.last_latency = {"total_ms": round(perceived), "asr_ms": round(asr_ms),
                             "paste_ms": round(res.get("paste_ms", 0.0))}
        print(f"  ⏱  放開→文字出現 {perceived:.0f} ms"
              f"（辨識 {asr_ms:.0f} + 貼上 {res.get('paste_ms', 0):.0f}）"
              f"，剪貼簿還原另計 {res.get('restore_ms', 0):.0f} ms\n")
        if self.on_result and res["ok"]:
            try:
                self.on_result({"text": out, "ms": round(perceived),
                                "chars": len(out), "method": res["method"]})
            except Exception:
                pass
        self._set_state("IDLE")

    # ------------------------------------------------ Raw Input
    def _bindings(self):
        """目前生效的錄音鍵（**多組**、每組各自帶觸發方式，跟著設定檔更新）。

        為什麼不寫死、為什麼是多組：使用者要能完全脫離硬體 —— 用鍵盤、
        用別的鍵、用組合鍵都要能動，而且**可以同時綁好幾組**（藍牙麥克風的
        RightCtrl ＋ 鍵盤的 F9），每組的觸發方式也可以不同
        （按住說話 vs 雙擊），不必每次換情境就進設定頁改。

        ⚠️ 解析失敗時的行為：`hotkey.bindings()` 會保留**認得的那幾組**，
        並回傳原因。這裡把原因記在 `_hotkey_error`（一路傳到 UI），
        不是靜默退回預設值 —— 那會讓使用者以為設定生效了。
        """
        raw = tuple(self._live()["hotkeys"] or [])
        if raw == self._hotkey_raw and self._hotkey_bindings is not None:
            return self._hotkey_bindings
        if not raw and self.cfg_provider is None:
            # 純終端機模式：沒有設定檔，用建構時給的 `key_vk` / `require_e0`。
            # （有 cfg_provider 時 `_live()` 一定會給至少一組，不會走到這裡。）
            self._hotkey_raw = raw
            self._hotkey_bindings = self._fallback_binding
            self._hotkey_spec = self._fallback_spec
            return self._hotkey_bindings
        bind, err = hotkey.bindings(list(raw), fallback=hotkey.DEFAULT_SPEC,
                                    default_mode=self._live()["trigger_mode"])
        self._hotkey_raw = raw
        self._hotkey_bindings = bind
        self._hotkey_spec = bind.specs[0] if bind.specs else None
        # 綁定換了就清掉「等待第二下」的殘留狀態 —— 舊的配對屬於舊的鍵。
        self._tap_at = {}
        if err and err != self._hotkey_error:
            self._hotkey_error = err
            print(f"  ⚠️ 錄音鍵設定有問題（{err}）—— 目前生效的是 "
                  f"{bind.label}", flush=True)
        elif not err:
            self._hotkey_error = None
        if self.debug:
            print(f"  (debug) 錄音鍵 {list(raw)} → {bind.label}", flush=True)
        return bind

    def _mode_of(self, spec) -> str:
        """這一組的觸發方式（預設按住說話）。"""
        return self._bindings().mode_of(spec)

    def _end_owner(self, vk: int, e0: bool):
        """這顆鍵是不是某一組的**結束鍵**？回傳那一組的開始規格。

        配對語法：`F9,Esc` ＝ 開始 F9、結束 Esc（見 `hotkey.parse_binding`）。
        ⚠️ 只看**啟用中**的組合（停用的不該有反應）。
        """
        b = self._bindings()
        for spec in b.active:
            end = b.end_of(spec)
            if end is None:
                continue
            if self._spec_matches(end, vk, e0) and self._mods_ok(end, vk):
                return spec
        return None

    # ------------------------------------------------ 測試這個組合
    def start_key_test(self, seconds: float = 20.0, which: str = "") -> dict:
        """開始「測試」：聽按鍵，但**不錄音、不注入**，只回報收到什麼。

        為什麼要做（實測回饋：「除了沒有測試之外其他基本上都像是我想要的」）：
        設定好一顆鍵之後，使用者唯一的驗證方式是「按下去看有沒有反應」——
        但那會真的開始錄音、注入文字到他正在打的地方。
        所以需要一個**只聽不做**的模式：按下去，畫面回報
        「✓ 收到 F9，會開始錄音」，不會有任何副作用。

        `which`＝只測這一組的開始鍵（空字串＝測全部）。
        """
        self._test_until = time.monotonic() + max(3.0, float(seconds))
        self._test_which = which
        self._test_hits = []
        if self.debug or True:
            print(f"  🧪 測試模式：請按你要測的按鍵（{seconds:.0f} 秒內有效；"
                  f"不會錄音）", flush=True)
        return {"ok": True, "seconds": seconds, "which": which}

    def cancel_key_test(self) -> None:
        self._test_until = 0.0
        self._test_hits = []
        self._test_which = ""

    def _note_test_hit(self, vk: int, e0: bool, hits: list, down: bool) -> None:
        """測試模式下收到一個事件 → 記下來（給 UI 顯示）。"""
        if not down or time.monotonic() > self._test_until:
            return
        b = self._bindings()
        # 這顆鍵的「角色」：開始鍵？結束鍵？還是不相關？
        roles: list[str] = []
        for spec in b.active:
            if self._spec_matches(spec, vk, e0) and self._mods_ok(spec, vk):
                roles.append(f"開始鍵（{b.label_of(spec)}）")
            end = b.end_of(spec)
            if end is not None and self._spec_matches(end, vk, e0) \
                    and self._mods_ok(end, vk):
                roles.append(f"結束鍵（{b.label_of(spec)}）")
        if not hits and not roles:
            # 沒命中的鍵也報出來 —— 「按了沒反應」時最需要知道的就是
            # 「有沒有收到」，而不是只有「沒有」。
            roles = ["不相關的鍵（不會觸發）"]
        self._test_hits.append({
            "vk": vk, "e0": e0, "down": down,
            "mods": sorted(self._mods_down),
            "roles": roles,
            "at": time.strftime("%H:%M:%S"),
        })
        if self.debug:
            print(f"  🧪 測試收到 vk=0x{vk:02X} e0={e0} → {roles}", flush=True)

    def test_state(self) -> dict:
        """測試模式的狀態（給 UI 輪詢）。"""
        now = time.monotonic()
        running = bool(self._test_until) and now < self._test_until
        if self._test_until and not running:
            self._test_until = 0.0            # 逾時自動結束
        return {
            "running": running,
            "remaining": round(max(0.0, self._test_until - now), 1) if running else 0.0,
            "which": self._test_which,
            "hits": list(self._test_hits)[-5:],
        }

    def _finish_explicit(self) -> None:
        """由**明確的結束鍵**收尾（不是靠開始行為，也不是靠放開開始鍵）。"""
        self._capturing = False
        self._pressed = False
        self._key_held = False          # 使用者不是靠放開結束的 → 不自動重錄
        self._end_armed = False
        self._end_pressed = False
        self._finish_recording()

    def _spec_matches(self, spec, vk: int, e0: bool) -> bool:
        """這顆鍵是不是這一組的主鍵？

        詳細的 VK 家族對應與左右側判定都在 `hotkey.spec_hit()`
        （單一真相來源 —— 這裡不再自己實作一份）。
        """
        return hotkey.spec_hit(spec, vk, e0)

    def _mods_ok(self, spec, main_vk: int | None = None) -> bool:
        """這一組的修飾鍵是否都按住了（不多也不少）。

        `main_vk` 用來處理「主鍵本身就是修飾鍵」的情況（例如單獨一顆
        RightCtrl 當錄音鍵）—— 那時它會出現在 `_mods_down` 裡，
        但它不是「需要另外按住的修飾鍵」，所以要排除掉再比對。
        """
        return hotkey.mods_ok(spec, self._mods_down)

    def _toggle_capture(self, label: str) -> None:
        """toggle / double 模式的「開始 ↔ 停止」。

        ⚠️ 停止時要把 `_key_held` 清掉：這兩種模式的錄音是「按一下開始」，
        使用者早就放開了，若留著 True，之後的自動重錄會以為他還按著，
        變成對著空氣重錄一段（而且錄到的是環境音）。
        """
        if self._capturing or self._cap is not None:
            self._capturing = False
            self._pressed = False
            self._key_held = False
            self._finish_recording()
        else:
            self._capturing = True
            self.stats["presses"] += 1
            self._start_recording(label)

    def _on_main_down(self, spec, label: str) -> None:
        """主鍵按下。**依這一組自己的觸發方式**決定要做什麼。

        ⚠️ 為什麼模式是「每一組」而不是全域：實測回饋「無法錄製雙擊」——
        真實情境是「藍牙麥克風按住說話 ＋ 鍵盤 F9 雙擊」。
        全域一個模式做不到，而兩組各自的狀態也必須分開記
        （`_tap_at` 用規格當 key，見 `tick_trigger`）。
        """
        mode = self._mode_of(spec)
        if mode == hotkey.MODE_TOGGLE:
            self._toggle_capture(label)
            return
        if mode == hotkey.MODE_DOUBLE:
            # 單擊不動作，等第二下（模仿 macOS 的聽寫手勢）
            now = time.monotonic()
            last = self._tap_at.get(spec, 0.0)
            if last and (now - last) <= self._double_window:
                self._tap_at[spec] = 0.0        # 用掉這次配對
                self._retry_armed = True        # 新的錄音開始 → 重新武裝自動重錄
                self._retrying = False
                self._toggle_capture(label)
            else:
                self._tap_at[spec] = now
            return
        # hold（預設）：按住就開始
        if not self._capturing:
            self._capturing = True
            self._retry_armed = True            # 新的錄音開始 → 重新武裝自動重錄
            self._retrying = False
            self.stats["presses"] += 1
            self._start_recording(label)

    def _on_main_up(self, spec) -> None:
        """主鍵放開。

        ⚠️ **只有 `hold` 模式該在這裡動作。** toggle / double 是「按下才算
        一次」，若放開也處理，按一下就會「開始＋馬上停」——
        實測被測試抓到（第一下按完 `_capturing` 又變回 False）。

        ⚠️ **而且設定了結束鍵時也不能在放開時停**：`F9,Escape` 的意思是
        「按 F9 開始、按 Escape 結束」，F9 放開只代表「開始鍵按完了」。
        這條規則讓「用同一顆鍵的組合鍵當開始鍵」也說得通
        （`Ctrl+Alt+R` 放開 R 不該結束錄音）。
        """
        b = self._bindings()
        if b.end_of(spec) is not None:
            return                      # 收尾交給結束鍵
        if self._mode_of(spec) == hotkey.MODE_HOLD and self._capturing:
            self._capturing = False
            self._finish_recording()

    def tick_trigger(self) -> None:
        """double 模式的計時器：超過配對視窗就清掉「等待第二下」的狀態。

        ⚠️ 必須由主迴圈定期呼叫。沒有它，double 模式會永遠配不到第二下。
        ⚠️ 待配對的狀態是**每一組各記一份**（`_tap_at`）。用單一變數的話，
        在兩顆雙擊鍵之間輪流按就會互相蓋掉（A 一下、B 一下 → B 變成雙擊）。
        """
        now = time.monotonic()
        for spec, at in list(self._tap_at.items()):
            if at and (now - at) > self._double_window:
                self._tap_at[spec] = 0.0

    def _handle_key(self, hdevice, kb: RAWKEYBOARD) -> None:
        vk = int(kb.VKey)
        e0 = bool(int(kb.Flags) & RI_KEY_E0)
        down = kb.Message in (WM_KEYDOWN, WM_SYSKEYDOWN)

        # 修飾鍵狀態要**先**更新 —— 主鍵的判定會用到它。
        # Raw Input 只送單一事件，不告訴我們「現在 Ctrl 有沒有按住」。
        mod_name = hotkey.MODIFIER_VKS.get(vk)
        if mod_name:
            if down:
                self._mods_down.add(mod_name)
            else:
                self._mods_down.discard(mod_name)

        # 多組錄音鍵：只要有**任何一組**（啟用中的）主鍵與修飾鍵條件成立就觸發。
        # 標籤取第一組命中的，只為了讓除錯輸出說得出「是哪一組」。
        hits = [s for s in self._bindings().active
                if self._spec_matches(s, vk, e0) and self._mods_ok(s, vk)]

        # 「測試這個組合」模式：只回報「有沒有收到」，**絕不錄音**
        # （見 `start_key_test`）。放在最前面，因為測試時不該有任何副作用。
        # ⚠️ 時間判斷要在這裡做（不能只看 `_test_until` 有沒有值）——
        # 過期後那個時間戳還留著，只判斷「有值」會讓錄音功能**永遠失效**。
        if self._test_until and time.monotonic() < self._test_until:
            self._note_test_hit(vk, e0, hits, down)
            if self._test_hits:
                return

        # ⚠️ **結束鍵優先於行為**（實測回饋：「也可以接受按鍵不一樣做
        # 開始和結束的配對」）。使用者在設定裡明確寫了結束鍵（`F9,Esc`）時，
        # 就以那顆鍵為準 —— 這時「開始行為」只是開始的方式，不再決定怎麼收尾。
        # 結束鍵自己有行為（`F9,Esc@hold`＝**鬆開**才停、`@toggle`＝再按一下停）。
        if not hits and self._capturing:
            owner = self._end_owner(vk, e0)
            if owner is not None:
                end_mode = self._bindings().end_mode_of(owner)
                # down 才動作（toggle / double 的判斷也走同一條路）
                if down:
                    if end_mode == hotkey.MODE_HOLD:
                        # 「鬆開才停」→ 先記下來，等 keyup 再收尾
                        self._end_armed = True
                        if self.debug:
                            print(f"  · 結束鍵按下（{self._bindings().label_of(owner)}）"
                                  f"→ 等鬆開才停")
                    else:
                        self._end_pressed = True
                        if self.debug:
                            print(f"  · 結束鍵 {self._bindings().label_of(owner)} → 停止錄音")
                        with self._lock:
                            self._finish_explicit()
                elif self._end_armed or self._end_pressed:
                    self._end_armed = False
                    self._end_pressed = False
                    if self.debug:
                        print(f"  · 結束鍵鬆開 → 停止錄音")
                    with self._lock:
                        self._finish_explicit()
                return

        if not hits:
            self._main_down.discard(vk)     # 沒命中的鍵不該留在按下集合裡
            return
        spec = hits[0]
        target, label = self._is_target_device(hdevice)

        # 被拒絕的時候要說得出原因，否則只能看到「按了沒反應」。
        # 實測踩過：送合成的 Ctrl 進來時 make=0、裝置無名、沒有 E0，
        # 結果整個事件被靜默丟棄，完全查不出為什麼。
        #
        # ⚠️ 修飾鍵不符原本也在這裡回報，現在改由上面的 `hits` 一起判斷
        # （多組的情況下「修飾鍵不符」不再是單一原因，而是「沒有一組成立」）。
        # 裝置篩選只在「用裝置原生鍵」時才有意義。使用者若刻意把錄音鍵
        # 改成別的鍵（完全脫離硬體），就不該再要求裝置符合 filter。
        if not target and self.device_filter and not self._allow_any_device():
            if self.debug:
                print(f"  · 忽略 {label} 的 {spec.label}（不是目標裝置）")
            return

        # 作業系統的自動重複（按住不放時每 ~30ms 一次 keydown）要吃掉。
        # ⚠️ 這對 double 模式是**必要**的：重複事件會落在配對視窗內，
        # 被誤認為「按了兩下」，於是按住不放反而開始錄音。
        if down:
            if vk in self._main_down:
                if self.debug:
                    print(f"  · 忽略自動重複 {spec.label}")
                return
            self._main_down.add(vk)
        else:
            self._main_down.discard(vk)

        if self.debug:
            print(f"  · 接受 {spec.label}（{self._mode_of(spec)}）"
                  f"{'DOWN' if down else 'UP  '} "
                  f"flags=0x{int(kb.Flags):X} dev={label} capturing={self._capturing}")

        with self._lock:
            if down:
                self._on_main_down(spec, label)
            else:
                self._on_main_up(spec)

    def _allow_any_device(self) -> bool:
        """按鍵設定是否允許來自任何裝置。

        判準：**有任一組**錄音鍵「不是裝置原生那顆」時，就代表使用者想用
        別的來源（鍵盤、滑鼠側鍵…）。這種情況不該再要求裝置符合
        `device_filter` —— 否則「完全脫離硬體」做不到。

        裝置原生鍵的定義：VK_CONTROL + 限定右側 + 沒有其他修飾鍵。
        """
        return self._bindings().accepts_any_device

    def _wndproc(self, hwnd, msg, wparam, lparam):
        self._msg_count += 1
        if msg == WM_INPUT:
            self._input_count += 1
            try:
                size = wintypes.UINT(0)
                hdr_size = ctypes.sizeof(RAWINPUTHEADER)
                user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), hdr_size)
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    got = user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                                                 hdr_size)
                    if got not in (0, 0xFFFFFFFF):
                        hdr = RAWINPUTHEADER.from_buffer(buf)
                        if hdr.dwType == RIM_TYPEKEYBOARD:
                            self._handle_key(hdr.hDevice,
                                             RAWKEYBOARD.from_buffer(buf, hdr_size))
            except Exception as exc:
                print(f"  ⚠️ 解析 raw input 失敗：{exc!r}", file=sys.stderr)
            return 0
        if msg == 0x0002:                       # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self):
        from keycode_logger import WNDPROC

        hinst = kernel32.GetModuleHandleW(None)
        class_name = "VibeTalkiePtt"

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):
                raise ctypes.WinError(err)
        hwnd = user32.CreateWindowExW(0, class_name, "ptt", 0, 0, 0, 0, 0,
                                      HWND_MESSAGE, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    def _register(self) -> None:
        # 只註冊鍵盤即可 —— 錄音鍵是標準鍵盤 collection 的 Right Ctrl
        devs = (RAWINPUTDEVICE * 1)()
        devs[0].usUsagePage = 0x01
        devs[0].usUsage = 0x06
        devs[0].dwFlags = RIDEV_INPUTSINK
        devs[0].hwndTarget = self._hwnd
        if not user32.RegisterRawInputDevices(devs, 1, ctypes.sizeof(RAWINPUTDEVICE)):
            raise ctypes.WinError(ctypes.get_last_error())

    def warmup_audio(self, timeout: float = 6.0, keep: bool | None = None) -> float | None:
        """先把麥克風開起來、等到**真的收到音訊**，然後**留著它**（暖機待命）。

        為什麼需要？
            實測：程式啟動後**第一次**按下錄音鍵會收到 `音訊 0.00s`
            （完全沒有資料），第二次之後就正常。
            藍牙 HFP 的 SCO 音訊連線是在串流真的開始時才建立，
            在那之前 waveIn 拿不到任何東西。

            固定睡 0.8 秒實測還是不够（照樣 0 bytes），
            所以改成**輪詢到有資料為止**，並把實測到的延遲印出來。

        回傳首次收到音訊的秒數；逾時回 None。

        ⚠️ **不再有「暖機完就關掉」的分支。** 2026-09 的實測回饋是
        「第一段錄音經常無法成功」—— 只要每次按下都得重新協商 SCO，
        第一次就一定會掉。現在暖機完成後串流一律留著（交給 `_ensure_capture()`
        沿用同一個），待命期間由 `_tick_capture()` 維護前捲。

        `keep` 參數只是為了相容舊呼叫端，現在傳什麼都不會把串流關掉。
        """
        del keep
        try:
            print(f"  ⏳ 暖機麥克風（等藍牙音訊連線，最多 {timeout:.0f}s）…",
                  end="", flush=True)
            cap = self._ensure_capture()             # 開好並登記成待命串流
            t0 = time.monotonic()
            # ⚠️ 「已經有音訊」要算成功（暖機只是確認連線通了）。
            # 只比「有沒有比開始時多」的話，暖機前就累積了音訊的情況
            # 會一直等到逾時 —— 明明連線早就好了（實測被測試抓到）。
            first: float | None = 0.0 if len(cap.recorded()) > 0 else None
            while first is None and time.monotonic() - t0 < timeout:
                if len(cap.recorded()) > 0:
                    first = time.monotonic() - t0
                    break
                time.sleep(0.05)
        except Exception as exc:
            print(f" 失敗：{exc}")
            print("     錄音裝置可能被占用。若第一次按下收不到音訊，請確認裝置。")
            return None

        if first is None:
            print(f" 逾時（{timeout:.0f}s 內沒收到任何音訊）")
            print("     請確認裝置已連線、未被其他程式獨占（原廠工具要關掉）。")
        else:
            print(f" 完成，首次收到音訊花了 {first:.2f}s（串流留著待命）")
        return first

    def run(self, seconds: float | None = None) -> None:
        self._hwnd = self._create_window()
        self._register()
        # ⚠️ 暖機**發生在主迴圈之前**，所以一定要先把設定檔讀進來 ——
        # 否則 `_live()` 只會給後備值，`mic_stream` 永遠看起來是 `per_press`，
        # 暖機就把剛建好的 SCO 連線關掉（實測踩到：暖機明明成功，
        # 第一次按下卻還是 0 bytes）。
        self._sync_config()
        self.warmup_audio()
        if self.debug:
            print(f"  (debug) 視窗 hwnd={int(self._hwnd or 0)}，raw input 已註冊"
                  f"（usage 0x01/0x06，RIDEV_INPUTSINK）")
        # ⚠️ 提示要說出「這顆鍵怎麼開始、怎麼結束」。
        # 實測回饋：「未看見能夠結束喎」—— 因為提示只寫「按住…放開」，
        # 而使用者可能設的是 toggle / double，那時「放開」根本不是結束動作。
        # 按鍵與觸發方式都是可設定的，提示就不該寫死一種說法。
        modes = {hotkey.MODE_HOLD: "按住說話、放開結束",
                 hotkey.MODE_TOGGLE: "按一下開始、再按一下結束",
                 hotkey.MODE_DOUBLE: "連按兩下開始、再連按兩下結束"}
        bl = self._bindings()
        desc = "；".join(f"{s.label.split('·')[0]}＝{modes.get(m, m)}"
                         for s, m in bl.pairs())
        print("準備完成。請把游標放到記事本，然後開始錄音：")
        print(f"　{desc}")
        print("（Ctrl+C 結束）\n")
        msg = wintypes.MSG()
        deadline = time.monotonic() + seconds if seconds else None
        next_beat = time.monotonic() + 2.0
        try:
            while True:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0012:
                        return
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                if self.debug and time.monotonic() > next_beat:
                    next_beat = time.monotonic() + 2.0
                    print(f"  (debug) 收到訊息 {self._msg_count} 個，"
                          f"其中 WM_INPUT {self._input_count} 個", flush=True)
                if deadline and time.monotonic() > deadline:
                    break
                # 手改 config.toml 也要生效（不然設定會被「儲存設定」覆蓋掉）
                self._tick_counts["sync"] = self._tick_counts.get("sync", 0) + 1
                self._sync_config()
                # 設定檔換了模型就在待命時換掉引擎（否則只會換一半）
                self._tick_counts["model"] = self._tick_counts.get("model", 0) + 1
                self._tick_model()
                # 目標麥克風（藍牙）醒來就自動跟上
                self._tick_counts["device"] = self._tick_counts.get("device", 0) + 1
                self._tick_device()
                # idle_timeout 模式：閒置夠久就把串流關掉，讓藍牙播放回來
                self._tick_counts["idle"] = self._tick_counts.get("idle", 0) + 1
                self._tick_idle(self._live()["idle_timeout_s"])
                # 常駐串流的緩衝區累積到一半就回收（否則擷取會停掉）
                self._tick_counts["buffer"] = self._tick_counts.get("buffer", 0) + 1
                self._tick_buffer()
                # double 觸發模式的配對計時器（沒有它會永遠配不到第二下）
                self._tick_counts["trigger"] = self._tick_counts.get("trigger", 0) + 1
                self.tick_trigger()
                time.sleep(0.005)
        except KeyboardInterrupt:
            print("\n（使用者中斷）")
        finally:
            self._close_capture("程式結束")
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
            print(f"\n統計：按下 {self.stats['presses']} 次，"
                  f"注入成功 {self.stats['inserted']}，"
                  f"空結果 {self.stats['empty']}，失敗 {self.stats['failed']}"
                  + (f"，自動重錄 {self.stats['retried']}"
                     if self.stats.get("retried") else ""))


def cmd_list(needle: str | None) -> int:
    for d in list_raw_input_devices():
        name = d.get("name", "")
        mark = ""
        if needle and needle.lower() in name.lower():
            mark = "  ← 符合"
        print(f"  {device_label(name):<14} {name}{mark}")
    print("\n提示：藍牙 HID 的 interface path 不含裝置名，用 --device-filter 00001124")
    return 0


def _load_config_for_cli():
    """命令列模式也讀 `config.toml`。回傳 `(cfg, path)`，讀不到就 `(None, None)`。

    ## 為什麼要讀（實測踩到）

    使用者用 `python app/core/ptt.py` 測試時，log 出現「第一次按下收不到音訊」，
    但他的設定檔明明寫著 `mic_stream = "session"`（開著不關）。
    原因就是**命令列模式從來沒讀過設定檔** —— `_live()` 永遠拿後備值
    （`per_press`），於是暖機變成「開串流 → 等到有音訊 → 關掉」，
    等於**每次按下都要重新協商一次 SCO**，第一次當然來不及。

    ⚠️ 讀失敗只警告，不讓程式起不來 —— 設定檔壞掉不該等於不能用。
    """
    try:
        sys.path.insert(0, str(HERE.parent))        # app/
        import config as config_module              # noqa: PLC0415
        path = config_module.CONFIG_PATH
        if not path.exists():
            print(f"（沒有設定檔 {path.name}，用命令列參數與內建預設值）")
            return None, None
        cfg = config_module.Config.load(path)
        print(f"設定檔：{path}")
        keys = "、".join(cfg.effective_hotkeys()) or "（空）"
        print(f"  錄音鍵：{keys}　麥克風串流：{cfg.mic_stream}")
        return cfg, path
    except Exception as exc:
        print(f"⚠️ 讀不到設定檔（{exc}）—— 沿用命令列參數")
        return None, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1 push-to-talk 常駐程式")
    parser.add_argument("--device-filter", default=None,
                        help="只認 interface path 含此字串的裝置"
                             "（預設：設定檔，沒有設定檔才用藍牙 HID UUID）")
    parser.add_argument("--any-device", action="store_true",
                        help="不限定裝置（任何鍵盤的 Right Ctrl 都觸發）")
    parser.add_argument("--key", default="ctrl", choices=("ctrl",),
                        help="觸發鍵（目前只支援 ctrl；自訂按鍵請用設定檔）")
    parser.add_argument("--no-e0", action="store_true",
                        help="不要求 E0（左右 Ctrl 都接受）")
    parser.add_argument("--engine", default=None)
    parser.add_argument("--traditional", action="store_true", default=None)
    parser.add_argument("--simplified", dest="traditional", action="store_false",
                        help="保留模型原始的簡體輸出")
    parser.add_argument("--mode", default=None, choices=("auto", "paste", "type"))
    parser.add_argument("--dry-run", action="store_true", help="只顯示，不注入文字")
    parser.add_argument("--device-index", type=int, default=None, help="音訊裝置索引")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="顯示被忽略的 Ctrl 事件與原因（排查『按了沒反應』用）")
    parser.add_argument("--seconds", type=float, default=None, help="執行 N 秒後結束（測試用）")
    args = parser.parse_args(argv)
    setup_console()

    if args.list_devices:
        return cmd_list(args.device_filter if not args.any_device else None)

    # ⚠️ 先讀設定檔，再讓命令列參數覆蓋它（明確給的旗標優先）。
    cfg, cfg_path = _load_config_for_cli()
    engine_name = args.engine or (cfg.engine if cfg else "sherpa-onnx")
    traditional = (args.traditional if args.traditional is not None
                   else (cfg.traditional if cfg else True))
    mode = args.mode or (cfg.mode if cfg else "auto")
    device_index = args.device_index if args.device_index is not None \
        else (cfg.device_index if cfg else 1)
    device_filter = args.device_filter
    if device_filter is None:
        device_filter = "00001124" if cfg is None else None

    engine = build_engine(engine_name)
    ok, why = engine.is_available()
    if not ok:
        print(f"❌ 引擎不可用：{why}")
        return 1
    engine.warmup()
    print(f"引擎：{engine.name}（本地）　繁體輸出：{'是' if traditional else '否'}"
          f"　OpenCC：{'有' if has_opencc() else '無'}")

    daemon = PttDaemon(
        engine,
        device_filter=None if args.any_device else device_filter,
        require_e0=not args.no_e0,
        traditional=traditional,
        mode=mode,
        dry_run=args.dry_run,
        device_index=device_index,
        debug=args.debug,
        # 讓命令列模式也吃設定檔（錄音鍵、觸發方式、串流模式、輸出偏好）。
        # 沒有這一行，`_live()` 只會回後備值 —— 那正是「第一次收不到音訊」
        # 與「改了設定沒生效」的根源。
        cfg_provider=(lambda: cfg) if cfg is not None else None,
        config_path=cfg_path,
    )
    daemon.run(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
