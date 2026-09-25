#!/usr/bin/env python3
"""UI 與 /api/status 的**欄位契約測試**。

為什麼要有這個？
    實際踩到的 bug：我在 `Status.snapshot()` 的回傳裡加了 `mic_warning`，
    卻忘了在來源 dict `d` 裡也加。結果是 `KeyError: 'mic_warning'`，
    而 UI **每 500ms 輪詢一次** → 每秒兩次的 traceback 風暴。

    這種「生產端與消費端欄位漂移」的 bug 用肉眼很難持續盯住，
    但用程式檢查很簡單：**app.js 讀了哪些 `s.<欄位>`，snapshot() 就必須提供。**

執行：python app/test_status_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools" / "p1"))
sys.path.insert(0, str(ROOT / "tools" / "p0"))

from config import Config  # noqa: E402
from vibetalkie import Status  # noqa: E402

UI_JS = HERE / "ui" / "app.js"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def fields_used_by_ui() -> set[str]:
    """從 app.js 抓出所有 `s.<欄位>` 的引用。"""
    js = UI_JS.read_text(encoding="utf-8")
    used = set(re.findall(r"\bs\.([A-Za-z_][A-Za-z0-9_]*)", js))
    # JS 內建屬性不是 API 欄位
    return {u for u in used if u not in {"length", "map", "filter", "join", "textContent"}}


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

    print("=" * 68)
    print("UI ↔ /api/status 欄位契約測試")
    print("=" * 68)

    print("\n[1] snapshot() 不應該拋錯")
    status = Status(Config())
    try:
        snap = status.snapshot()
        check("snapshot() 可呼叫", True)
    except Exception as exc:
        check("snapshot() 可呼叫", False, f"{type(exc).__name__}: {exc}")
        print("\n❌ 無法繼續，snapshot() 本身就是壞的")
        return 1

    print("\n[2] app.js 讀的每個欄位，snapshot() 都要提供")
    used = sorted(fields_used_by_ui())
    print(f"     app.js 共引用 {len(used)} 個欄位：{', '.join(used)}")
    missing = [f for f in used if f not in snap]
    check("沒有缺少的欄位", not missing,
          f"缺少 {missing}" if missing else "全部具備")

    print("\n[3] snapshot() 的 JSON 必須可序列化")
    import json
    try:
        json.dumps(snap, ensure_ascii=False)
        check("可 JSON 序列化", True)
    except Exception as exc:
        check("可 JSON 序列化", False, f"{type(exc).__name__}: {exc}")

    print("\n[4] 關鍵欄位的型別")
    for key, typ in (("state", str), ("level", (int, float)),
                     ("presses", int), ("history", list)):
        check(f"{key} 是 {getattr(typ, '__name__', typ)}",
              isinstance(snap.get(key), typ),
              f"實際 {type(snap.get(key)).__name__}")

    print("\n[5] 警告欄位預設為 None（沒有警告時 UI 要能正確隱藏）")
    check("vendor_warning 預設 None", snap.get("vendor_warning") is None)
    check("mic_warning 預設 None", snap.get("mic_warning") is None)

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
