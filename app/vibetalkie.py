#!/usr/bin/env python3
"""VibeTalkie 主程式：一鍵啟動的常駐工具。

架構（刻意保持單純）：

    主執行緒   PTT 常駐迴圈（Raw Input 訊息迴圈）—— 熱鍵與注入的核心
    背景執行緒 本機 HTTP 伺服器 —— 提供 UI 與狀態 API

UI 是**純靜態檔案**（`app/ui/index.html`），改了重新整理就看到，
**不需要編譯**。這也是選它而不用 Tauri/Qt 的原因。

用法:
    python app/vibetalkie.py                # 啟動（會自動開瀏覽器）
    python app/vibetalkie.py --no-browser   # 不自動開瀏覽器
    python app/vibetalkie.py --debug        # 顯示每個被接受/拒絕的 Ctrl 事件
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "core"))          # 執行期模組

from config import Config  # noqa: E402
import config as config_module  # noqa: E402
import hotkey  # noqa: E402  # 錄音鍵規格解析（見 app/core/hotkey.py）
import models  # noqa: E402
import model_index  # noqa: E402
from models import ModelManager  # noqa: E402
from speech_engine import build_engine, has_opencc  # noqa: E402
from ptt import PttDaemon  # noqa: E402
from record_wav import list_devices, setup_console  # noqa: E402

UI_DIR = HERE / "ui"
VENDOR_PROCESS = "shandianshuo"
# 本專案目標裝置的名稱特徵（見 docs/hardware.md §1）。
# 只用於「使用者還沒綁定時」的自動識別，綁定後就以 mic_name 為準。
TARGET_HINTS = ("AI_VOICE",)


# ---------------------------------------------------------------- 共用狀態

class Status:
    """PTT 迴圈與 HTTP 伺服器之間共享的狀態。"""

    def __init__(self, cfg: Config):
        self.lock = threading.Lock()
        self.cfg = cfg
        self.state = "idle"          # idle | recording | pressing(內部) | processing | inserting
        self.level = 0.0
        self.stats = {"presses": 0, "inserted": 0, "empty": 0, "failed": 0}
        self.history: deque[dict] = deque(maxlen=30)
        self.latency: dict | None = None
        self.vendor_warning: str | None = None
        self.mic_warning: str | None = None
        self.bt_warning: str | None = None
        self.engine_name = cfg.engine
        self.model_name = ""
        self.error: str | None = None
        # 非序列化欄位：給 API handler 用的參考
        self.engine = None
        self.models: ModelManager | None = None
        self.daemon = None      # 切換模型前要檢查它是不是 IDLE
        # 「關於」分頁要顯示的資訊
        self.models_dir = str(models.MODELS_DIR)
        self.config_path = str(config_module.CONFIG_PATH)
        self.opencc = False

    # -- 給 daemon 的回呼 --
    def on_state(self, state: str) -> None:
        with self.lock:
            self.state = state.lower()

    def on_result(self, r: dict) -> None:
        with self.lock:
            self.history.appendleft({
                "text": r["text"],
                "ms": r["ms"],
                "chars": r["chars"],
                "time": time.strftime("%H:%M:%S"),
            })

    def snapshot(self, daemon: PttDaemon | None = None) -> dict:
        with self.lock:
            d = {
                "state": self.state,
                "level": round(self.level, 3),
                "stats": dict(self.stats),
                "history": list(self.history),
                "latency": self.latency,
                "vendor_warning": self.vendor_warning,
                "mic_warning": self.mic_warning,
                "bt_warning": self.bt_warning,
                "engine": self.engine_name,
                "model": self.model_name,
                "error": self.error,
                "mic_stream": self.cfg.mic_stream,
                # 串流現在到底有沒有開著 —— 使用者唯一能自行確認
                # 「session 模式有沒有真的常駐」的方法（實測踩到：改了設定
                # 卻因為路徑不同而沒生效，白白誤判一整輪）。
                "mic_open": bool(getattr(daemon, "_cap", None) is not None),
                "mic_device": getattr(daemon, "_mic_device", None),
                # 目標麥克風在不在？（藍牙省電休眠時會消失）
                # None = 還沒檢查過；使用者靠這個知道「現在錄的是哪一支」
                "target_online": (getattr(daemon, "_target_seen_online", None)
                                  if daemon is not None else None),
                # 麥克風優先順序與各自在不在線（主／副）
                "mic_order": mic_order_status(self.cfg),
                # 錄音鍵（**多組**）。`hotkey` 保留單一字串是為了相容舊 UI，
                # 但真正的來源是 `hotkeys` 清單。
                "hotkeys": list(self.cfg.effective_hotkeys()),
                # 每一組的啟用狀態（順序與 hotkeys 一致）—— 停用的也列出來，
                # 這樣狀態頁才能說「共 N 組，其中 M 組啟用中」。
                "key_enabled": list(self.cfg.hotkey_enabled()),
                "hotkey": (self.cfg.effective_hotkeys() or [""])[0],
                "hotkeys_label": (getattr(daemon, "_hotkey_bindings", None).label
                                  if getattr(daemon, "_hotkey_bindings", None) else ""),
                "hotkey_label": (getattr(daemon, "_hotkey_spec", None).label
                                 if getattr(daemon, "_hotkey_spec", None) else ""),
                "hotkey_error": getattr(daemon, "_hotkey_error", None) if daemon else None,
                # 測試模式（只聽不錄）的狀態：UI 要顯示「有沒有收到那顆鍵」
                "key_test": (daemon.test_state() if daemon is not None
                             and hasattr(daemon, "test_state") else None),
                "trigger_mode": self.cfg.trigger_mode,
                # 執行期事實：這個行程的 tick 各跑了幾次。
                # 「程式碼明明是對的，執行起來卻沒效果」時，這是最快的分辨方法 ——
                # 空字典就代表這個行程根本沒有在跑主迴圈（例如舊行程或啟動失敗）。
                "ticks": dict(getattr(daemon, "_tick_counts", {}) or {}),
                # 設定檔指定的模型 vs 引擎**實際載入**的模型。
                # 為什麼要分開顯示：實測踩到「設定改了、模型沒換」——
                # 使用者聽到舊模型的效果，卻以為是模型本身不好。
                # 這兩行只要不一致，就是「設定還沒生效」。
                "model_wanted": self.cfg.model_dir,
                # engine 可能還沒起來（啟動初期），那就報「設定指定的」而不是空白 ——
                # 否則狀態頁會顯示「—」，看起來像壞掉
                "model_loaded": (Path(self.engine.model_dir).name
                                 if self.engine is not None
                                 else self.cfg.model_dir),
            }
        # UI 的欄位名稱
        s = d["stats"]
        lat = d["latency"] or {}
        return {
            "state": d["state"],
            "level": d["level"],
            "presses": s["presses"], "inserted": s["inserted"],
            "empty": s["empty"], "failed": s["failed"],
            "history": d["history"],
            "latency_ms": lat.get("total_ms"),
            "asr_ms": lat.get("asr_ms"),
            "paste_ms": lat.get("paste_ms"),
            "vendor_warning": d["vendor_warning"],
            "mic_warning": d["mic_warning"],
            "bt_warning": d["bt_warning"],
            "mic_stream": d["mic_stream"],
            "mic_open": d["mic_open"],
            "mic_device": d["mic_device"],
            "target_online": d["target_online"],
            "mic_order": d["mic_order"],
            "hotkeys": d["hotkeys"],
            "key_enabled": d["key_enabled"],
            "hotkey": d["hotkey"],
            "hotkeys_label": d["hotkeys_label"],
            "hotkey_label": d["hotkey_label"],
            # 錄音鍵設定有問題時一路傳到 UI 顯示 —— 設定頁寫了看不懂的字串
            # 卻沒有任何提示，是最容易讓人白費一整輪的失敗模式。
            "hotkey_warning": (f"錄音鍵設定有問題：{d['hotkey_error']}"
                               if d["hotkey_error"] else None),
            "hotkey_error": d["hotkey_error"],
            # 測試模式（只聽不錄）：UI 靠這個顯示「✓ 收到 F9」之類的回報
            "key_test": d["key_test"],
            "trigger_mode": d["trigger_mode"],
            "ticks": d["ticks"],
            "model_wanted": d["model_wanted"],
            "model_loaded": d["model_loaded"],
            # 只要這兩個不一致，就是「設定還沒生效」——讓 UI 直接顯示，不必查
            "model_mismatch": bool(d["model_wanted"] and d["model_loaded"]
                                   and d["model_wanted"] != d["model_loaded"]),
            "engine": d["engine"],
            "model": d["model"],
            "error": d["error"],
            "downloads": (self.models.active_downloads() if self.models else []),
            "models_dir": self.models_dir,
            "config_path": self.config_path,
            "opencc": self.opencc,
        }


# `start_key_text()` **搬到 `config.py` 了** —— 兩個平台（Windows 的
# `vibetalkie.py` 與 mac 的 `ui_server.py`）都要用，各留一份就會漂移
# （漂移的症狀正是它 docstring 記的那個 bug：舊欄位變成 `F9@double`）。
# 這裡保留同名別名，讓既有呼叫端與測試不必改。
start_key_text = config_module.start_key_text


# ---------------------------------------------------------------- 裝置解析

def resolve_device(cfg: Config, quiet: bool = False) -> tuple[int, str, list[dict]]:
    """把設定檔裡的麥克風**解析成目前的裝置索引**。

    ⚠️ 為什麼不能直接用 `device_index`？
        實測踩到：`AI_VOICE_MAX` 原本是 index 1，某次藍牙重連後變成 index 0，
        index 1 變成使用者的另一支耳機。設定檔若只存 index，
        就會**從錯的麥克風錄音**，而且完全不會報錯。

    所以 `mic_name` 才是主要識別，`device_index` 只是快取與後備。
    WAVEINCAPS 的名稱上限是 31 個字元，所以比對用「開頭符合」而非全等。
    """
    try:
        devs = [{"index": i, "name": n.strip()} for i, n, *_ in list_devices()]
    except Exception as exc:
        if not quiet:
            print(f"⚠️ 列舉音訊裝置失敗：{exc}")
        return cfg.device_index, "", []

    if not devs:
        return cfg.device_index, "", []

    name = (cfg.mic_name or "").strip()
    if name:
        for d in devs:                                    # 全等
            if d["name"] == name:
                return d["index"], d["name"], devs
        for d in devs:                                    # 開頭符合（名稱被截斷）
            if d["name"].lower().startswith(name.lower()[:20]):
                return d["index"], d["name"], devs
        if not quiet:
            print(f"⚠️ 設定檔指定的麥克風「{name}」目前不在清單中。")
            print(f"   改用 index {cfg.device_index}："
                  f"{next((d['name'] for d in devs if d['index'] == cfg.device_index), '（不存在）')}")
            print("   請在設定介面重新選擇麥克風。")
        return cfg.device_index, "", devs

    # 還沒綁定名稱 → 嘗試認出本專案的目標裝置（見 docs/hardware.md §1）
    for d in devs:
        if any(h in d["name"] for h in TARGET_HINTS):
            if not quiet:
                print(f"ℹ️ 尚未綁定麥克風，自動認出目標裝置：{d['name']}")
            return d["index"], d["name"], devs

    return cfg.device_index, "", devs


def target_mic_online(cfg: Config) -> bool:
    """設定指定的麥克風**現在**在不在裝置清單裡？

    為什麼要單獨問這一個問題：藍牙麥克風閒置久了會進省電休眠、從系統消失
    （實測：`AI_VOICE_MAX` 會變 UNPLUGGED）。使用者要的是
    **「它回來的時候自動跟上」**，而不是每次都要自己處理。

    所以 `ptt.py` 會定期問這個函式，一發現裝置回來了就換回去。
    比對規則與 `resolve_device()` 一致（全等，或名稱前 20 字開頭符合 ——
    WAVEINCAPS 名稱上限 31 字元，長名會被截斷）。
    """
    name = (cfg.mic_name or "").strip()
    if not name:
        return False
    try:
        devs = [n.strip() for _i, n, *_ in list_devices()]
    except Exception:
        return False
    if any(d == name for d in devs):
        return True
    return any(d.lower().startswith(name.lower()[:20]) for d in devs)


def _find_device(devs: list[dict], name: str) -> dict | None:
    """在裝置清單裡找一個名稱相符的（全等優先，其次前 20 字開頭符合）。

    前 20 字是為了容忍 `WAVEINCAPS` 的 31 字元上限 ——
    `"Headset (AI_VOICE_MAX Hands-Fre"` 就是被截斷的名字。
    """
    want = (name or "").strip()
    if not want:
        return None
    for d in devs:
        if d["name"] == want:
            return d
    key = want.lower()[:20]
    for d in devs:
        if d["name"].lower().startswith(key):
            return d
    return None


def resolve_mic_priority(cfg: Config) -> tuple[int, str, list[dict]]:
    """依**優先順序**挑麥克風：第一個「目前在線」的勝出。

    為什麼要順序而不是單一裝置：藍牙麥克風會省電休眠、也會斷線。
    清單讓「主的不在就用副的，主的一回來就自動換回」變成自動的事。

    回傳 `(index, name, devices)`；都找不到時退回 `cfg.device_index`。
    """
    try:
        devs = [{"index": i, "name": n.strip()} for i, n, *_ in list_devices()]
    except Exception:
        return cfg.device_index, "", []

    order = cfg.effective_mic_order()
    if not order:
        return cfg.device_index, "", devs
    for name in order:
        hit = _find_device(devs, name)
        if hit:
            return hit["index"], hit["name"], devs
    # 全部不在線 → 退回快取索引（總比完全不能錄好）
    return cfg.device_index, "", devs


def mic_order_status(cfg: Config) -> list[dict]:
    """給 UI／狀態頁用：優先順序裡每一支麥克風現在在不在線。"""
    try:
        devs = [{"index": i, "name": n.strip()} for i, n, *_ in list_devices()]
    except Exception:
        devs = []
    out = []
    for i, name in enumerate(cfg.effective_mic_order()):
        hit = _find_device(devs, name)
        out.append({
            "name": name,
            "role": "主" if i == 0 else f"副 {i}",
            "online": hit is not None,
            "index": hit["index"] if hit else None,
        })
    return out


# ---------------------------------------------------------------- 環境檢查

def check_vendor_tool() -> str | None:
    """偵測原廠工具是否在執行。

    它會搶同一顆按鈕與同一個麥克風（`docs/hardware.md` §8）。
    **不自行終止它** —— 那是使用者的程式。
    """
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {VENDOR_PROCESS}.exe", "/NH"],
            capture_output=True, text=True, timeout=5, errors="replace").stdout
        if VENDOR_PROCESS in out.lower():
            return ("原廠工具「閃電說」正在執行。它會搶佔同一顆錄音鍵與麥克風，"
                    "可能導致按鍵沒反應或收音失敗。建議先關閉它再使用 VibeTalkie。")
    except Exception:
        pass
    return None


def check_bt_mic_warning(cfg: Config, devs: list[dict]) -> str | None:
    """若設定的麥克風是藍牙裝置，警告它會干擾其他藍牙音訊。

    實測（`docs/hardware.md` §2.2）：開藍牙麥克風會讓**同一顆藍牙控制器上
    的其他裝置**（例如耳機）整個 UNPLUGGED，錄音期間完全沒聲音，放開後
    約 0.4 秒才回來。這是控制器層級的資源衝突，程式修不掉。

    對策只有兩個：換 USB／有線麥克風，或錄音時別聽藍牙耳機。
    使用者不會自己知道這件事，所以一定要講。
    """
    name = ""
    for d in devs:
        if d["index"] == cfg.device_index:
            name = d["name"]
            break
    name = name or cfg.mic_name or ""
    # ⚠️ WAVEINCAPS 的名稱上限是 31 個字元，實測拿到的是
    #    "Headset (AI_VOICE_MAX Hands-Fre" —— "Hands-Free" 被截成 "Hands-Fre"。
    #    所以不能比對完整的 "hands-free"，要用共同的開頭。
    if "hands-fre" not in name.lower():
        return None
    return ("你用的是藍牙麥克風。錄音期間，其他藍牙音訊裝置（例如耳機）"
            "會暫時完全中斷、放開後才恢復 —— 這是藍牙控制器的資源限制，"
            "不是程式問題。改用 USB 或有線麥克風可完全避免。")


class AlreadyRunning(Exception):
    """已經有另一個 VibeTalkie 在用這個 port。

    ## 為什麼要有這個例外（實測踩到，症狀是「設定一直被還原」）

    原本 `pick_port()` 找不到可用的 port 就**直接回傳 preferred** ——
    使用者再啟動一次時，第二個行程會靜默地換一個 port 起來，
    而他完全不會知道。

    後果不只是「兩個視窗」：

      · 兩個行程**共用同一個 `config.toml`**，各自握一份記憶體
      · `cfg.save()` 有 5 處（啟動、換麥克風、換模型、UI 儲存…），
        每次都是**整個檔案重寫**
      · 於是舊行程會把它記憶體裡的舊值蓋回去 —— 使用者的感受是
        「我存了，過一陣子又變回去」，而且檔案永遠看起來是對的

    實測證據（同一台機器，兩個行程都活著）：

        磁碟：model_dir = "sherpa-onnx-paraformer-…"、mic_stream = "session"
        舊行程記憶體：model_dir = ""（空）、mic_stream = "per_press"

    所以**寧可拒絕啟動，也不要默默開第二個**。
    """


def port_in_use(port: int) -> bool:
    """這個 port 有東西在 listen 嗎？

    ⚠️ 用**綁定探測**，不是 `connect_ex()`。兩者的差別是實際踩到的 bug：
    `connect_ex` 問的是「連得上嗎」，所以對「已綁定但還沒開始 accept」
    或防火牆丟 RST 的 port 會回「沒人用」→ 於是回傳一個根本用不到的 port。
    綁定探測問的才是我們真正要問的問題：「我綁得上嗎？」

    `SO_REUSEADDR` **不要設**：設了會讓「已被佔用」的 port 也綁得上，
    那樣這個函式就永遠回 False（Windows 的行為與 Linux 不同，不可依賴）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def pick_port(preferred: int, span: int = 1) -> int:
    """取得要用的 port。**被佔用就丟 `AlreadyRunning`，不自動漂流。**

    ## ⚠️ 為什麼 `span` 預設是 1（這是一個實測踩到的錯誤設計）

    第一版寫成「從 `preferred` 起算 20 號裡找一個空的」，理由是「port 被
    佔用時讓位比較方便」。那個設計讓防護**完全失效**：

        port_in_use(8756) → True        # 偵測正確：舊實例在跑
        pick_port(8756)   → 8757        # 但 8757 是空的 → 讓位 → 靜默啟動第二個

    真實情境就是這樣：舊實例佔著 8756，8757 當然是空的。於是使用者
    **每次重複啟動都會成功**，而且他完全不知道 —— 接著兩個行程開始
    互相覆蓋 `config.toml`（見 `AlreadyRunning` 的 docstring）。
    （測試之所以沒抓到，是因為它只佔住 8756 一號，而 `span=20` 的邏輯
    向後讓位就通过了。**測試要照真實情境設計，不是照實作設計。**）

    ## 想刻意跑第二個

    明確指定 `--port` 就是「我要那個位置」：那個 port 被佔用時**也是**
    `AlreadyRunning`（誠實報錯，而不是偷偷換一個）。

    `span > 1` 只在**確定要讓位**的場合才傳（目前沒有這種呼叫端）——
    例如未來做「自動挑一個空 port 的暫時實例」。
    """
    for p in range(preferred, preferred + span):
        if not port_in_use(p):
            return p
    raise AlreadyRunning(
        f"port {preferred} 已被佔用"
        + (f"（{preferred}–{preferred + span - 1} 都滿了）" if span > 1 else ""))


