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
            "engine": d["engine"],
            "model": d["model"],
            "error": d["error"],
            "downloads": (self.models.active_downloads() if self.models else []),
            "models_dir": self.models_dir,
            "config_path": self.config_path,
            "opencc": self.opencc,
        }


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


def pick_port(preferred: int) -> int:
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return preferred


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
            path = cfg.save()
            self._json({"ok": True, "saved": str(path),
                        "note": "裝置、輸出入方式與句號設定都會立即生效，不必重啟"})

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
            status.cfg.save()
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

    engine = build_engine(cfg.engine, threads=cfg.threads)
    if not models.is_ready(cfg.model_dir):
        # 設定檔指的模型不在 → 退回任何一個已安裝的，沒有就給明確指引
        have = models.installed_models()
        if have:
            print(f"⚠️ 設定的模型「{cfg.model_dir}」不存在，改用「{have[0]}」")
            cfg.model_dir = have[0]
            cfg.save()
            engine = build_engine(cfg.engine, threads=cfg.threads,
                                  model_dir=models.model_dir(cfg.model_dir))
        else:
            print("❌ 尚未安裝任何語音模型。")
            print("   啟動後在設定介面下載，或執行：")
            print(f"   python tools/p1/fetch_model.py --get "
                  f"{models.CATALOG[0].name}")
            return 1
    else:
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
        # 每次錄音都重新解析裝置 → 在設定介面換麥克風之後**不必重啟**
        device_provider=lambda: resolve_device(cfg, quiet=True)[0],
        # 即時讀設定 → 改「繁體輸出／移除句號／注入方式」也不必重啟
        cfg_provider=lambda: cfg,
    )
    status.daemon = daemon

    port = pick_port(args.port or cfg.port)
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
