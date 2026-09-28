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


# 同一家族但 VK 不同的鍵要視為相等。
#
# 為什麼需要：**兩種來源用的 VK 不一樣。** 實測（docs/hardware.md §3.3）：
#   · Raw Input 回報**通用** VK_CONTROL（0x11），靠 E0 旗標分左右
#   · Low-Level Hook 回報**專用** VK_RCONTROL（0xA3）
# 設定檔寫 `RightCtrl` 時，解析出來是 0x11；但事件可能帶 0xA3。
# 不做這層對應，單獨一顆 RightCtrl 就永遠觸發不了（實際踩到）。
_VK_FAMILIES: tuple[frozenset[int], ...] = (
    frozenset({0x10, 0xA0, 0xA1}),          # Shift / LShift / RShift
    frozenset({0x11, 0xA2, 0xA3}),          # Ctrl / LCtrl / RCtrl
    frozenset({0x12, 0xA4, 0xA5}),          # Alt / LAlt / RAlt
    frozenset({0x5B, 0x5C}),                # LWin / RWin
)


def _vk_matches(a: int, b: int) -> bool:
    """兩個 VK 是不是同一顆鍵（含左右通用／專用的對應）。"""
    if a == b:
        return True
    for fam in _VK_FAMILIES:
        if a in fam and b in fam:
            return True
    return False


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
        # `key_vk` / `require_e0` 保留給純終端機模式，沒有 cfg_provider 時才用。
        self._hotkey_spec = None
        self._hotkey_raw: str | None = None
        self._hotkey_error: str | None = None
        # 目前按住的修飾鍵（自己維護 —— Raw Input 只給單一事件，
        # 不給「現在 Ctrl 有沒有按住」）
        self._mods_down: set[str] = set()
        # 觸發狀態機（hold / toggle / double）
        self._capturing = False
        self._last_tap_at = 0.0
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
        # 麥克風串流常駐時的「這一段已經播到哪裡了」——每次錄音從這裡往後切
        self._cap_seen = 0
        self._mic_device: int | None = None   # 目前串流開在哪個裝置（UI 顯示用）
        self._idle_at: float | None = None   # 上次放開的時間（idle_timeout 用）
        self._rec_started = 0.0
        self._pressed = False
        self._lock = threading.Lock()
        self._wndproc_ref = None
        self._hwnd = None
        self.stats = {"presses": 0, "inserted": 0, "failed": 0, "empty": 0}
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
        """
        fallback = {"traditional": self.traditional,
                    "remove_period": self.remove_period,
                    "mode": self.mode,
                    "mic_stream": "per_press",
                    "idle_timeout_s": 7.0,
                    "hotkey": "RightCtrl",
                    "trigger_mode": "hold",
                    "double_tap_ms": 400}
        if self.cfg_provider is None:
            return fallback
        try:
            c = self.cfg_provider()
            return {"traditional": bool(getattr(c, "traditional", True)),
                    "remove_period": bool(getattr(c, "remove_trailing_period", True)),
                    "mode": getattr(c, "mode", "auto") or "auto",
                    "mic_stream": getattr(c, "mic_stream", "per_press") or "per_press",
                    "idle_timeout_s": float(getattr(c, "idle_timeout_s", 7.0) or 7.0),
                    "hotkey": getattr(c, "hotkey", "RightCtrl") or "RightCtrl",
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
        if cap is not None:
            if self.debug:
                print(f"  (debug) 關閉麥克風串流（{why or '未標明'}）", flush=True)
            try:
                cap.__exit__(None, None, None)
            except Exception as exc:
                if self.debug:
                    print(f"  · 關閉麥克風串流失敗：{exc}")

    def _ensure_capture(self) -> Capture:
        """取得一個可用的擷取物件。

        - `per_press`：每次呼叫都開一個新的（呼叫端負責關）。
        - 其他模式：串流常駐，重複使用同一個；關閉只由 idle 逾時或程式結束負責。

        為什麼要記錄 `_cap_seen`：串流常駐時 `recorded()` 會一直累積，
        所以每次開始錄音要記下「目前為止的長度」，結束時只取後面新增的部分。
        """
        mode = self._live()["mic_stream"]
        want = self.current_device()
        if mode != "per_press" and self._cap is not None:
            have = getattr(self._cap, "device_id", None)
            if have == want:
                self._cap_seen = len(self._cap.recorded())
                if self.debug:
                    print(f"  (debug) 沿用既有串流（device={have}, 已收 "
                          f"{self._cap_seen} bytes）", flush=True)
                return self._cap
            # ⚠️ 這裡是「每按一次就重開串流」的頭號嫌疑：裝置索引一變就重開。
            # 除錯輸出要能讓我們分辨是「索引真的變了」還是「比對邏輯有問題」。
            if self.debug:
                print(f"  (debug) 串流 device_id={have} 與目前解析到的 {want} 不符"
                      f" → 關掉重開", flush=True)
            self._close_capture("裝置索引變更")
        elif mode == "per_press" and self.debug and self._cap is not None:
            print(f"  (debug) per_press 模式但仍有殘留串流 → 先關掉", flush=True)
            self._close_capture("per_press 殘留")

        cap = Capture(want, rate=16000, max_seconds=self.max_s)
        cap.__enter__()
        self._mic_device = want
        if self.debug:
            print(f"  (debug) 開啟麥克風串流（mode={mode}, device={want}）", flush=True)
        if mode == "per_press":
            return cap
        self._cap = cap
        self._cap_seen = len(cap.recorded())
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

    def _tick_buffer(self) -> None:
        """常駐串流的緩衝區累積到一半就回收（**這是藍牙錄音壞掉的根因**）。

        `Capture` 只掛一個 `WAVEHDR`；填滿之後 `waveIn` 沒有 header 可寫，
        **就完全停止擷取**。一次性使用沒差，但常駐串流會一直開著 ——
        於是「串流開啟約 `max_s` 秒之後，錄到的全部是空的」。

        實測症狀：連續錄音時前幾次拿到 `0 bytes`，之後偶爾正常；
        短句還辨識得出來、長句全是垃圾。**而且串流看起來完全正常**
        （`mic_open=True`、沒有任何錯誤），只有音訊永遠是空的。

        ## 為什麼是「一半」而不是「滿了」

        滿了＝擷取已經停止，那時才回收會在中間留下空窗。提早回收則完全不影響
        任何一次錄音：每次按下時 `_cap_seen` 會重設，錄音資料在放開時就被取走，
        所以待命期間的殘留沒有人要。

        ⚠️ 只在 IDLE 時做 —— 錄音中回收會丟掉正在錄的音。
        """
        cap = self._cap
        if cap is None or self.state != "IDLE":
            return
        limit = getattr(cap, "max_bytes", 0)
        if not limit or self._cap_seen < limit * self._BUFFER_RECYCLE_AT:
            return
        try:
            if cap.recycle():
                self._cap_seen = 0
                return
        except Exception as exc:
            if self.debug:
                print(f"  · 緩衝區回收失敗：{exc}")
        # 回收失敗 → 只能關掉重開（會多一次藍牙協商，但比拿到空音訊好）
        self._close_capture("緩衝區回收失敗")

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
                     "mic_names", "hotkey", "trigger_mode", "double_tap_ms",
                     "engine", "model_dir", "language", "threads"):
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
            cap = self._ensure_capture()
        except Exception as exc:
            print(f"  ❌ 無法開始錄音：{exc}")
            self._close_capture("開始錄音失敗")
            self._set_state("IDLE")
            return
        self._cap = cap
        self._idle_at = None                 # 有動作了，取消閒置計時
        self._rec_started = time.monotonic()
        self._set_state("RECORDING")
        print(f"  🔴 錄音中…（{label}）", flush=True)

    def _finish_recording(self) -> None:
        # ⚠️ 先把「常駐模式要保留串流」這件事做完，再處理任何 early return。
        #
        # 實際踩到的 bug（很難查）：原本 `self._cap` 在這裡就被清成 None，
        # 而 `keep` 的還原寫在**後面**。於是只要走到後面的 early return
        # —— 「完全沒收到音訊」或「錄太短」——串流就**再也沒被指定回來**，
        # session 模式從此永久失效（`mic_open` 一直是 False、耳機恢復被斷），
        # 而 `_idle_at` 也沒設定，所以連閒置逾時都不會去關它。
        # 觸發條件很普通：一次沒收到音訊的按下就夠了。
        cap, self._cap = self._cap, None
        if cap is None:
            self._set_state("IDLE")
            return
        duration = time.monotonic() - self._rec_started
        stream = self._live()["mic_stream"]
        keep = stream != "per_press"          # 常駐模式：放開時**不關**串流
        try:
            if keep:
                # 串流還在跑：只取「這次按下之後新增的」那一段
                pcm = cap.recorded()[self._cap_seen:]
            else:
                cap.__exit__(None, None, None)
                pcm = cap.recorded()
        except Exception as exc:
            print(f"  ❌ 停止錄音失敗：{exc}")
            self._set_state("IDLE")
            return
        if keep:
            # 立刻放回去 —— 後面的任何 early return 都不能讓串流「消失」
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
            print("  ⚠️ 完全沒收到音訊 —— 藍牙音訊連線可能還沒建立好（見啟動時的暖機）")
            print("     請再按一次。若持續如此，用 --device-index 確認裝置索引。")
            self.stats["failed"] += 1
            self._set_state("IDLE")
            return
        if secs < self.min_s:
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
    def _spec(self):
        """目前生效的按鍵規格（跟著設定檔即時更新）。

        為什麼不寫死：使用者要能完全脫離硬體 —— 用鍵盤、用別的鍵、
        用組合鍵都要能動。解析失敗時**沿用上一個成功的規格**並記錄原因，
        不會靜默退回預設值（那會讓使用者以為設定生效了）。
        """
        raw = self._live()["hotkey"]
        if raw == self._hotkey_raw and self._hotkey_spec is not None:
            return self._hotkey_spec
        spec, err = hotkey.parse_or_default(raw)
        self._hotkey_raw = raw
        self._hotkey_spec = spec
        if err and err != self._hotkey_error:
            self._hotkey_error = err
            print(f"  ⚠️ 錄音鍵設定無法解析（{err}）—— 暫時沿用 "
                  f"{spec.label}", flush=True)
        elif not err:
            self._hotkey_error = None
        return spec

    def _spec_matches(self, vk: int, e0: bool) -> bool:
        """這顆鍵是不是設定的主鍵？

        ⚠️ **必須做「通用 ↔ 左右專用」的 VK 對應。** 這是實測踩到的 bug：

            Raw Input 回報的是**通用** VK_CONTROL（0x11）＋ E0 旗標，
            但 `hotkey.parse("RightCtrl")` 得到的是 vk=0x11（通用），
            而 `hotkey.parse("LeftCtrl")` 得到 vk=0xA2（專用）。

        兩種來源（Raw Input 與 Low-Level Hook）用的 VK 不一樣
        （見 docs/hardware.md §3.3），所以比對時要把同一家族的鍵視為相等：
        設定 `RightCtrl` → 通用 0x11 + E0 要中，專用 0xA3 也要中。
        """
        spec = self._spec()
        if not _vk_matches(vk, spec.vk):
            return False
        # 右 Ctrl 的區分：裝置原生送的是 VK_CONTROL + E0 旗標
        if spec.require_e0 and not e0:
            return False
        return True

    def _mods_ok(self, main_vk: int | None = None) -> bool:
        """設定的修飾鍵是否都按住了（不多也不少）。

        `main_vk` 用來處理「主鍵本身就是修飾鍵」的情況（例如單獨一顆
        RightCtrl 當錄音鍵）—— 那時它會出現在 `_mods_down` 裡，
        但它不是「需要另外按住的修飾鍵」，所以要排除掉再比對。
        """
        spec = self._spec()
        have = set(self._mods_down)
        if main_vk is not None and main_vk in hotkey.MODIFIER_VKS:
            have.discard(hotkey.MODIFIER_VKS[main_vk])
        return have == set(spec.modifiers)

    def _toggle_capture(self, label: str) -> None:
        """toggle / double 模式的「開始 ↔ 停止」。"""
        if self._capturing or self._cap is not None:
            self._capturing = False
            self._finish_recording()
        else:
            self._capturing = True
            self.stats["presses"] += 1
            self._start_recording(label)

    def _on_main_down(self, label: str) -> None:
        mode = self._live()["trigger_mode"]
        if mode == "hold":
            if not self._capturing:
                self._capturing = True
                self.stats["presses"] += 1
                self._start_recording(label)
            return
        if mode == "toggle":
            self._toggle_capture(label)
            return
        # double：單擊不動作，等第二下（模仿 macOS 的聽寫手勢）
        now = time.monotonic()
        if (now - self._last_tap_at) <= self._double_window:
            self._last_tap_at = 0.0            # 用掉這次配對
            self._toggle_capture(label)
        else:
            self._last_tap_at = now

    def _on_main_up(self) -> None:
        """主鍵放開。

        ⚠️ **只有 `hold` 模式該在這裡動作。** toggle / double 是「按下才算
        一次」，若放開也處理，按一下就會「開始＋馬上停」——
        實測被測試抓到（第一下按完 `_capturing` 又變回 False）。
        """
        if self._live()["trigger_mode"] == "hold" and self._capturing:
            self._capturing = False
            self._finish_recording()

    def tick_trigger(self) -> None:
        """double 模式的計時器：超過配對視窗就清掉「等待第二下」的狀態。

        ⚠️ 必須由主迴圈定期呼叫。沒有它，double 模式會永遠配不到第二下。
        """
        if self._last_tap_at and (time.monotonic() - self._last_tap_at) > self._double_window:
            self._last_tap_at = 0.0

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

        if not self._spec_matches(vk, e0):
            return

        spec = self._spec()
        target, label = self._is_target_device(hdevice)

        # 被拒絕的時候要說得出原因，否則只能看到「按了沒反應」。
        # 實測踩過：送合成的 Ctrl 進來時 make=0、裝置無名、沒有 E0，
        # 結果整個事件被靜默丟棄，完全查不出為什麼。
        if not self._mods_ok(vk):
            if self.debug:
                print(f"  · 忽略：修飾鍵不符（現在 {sorted(self._mods_down)}，"
                      f"要 {sorted(spec.modifiers)}）")
            return
        # 裝置篩選只在「用裝置原生鍵」時才有意義。使用者若刻意把錄音鍵
        # 改成別的鍵（完全脫離硬體），就不該再要求裝置符合 filter。
        if not target and self.device_filter and not self._allow_any_device():
            if self.debug:
                print(f"  · 忽略 {label} 的 {spec.label}（不是目標裝置）")
            return

        if self.debug:
            print(f"  · 接受 {spec.label} {'DOWN' if down else 'UP  '} "
                  f"flags=0x{int(kb.Flags):X} dev={label} capturing={self._capturing}")

        with self._lock:
            if down:
                self._on_main_down(label)
            else:
                self._on_main_up()

    def _allow_any_device(self) -> bool:
        """按鍵設定是否允許來自任何裝置。

        判準：使用者把錄音鍵改成「不是裝置原生的那顆」時，就代表他想用
        別的來源（鍵盤、滑鼠側鍵…）。這種情況不該再要求裝置符合
        `device_filter` —— 否則「完全脫離硬體」做不到。

        裝置原生鍵的定義：VK_CONTROL + 限定右側 + 沒有其他修飾鍵。
        """
        spec = self._spec()
        return not (spec.vk == 0x11 and spec.require_e0 and not spec.modifiers)

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

    def warmup_audio(self, timeout: float = 6.0) -> float | None:
        """先開麥克風，等到**真的收到音訊**為止，再關掉。

        為什麼需要？
            實測：程式啟動後**第一次**按下錄音鍵會收到 `音訊 0.00s`
            （完全沒有資料），第二次之後就正常。
            藍牙 HFP 的 SCO 音訊連線是在串流真的開始時才建立，
            在那之前 waveIn 拿不到任何東西。

            固定睡 0.8 秒實測還是不够（照樣 0 bytes），
            所以改成**輪詢到有資料為止**，並把實測到的延遲印出來。

        回傳首次收到音訊的秒數；逾時回 None。

        ⚠️ 在 `session` / `idle_timeout` 模式下**不關掉串流** ——
        常駐模式的重點就是「只跟無線電協商一次」，暖機後關掉等於白白多斷一次。
        """
        keep = self._live()["mic_stream"] != "per_press"
        try:
            print(f"  ⏳ 暖機麥克風（等藍牙音訊連線，最多 {timeout:.0f}s）…",
                  end="", flush=True)
            cap = Capture(self.current_device(), rate=16000, max_seconds=timeout + 2.0)
            cap.__enter__()
            t0 = time.monotonic()
            first: float | None = None
            while time.monotonic() - t0 < timeout:
                if len(cap.recorded()) > 0:
                    first = time.monotonic() - t0
                    break
                time.sleep(0.05)
            if keep:
                # 串流留著：後續每次錄音直接沿用，不再重新協商
                self._cap = cap
                self._cap_seen = len(cap.recorded())
            else:
                cap.__exit__(None, None, None)
        except Exception as exc:
            print(f" 失敗：{exc}")
            print("     錄音裝置可能被占用。若第一次按下收到 0 bytes，再按一次即可。")
            return None

        if first is None:
            print(f" 逾時（{timeout:.0f}s 內沒收到任何音訊）")
            print("     請確認裝置已連線、未被其他程式獨占（原廠工具要關掉）。")
        else:
            print(f" 完成，首次收到音訊花了 {first:.2f}s")
        return first

    def run(self, seconds: float | None = None) -> None:
        self._hwnd = self._create_window()
        self._register()
        self.warmup_audio()
        if self.debug:
            print(f"  (debug) 視窗 hwnd={int(self._hwnd or 0)}，raw input 已註冊"
                  f"（usage 0x01/0x06，RIDEV_INPUTSINK）")
        print("準備完成。請把游標放到記事本，然後**按住裝置上的錄音鍵說話**，放開後文字會自動出現。")
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
                  f"空結果 {self.stats['empty']}，失敗 {self.stats['failed']}")


def cmd_list(needle: str | None) -> int:
    for d in list_raw_input_devices():
        name = d.get("name", "")
        mark = ""
        if needle and needle.lower() in name.lower():
            mark = "  ← 符合"
        print(f"  {device_label(name):<14} {name}{mark}")
    print("\n提示：藍牙 HID 的 interface path 不含裝置名，用 --device-filter 00001124")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1 push-to-talk 常駐程式")
    parser.add_argument("--device-filter", default="00001124",
                        help="只認 interface path 含此字串的裝置（預設藍牙 HID UUID）")
    parser.add_argument("--any-device", action="store_true",
                        help="不限定裝置（任何鍵盤的 Right Ctrl 都觸發）")
    parser.add_argument("--key", default="ctrl", choices=("ctrl",),
                        help="觸發鍵（目前只支援 ctrl）")
    parser.add_argument("--no-e0", action="store_true",
                        help="不要求 E0（左右 Ctrl 都接受）")
    parser.add_argument("--engine", default="sherpa-onnx")
    parser.add_argument("--traditional", action="store_true", default=True)
    parser.add_argument("--simplified", dest="traditional", action="store_false",
                        help="保留模型原始的簡體輸出")
    parser.add_argument("--mode", default="auto", choices=("auto", "paste", "type"))
    parser.add_argument("--dry-run", action="store_true", help="只顯示，不注入文字")
    parser.add_argument("--device-index", type=int, default=1, help="音訊裝置索引")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="顯示被忽略的 Ctrl 事件與原因（排查『按了沒反應』用）")
    parser.add_argument("--seconds", type=float, default=None, help="執行 N 秒後結束（測試用）")
    args = parser.parse_args(argv)
    setup_console()

    if args.list_devices:
        return cmd_list(args.device_filter if not args.any_device else None)

    engine = build_engine(args.engine)
    ok, why = engine.is_available()
    if not ok:
        print(f"❌ 引擎不可用：{why}")
        return 1
    engine.warmup()
    print(f"引擎：{engine.name}（本地）　繁體輸出：{'是' if args.traditional else '否'}"
          f"　OpenCC：{'有' if has_opencc() else '無'}")

    daemon = PttDaemon(
        engine,
        device_filter=None if args.any_device else args.device_filter,
        require_e0=not args.no_e0,
        traditional=args.traditional,
        mode=args.mode,
        dry_run=args.dry_run,
        device_index=args.device_index,
        debug=args.debug,
    )
    daemon.run(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
