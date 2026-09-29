#!/usr/bin/env python3
"""UI 與 /api/status 的**欄位契約測試**。

為什麼要有這個？
    實際踩到的 bug：我在 `Status.snapshot()` 的回傳裡加了 `mic_warning`，
    卻忘了在來源 dict `d` 裡也加。結果是 `KeyError: 'mic_warning'`，
    而 UI **每 500ms 輪詢一次** → 每秒兩次的 traceback 風暴。

    這種「生產端與消費端欄位漂移」的 bug 用肉眼很難持續盯住，
    但用程式檢查很簡單：**app.js 讀了哪些 `s.<欄位>`，snapshot() 就必須提供。**

執行：python tests/test_status_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

from config import Config  # noqa: E402
from vibetalkie import Status  # noqa: E402

UI_JS = ROOT / "app" / "ui" / "app.js"

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


def check_snapshot(snap: dict, label: str) -> None:
    """一個 `snapshot()` 物件與 app.js 的欄位契約。

    **兩個平台共用同一份 app/ui/，所以這個檢查要能重複套用在兩邊** ——
    抽成函式而不是複製兩段，理由與 `snapshot()` 本身一樣：兩份檢查會漂移，
    而漂移的那一邊就是「沒被驗到的平台」。
    """
    import json
    used = sorted(fields_used_by_ui())
    missing = [f for f in used if f not in snap]
    check(f"[{label}] 沒有缺少的欄位", not missing,
          f"缺少 {missing}" if missing else f"{len(used)} 個欄位全部具備")
    try:
        json.dumps(snap, ensure_ascii=False)
        check(f"[{label}] 可 JSON 序列化", True)
    except Exception as exc:                           # noqa: BLE001
        check(f"[{label}] 可 JSON 序列化", False, f"{type(exc).__name__}: {exc}")
    for key, typ in (("state", str), ("level", (int, float)),
                     ("presses", int), ("history", list)):
        check(f"[{label}] {key} 是 {getattr(typ, '__name__', typ)}",
              isinstance(snap.get(key), typ),
              f"實際 {type(snap.get(key)).__name__}")


def main() -> int:
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
    # ⚠️ 這一條**只對 Windows 成立**。macOS 的 `mic_warning` 一定要有內容
    #    （AGENTS.md §8.7：mac 的 AVAudioEngine 只能用系統預設輸入裝置，
    #    「麥克風優先順序」在那裡無效，不講的話使用者會排了半天順序然後
    #    發現完全沒作用）。所以 mac 的斷言在 [7]。
    check("mic_warning 預設 None（Windows）", snap.get("mic_warning") is None)

    # ------------------------------------------------------------------
    # 兩個平台共用同一份 app/ui/ —— 所以契約要對**兩邊**都成立。
    #
    # ⚠️ 為什麼要在這裡驗 mac 的 `ui_server.Status`：它的 `snapshot()` 是
    #    **另一份實作**（檔案不同、class 不同）。分成兩份的症狀是
    #    「Windows 的設定頁正常、mac 的整片 undefined」，而兩邊都不會報錯。
    #    實際踩過的就是這一種（見 AGENTS.md §8.7 的「形狀也是契約」）。
    # ------------------------------------------------------------------
    print("\n[6] macOS 的 ui_server.Status 也要滿足同一份欄位契約")
    mac_snap = None
    try:
        from ui_server import Status as MacStatus
        mac_snap = MacStatus(Config(), platform="macos").snapshot()
        check_snapshot(mac_snap, "macos")
    except Exception as exc:                           # noqa: BLE001
        check("[macos] snapshot() 可呼叫", False, f"{type(exc).__name__}: {exc}")

    if mac_snap is not None:
        print("\n[7] mac 平台的警告欄位（與 Windows 的語意不同）")
        check("macos 的 mic_warning 必須有內容（§8.7 的硬規則）",
              mac_snap.get("mic_warning") not in (None, ""),
              repr(mac_snap.get("mic_warning")))
        check("macos 的 vendor_warning 是 None（不假裝有）",
              mac_snap.get("vendor_warning") is None,
              repr(mac_snap.get("vendor_warning")))
        check("macos 的 bt_warning 是 None（不假裝有）",
              mac_snap.get("bt_warning") is None,
              repr(mac_snap.get("bt_warning")))
        check("macos 的 platform 欄位是 'macos'",
              mac_snap.get("platform") == "macos", repr(mac_snap.get("platform")))

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
