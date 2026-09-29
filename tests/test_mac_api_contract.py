#!/usr/bin/env python3
"""API 回應格式的契約測試（macOS 版）。

## 為什麼需要這個（實際踩到）

`/api/models` 第一版我**自作主張回了一個不同的結構**：

    我回的：  {"installed": [...], "catalog": [...], "current": "..."}
    UI 要的： {"models": [{name, title, state, download, active, supported, ...}]}

後果：`app.js` 讀 `d.models` 得到 `undefined` → **模型分頁整片空白**，
而且**後端完全沒有錯誤**（HTTP 200、JSON 合法）。

同一個 commit 裡我還把 `snapshot().downloads` 寫死成 `[]`，
於是「按了下載完全沒反應」——其實下載有在跑，只是 UI 看不到進度。

**兩個平台共用同一份 UI，所以 HTTP 回應格式不是實作細節，是契約。**
`test_mac_ui_contract.py` 只驗證 `/api/status` 的欄位，
這個檔案補上其他端點的**回應形狀**。

執行：python tests/test_mac_api_contract.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

import config as config_module      # noqa: E402
import ui_server                    # noqa: E402

UI_JS = ROOT / "app" / "ui" / "app.js"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def ui_api_paths() -> set[str]:
    """從 app.js 抓出它呼叫的 API 路徑。"""
    js = UI_JS.read_text(encoding="utf-8")
    return set(re.findall(r"['\"`](/api/[a-z/]+)['\"`]", js))


def main() -> int:
    print("=" * 74)
    print("API 回應格式契約測試（macOS）")
    print("=" * 74)

    if not UI_JS.is_file():
        print(f"\n  ❌ 找不到 {UI_JS}")
        return 2

    cfg = config_module.Config.load()

    print("\n▍UI 呼叫的端點，我都有實作嗎")
    # 從原始碼抓出 ui_server 有處理的路徑
    src = (ROOT / "app" / "core" / "ui_server.py").read_text(encoding="utf-8")
    handled = set(re.findall(r'path == "(/api/[a-z/]+)"', src))
    called = ui_api_paths()
    # UI 也可能用 query string（例如 /api/models?refresh=1）
    check("UI 呼叫的端點都有處理", called <= handled,
          f"沒處理：{sorted(called - handled)}" if called - handled
          else f"{len(called)} 個端點")

    # ---- /api/models ----
    print("\n▍/api/models：形狀必須是 {'models': [...]}（不是自創結構）")
    payload = ui_server._models_payload(cfg)                # noqa: SLF001
    check("頂層有 'models' 鍵（UI 讀 d.models）", "models" in payload,
          f"實際頂層鍵：{sorted(payload.keys())}")
    ms = payload.get("models")
    check("'models' 是陣列", isinstance(ms, list), type(ms).__name__)
    if isinstance(ms, list) and ms:
        m = ms[0]
        # UI 的 modelCard() 直接讀這些
        for k in ("name", "title", "state", "supported"):
            check(f"每個模型有 {k!r}", k in m, f"第一個模型的鍵：{sorted(m.keys())}")
        # state 必須是 MODEL_STATE 認得的值
        js = UI_JS.read_text(encoding="utf-8")
        mm = re.search(r"const MODEL_STATE\s*=\s*\{(.*?)\};", js, re.S)
        if mm:
            known = set(re.findall(r"(\w+)\s*:", mm.group(1)))
            states = {x.get("state") for x in ms}
            check("所有 state 都是 UI 認得的",
                  all(s in known for s in states),
                  f"state 值 {sorted(states)} vs MODEL_STATE {sorted(known)}")
    else:
        check("有模型可測", False, "models 是空的")

    # ---- /api/status 的 downloads ----
    print("\n▍/api/status 的 downloads：必須是真的下載進度，不是寫死的空陣列")
    st = ui_server.Status(cfg, platform="macos")
    snap = st.snapshot()
    check("downloads 是 list", isinstance(snap.get("downloads"), list),
          type(snap.get("downloads")).__name__)
    # 原始碼裡不可以再出現寫死的空陣列
    check("原始碼沒有把 downloads 寫死成 []",
          '"downloads": []' not in (ROOT / "app" / "core" / "ui_server.py")
          .read_text(encoding="utf-8"),
          "寫死的話 UI 進度條永遠不會出現")

    # ---- 每個 /api/* 的回應都要能 JSON 序列化 ----
    print("\n▍所有回應都要能序列化成 JSON（要透過 HTTP 送出去）")
    for name, obj in (("/api/status", snap),
                      ("/api/config", {**cfg.public(), "devices": []}),
                      ("/api/models", payload)):
        try:
            json.dumps(obj, ensure_ascii=False)
            ok, err = True, ""
        except Exception as exc:                        # noqa: BLE001
            ok, err = False, f"{type(exc).__name__}: {exc}"
        check(f"{name} 可序列化", ok, err)

    # ---- POST 端點的錯誤處理 ----
    print("\n▍POST 端點要檢查回傳值（不然失敗會被靜默吞掉）")
    src = (ROOT / "app" / "core" / "ui_server.py").read_text(encoding="utf-8")
    for fn, why in (("_post_models_download", "mgr.start() 回傳 (ok, msg)"),
                    ("_post_models_cancel", "mgr.cancel() 回傳 (ok, msg)"),
                    ("_post_models_retry", "mgr.retry() 回傳 (ok, msg)")):
        m = re.search(rf"def {fn}\(.*?\n(.*?)(?=\n        def |\n    return Handler)",
                      src, re.S)
        body = m.group(1) if m else ""
        check(f"{fn} 有檢查回傳值", "if not ok" in body or "ok, msg" in body,
              why)

    print("\n" + "=" * 74)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
