#!/usr/bin/env python3
"""`/api/config` 的欄位契約與驗證測試。

## 為什麼要有這支

`test_status_contract.py` 顧的是「UI ↔ `/api/status`」，這支顧的是
「UI ↔ `/api/config`」—— 兩條路徑不同，壞掉的方式也不同。

實際踩到的兩個坑，都在這裡變成永久檢查：

1. **設定值沒有被驗證**：`mic_stream` 若接受任意字串，設定檔會被塞進
   無效值，程式再悄悄退回某個模式 → 使用者只看到「怎麼沒效果」，
   卻查不出原因。所以非法值必須**明確回 400**，且**不可改壞原設定**。

2. **⚠️ 測試自己覆蓋了使用者的 `config.toml`**（第一次寫這支測試時真的發生）：
   `/api/config` 的 handler 用的是記憶體中的 `cfg`，它不知道呼叫端想存到哪，
   於是 `cfg.save()` 就寫到真正的 `config.toml`，把 `device_index` 與
   `mic_name` 一起重設掉。修法是把 `cfg.save` 導向暫存檔（見下方）。
   **測試不准動使用者的設定。**

執行：python tests/test_config_api.py
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import config as config_module  # noqa: E402
import vibetalkie as vt  # noqa: E402
from config import IDLE_TIMEOUT_MAX, IDLE_TIMEOUT_MIN, MIC_STREAM_VALUES  # noqa: E402

failures: list[str] = []
TMP = ROOT / "artifacts" / "pytest-config-api.toml"


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    print("=" * 70)
    print("/api/config 契約測試")
    print("=" * 70)

    TMP.unlink(missing_ok=True)
    cfg = config_module.Config()
    # 一律寫暫存檔 —— 絕不碰使用者的 config.toml（理由見檔頭）
    cfg.save = lambda path=None: config_module.Config.save(cfg, TMP)
    cfg.save()

    status = vt.Status(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), vt.make_handler(status))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path: str):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return json.loads(r.read().decode())

    def post(path: str, body: dict):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    try:
        print("\n[1] UI 需要的欄位都要在")
        c = get("/api/config")
        for key in ("mic_stream", "idle_timeout_s", "mic_stream_options",
                    "devices", "device_index", "mode", "traditional"):
            check(f"有 {key}", key in c)
        opts = c.get("mic_stream_options") or []
        check("三個模式都提供", len(opts) == 3, str([o.get("value") for o in opts]))
        check("每個模式都有 label", all(o.get("label") for o in opts))
        check("每個模式都寫明代價（UI 不自己編）", all(o.get("note") for o in opts))
        check("選項值與設定層一致",
              tuple(o["value"] for o in opts) == MIC_STREAM_VALUES)

        print("\n[2] 合法值要生效，而且立刻反映在下一次 GET")
        for value in MIC_STREAM_VALUES:
            st, body = post("/api/config", {"mic_stream": value})
            check(f"接受 {value}", st == 200 and body.get("ok"), f"HTTP {st}")
            check(f"{value} 生效", cfg.mic_stream == value, cfg.mic_stream)
            check(f"{value} 反映在 GET", get("/api/config")["mic_stream"] == value)

        print("\n[3] 非法值必須被拒絕，不可靜默退回")
        st, body = post("/api/config", {"mic_stream": "banana"})
        check("非法值回 400", st == 400, f"HTTP {st}")
        check("錯誤訊息有說明", "mic_stream" in str(body.get("error", "")))
        check("原設定沒有被改壞", cfg.mic_stream in MIC_STREAM_VALUES, cfg.mic_stream)

        print("\n[4] idle_timeout_s 的型別與範圍")
        post("/api/config", {"idle_timeout_s": 9999})
        check(f"過大夾到 {IDLE_TIMEOUT_MAX:g}",
              cfg.idle_timeout_s == IDLE_TIMEOUT_MAX, str(cfg.idle_timeout_s))
        post("/api/config", {"idle_timeout_s": -5})
        check(f"過小夾到 {IDLE_TIMEOUT_MIN:g}"
              f"（下限刻意不是 1 —— 太短會讓中間路線失效）",
              cfg.idle_timeout_s == IDLE_TIMEOUT_MIN, str(cfg.idle_timeout_s))
        post("/api/config", {"idle_timeout_s": 2})
        check("實測踩過的陷阱值 2 秒會被抬到下限",
              cfg.idle_timeout_s == IDLE_TIMEOUT_MIN, str(cfg.idle_timeout_s))
        st, _ = post("/api/config", {"idle_timeout_s": "abc"})
        check("非數字回 400", st == 400, f"HTTP {st}")

        print("\n[5] 存檔後要能重新載入（否則重啟就白設定了）")
        post("/api/config", {"mic_stream": "session", "idle_timeout_s": 12})
        again = config_module.Config.load(TMP)
        check("mic_stream 保留", again.mic_stream == "session", again.mic_stream)
        check("idle_timeout_s 保留", again.idle_timeout_s == 12.0,
              str(again.idle_timeout_s))
        raw = TMP.read_text(encoding="utf-8")
        check("TOML 有 [bluetooth] 區段", "[bluetooth]" in raw)
        check("TOML 裡有 mic_stream", "mic_stream" in raw)
    finally:
        httpd.shutdown()
        TMP.unlink(missing_ok=True)

    print("\n" + "=" * 70)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
