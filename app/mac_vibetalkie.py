#!/usr/bin/env python3
"""macOS 版 VibeTalkie 常駐程式：按住錄音鍵 → 錄音 → ASR → 注入。

## 狀態機（與 Windows 版完全一致）

    IDLE ──press──> RECORDING ──release──> PROCESSING ──ok──> INSERTING ──> IDLE
                        │                        │
                        └──── cancel/error ──────┴──────────> IDLE

## 這個檔案的角色

Windows 版的 `ptt.py`（1115 行）把「狀態機」與「Win32 事件迴圈」寫在一起。
mac 版**不重寫狀態機**，而是：

    keys.py            ← 可攜的按鍵名稱 ↔ 平台鍵碼
    mac_keylistener.py ← CGEventTap → KeyEvent
    mac_recorder.py    ← AVAudioEngine → PCM16 bytes
    mac_textin.py      ← NSPasteboard + Cmd+V
    mac_daemon.py      ← 這個檔案：狀態機（平台無關）

## 與 Windows 版的差異（刻意為之）

| 項目 | Windows | macOS |
|---|---|---|
| 裝置辨識 | Raw Input 的 `hDevice` | **不做**（CGEventTap 拿不到，已實測） |
| 錄音裝置 | 可選 `device_index` | 系統預設輸入（CoreAudio 決定） |
| 重採樣 | 不需要（裝置直接給 16k） | 需要（內建麥克風 44.1k） |
| 熱鍵觸發 | hold / toggle / double | hold / toggle / double（**相同**） |

裝置辨識拿掉之後，靠「錄音鍵可自由設定」達到同樣效果 ——
這正是 `hotkey.py` 開頭寫的設計原則：「完全脫離硬體綁定」。

## 用法

    python app/mac_vibetalkie.py                 # 正常啟動
    python app/mac_vibetalkie.py --debug         # 顯示每個按鍵事件
    python app/mac_vibetalkie.py --dry-run       # 辨識但不注入
    python app/mac_vibetalkie.py --list-keys     # 列出所有可用的按鍵名稱
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))

# 本機的 pip 可能壞掉；套件放在 third_party/（與 speech_engine.py 同樣的做法）
sys.path.insert(0, str(ROOT / "third_party"))

# ---------------------------------------------------------------- Python 版本
#
# ⚠️ **這一段必須在最前面，早於任何需要 3.11+ 的 import。**
#
# 實際踩到：使用者的 `python3` 是 conda 的 3.10（PATH 最前面），
# 直接跑 `python3 app/mac_vibetalkie.py` 會在 import config 時炸掉：
#
#     ModuleNotFoundError: No module named 'tomllib'
#
# `tomllib` 是 3.11 才進標準庫的。使用者看到的是一個指向 import 的錯誤，
# 完全看不出「你只是用錯 Python」。
#
# `launch.py` 已經有完整的「找一個合格的 Python 並重新執行自己」邏輯，
# 直接重用 —— 不要在這裡重寫一份（兩份清單會漂移）。
# 傳入 `__file__` 是關鍵：否則會把使用者丟回啟動器，
# 而啟動器在 macOS 上只會說「尚未支援」。
def _reexec_with_better_python() -> int | None:
    if sys.version_info >= (3, 11):
        return None
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("vt_launch", ROOT / "launch.py")
        if spec is None or spec.loader is None:
            return None
        launcher = _ilu.module_from_spec(spec)
        spec.loader.exec_module(launcher)
    except Exception as exc:                            # noqa: BLE001
        print(f"  ⚠️ 無法載入 launch.py 來尋找合格的 Python：{exc}")
        return None
    # 把「要重跑的檔案」指定成自己，不是 launch.py
    return launcher.reexec_if_python_too_old(ROOT / "app" / "mac_vibetalkie.py")


_rc = _reexec_with_better_python()
if _rc is not None:
    raise SystemExit(_rc)

if sys.version_info < (3, 11):
    # 找不到合格的 Python，而且目前的太舊 —— 講清楚，不要讓它去炸 import
    print()
    print("  ❌ 需要 Python 3.11 以上（tomllib 是 3.11 才進標準庫的）")
    print(f"     目前：{sys.version.split()[0]}　{sys.executable}")
    print()
    print("  系統上找不到其他合格的 Python。請安裝：")
    print("     brew install python@3.12")
    print("  或指定一個合格的路徑直接執行：")
    print("     /opt/homebrew/bin/python3 app/mac_vibetalkie.py")
    raise SystemExit(4)

# ---------------------------------------------------------------- 自動補齊相依
#
# ⚠️ 順序很重要：**必須在 import 那些 mac 模組之前**。
#
# `mac_keylistener` / `mac_recorder` / `mac_textin` 都會 import pyobjc，
# 所以「先 import 再檢查」的話，缺 pyobjc 時會在 import 階段就爆掉 ——
# 根本沒機會執行自動安裝。
#
# 所以：先 import 不依賴原生的東西（autosetup / config / hotkey / keys），
# 補齊缺的套件，**然後**才 import 需要 pyobjc 的模組。
import autosetup  # noqa: E402


def ensure_dependencies(auto: bool = True) -> bool:
    """檢查並（可選）安裝缺少的 Python 套件。回傳是否全部就緒。"""
    missing = autosetup.missing_packages()
    if not missing:
        return True
    if not auto:
        print("\n  ⚠️ 缺少相依套件，而且指定了 --no-install：")
        for module, pkg in missing:
            print(f"     · {module} ← {pkg}")
        print("\n  安裝方式：")
        print("     python3 app/mac_vibetalkie.py          # 讓它自己裝")
        print("     python3 tools/p1/fetch_wheels.py "
              + " ".join(p for _, p in missing))
        return False
    return autosetup.install(missing)


import config as config_module          # noqa: E402
import hotkey as hotkey_mod             # noqa: E402
import keys as keys_mod                 # noqa: E402
import trigger as trigger_mod           # noqa: E402（平台無關的觸發狀態機）
from speech_engine import (             # noqa: E402
    EngineNotConfigured,
    EngineUnavailable,
    build_engine,
    has_opencc,
    pcm_to_samples,
    strip_trailing_period,
    to_traditional,
)

# 這三個需要 pyobjc —— 由 ensure_dependencies() 確保 prerequisite 已就緒，
# 但**在 import 時期還沒跑**，所以這裡要能容忍失敗：
# 讓 `--help` 之類不需要原生的操作仍然可用，真正要用時再檢查。
try:
    import mac_keylistener as keylistener   # noqa: E402
    import mac_recorder as recorder         # noqa: E402
    import mac_textin as textin             # noqa: E402
    from mac_keylistener import KeyEvent    # noqa: E402
    NATIVE_OK = True
    NATIVE_ERR = ""
except ImportError as _exc:                 # pyobjc 還沒裝
    keylistener = recorder = textin = None  # type: ignore[assignment]
    KeyEvent = None                         # type: ignore[assignment,misc]
    NATIVE_OK = False
    NATIVE_ERR = str(_exc)

# UI 伺服器（純標準函式庫，不需要 pyobjc）—— 服務 app/ui/ 的設定頁面
import ui_server                        # noqa: E402

MIN_AUDIO_S = 0.2          # 短於這個視為誤觸（與 architecture.md §2 一致）
MAX_RECORD_S = 120.0



def setup_console() -> None:
    """把輸出切成 UTF-8 並處理 Windows 主控台（跨平台共用）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                              # noqa: BLE001
            pass


