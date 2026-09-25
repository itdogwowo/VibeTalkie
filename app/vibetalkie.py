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
sys.path.insert(0, str(ROOT / "tools" / "p1"))
sys.path.insert(0, str(ROOT / "tools" / "p0"))

from config import Config  # noqa: E402
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
        self.engine_name = cfg.engine
        self.model_name = ""
        self.error: str | None = None

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
                "engine": self.engine_name,
                "model": self.model_name,
                "error": self.error,
            }
        # UI 的欄位名稱
        s = d["stats"]
        return {
            "state": d["state"],
            "level": d["level"],
            "presses": s["presses"], "inserted": s["inserted"],
            "empty": s["empty"], "failed": s["failed"],
            "history": d["history"],
            "latency_ms": (d["latency"] or {}).get("total_ms"),
            "asr_ms": (d["latency"] or {}).get("asr_ms"),
            "paste_ms": (d["latency"] or {}).get("paste_ms"),
            "vendor_warning": d["vendor_warning"],
            "engine": d["engine"],
            "model": d["model"],
            "error": d["error"],
        }


# ---------------------------------------------------------------- 裝置解析

def resolve_device(cfg: Config) -> tuple[int, str, list[dict]]:
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
        print(f"⚠️ 設定檔指定的麥克風「{name}」目前不在清單中。")
        print(f"   改用 index {cfg.device_index}："
              f"{next((d['name'] for d in devs if d['index'] == cfg.device_index), '（不存在）')}")
        print("   請在設定介面重新選擇麥克風。")
        return cfg.device_index, "", devs

    # 還沒綁定名稱 → 嘗試認出本專案的目標裝置（見 docs/hardware.md §1）
    for d in devs:
        if any(h in d["name"] for h in TARGET_HINTS):
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


def pick_port(preferred: int) -> int:
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return preferred


# ---------------------------------------------------------------- HTTP

def make_handler(status: Status):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VibeTalkie"

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
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                f = UI_DIR / "index.html"
                if not f.exists():
                    return self._send(500, b"ui/index.html missing", "text/plain")
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(status.snapshot())
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
            if self.path != "/api/config":
                return self._json({"error": "unknown endpoint"}, 404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                patch = json.loads(self.rfile.read(n) or b"{}")
            except Exception as exc:
                return self._json({"error": f"bad json: {exc}"}, 400)

            cfg = status.cfg
            for k in ("traditional", "mode", "device_index", "language", "mic_name"):
                if k in patch:
                    setattr(cfg, k, patch[k])
            path = cfg.save()
            self._json({"ok": True, "saved": str(path),
                        "note": "部分設定需要重新啟動 VibeTalkie 才生效"})

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
    if dev_name:
        print(f"使用麥克風：[{dev_index}] {dev_name}")
        if cfg.mic_name != dev_name:          # 第一次或名稱被截斷 → 記下來
            cfg.mic_name, cfg.device_index = dev_name, dev_index
            cfg.save()
    else:
        print(f"使用麥克風：index {dev_index}（尚未綁定名稱，"
              f"請在設定介面選一次以固定下來）")

    engine = build_engine(cfg.engine, threads=cfg.threads)
    ok, why = engine.is_available()
    if not ok:
        print(f"❌ 引擎不可用：{why}")
        return 1
    print(f"辨識引擎：{engine.name}（本地）　繁體輸出："
          f"{'是' if cfg.traditional and has_opencc() else '否'}")
    engine.warmup()

    status = Status(cfg)
    status.model_name = getattr(engine, "model_dir", Path("")).name or "—"
    status.vendor_warning = check_vendor_tool()

    daemon = PttDaemon(
        engine,
        device_filter="00001124",
        traditional=cfg.traditional,
        mode=cfg.mode,
        dry_run=args.dry_run,
        device_index=dev_index,
        debug=args.debug,
        on_state=status.on_state,
        on_result=status.on_result,
    )

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