# ---------------------------------------------------------------- HTTP

def make_handler(status: Status):
    # 同一種錯誤只印一次。UI 每 500ms 輪詢一次，若 handler 每次都在同一個
    # 欄位錯誤上爆掉，就會變成每秒兩次的 traceback 風暴，把真正有用的訊息淹掉。
    # （實際發生過：snapshot() 少了一個 key，log 被洗掉。）
    reported: set[str] = set()
    reported_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "VibeTalkie"

        def _report_once(self, exc: BaseException) -> None:
            sig = f"{type(exc).__name__}: {exc}"
            with reported_lock:
                if sig in reported:
                    return
                reported.add(sig)
            print(f"\n⚠️ UI API 錯誤（同一種只顯示一次）：{sig}", file=sys.stderr)

        def log_message(self, fmt, *args):        # 安靜一點
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            try:
                self._do_get()
            except Exception as exc:
                self._report_once(exc)
                try:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                except Exception:
                    pass

        def _do_get(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                f = UI_DIR / "index.html"
                if not f.exists():
                    return self._send(500, b"ui/index.html missing", "text/plain")
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(status.snapshot())
            if path == "/api/models":
                mgr = status.models
                if mgr is None:
                    return self._json({"models": [], "error": "模型管理未初始化"})
                # refresh=1 強制重抓 GitHub（UI 的「重新整理清單」按鈕）
                force = "refresh=1" in self.path
                cat = model_index.catalog(force=force)
                active = status.cfg.model_dir
                with mgr._lock:
                    dl = {k: v.snapshot() for k, v in mgr._downloads.items()}
                for m in cat["models"]:
                    info = dl.get(m["name"])
                    if info and info["state"] in ("queued", "downloading", "extracting"):
                        m["state"] = info["state"]
                    elif models.is_ready(m["name"]):
                        m["state"] = "ready"
                    elif info and info["state"] == "error":
                        m["state"] = "error"
                    else:
                        m["state"] = "absent"
                    m["download"] = info
                    m["active"] = (m["name"] == active)
                    # 目錄裡沒列、但本機已安裝的（例如自己放的）也要算 ready
                    m["title"] = m["name"]
                # 本機額外安裝、但不在官方清單裡的模型
                listed = {m["name"] for m in cat["models"]}
                for extra in models.installed_models():
                    if extra in listed:
                        continue
                    cat["models"].insert(0, {
                        "name": extra, "asset": None, "size_mb": models.disk_usage_mb(extra),
                        "family": "unknown", "supported": True, "reason": "",
                        "langs": "", "url": None, "state": "ready",
                        "download": None, "active": extra == active, "title": extra,
                        "note": "本機既有（不在官方清單中）",
                    })
                return self._json(cat)
            if path == "/api/config":
                cfg = status.cfg
                devs = []
                try:
                    for i, name, *_ in list_devices():
                        devs.append({"index": i, "name": name.strip()})
                except Exception:
                    pass
                return self._json({**cfg.public(), "devices": devs})
            # 其他靜態檔案（未來放 css/js 用）
            f = (UI_DIR / path.lstrip("/")).resolve()
            if UI_DIR in f.parents and f.is_file():
                ctype = "text/plain"
                if f.suffix == ".css":
                    ctype = "text/css; charset=utf-8"
                elif f.suffix == ".js":
                    ctype = "application/javascript; charset=utf-8"
                return self._send(200, f.read_bytes(), ctype)
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                self._do_post()
            except Exception as exc:
                self._report_once(exc)
                try:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                except Exception:
                    pass

        def _do_post(self):
            if self.path == "/api/models/download":
                return self._model_action("download")
            if self.path == "/api/models/cancel":
                return self._model_action("cancel")
            if self.path == "/api/models/select":
                return self._model_select()
            if self.path == "/api/test-key":
                return self._test_key()
            if self.path != "/api/config":
                return self._json({"error": "unknown endpoint"}, 404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                patch = json.loads(self.rfile.read(n) or b"{}")
            except Exception as exc:
                return self._json({"error": f"bad json: {exc}"}, 400)

            cfg = status.cfg
            for k in ("traditional", "mode", "device_index", "language", "mic_name",
                      "remove_trailing_period"):
                if k in patch:
                    setattr(cfg, k, patch[k])

            # 麥克風優先順序（第一個是主、其餘是副）。只留字串，去空白與重複。
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
                # 兼容舊欄位：主麥克風同步到 mic_name，讓舊程式與文件一致
                cfg.mic_name = order[0] if order else ""

            # 錄音鍵：**一定要能解析才存**。存進一個看不懂的字串，
            # 使用者會以為設定生效了，實際上還在用上一個鍵 ——
            # 這種「靜默退回」最難查。
            #
            # ⚠️ 支援**多組**，而且每一組可以帶自己的觸發方式與結束鍵
            # （`F9@double`、`F9,Escape`、`Ctrl+Alt+R,Escape@toggle`；
            # 沒寫 `@` 就用全域 trigger_mode）。
            # 也接受舊的單一字串 `hotkey`（舊版 UI／手寫的呼叫端）。
            #
            # ⚠️ 驗證邏輯在 `config.apply_hotkeys_patch()` —— **兩個平台共用
            #    同一份**。在這裡自己寫一份的症狀是「同一筆設定在 Windows
            #    存得進去、在 mac 被拒」，而使用者完全無法理解為什麼。
            if "hotkeys" in patch or "hotkey" in patch:
                raw = patch.get("hotkeys", None)
                if raw is None:
                    one = str(patch.get("hotkey") or "").strip()
                    raw = [one] if one else []
                err = config_module.apply_hotkeys_patch(cfg, raw)
                if err:
                    return self._json({"error": err}, 400)

            if "trigger_mode" in patch:
                val = str(patch["trigger_mode"])
                if val not in config_module.TRIGGER_MODES_VALUES:
                    return self._json(
                        {"error": f"trigger_mode 只能是 "
                                  f"{'、'.join(config_module.TRIGGER_MODES_VALUES)}"}, 400)
                cfg.trigger_mode = val
            if "double_tap_ms" in patch:
                try:
                    ms = int(patch["double_tap_ms"])
                except (TypeError, ValueError):
                    return self._json({"error": "double_tap_ms 必須是整數"}, 400)
                cfg.double_tap_ms = max(150, min(1000, ms))

            # 麥克風串流模式：只接受已知值，避免設定檔被塞進無效字串後
            # 悄悄退回某個模式（那會讓「為什麼沒效果」變成無解的謎）
            if "mic_stream" in patch:
                val = str(patch["mic_stream"])
                if val in config_module.MIC_STREAM_VALUES:
                    cfg.mic_stream = val
                else:
                    return self._json(
                        {"error": f"mic_stream 只能是 "
                                  f"{'、'.join(config_module.MIC_STREAM_VALUES)}"}, 400)
            if "idle_timeout_s" in patch:
                try:
                    secs = float(patch["idle_timeout_s"])
                except (TypeError, ValueError):
                    return self._json({"error": "idle_timeout_s 必須是數字"}, 400)
                cfg.idle_timeout_s = max(config_module.IDLE_TIMEOUT_MIN,
                                         min(config_module.IDLE_TIMEOUT_MAX, secs))
            path = cfg.save()
            # 告訴 daemon「這是我們自己寫的」—— 否則 _sync_config() 會把它
            # 當成外部修改而從磁碟重讀，造成設定反覆被覆蓋、模型反覆重新載入。
            if status.daemon is not None:
                try:
                    status.daemon.config_saved(path)
                except Exception:
                    pass
            self._json({"ok": True, "saved": str(path),
                        "note": "裝置、輸出入方式、句號與麥克風串流模式都會立即生效，不必重啟"})

        # ---- 模型 ----
        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def _model_action(self, action: str):
            mgr = status.models
            if mgr is None:
                return self._json({"ok": False, "message": "模型管理未初始化"}, 503)
            try:
                name = (self._body().get("name") or "").strip()
            except Exception as exc:
                return self._json({"ok": False, "message": f"bad json: {exc}"}, 400)
            if not name:
                return self._json({"ok": False, "message": "缺少 name"}, 400)
            ok, msg = (mgr.start(name) if action == "download" else mgr.cancel(name))
            # 被拒絕的請求不該回 200。實測踩過：路徑跳脫的名稱回 200 + 「名稱不合法」，
            # 語意上等於「成功但訊息很奇怪」，讓呼叫端很難判斷。
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        def _test_key(self):
            """測試某個組合：聽按鍵、回報結果，**不錄音也不注入**。

            為什麼需要：設定好之後，驗證方式只有「按下去看有沒有反應」，
            但那會真的開始錄音、把文字注入使用者正在打的地方。
            這裡提供一個沒有副作用的驗證路徑（實測回饋：「除了沒有測試之外…」）。
            """
            daemon = status.daemon
            if daemon is None or not hasattr(daemon, "start_key_test"):
                return self._json({"ok": False,
                                   "message": "常駐程式未啟動（用 app/ 的介面才有）"},
                                  503)
            try:
                body = self._body()
            except Exception as exc:
                return self._json({"ok": False, "message": f"bad json: {exc}"}, 400)
            if body.get("cancel"):
                daemon.cancel_key_test()
                return self._json({"ok": True, "cancelled": True})
            try:
                secs = float(body.get("seconds") or 20.0)
            except (TypeError, ValueError):
                return self._json({"ok": False, "message": "seconds 必須是數字"}, 400)
            res = daemon.start_key_test(secs, str(body.get("which") or ""))
            return self._json(res)

        def _model_select(self):
            mgr = status.models
            if mgr is None:
                return self._json({"ok": False, "message": "模型管理未初始化"}, 503)
            try:
                name = (self._body().get("name") or "").strip()
            except Exception as exc:
                return self._json({"ok": False, "message": f"bad json: {exc}"}, 400)

            if not models.is_ready(name):
                return self._json({"ok": False,
                                   "message": "這個模型還沒下載完成"}, 400)

            # ⚠️ 錄音中換模型會讓正在跑的辨識拿到半個狀態
            d = status.daemon
            if d is not None and d.state != "IDLE":
                return self._json({
                    "ok": False,
                    "message": f"目前狀態是 {d.state}，請等它回到待命再切換模型",
                }, 409)

            eng = status.engine
            if eng is None:
                return self._json({"ok": False, "message": "引擎未初始化"}, 503)
            try:
                eng.reload(models.model_dir(name))
            except Exception as exc:
                status.error = f"切換模型失敗：{exc}"
                return self._json({"ok": False,
                                   "message": f"載入失敗：{exc}"}, 500)

            status.cfg.model_dir = name
            path = status.cfg.save()
            # 同上：這是我們自己寫的，別讓 _sync_config() 再從磁碟覆蓋一次
            if status.daemon is not None:
                try:
                    status.daemon.config_saved(path)
                except Exception:
                    pass
            status.model_name = name
            status.error = None
            print(f"🔁 已切換模型：{name}")
            return self._json({"ok": True, "message": f"已切換到 {name}",
                               "model": name})

    return Handler


def start_server(status: Status, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(status))
    t = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    t.start()
    return httpd


# ---------------------------------------------------------------- 主程式

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VibeTalkie 常駐工具")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只辨識不注入")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--seconds", type=float, default=None, help="跑 N 秒後結束（測試）")
    args = parser.parse_args(argv)
    setup_console()

    cfg = Config.load()

    # ------------------------------------------------------------------
    # ⚠️ **先確認沒有另一個實例在跑，再開始做任何事。**
    #
    # 放在最前面（早於列舉裝置、載入模型）有三個理由：
    #
    #   1. **快**：重複啟動會在幾十毫秒內被拒絕。放在後面的版本要等
    #      模型載入完（數秒）才知道，使用者看到的是「按了啟動之後
    #      卡住」而不是「已經在執行了」。
    #   2. **不要有副作用**：下面會 `cfg.save()`（L734 原本那行）。
    #      若先存檔才發現重複，那個「重複的行程」已經動過設定檔了 ——
    #      而這整件事的問題就是兩個行程在動同一個檔案。
    #   3. **不要白做**：載入模型、暖機都要時間與記憶體，全部丟掉很浪費。
    #
    # ⚠️ 仍然尊重 `--port`：明確指定別的 port 就是「我要刻意跑第二個」，
    #    那是有正當用途的（測試、同時比較兩個模型），不該擋。
    # ------------------------------------------------------------------
    try:
        port = pick_port(args.port or cfg.port)
    except AlreadyRunning as exc:
        # ⚠️ 訊息要**具體可行**，不能只說「失敗」——
        #    使用者看到的是「我明明按了啟動」，他需要知道去哪裡關掉舊的。
        print("\n  ⚠️ VibeTalkie 好像已經在執行了。")
        print(f"     {exc}")
        print("\n  同時跑兩個會讓**設定互相覆蓋**（各自記一份，存檔時整個寫回），")
        print("  症狀是「設定存了又變回去、模型自己換掉」。所以這裡刻意拒絕啟動。")
        print("\n  請先關掉舊的那一個：")
        print("    · 舊的 VibeTalkie 視窗（那個黑色命令提示字元視窗）按 Ctrl+C")
        print("    · 或在工作管理員結束 Python 行程")
        print("\n  想刻意同時跑兩個（例如測試）請指定不同 port：")
        print(f"    python app/vibetalkie.py --port {cfg.port + 100}")
        return 1

    print("=" * 62)
    print("  VibeTalkie")
    print("=" * 62)
    print(f"設定檔：{cfg.save()}")

    devs = []
    try:
        devs = [{"index": i, "name": n.strip()} for i, n, *_ in list_devices()]
    except Exception as exc:
        print(f"⚠️ 列舉音訊裝置失敗：{exc}")
    if devs:
        names = ", ".join(f"[{d['index']}] {d['name']}" for d in devs)
        print(f"音訊裝置：{names}")

    # 用名稱解析出「現在」的索引（index 會隨重連改變，見 resolve_device）
    dev_index, dev_name, _ = resolve_device(cfg)
    mic_warning = None
    if dev_name:
        print(f"使用麥克風：[{dev_index}] {dev_name}")
        if cfg.mic_name != dev_name:          # 第一次或名稱被截斷 → 記下來
            cfg.mic_name, cfg.device_index = dev_name, dev_index
            cfg.save()
    else:
        print(f"使用麥克風：index {dev_index}（尚未綁定名稱，"
              f"請在設定介面選一次以固定下來）")
        if cfg.mic_name:
            # 設定檔綁的麥克風不見了（藍牙關機／斷線／換裝置）
            mic_warning = (f"設定的麥克風「{cfg.mic_name}」目前不在裝置清單中，"
                           f"已暫時改用 index {dev_index}。"
                           f"請確認麥克風已開機連線，或在設定介面重新選擇。")

    # ⚠️ 只建一次引擎，而且用**修正後**的 model_dir。
    #    原本兩條分支各建一次，而 `engine.reload()` 只在第一次載入 ——
    #    所以「設定的模型不存在 → 改用別的」那條路上，引擎其實已經被
    #    建成指向不存在的目錄了（先建再改，改的是 cfg 不是 engine）。
    if not models.is_ready(cfg.model_dir):
        # 設定檔指的模型不在 → 退回任何一個已安裝的，沒有就給明確指引
        have = models.installed_models()
        if not have:
            print("❌ 尚未安裝任何語音模型。")
            print("   啟動後在設定介面下載，或執行：")
            print(f"   python tools/p1/fetch_model.py --get "
                  f"{models.CATALOG[0].name}")
            return 1
        print(f"⚠️ 設定的模型「{cfg.model_dir}」不存在，改用「{have[0]}」")
        cfg.model_dir = have[0]
        cfg.save()

    engine = build_engine(cfg.engine, threads=cfg.threads,
                          model_dir=models.model_dir(cfg.model_dir))

    ok, why = engine.is_available()
    if not ok:
        print(f"❌ 引擎不可用：{why}")
        return 1
    print(f"辨識引擎：{engine.name}（本地）　模型：{engine.describe()}")
    print(f"繁體輸出：{'是' if cfg.traditional and has_opencc() else '否'}")
    engine.warmup()

    status = Status(cfg)
    status.model_name = cfg.model_dir
    status.vendor_warning = check_vendor_tool()
    status.mic_warning = mic_warning
    status.bt_warning = check_bt_mic_warning(cfg, devs)
    status.engine = engine
    status.models = ModelManager(on_change=lambda: None)
    status.opencc = has_opencc()

    daemon = PttDaemon(
        engine,
        device_filter="00001124",
        traditional=cfg.traditional,
        remove_period=cfg.remove_trailing_period,
        mode=cfg.mode,
        dry_run=args.dry_run,
        device_index=dev_index,
        debug=args.debug,
        on_state=status.on_state,
        on_result=status.on_result,
        # 每次錄音都重新解析裝置 → 在設定介面換麥克風之後**不必重啟**。
        # 走優先順序：主麥克風不在就用副的，主的一回來就自動換回。
        device_provider=lambda: resolve_mic_priority(cfg)[0],
        # 即時讀設定 → 改「繁體輸出／移除句號／注入方式」也不必重啟
        cfg_provider=lambda: cfg,
        # 手改 config.toml 也偵測得到（見 PttDaemon._sync_config）
        config_path=config_module.CONFIG_PATH,
        config_loader=config_module.Config.load,
        # 藍牙麥克風省電休眠後回來時，自動換回設定指定的那一支
        target_online_probe=lambda: target_mic_online(cfg),
    )
    status.daemon = daemon

    # port 已經在上面確認過了（見 main() 開頭的說明）
    start_server(status, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n設定介面：{url}")
    if not args.no_browser and cfg.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    # 把錄音音量餵給 UI（每秒 20 次）
    stop = threading.Event()

    def level_pump() -> None:
        while not stop.is_set():
            if daemon.state == "RECORDING":
                status.level = daemon.poll_level()
            else:
                status.level = 0.0
            status.stats = daemon.stats
            status.latency = daemon.last_latency
            time.sleep(0.05)

    threading.Thread(target=level_pump, name="level", daemon=True).start()

    try:
        daemon.run(args.seconds)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