class MacPttDaemon:
    """PTT 常駐。狀態機與 Windows 版一致，平台層由注入的後端提供。"""

    def __init__(self, cfg, dry_run: bool = False, debug: bool = False,
                 min_s: float = MIN_AUDIO_S, auto_install: bool = True):
        self.cfg = cfg
        self.dry_run = dry_run
        self.debug = debug
        self.min_s = min_s
        # 模型不存在時要不要自動下載（`--no-install` 會設成 False）
        self.auto_install = auto_install
        # UI 的狀態容器（`run()` 會建立；測試可以直接注入）
        self.ui: ui_server.Status | None = None

        self.state = "IDLE"
        self.engine = None
        self.listener: keylistener.MacKeyListener | None = None

        self._cap: recorder.MacCapture | None = None
        self._lock = threading.RLock()
        self._rec_started = 0.0
        self._stop = False
        self.stats = {"inserted": 0, "empty": 0, "failed": 0, "too_short": 0}
        self.last_latency: dict | None = None
        # 錄音鍵（多組）的解析快取 —— 與 Windows 版同一套規則
        self._hotkey_raw: tuple[str, ...] | None = None
        self._hotkey_bindings = None
        self._hotkey_error: str | None = None

        # ------------------------------------------------ 觸發狀態機
        #
        # 與 Windows 版**共用同一顆引擎**（`app/core/trigger.py`）。
        #
        # ⚠️ 這裡原本有一份自己的實作（`_last_tap` ＋ `_spec_matches` ＋
        #    一個 `mode` 分支），但它只有「單一顆鍵 + 全域模式」——
        #    main 新增的「多組／開始結束配對／兩邊各自的行為／停用／測試模式」
        #    它一項都沒有。兩份實作一定會漂移，所以刪掉自己的，改用共用的。
        self.trigger = trigger_mod.TriggerEngine(
            get_bindings=self._bindings,
            on_start=self._on_trigger_start,
            on_finish=self._on_trigger_finish,
            double_window=float(getattr(cfg, "double_tap_ms", 400) or 400) / 1000.0,
            debug=debug,
        )

    # ------------------------------------------------------------ 設定

    def _bindings(self):
        """目前生效的錄音鍵（**多組**、每組各自帶觸發方式，跟著設定檔更新）。

        與 Windows 版的差別**只有資料來源**（這裡直接讀 `self.cfg`，
        Windows 版走 `_live()` 的熱重載快取）。解析規則完全共用
        —— 包含「打錯字的那一組被指出來、認得的仍然生效」。

        ⚠️ 每一次呼叫都會重讀設定（`effective_hotkeys()`），所以改
        `config.toml` 之後**不必重啟**（`test_mac_trigger.py` 有釘住這條）。
        """
        raw = tuple(getattr(self.cfg, "effective_hotkeys", lambda: [])() or [])
        if raw == self._hotkey_raw and self._hotkey_bindings is not None:
            return self._hotkey_bindings
        # ⚠️ 要先記下舊值再覆蓋 —— `err != self._hotkey_error` 這種比法在
        #    「換了綁定但錯誤訊息一樣」時會漏掉清配對（實測踩到自己的 bug）。
        changed = raw != self._hotkey_raw
        bind, err = hotkey_mod.bindings(
            list(raw), default_mode=getattr(self.cfg, "trigger_mode", "hold"))
        self._hotkey_raw = raw
        self._hotkey_bindings = bind
        self._hotkey_error = err
        # 綁定換了就清掉「等待第二下」的殘留狀態 —— 舊的配對屬於舊的鍵。
        if changed:
            self.trigger.tap_at = {}
        # 使用者可能在 UI 改過「雙擊間隔」—— 每次重讀設定時一起同步
        self.trigger.double_window = \
            float(getattr(self.cfg, "double_tap_ms", 400) or 400) / 1000.0
        if err:
            print(f"  ⚠️ 錄音鍵設定有問題（{err}）—— 目前生效的是 {bind.label}")
        return bind

    def spec(self) -> hotkey_mod.HotkeySpec:
        """目前錄音鍵的**第一組**規格（相容舊呼叫端；新程式請用 `_bindings()`）。"""
        bind = self._bindings()
        if bind.specs:
            return bind.specs[0]
        return hotkey_mod.parse(hotkey_mod.DEFAULT_SPEC)

    @property
    def _pressed(self) -> bool:
        """使用者此刻是不是按著（引擎的 `pressed`；相容舊名稱）。"""
        return self.trigger.pressed

    @_pressed.setter
    def _pressed(self, value: bool) -> None:
        self.trigger.pressed = bool(value)

    @property
    def _mods_down(self) -> set[str]:
        """目前按住的修飾鍵（引擎的狀態；相容舊名稱）。"""
        return self.trigger.mods_down

    @_mods_down.setter
    def _mods_down(self, value) -> None:
        self.trigger.mods_down = set(value)

    def _live(self) -> dict:
        c = self.cfg
        return {
            "mode": getattr(c, "mode", "auto"),
            "traditional": getattr(c, "traditional", True),
            "remove_period": getattr(c, "remove_trailing_period", True),
            "min_s": self.min_s,
        }

    # ------------------------------------------------------------ 引擎

    def ensure_engine(self):
        """建立 ASR 引擎（第一次按下時才載入，避免拖慢啟動）。

        模型不存在時會**自動下載**（除非 `self.auto_install` 為 False）。
        """
        if self.engine is not None:
            return self.engine
        model_dir = getattr(self.cfg, "model_dir", "")
        target = ROOT / "models" / model_dir
        if not target.is_dir():
            # 允許絕對路徑（測試與非標準安裝用）
            p = Path(model_dir)
            if p.is_absolute() and p.is_dir():
                target = p
            elif self.auto_install:
                got = autosetup.ensure_model(model_dir)
                if got is not None:
                    target = got
        self.engine = build_engine(
            getattr(self.cfg, "engine", "sherpa-onnx"),
            model_dir=target,
            threads=int(getattr(self.cfg, "threads", 2)),
        )
        ok, why = self.engine.is_available()
        if not ok:
            raise EngineUnavailable(why)
        self.engine.warmup()
        return self.engine

    # ------------------------------------------------------------ 狀態

    def _set_state(self, state: str) -> None:
        self.state = state
        # 同步給 UI —— 狀態燈即時反映 IDLE/RECORDING/PROCESSING/INSERTING
        if self.ui is not None:
            self.ui.on_state(state)
        if self.debug:
            print(f"  · 狀態 → {state}")

    def _sync_ui(self) -> None:
        """把常駐程式的即時數據推給 UI（每輪迴圈呼叫）。

        ⚠️ 這是**唯一的同步點**。散在各處逐一更新的話，一定會漏掉某個欄位
        （實際踩過的教訓：`snapshot()` 生產端與 UI 消費端欄位漂移，
        症狀是 UI 每 500ms 輪詢一次就噴一次 traceback）。
        """
        if self.ui is None:
            return
        # ⚠️ 走 `on_state()` 而不是直接指派 —— 它負責轉小寫
        #    （UI 的 STATE_TEXT 用小寫鍵，見 ui_server.Status.on_state）。
        #    直接指派會送大寫，UI 就顯示「未知」。
        self.ui.on_state(self.state)
        self.ui.stats = dict(self.stats)
        self.ui.latency = self.last_latency or {}
        self.ui.engine_name = getattr(self.engine, "name", "") if self.engine else ""
        self.ui.mic_open = self._cap is not None
        self.ui.mic_stream = getattr(self.cfg, "mic_stream", "per_press")
        self.ui.model_wanted = getattr(self.cfg, "model_dir", "")
        if self.engine is not None and getattr(self.engine, "model_dir", None):
            self.ui.model_loaded = Path(self.engine.model_dir).name
            self.ui.model_mismatch = bool(
                self.ui.model_wanted
                and self.ui.model_loaded
                and self.ui.model_wanted != self.ui.model_loaded)
        # 音量：只有在錄音時才有意義
        if self._cap is not None:
            try:
                self.ui.level = self._cap.poll_level()
            except Exception:                          # noqa: BLE001
                pass
        else:
            self.ui.level = 0.0

        # ---- 錄音鍵（多組）＋ 測試模式 ----
        #
        # ⚠️ 這幾個欄位是**新 UI 要求**的（`app/ui/app.js` 讀
        #    `s.hotkeys` / `s.key_enabled` / `s.hotkeys_label` /
        #    `s.hotkey_warning` / `s.key_test`）。少了它們的症狀是
        #    「畫面有欄位、後端不認」—— UI 顯示 undefined 而後端毫無錯誤。
        #    兩個平台共用同一份 UI，所以欄位名要與 Windows 版一字不差。
        bind = self._bindings()
        self.ui.hotkey_label = bind.label
        self.ui.hotkeys_label = bind.label
        self.ui.hotkey_error = self._hotkey_error
        self.ui.hotkey_warning = (f"錄音鍵設定有問題：{self._hotkey_error}"
                                  if self._hotkey_error else None)
        self.ui.key_test = self.trigger.test_state()

    # ------------------------------------------------------------ 錄音

    def _start_recording(self, label: str) -> None:
        try:
            cap = recorder.MacCapture(max_seconds=MAX_RECORD_S)
            cap.__enter__()
        except Exception as exc:                       # noqa: BLE001
            print(f"  ❌ 無法開始錄音：{exc}")
            self._set_state("IDLE")
            return
        self._cap = cap
        self._rec_started = time.monotonic()
        self._set_state("RECORDING")
        print(f"  🔴 錄音中…（{label}）", flush=True)

    def _finish_recording(self) -> None:
        cap, self._cap = self._cap, None
        if cap is None:
            self._set_state("IDLE")
            return
        duration = time.monotonic() - self._rec_started
        try:
            pcm = cap.recorded()
            err = cap.error
        except Exception as exc:                       # noqa: BLE001
            print(f"  ❌ 停止錄音失敗：{exc}")
            self._set_state("IDLE")
            return
        finally:
            cap.__exit__(None, None, None)

        if err:
            print(f"  ⚠️ 錄音期間有錯誤：{err}")

        n = len(pcm) // 2
        secs = n / 16000
        print(f"  ⏹  停止（{duration:.1f}s，音訊 {secs:.2f}s）")
        if n == 0:
            print("  ⚠️ 完全沒收到音訊 —— 檢查麥克風權限或系統輸入裝置")
            self.stats["failed"] += 1
            self._set_state("IDLE")
            return
        if secs < self.min_s:
            print(f"  ⏭  太短（{secs:.2f}s < {self.min_s}s），視為誤觸")
            self.stats["too_short"] += 1
            self._set_state("IDLE")
            return

        self._set_state("PROCESSING")
        t0 = time.perf_counter()
        try:
            engine = self.ensure_engine()
            result = engine.transcribe(pcm_to_samples(pcm), 16000)
        except (EngineNotConfigured, EngineUnavailable) as exc:
            print(f"  ❌ 辨識失敗：{exc}")
            self.stats["failed"] += 1
            self._set_state("IDLE")
            return
        except Exception as exc:                       # noqa: BLE001
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
        res = textin.inject_text(out, mode=live["mode"], verbose=self.debug)
        if res["ok"]:
            self.stats["inserted"] += 1
            print(f"  ✅ 已注入（{res['method']}）"
                  f"{'' if res['clipboard_restored'] else ' ⚠️ 剪貼簿未還原'}")
        else:
            self.stats["failed"] += 1
            print(f"  ❌ 注入失敗：{res['detail']}")

        perceived = asr_ms + res.get("paste_ms", 0.0)
        self.last_latency = {"total_ms": round(perceived), "asr_ms": round(asr_ms),
                             "paste_ms": round(res.get("paste_ms", 0.0))}
        print(f"  ⏱  放開→文字出現 {perceived:.0f} ms"
              f"（辨識 {asr_ms:.0f} + 貼上 {res.get('paste_ms', 0):.0f}）"
              f"，剪貼簿還原另計 {res.get('restore_ms', 0):.0f} ms\n")
        # 推給 UI：歷史紀錄與延遲數字
        if self.ui is not None and res["ok"]:
            self.ui.on_result({"text": out, "ms": round(perceived)})
        self._set_state("IDLE")

    # ------------------------------------------------------------ 熱鍵分派

    def _on_trigger_start(self, label: str) -> None:
        """引擎說「開始」→ 這裡只負責開麥克風。"""
        self._start_recording(label)

    def _on_trigger_finish(self, pressed: bool) -> None:
        """引擎說「停止」→ 這裡只負責收尾。

        ⚠️ `pressed` 留給 Windows 的自動重錄用；mac 版沒有那道手續
        （AVAudioEngine 的串流不像藍牙 SCO 有「第一次收不到音」的問題 ——
        那個問題在 mac 上**還沒量過**，見 AGENTS.md §7 的待驗證清單）。
        """
        self._finish_recording()

    def _spec_matches(self, ev: KeyEvent) -> bool:
        """這個事件是不是「我們要的那顆鍵」。（**相容用**，內部走引擎）

        ⚠️ 這原本是 mac 自己的一份比對實作，靠 `_main_key_name()` 把
        `HotkeySpec`（那時是 VK 中心）翻譯成名稱。現在 `HotkeySpec`
        自己就講 canonical 名稱（見 `app/core/hotkey.py`），比對也統一由
        `hotkey.spec_hit()` 負責 —— **翻譯層整段不需要了**。

        保留這個方法是為了 `tests/test_mac_trigger.py` 的舊斷言
        （「改了 config.toml 之後 f9 不再觸發」），它與新介面同義。
        """
        b = self._bindings()
        return any(hotkey_mod.spec_hit(s, ev.key) for s in b.active)

    def _allow_any_device(self) -> bool:
        """mac 版恆為 True —— 沒有裝置辨識，也不該假裝有。

        AGENTS.md §8.7 已實測：CGEventTap 的四個候選欄位（keyboardType／
        sourceUserData／sourceStateID／tabletEventDeviceID）在藍牙裝置與
        實體鍵盤上**完全相同**，做不到裝置辨識。

        引擎只照做 `Event.device_ok`（mac 恆為 True），所以這個方法存在的
        意義是「與 Windows 版形狀一致」＋讓測試能明確斷言這條限制。
        """
        return True

    def _handle_key(self, ev: KeyEvent) -> None:
        """CGEventTap 事件 → 平台無關的 `trigger.Event`（只做轉譯）。

        ⚠️ 這一層**不做任何觸發判斷** —— 判斷都在 `app/core/trigger.py`。
        mac 的 keycode **本身就分左右**（左 Ctrl=59、右 Ctrl=62），
        所以側別直接寫在 `ev.key` 裡（`"rightctrl"`），不必也不能靠旗標。
        """
        with self._lock:
            self.trigger.feed(trigger_mod.Event(
                key=ev.key, down=ev.down,
                mods=frozenset(self.trigger.mods_down),
                device=ev.device,          # macOS 恆為 None（實測無法取得）
                device_ok=True,            # 不做裝置政策（見 _allow_any_device）
                autorepeat=ev.autorepeat,
            ))

    # ------------------------------------------------------------ 主迴圈

    def run(self, seconds: float | None = None, ui: bool = True,
            ui_port: int = 8756, open_browser: bool = True) -> int:
        # ⚠️ 修飾鍵狀態由引擎持有（`self.trigger.mods_down`），這裡只清空它。
        #    直接指派 `self._mods_down = set()` 也可以（有 property setter），
        #    但寫成「清空既有的集合」更明確：引擎握著同一個物件。
        self.trigger.mods_down.clear()

        # 先把權限與引擎問題講清楚，不要等使用者按了才發現
        listener = keylistener.MacKeyListener(self._handle_key, debug=self.debug)
        ok, why = listener.check_permission()
        if not ok:
            print("\n  ❌ 無法監聽按鍵\n")
            for line in str(why).splitlines():
                print(f"  {line}")
            return 2
        self.listener = listener

        spec = self.spec()
        print(f"  錄音鍵：{spec.label}")
        print(f"  觸發方式：{getattr(self.cfg, 'trigger_mode', 'hold')}"
              f"　輸出模式：{getattr(self.cfg, 'mode', 'auto')}")
        print(f"  簡繁轉換：{'開' if getattr(self.cfg, 'traditional', True) else '關'}"
              f"（OpenCC {'可用' if has_opencc() else '**未安裝**'}）")
        if self.dry_run:
            print("  ⚠️ --dry-run：只辨識，不注入")

        # 預先載入模型 —— 否則第一次按下的延遲會多出好幾百毫秒
        print("\n  載入 ASR 模型…")
        t0 = time.perf_counter()
        try:
            self.ensure_engine()
            print(f"  ✅ 模型就緒（{(time.perf_counter()-t0)*1000:.0f} ms）")
        except Exception as exc:                       # noqa: BLE001
            print(f"  ❌ 模型載入失敗：{exc}")
            print("\n  請用下列指令下載模型：")
            print(f"    python tools/p1/fetch_model.py --get "
                  f"{getattr(self.cfg, 'model_dir', '')}")
            return 3

        # ---- UI 伺服器（設定頁面）----
        if ui:
            if self.ui is None:
                self.ui = ui_server.Status(self.cfg, platform="macos")
            self.ui.hotkey_label = spec.label
            # 麥克風名稱：UI 用它顯示「device ○○」。
            # ⚠️ 不填的話 UI 會顯示成「device 」（後面空的）——
            #    因為 app.js 的判斷是 `mic_device != null`，空字串會通過。
            self.ui.mic_device = _mic_label()
            try:
                port = ui_server.pick_port(ui_port)
                ui_server.start_server(self.ui, port)
                url = f"http://127.0.0.1:{port}/"
                print(f"\n  🖥  設定頁面：{url}")
                print("     （錄音鍵、觸發方式、模型、輸出模式都在那裡改）")
                if open_browser:
                    ui_server.open_browser(url)
            except ui_server.AlreadyRunning as exc:
                # ⚠️ 這一條要單獨處理，不要落進下面的通用 except ——
                #    「已經在執行了」不是「設定頁面壞了」，而且它會讓
                #    **兩個行程共用 config.toml 互相覆蓋**（實測症狀：
                #    設定存了又變回去、模型自己換掉）。訊息要具體可行。
                print("\n  ⚠️ 已經有另一個 VibeTalkie 在用這個 port。")
                print(f"     {exc}")
                print("\n  同時跑兩個會讓設定互相覆蓋。請先關掉舊的那一個"
                      "（⌘Q 完全結束，不是關視窗），")
                print(f"  或改用別的 port：--port {ui_port + 100}")
            except Exception as exc:                    # noqa: BLE001
                print(f"\n  ⚠️ 設定頁面啟動失敗（不影響錄音功能）：{exc}")
        else:
            print("\n  （--no-ui：不啟動設定頁面）")

        print("\n" + "=" * 66)
        print("  準備完成 —— 按住錄音鍵說話，放開就會出字")
        print("  Ctrl+C 結束")
        print("=" * 66 + "\n")

        def on_signal(signum, frame):                  # noqa: ARG001
            self._stop = True
            listener.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, on_signal)
            except Exception:                          # noqa: BLE001
                pass

        try:
            # on_tick：每輪把狀態推給 UI（狀態燈、音量表、歷史）
            listener.run(seconds=seconds, on_tick=self._sync_ui)
        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()

        s = self.stats
        print(f"\n  結束 —— 注入 {s['inserted']}、沒聽到 {s['empty']}、"
              f"太短 {s['too_short']}、失敗 {s['failed']}")
        return 0


