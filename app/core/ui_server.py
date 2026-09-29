#!/usr/bin/env python3
"""本機 UI 伺服器：把 `app/ui/` 的設定頁面服務起來（平台無關）。

## 為什麼需要這個

UI（`app/ui/index.html` + `app.js` + `style.css`，共 943 行）是**純靜態檔案**，
Windows 版靠 `vibetalkie.py` 裡的 HTTP server 服務它。macOS 版原本沒有 ——
所以使用者只能在命令列打 `--hotkey F9`，不能用那個設定頁面。

這個模組把「服務 UI ＋ 讀寫設定」這段抽出來，讓兩個平台共用同一份 UI 檔案。

## ⚠️ `snapshot()` 是 UI 的契約

`tests/test_status_contract.py` 會讀 `app/ui/app.js`，檢查它引用到的每個
`s.<欄位>` 都存在於 snapshot 的回傳裡。**這不是形式主義** ——
實際踩過：在 snapshot 加了 `mic_warning` 卻忘了在來源 dict 也加 →
`KeyError`，而 UI 每 500ms 輪詢一次，變成每秒兩次的 traceback 風暴。

所以這裡的欄位清單是**照著 app.js 實際讀的欄位**列的，不是憑印象。

## 平台差異怎麼處理

有些欄位是 Windows 專屬（藍牙中斷警告、原廠工具偵測、音訊裝置索引）。
macOS 沒有等價的東西 → 回 `None`／空值，**不假裝有**。
UI 對 None 的解讀是「沒有警告」，這是正確的語意。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = ROOT / "app" / "ui"

# ⚠️ 直接執行這支檔案時（`python app/core/ui_server.py`），
#    `app/core` 不會自動進 sys.path —— 那是「被 import 時」才有的待遇。
#    少了這段，直接跑會得到 `ModuleNotFoundError: No module named 'config'`，
#    而錯誤訊息指向 `import config`，看不出真正原因是路徑。
for _p in (ROOT / "app", ROOT / "app" / "core", ROOT / "third_party"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class Status:
    """UI 要的狀態容器（平台無關）。

    常駐程式負責更新 `state` / `level` / `stats` / `history`，
    這個類別只負責**把它們整理成 UI 看得懂的形狀**。
    """

    def __init__(self, cfg, platform: str = "macos"):
        self.cfg = cfg
        self.platform = platform
        self.state = "IDLE"
        self.level = 0.0
        self.stats = {"presses": 0, "inserted": 0, "empty": 0, "failed": 0}
        self.history: deque = deque(maxlen=20)
        self.latency: dict = {}
        self.engine_name = ""
        self.error: str | None = None
        self.started = time.time()
        # 平台專屬欄位：mac 沒有這些東西，明確設 None（不假裝有）
        self.vendor_warning: str | None = None
        self.mic_warning: str | None = None
        self.bt_warning: str | None = None
        # ⚠️ 不能是空字串 —— `app.js` 的判斷是 `s.mic_device != null`，
        #    空字串會通過，於是畫面顯示成「device 」（後面空的）。
        self.mic_device = "系統預設輸入裝置"
        self.mic_stream = "per_press"
        self.mic_open = False
        self.target_online = True
        self.mic_order: list[str] = []
        self.hotkey_label = ""
        self.hotkey_error: str | None = None
        # 錄音鍵（**多組**）—— 與 Windows 版 `vibetalkie.Status` 同一個形狀。
        # ⚠️ 這幾個欄位是新 UI 要求的（`app/ui/app.js` 讀 `s.hotkeys` /
        #    `s.key_enabled` / `s.hotkeys_label` / `s.hotkey_warning` /
        #    `s.key_test`）。**兩個平台共用同一份 UI**，所以欄位名要一字不差；
        #    少一個的症狀是 UI 顯示 undefined 而後端毫無錯誤。
        #    由 `tests/test_status_contract.py` [6] 對兩個平台一起驗。
        self.hotkeys_label = ""          # 多組的完整標籤（含停用）
        self.hotkey_warning: str | None = None   # 解析失敗的原因（一路傳到 UI）
        self.key_test: dict | None = None        # 「測試」模式的狀態
        self.model_wanted = ""
        self.model_loaded = ""
        self.model_mismatch = False
        self.ticks = 0

    def on_state(self, state: str) -> None:
        # ⚠️ **必須轉小寫。** `app/ui/app.js` 的 `STATE_TEXT` 用小寫鍵
        #    （`idle` / `recording` / `processing` / `inserting`）。
        #
        #    實際踩到：常駐程式送 `"IDLE"`（狀態機內部用大寫），
        #    UI 查表查不到 → 顯示「未知」，狀態燈的 CSS class 也對不上。
        #
        #    Windows 版在同樣位置有 `state.lower()`，mac 版第一版漏了 ——
        #    兩邊共用同一份 UI，所以「一邊有轉、一邊沒轉」會變成
        #    「Windows 正常、mac 顯示未知」這種平台專屬的怪症狀。
        self.state = (state or "").lower()

    def on_result(self, r: dict) -> None:
        """ASR 完成時呼叫（由常駐程式掛上）。"""
        self.stats["inserted"] += 1
        self.history.appendleft({
            "text": r.get("text", ""),
            "ms": r.get("ms", 0),
            "at": time.strftime("%H:%M:%S"),
        })

    def snapshot(self) -> dict:
        """**UI 的契約** —— 欄位照 `app/ui/app.js` 實際讀的列。"""
        lat = self.latency or {}
        return {
            # 狀態與統計
            "state": self.state,
            "level": round(self.level, 3),
            "stats": dict(self.stats),
            "history": list(self.history),
            "latency_ms": lat.get("total_ms"),
            "asr_ms": lat.get("asr_ms"),
            "paste_ms": lat.get("paste_ms"),
            "presses": self.stats.get("presses", 0),
            "inserted": self.stats.get("inserted", 0),
            "empty": self.stats.get("empty", 0),
            "failed": self.stats.get("failed", 0),
            # 警告（mac 沒有藍牙/原廠工具那些偵測，但**有一條必須講**，見下）
            "vendor_warning": self.vendor_warning,
            "mic_warning": self.mic_warning or _platform_mic_note(self.platform),
            "bt_warning": self.bt_warning,
            # 麥克風
            "mic_stream": self.mic_stream,
            "mic_open": self.mic_open,
            "mic_device": self.mic_device,
            "target_online": self.target_online,
            "mic_order": self.mic_order,
            # 錄音鍵（**多組**）
            #
            # ⚠️ 欄位名與 Windows 版 `vibetalkie.Status.snapshot()` 一字不差 ——
            #    兩個平台共用同一份 `app/ui/app.js`，名字不同就是靜默失敗。
            #    `hotkey` 保留單一字串是為了相容舊 UI，真正的來源是 `hotkeys`。
            "hotkeys": list(_effective_hotkeys(self.cfg)),
            "key_enabled": list(_hotkey_enabled(self.cfg)),
            "hotkey": (_effective_hotkeys(self.cfg) or [""])[0],
            "hotkeys_label": self.hotkeys_label,
            "hotkey_label": self.hotkey_label,
            "hotkey_error": self.hotkey_error,
            # 錄音鍵設定有問題時一路傳到 UI 顯示 —— 設定頁寫了看不懂的字串
            # 卻沒有任何提示，是最容易讓人白費一整輪的失敗模式。
            "hotkey_warning": self.hotkey_warning,
            # 「測試」模式（只聽不錄）：UI 靠這個顯示「✓ 收到 F9」之類的回報
            "key_test": self.key_test,
            "trigger_mode": getattr(self.cfg, "trigger_mode", "hold"),
            # 模型
            "model_wanted": self.model_wanted,
            "model_loaded": self.model_loaded,
            "model_mismatch": self.model_mismatch,
            "engine": self.engine_name,
            "model": getattr(self.cfg, "model_dir", ""),
            "error": self.error,
            # 環境
            # ⚠️ `downloads` **必須是下載中的進度**，不是空陣列。
            #    UI 的模型分頁靠它畫進度條、並且在下載完成時自動刷新清單
            #    （`app.js` 的 `renderModels()` / `refreshModels()`）。
            #    第一版寫死 `[]`，症狀是「按了下載完全沒反應」——
            #    其實下載有在跑，只是 UI 看不到。
            "downloads": self.active_downloads(),
            "models_dir": str(ROOT / "models"),
            "config_path": str(config_path()),
            "opencc": _has_opencc(),
            "ticks": self.ticks,
            "platform": self.platform,
        }

    def active_downloads(self) -> list[dict]:
        """目前進行中的模型下載（給 UI 畫進度）。

        `models` 還沒初始化、或載入失敗時回空清單 —— UI 要能容忍。
        """
        try:
            mgr = _manager()
            if mgr is None:
                return []
            return mgr.active_downloads()
        except Exception:                              # noqa: BLE001
            return []


def config_path() -> Path:
    """設定檔路徑（與 config.py 保持一致）。"""
    import config as config_module
    return Path(getattr(config_module, "CONFIG_PATH", ROOT / "config.toml"))


def _effective_hotkeys(cfg) -> list:
    """實際生效的錄音鍵清單（新舊欄位並存的判定在 `Config`，這裡不重寫一份）。

    ⚠️ 退回 `getattr` 是刻意的：`ui_server` 是**共用層**，不該假設 cfg 一定
    是完整的 `Config`（測試與工具會傳輕量的物件）。判定邏輯仍然只有
    `Config.effective_hotkeys()` 一份 —— 這裡只是「沒有那個方法時」的後備。
    """
    if hasattr(cfg, "effective_hotkeys"):
        return list(cfg.effective_hotkeys())
    one = getattr(cfg, "hotkey", "")
    return [one] if one else []


def _hotkey_enabled(cfg) -> list:
    """每一組是否啟用（順序與 `_effective_hotkeys()` 一致）。

    沒有 `Config.hotkey_enabled()` 時，直接照 `~` 前綴判斷 ——
    那也是 `hotkey.DISABLED_PREFIX` 的定義，不會有第二種說法。
    """
    if hasattr(cfg, "hotkey_enabled"):
        return list(cfg.hotkey_enabled())
    import hotkey as hotkey_mod
    return [not str(k).strip().startswith(hotkey_mod.DISABLED_PREFIX)
            for k in _effective_hotkeys(cfg)]


def _has_opencc() -> bool:
    try:
        import opencc  # noqa: F401
        return True
    except ImportError:
        return False


def _platform_mic_note(platform: str) -> str | None:
    """平台限制的誠實說明（會顯示在 UI 的警告框）。

    ⚠️ **這不是裝飾，是必要的。** macOS 的 AVAudioEngine 只能用
    「系統預設輸入裝置」——程式無法指定要用哪一支（那由系統設定決定）。

    但設定頁有一個「麥克風優先順序」清單（那是為 Windows 做的）。
    在 mac 上那個清單**可以編輯但不會生效** —— 如果不講，
    使用者會在那裡排了半天順序，然後發現完全沒作用，卻找不到原因。

    UI 已經有現成的警告框（`app.js` 收集 `mic_warning` / `bt_warning` /
    `vendor_warning` 顯示），所以用那個管道傳，不必改 UI。
    """
    if platform == "macos":
        return ("macOS 的音訊輸入由「系統設定 → 聲音 → 輸入」決定，"
                "本程式只能使用系統預設裝置。"
                "下面的「麥克風優先順序」在 macOS 上不會改變實際使用的裝置 —— "
                "要換麥克風請到系統設定，或在該裝置上按「設為預設輸入」。")
    return None


def _list_devices() -> list[dict]:
    """可選的麥克風清單。

    ⚠️ 平台差異：Windows 有多個可選的音訊裝置索引；macOS 由系統設定決定
    預設輸入裝置，程式只能用那一個。**誠實回報**而不是假裝有清單。
    """
    from speech_engine import has_opencc  # noqa: F401  只是確認可 import
    try:
        import mac_recorder
        return mac_recorder.list_input_devices()
    except Exception:                                  # noqa: BLE001
        return []


def make_handler(status: Status):
    """建立 HTTP handler（靜態檔 ＋ /api/*）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "VibeTalkieUI/1.0"

        # -------------------------------------------------- 工具

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # UI 是本機頁面，但瀏覽器會快取 js/css —— 開發時很困擾
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def log_message(self, fmt, *args) -> None:
            """安靜 —— UI 每 500ms 輪詢一次，不該洗版。"""

        # -------------------------------------------------- GET

        def do_GET(self) -> None:                       # noqa: N802
            try:
                self._do_get()
            except Exception as exc:                    # noqa: BLE001
                try:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                except Exception:                       # noqa: BLE001
                    pass

        def _do_get(self) -> None:
            path = self.path.split("?")[0]

            if path == "/api/status":
                status.ticks += 1
                return self._json(status.snapshot())

            if path == "/api/config":
                cfg = status.cfg
                return self._json({**cfg.public(), "devices": _list_devices()})

            if path == "/api/models":
                # refresh=1 強制重抓 GitHub（UI 的「重新整理清單」按鈕）
                force = "refresh=1" in self.path
                return self._json(_models_payload(status.cfg, force=force))

            # 靜態檔
            if path in ("/", "/index.html"):
                f = UI_DIR / "index.html"
                if not f.is_file():
                    return self._send(500, b"ui/index.html missing", "text/plain")
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

            f = (UI_DIR / path.lstrip("/")).resolve()
            if UI_DIR in f.parents and f.is_file():
                ctype = "text/plain"
                if f.suffix == ".css":
                    ctype = "text/css; charset=utf-8"
                elif f.suffix == ".js":
                    ctype = "application/javascript; charset=utf-8"
                elif f.suffix == ".html":
                    ctype = "text/html; charset=utf-8"
                return self._send(200, f.read_bytes(), ctype)

            return self._send(404, b"not found", "text/plain")

        # -------------------------------------------------- POST

        def do_POST(self) -> None:                      # noqa: N802
            try:
                self._do_post()
            except Exception as exc:                    # noqa: BLE001
                try:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                except Exception:                       # noqa: BLE001
                    pass

        def _do_post(self) -> None:
            path = self.path.split("?")[0]
            try:
                n = int(self.headers.get("Content-Length", 0))
                patch = json.loads(self.rfile.read(n) or b"{}")
            except Exception as exc:                    # noqa: BLE001
                return self._json({"error": f"bad json: {exc}"}, 400)

            if path == "/api/config":
                return self._post_config(patch)
            if path == "/api/models/download":
                return self._post_models_download(patch)
            if path == "/api/models/select":
                return self._post_models_select(patch)
            if path == "/api/models/cancel":
                return self._post_models_cancel(patch)
            if path == "/api/models/retry":
                return self._post_models_retry(patch)
            return self._json({"error": "unknown endpoint"}, 404)

        def _post_config(self, patch: dict) -> None:
            """寫入設定。**與 Windows 版相同的欄位與驗證**。"""
            import config as config_module
            import hotkey as hotkey_mod

            cfg = status.cfg

            for k in ("traditional", "mode", "device_index", "language",
                      "mic_name", "remove_trailing_period"):
                if k in patch:
                    setattr(cfg, k, patch[k])

            if "mic_names" in patch:
                raw = patch["mic_names"]
                if not isinstance(raw, list):
                    return self._json({"error": "mic_names 必須是陣列"}, 400)
                seen, order = set(), []
                for x in raw:
                    name = str(x or "").strip()
                    if name and name not in seen:
                        seen.add(name)
                        order.append(name)
                cfg.mic_names = order
                cfg.mic_name = order[0] if order else ""

            # 錄音鍵：**多組**，而且每一組可以帶自己的觸發方式與結束鍵。
            #
            # ⚠️ 驗證與 Windows 版**共用同一份**（`config.apply_hotkeys_patch`）。
            #    這裡原本只認單一字串 `hotkey`，而且會用
            #    `keys.name_to_code()` 去驗「本平台認不認得這顆鍵」——
            #    那個驗證在**測試環境**（Windows 上跑 mac 的程式）會誤判，
            #    因為 `name_to_code()` 回的是**執行這支程式的那個平台**的鍵碼。
            #    真正的把關是 `hotkey.parse_binding()`（平台無關）＋
            #    `keys.py` 提供候選清單給 UI 選。
            if "hotkeys" in patch or "hotkey" in patch:
                raw = patch.get("hotkeys", None)
                if raw is None:
                    one = str(patch.get("hotkey") or "").strip()
                    raw = [one] if one else []
                err = config_module.apply_hotkeys_patch(cfg, raw)
                if err:
                    return self._json({"error": err}, 400)
                bind = hotkey_mod.bindings(
                    list(_effective_hotkeys(cfg)),
                    default_mode=getattr(cfg, "trigger_mode", "hold"))
                status.hotkey_label = bind.label
                status.hotkeys_label = bind.label
                status.hotkey_error = None
                status.hotkey_warning = None

            if "trigger_mode" in patch:
                # ⚠️ 用 `TRIGGER_MODES_VALUES`（字串集合），**不是** `TRIGGER_MODES`。
                #    後者是給 UI 顯示用的 dict 清單（含 label/note），
                #    拿它來比對字串會**永遠不相等** —— 症狀是「合法的值也被拒絕」。
                #    實際踩到：設定 toggle 被回「未知的觸發方式：toggle」。
                val = str(patch["trigger_mode"] or "").strip()
                if val not in config_module.TRIGGER_MODES_VALUES:
                    return self._json(
                        {"error": "trigger_mode 只能是 "
                                  + "、".join(config_module.TRIGGER_MODES_VALUES)}, 400)
                cfg.trigger_mode = val

            if "double_tap_ms" in patch:
                try:
                    ms = int(patch["double_tap_ms"])
                except (TypeError, ValueError):
                    return self._json({"error": "double_tap_ms 必須是整數"}, 400)
                cfg.double_tap_ms = max(150, min(1000, ms))

            if "mic_stream" in patch:
                val = str(patch["mic_stream"] or "").strip()
                if val not in config_module.MIC_STREAM_VALUES:
                    return self._json(
                        {"error": "mic_stream 只能是 "
                                  + "、".join(config_module.MIC_STREAM_VALUES)}, 400)
                cfg.mic_stream = val

            if "idle_timeout_s" in patch:
                try:
                    v = float(patch["idle_timeout_s"])
                except Exception:                       # noqa: BLE001
                    return self._json({"error": "idle_timeout_s 必須是數字"}, 400)
                cfg.idle_timeout_s = max(config_module.IDLE_TIMEOUT_MIN, v)

            try:
                cfg.save()
            except Exception as exc:                    # noqa: BLE001
                return self._json({"error": f"寫入設定失敗：{exc}"}, 500)
            return self._json({"ok": True})

        # ---- 模型管理 ----

        def _post_models_download(self, patch: dict) -> None:
            name = str(patch.get("name") or "").strip()
            if not name:
                return self._json({"error": "缺少 name"}, 400)
            mgr = _manager()
            if mgr is None:
                return self._json({"error": "模型管理不可用"}, 500)
            # ⚠️ `start()` 回傳 (成功, 原因) —— 一定要檢查，
            #    否則「名稱不合法」之類的失敗會被靜默吞掉，
            #    UI 只會看到「按了下載但什麼都沒發生」。
            ok, msg = mgr.start(name)
            if not ok:
                return self._json({"error": msg or "無法開始下載"}, 400)
            return self._json({"ok": True, "name": name})

        def _post_models_select(self, patch: dict) -> None:
            name = str(patch.get("name") or "").strip()
            if not name:
                return self._json({"error": "缺少 name"}, 400)
            from models import is_ready
            if not is_ready(name):
                return self._json({"error": f"模型尚未下載完成：{name}"}, 400)
            status.cfg.model_dir = name
            try:
                status.cfg.save()
            except Exception as exc:                    # noqa: BLE001
                return self._json({"error": f"寫入設定失敗：{exc}"}, 500)
            return self._json({"ok": True, "name": name})

        def _post_models_cancel(self, patch: dict) -> None:
            name = str(patch.get("name") or "").strip()
            mgr = _manager()
            if mgr is None:
                return self._json({"error": "模型管理不可用"}, 500)
            if name:
                ok, msg = mgr.cancel(name)
            else:
                # 沒指定就取消全部進行中的
                ok, msg = True, ""
                for d in mgr.active_downloads():
                    mgr.cancel(d["name"])
            if not ok:
                return self._json({"error": msg or "取消失敗"}, 400)
            return self._json({"ok": True})

        def _post_models_retry(self, patch: dict) -> None:
            name = str(patch.get("name") or "").strip()
            if not name:
                return self._json({"error": "缺少 name"}, 400)
            mgr = _manager()
            if mgr is None:
                return self._json({"error": "模型管理不可用"}, 500)
            ok, msg = mgr.retry(name)
            if not ok:
                return self._json({"error": msg or "重試失敗"}, 400)
            return self._json({"ok": True, "name": name})

    return Handler


# `_main_key_from_spec()` 刪掉了（整合時）。
#
# 它把 `HotkeySpec`（那時內部是 **Windows VK 碼**）翻譯成 mac 的主鍵名稱，
# 只為了餵給 `keys.name_to_code()` 做驗證。那正是「共用層講 VK、平台層
# 只好自己翻譯」的症狀 —— 也因為它依賴**執行平台的鍵碼表**，在
# 「Windows 上跑 mac 的程式」這種情況下會給出錯的答案。
#
# 現在 `HotkeySpec` 自己就講 canonical 名稱（見 app/core/hotkey.py），
# 驗證由 `hotkey.parse_binding()` 負責（平台無關），這個翻譯層整段不需要了。


# ---------------------------------------------------------------- 模型狀態

_manager_lock = threading.Lock()
_manager_obj = None


def _manager():
    """延遲建立 ModelManager（它會載入 model_index，有網路動作）。"""
    global _manager_obj
    with _manager_lock:
        if _manager_obj is None:
            from models import ModelManager
            _manager_obj = ModelManager()
        return _manager_obj


def _models_payload(cfg, force: bool = False) -> dict:
    """`/api/models` 的回應。

    ## ⚠️ 格式必須與 Windows 版**完全一致**

    UI（`app/ui/app.js` 的 `renderModels()`）讀的是：

        d.models[].{name, title, state, download, active, supported,
                    size_mb, langs, note, family, reason}

    其中 `state` 是 `absent` / `ready` / `downloading` / `error`（見 `MODEL_STATE`）。

    **第一版我自作主張回了一個不同的結構**（`installed` / `catalog` / `current`），
    結果 UI 讀 `d.models` 得到 `undefined` → **模型分頁整片空白**。
    兩個平台共用同一份 UI，所以回應格式不是「實作細節」，是契約。
    """
    import models as models_mod

    active = getattr(cfg, "model_dir", "")
    mgr = _manager()
    dl: dict = {}
    if mgr is not None:
        try:
            with mgr._lock:                            # noqa: SLF001
                dl = {k: v.snapshot() for k, v in mgr._downloads.items()}  # noqa: SLF001
        except Exception:                              # noqa: BLE001
            dl = {}

    # 官方清單（含分類）。失敗時退回內建 CATALOG，至少讓 UI 有東西顯示。
    try:
        import model_index
        cat = model_index.catalog(force=force)
    except Exception as exc:                            # noqa: BLE001
        cat = {
            "models": [{
                "name": m.name, "asset": None, "size_mb": m.size_mb,
                "family": m.kind, "supported": True, "reason": "",
                "langs": m.langs, "url": None, "note": m.note,
            } for m in models_mod.CATALOG],
            "error": f"{type(exc).__name__}: {exc}",
        }

    for m in cat.get("models", []):
        name = m.get("name", "")
        info = dl.get(name)
        if info and info.get("state") in ("queued", "downloading", "extracting"):
            m["state"] = info["state"]
        elif models_mod.is_ready(name):
            m["state"] = "ready"
        elif info and info.get("state") == "error":
            m["state"] = "error"
        else:
            m["state"] = "absent"
        m["download"] = info
        m["active"] = (name == active)
        m["title"] = m.get("title") or name

    # 本機既有、但不在官方清單裡的模型也要顯示（例如使用者自己放的）
    listed = {m.get("name") for m in cat.get("models", [])}
    for extra in models_mod.installed_models():
        if extra in listed:
            continue
        cat.setdefault("models", []).insert(0, {
            "name": extra, "asset": None,
            "size_mb": models_mod.disk_usage_mb(extra),
            "family": "unknown", "supported": True, "reason": "",
            "langs": "", "url": None, "state": "ready",
            "download": None, "active": extra == active, "title": extra,
            "note": "本機既有（不在官方清單中）",
        })
    return cat


# ---------------------------------------------------------------- 啟動


def pick_port(preferred: int) -> int:
    """找一個可用的 port（被佔用就往後找）。"""
    import socket
    for port in range(preferred, preferred + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"{preferred}–{preferred + 19} 都被佔用了")


def start_server(status: Status, port: int) -> ThreadingHTTPServer:
    """在背景執行緒啟動 UI 伺服器，回傳 server 物件。

    ⚠️ 只綁 127.0.0.1 —— 這是本機 UI，不該讓同網段的其他機器連進來。
    """
    handler = make_handler(status)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="ui-server")
    t.start()
    return httpd


def open_browser(url: str, delay: float = 0.6) -> None:
    """延遲一下再開瀏覽器（等 server 真的起來）。"""
    def go():
        time.sleep(delay)
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:                               # noqa: BLE001
            pass
    threading.Thread(target=go, daemon=True, name="open-browser").start()


if __name__ == "__main__":
    import argparse

    import config as config_module

    ap = argparse.ArgumentParser(description="單獨啟動 UI（測試用）")
    ap.add_argument("--port", type=int, default=8756)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = config_module.Config.load()
    st = Status(cfg)
    st.engine_name = getattr(cfg, "engine", "")
    st.model_wanted = getattr(cfg, "model_dir", "")
    port = pick_port(args.port)
    start_server(st, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"UI 已經在 {url} 執行（Ctrl+C 結束）")
    if not args.no_browser:
        open_browser(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n結束")
