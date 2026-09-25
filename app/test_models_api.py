#!/usr/bin/env python3
"""模型 API 的測試（在行程內起一個伺服器，不碰真的下載）。

為什麼要有：
    這支 API 已經抓到兩個真實缺陷 ——
      1. 被拒絕的請求回 HTTP 200（語意上等於「成功但訊息很怪」）
      2. 路徑跳脫的名稱沒有被擋
    這類「端點行為」用手動測很容易漏，寫成測試才盯得住。

刻意**不**測真的下載：那會抓 78–234 MB，不該在測試裡做。
下載路徑只測「拒絕」與「取消」的分支。

執行：python app/test_models_api.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools" / "p1"))
sys.path.insert(0, str(ROOT / "tools" / "p0"))

import models  # noqa: E402
from config import Config  # noqa: E402
from models import ModelManager  # noqa: E402
from speech_engine import SenseVoiceEngine  # noqa: E402
from vibetalkie import Status, start_server  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    cfg = Config.load()
    ready = models.installed_models()
    if not ready:
        print("⚠️ 本機沒有任何已安裝模型，部分測試會略過。")
        print("   先跑 python tools/p1/fetch_model.py --get <模型名>")
        return 0

    cfg.model_dir = ready[0]
    status = Status(cfg)
    status.engine = SenseVoiceEngine(model_dir=models.model_dir(ready[0]))
    status.models = ModelManager()
    status.model_name = ready[0]

    port = free_port()
    httpd = start_server(status, port)
    base = f"http://127.0.0.1:{port}"
    print("=" * 68)
    print(f"模型 API 測試（{base}）")
    print("=" * 68)

    def call(path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except Exception:
                return e.code, {}

    try:
        print("\n[1] GET /api/models")
        st, r = call("/api/models")
        check("回 200", st == 200, f"HTTP {st}")
        check("有 models 陣列", isinstance(r.get("models"), list))
        check("目錄非空", len(r.get("models", [])) > 0,
              f"{len(r.get('models', []))} 筆")
        states = {m["state"] for m in r["models"]}
        check("狀態值合法",
              states <= {"ready", "absent", "queued", "downloading",
                         "extracting", "error", "cancelled"},
              str(states))
        active = [m for m in r["models"] if m["active"]]
        check("恰好一個使用中", len(active) == 1, f"{len(active)} 個")

        print("\n[2] 切換到已安裝的模型")
        st, r2 = call("/api/models/select", "POST", {"name": ready[0]})
        check("回 200", st == 200, f"HTTP {st} — {r2.get('message')}")

        print("\n[3] 切換到不存在的模型 → 必須被拒絕")
        st, r3 = call("/api/models/select", "POST", {"name": "no-such-model-xyz"})
        check("回 400（不是 200）", st == 400, f"HTTP {st}")
        check("有說明訊息", bool(r3.get("message")), str(r3.get("message")))

        print("\n[4] 下載時缺少 name → 400")
        st, r4 = call("/api/models/download", "POST", {})
        check("回 400", st == 400, f"HTTP {st}")

        print("\n[5] 下載名稱含路徑跳脫 → 必須被拒絕且回 400")
        for bad in ("../../evil", "a/b", "..\\win"):
            st, r5 = call("/api/models/download", "POST", {"name": bad})
            check(f"拒絕 {bad!r} 並回 400", st == 400,
                  f"HTTP {st} — {r5.get('message')}")

        print("\n[6] 對沒有在下載的模型按取消 → 400，不該假裝成功")
        st, r6 = call("/api/models/cancel", "POST", {"name": ready[0]})
        check("回 400", st == 400, f"HTTP {st} — {r6.get('message')}")

        print("\n[7] 未知端點 → 404")
        st, _ = call("/api/nope", "POST", {})
        check("回 404", st == 404, f"HTTP {st}")

        print("\n[8] /api/status 要帶 downloads 欄位（UI 靠它顯示進度）")
        st, s = call("/api/status")
        check("有 downloads 欄位", "downloads" in s)
        check("downloads 是陣列", isinstance(s.get("downloads"), list))
    finally:
        httpd.shutdown()

    print("\n" + "=" * 68)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