def _mic_label() -> str:
    """目前**實際使用**的麥克風名稱（給 UI 顯示）。

    ⚠️ 要挑「標記為預設」的那一支，不是清單裡的第一支 ——
    實際踩到：取 `devs[0]` 會顯示成別支麥克風，與真正在錄音的不符。
    """
    try:
        devs = recorder.list_input_devices()
        d = next((x for x in devs if x.get("is_default")), None) or (devs[0] if devs else None)
        if d:
            rate = d.get("rate") or 0
            ch = d.get("channels") or 0
            name = d.get("name") or "系統預設輸入"
            return f"{name}（{rate} Hz / {ch}ch）" if rate else name
    except Exception:                                   # noqa: BLE001
        pass
    return "系統預設輸入裝置"


def run_mic_test(seconds: float = 4.0, model: str | None = None) -> int:
    """`--test-mic`：不按按鈕、直接錄一段並辨識。

    ## 為什麼需要這個模式

    完整迴路出問題時，原因可能是「麥克風沒收到聲音」或「ASR 有問題」或
    「熱鍵沒觸發」——三者症狀都是「按了沒反應」，分不出來。

    這個模式把熱鍵那一環拿掉：直接錄 N 秒 → 立即辨識 → 印出結果。
    這樣就能在幾秒內二分法：**看得到文字就代表錄音與 ASR 都好**，
    問題在熱鍵或注入；看不到文字就是前兩者之一。
    """
    print("=" * 66)
    print("  麥克風與 ASR 診斷（不需要按錄音鍵）")
    print("=" * 66)

    try:
        cap = recorder.MacCapture(max_seconds=seconds + 5)
        cap.__enter__()
    except Exception as exc:                           # noqa: BLE001
        print(f"\n  ❌ 無法開啟麥克風：{exc}\n")
        return 2

    print(f"\n  🔴 錄音 {seconds:.0f} 秒 —— 請說話…")
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            lv = cap.poll_level()
            bars = "█" * int(lv * 40)
            print(f"\r  音量 {lv:5.3f} {bars:<40}", end="", flush=True)
            time.sleep(0.1)
        print()
    except KeyboardInterrupt:
        print("\n  （提前結束）")
    pcm = cap.recorded()
    err = cap.error
    cap.__exit__(None, None, None)

    n = len(pcm) // 2
    secs = n / 16000
    peak = 0
    if n:
        import struct
        s = struct.unpack(f"<{n}h", pcm[:n * 2])
        peak = max(abs(v) for v in s)
    print(f"\n  收到 {n} 樣本 = {secs:.2f} 秒，峰值 {peak} "
          f"({peak / 32768 * 100:.2f}% FS)")
    if err:
        print(f"  ⚠️ 錄音期間有錯誤：{err}")

    if n == 0:
        print("\n  ❌ 完全沒收到音訊。")
        print("     最可能：麥克風權限沒開。")
        for line in recorder.microphone_hint().splitlines():
            print(f"     {line}")
        return 2

    # ⚠️ 這裡的門檻刻意設得很低（30，約 0.09% FS）。
    #    原本設 200，結果對**正常的語音**誤報 —— 實測這支麥克風講話時
    #    峰值也只有 82–110。用 200 當門檻會讓使用者以為麥克風壞了。
    #    真正該擔心的是「幾乎等於零」（權限沒開／裝置選錯）。
    if peak < 30:
        print("\n  ⚠️ 訊號幾乎是零（峰值 "
              f"{peak}）—— 可能是：")
        print("     · 麥克風權限沒開")
        print("     · 系統輸入裝置選錯（系統設定 → 聲音 → 輸入）")
        print("     · 麥克風靜音")
        print("     仍然會試著辨識，但結果僅供參考。")

    # 立刻辨識
    cfg = config_module.Config.load()
    if model:
        cfg.model_dir = model
    print("\n  辨識中…")
    try:
        daemon = MacPttDaemon(cfg)
        engine = daemon.ensure_engine()
    except Exception as exc:                           # noqa: BLE001
        print(f"  ❌ 引擎不可用：{exc}")
        return 3

    t1 = time.perf_counter()
    try:
        res = engine.transcribe(pcm_to_samples(pcm), 16000)
    except Exception as exc:                           # noqa: BLE001
        print(f"  ❌ 辨識失敗：{exc}")
        return 3
    ms = (time.perf_counter() - t1) * 1000

    text = (res.text or "").strip()
    out = to_traditional(text) if has_opencc() else text
    print(f"\n  📝 原始：{text!r}")
    print(f"  📝 繁體：{strip_trailing_period(out)!r}   （{ms:.0f} ms）")
    print()

    # ⚠️ 判斷邏輯經過兩次修正，這裡是實測學到的教訓：
    #
    #   第一版：只檢查「有沒有文字」→ 對著安靜房間跑會回 'I.'／'Yeah.'
    #            （模型對靜音的幻覺），於是誤報「一切正常」。
    #   第二版：加了「峰值必須 > 200」的門檻 → 但實測發現峰值只有 82–110
    #            時，ASR **仍然正確辨識出真實語音**
    #            （'不是應該先反思，再說下一步嗎？'）。於是變成誤報「訊號太小」。
    #
    #   峰值不是可靠的判準 —— 這支麥克風的正常語音就只有 0.3% FS。
    #   真正的判準是「輸出的內容像不像話」：
    #     長句且連貫  → 幾乎不可能是幻覺（幻覺通常是短詞或重複字元）
    #     空字串／極短 → 無法判斷
    #
    # 所以：**文字證據優先於訊號強度**，訊號只當附註。
    very_quiet = peak < 30          # 這個級別真的等於沒收到東西
    quiet = peak < 200              # 這支麥克風的正常語音就在這個範圍

    if not text:
        print("  ⚠️ 沒有辨識出文字。")
        if very_quiet:
            print("     而且訊號幾乎是零 —— 最可能是麥克風權限沒開或裝置選錯。")
            for line in recorder.microphone_hint().splitlines():
                print(f"     {line}")
        else:
            print("     訊號有進來，但認不出內容 —— 可能不是語音，或太小聲。")
            print("     請對著麥克風正常音量說一句完整的話再測一次。")
        return 1

    if len(text) <= 2:
        print(f"  ⚠️ 只辨識出 {len(text)} 個字：{text!r}")
        print("     這麼短的輸出常常是模型對噪音的幻覺，不能當成通過。")
        if very_quiet:
            print(f"     而且訊號只有峰值 {peak}（幾乎是零）→ 應該是幻覺。")
            print("     請確認麥克風權限與輸入裝置。")
        else:
            print("     請說一句完整的話再測一次。")
        return 1

    # 走到這裡：有意義的文字輸出
    if very_quiet:
        # 訊號近乎零卻吐出長句 → 可疑
        print(f"  ⚠️ 辨識出 {len(text)} 個字，但訊號只有峰值 {peak}。")
        print("     訊號這麼小卻有長輸出，有可能是幻覺。")
        print("     請再說一次並確認音量。")
        return 1

    print(f"  ✅ 錄音與 ASR 都正常（辨識出 {len(text)} 個字，"
          f"峰值 {peak}，{ms:.0f} ms）")
    if quiet:
        print(f"     （訊號偏小：峰值 {peak} = {peak / 32768 * 100:.2f}% FS。")
        print("       這支麥克風的實測正常值就是這樣，不影響辨識。）")
    print("     若完整模式按了錄音鍵沒反應，問題在熱鍵或注入，不在麥克風。")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="VibeTalkie（macOS）")
    ap.add_argument("--debug", action="store_true", help="顯示每個按鍵事件")
    ap.add_argument("--dry-run", action="store_true", help="辨識但不注入")
    ap.add_argument("--seconds", type=float, default=None, help="跑幾秒後自動結束")
    ap.add_argument("--list-keys", action="store_true", help="列出可用按鍵名稱")
    ap.add_argument("--test-mic", action="store_true",
                    help="診斷：直接錄一段並辨識（不需要按錄音鍵）")
    ap.add_argument("--mic-seconds", type=float, default=4.0,
                    help="搭配 --test-mic：錄幾秒")
    ap.add_argument("--hotkey", default=None, help="覆寫錄音鍵（例如 F9）")
    ap.add_argument("--model", default=None, help="覆寫模型目錄")
    ap.add_argument("--no-install", action="store_true",
                    help="缺少相依時只報錯，不要自動下載安裝")
    ap.add_argument("--no-ui", action="store_true",
                    help="不啟動設定頁面（純命令列模式）")
    ap.add_argument("--port", type=int, default=None,
                    help="設定頁面的 port（預設讀 config.toml 的 ui.port＝8756，"
                         "被佔用會自動往後找）")
    ap.add_argument("--no-browser", action="store_true",
                    help="不要自動開瀏覽器（config.toml 的 ui.open_browser 也可控制）")
    ap.add_argument("--check", action="store_true",
                    help="只檢查相依與模型，不啟動")
    args = ap.parse_args(argv)

    # ---- 相依與模型：缺了就自己補（可用 --no-install 關掉）----
    if not ensure_dependencies(auto=not args.no_install):
        return 4

    # import 時期 pyobjc 可能還不存在（剛剛才裝好），所以現在重新載入一次。
    global keylistener, recorder, textin, KeyEvent, NATIVE_OK, NATIVE_ERR
    if not NATIVE_OK:
        import importlib
        importlib.invalidate_caches()
        autosetup.ensure_third_party_on_path()
        try:
            keylistener = importlib.import_module("mac_keylistener")
            recorder = importlib.import_module("mac_recorder")
            textin = importlib.import_module("mac_textin")
            KeyEvent = keylistener.KeyEvent
            NATIVE_OK = True
            NATIVE_ERR = ""
        except ImportError as exc:
            NATIVE_ERR = str(exc)

    if not NATIVE_OK and not args.check:
        print(f"\n  ❌ 原生模組仍無法載入：{NATIVE_ERR}")
        print("\n  可能原因：")
        print("     · pyobjc 的 wheel 與這個 Python 版本不相容")
        print(f"     · 目前 Python：{sys.version.split()[0]}（{sys.executable}）")
        print("\n  手動安裝：")
        print("     python3 -m pip install pyobjc-framework-Quartz "
              "pyobjc-framework-Cocoa pyobjc-framework-AVFoundation")
        return 4

    if args.list_keys:
        names = keys_mod.all_names()
        print(f"本平台（{'macOS' if keys_mod.is_macos() else sys.platform}）"
              f"認得 {len(names)} 個按鍵名稱：\n")
        for i in range(0, len(names), 8):
            print("  " + "  ".join(f"{n:14s}" for n in names[i:i + 8]))
        print("\n設定方式（config.toml）：")
        print('  [trigger]')
        print('  hotkey = "F9"')
        print("\n也可以用組合鍵，例如 Ctrl+Alt+R（修飾鍵順序不影響）")
        return 0

    if args.test_mic:
        return run_mic_test(seconds=args.mic_seconds, model=args.model)

    cfg = config_module.Config.load()
    if args.hotkey:
        cfg.hotkey = args.hotkey
    if args.model:
        cfg.model_dir = args.model

    # 模型也一併補齊（163 MB，只在下載一次）
    model_path = autosetup.ensure_model(cfg.model_dir)
    if model_path is None:
        print("\n  ❌ 模型未就緒，無法啟動。")
        print(f"     手動下載：python3 tools/p1/fetch_model.py --get {cfg.model_dir}")
        return 4

    if args.check:
        print("\n  ── 相依套件狀態 ──")
        for module, ok, why in autosetup.diagnose():
            mark = "✅" if ok else "❌"
            detail = "" if ok else f"  ← {why}"
            print(f"  {mark} {module:14s}{detail}")
        print("\n  ✅ 相依與模型都就緒 —— 可以啟動了")
        print(f"     錄音鍵：{getattr(cfg, 'hotkey', '?')}")
        print("     啟動：python3 app/mac_vibetalkie.py")
        return 0

    daemon = MacPttDaemon(cfg, dry_run=args.dry_run, debug=args.debug,
                          auto_install=not args.no_install)
    # port 與「要不要開瀏覽器」都尊重 config.toml，CLI 參數只是覆寫
    port = args.port if args.port is not None else int(getattr(cfg, "port", 8756))
    want_browser = (not args.no_browser) and bool(
        getattr(cfg, "open_browser", True))
    return daemon.run(seconds=args.seconds, ui=not args.no_ui,
                      ui_port=port, open_browser=want_browser)


if __name__ == "__main__":
    raise SystemExit(main())
