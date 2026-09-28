#!/usr/bin/env python3
"""UI ↔ snapshot 的欄位契約測試（macOS 版）。

## 為什麼要測這個

`app/ui/app.js`（541 行）是**兩個平台共用**的 UI。它靠 `s.<欄位>` 讀
`/api/status` 的回傳。少一個欄位的症狀是：

    UI 讀到 undefined → 畫面顯示「undefined」或整塊壞掉
    而後端**不會有任何錯誤** —— 因為它只是少給了一個 key

專案已經有 `test_status_contract.py` 做這件事（針對 Windows 的
`vibetalkie.Status`）。但 mac 版是**另一個 Status 實作**
（`app/core/ui_server.py`），所以需要自己一份驗證 ——
否則「Windows 好的、mac 壞的」這種漂移沒人看得見。

> 實際踩過的教訓（寫在 `test_status_contract.py`）：在 snapshot 加了
> `mic_warning` 卻忘了在來源 dict 也加 → `KeyError`，而 UI 每 500ms
> 輪詢一次，變成每秒兩次的 traceback 風暴。

## 怎麼測

不需要 pyobjc：`ui_server` 只用標準函式庫，`Status` 是純資料容器。

執行：python tests/test_mac_ui_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import config as config_module      # noqa: E402
import ui_server                    # noqa: E402

UI_JS = ROOT / "app" / "ui" / "app.js"

# `s.xxx` 也會抓到 JS 的內建方法與區域變數，不是 snapshot 欄位
JS_NOISE = {
    "find", "map", "to", "value", "length", "filter", "join", "split",
    "push", "slice", "forEach", "includes", "indexOf", "replace", "trim",
    "sort", "reduce", "some", "every", "concat", "pop", "shift", "keys",
    "values", "entries", "then", "catch", "test", "match", "exec",
}

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def ui_fields() -> set[str]:
    """從 app.js 抓出實際讀取的 `s.<欄位>`。"""
    js = UI_JS.read_text(encoding="utf-8")
    return set(re.findall(r"\bs\.([a-z_]+)\b", js)) - JS_NOISE


def test_state_keys_match_ui() -> None:
    """狀態值必須是 UI 查得到的 —— **大小寫是關鍵**。

    ⚠️ 實際踩到：常駐程式送 `"IDLE"`（狀態機內部用大寫），
    而 `app.js` 的 `STATE_TEXT` 用小寫鍵 → UI 顯示「未知」。
    Windows 版在 `on_state` 做了 `.lower()`，mac 版漏了。

    這個測試從 `app.js` **實際解析出** `STATE_TEXT` 的鍵，
    再把狀態機用到的每個狀態餵進去 —— **不自己抄一份清單**
    （抄一份就會漂移，而那正是這個 bug 的成因）。
    """
    print("\n▍狀態值必須是 UI 查得到的（大小寫是關鍵）")

    js = UI_JS.read_text(encoding="utf-8")
    m = re.search(r"const STATE_TEXT\s*=\s*\{(.*?)\};", js, re.S)
    check("從 app.js 解析到 STATE_TEXT", m is not None)
    if not m:
        return
    known = set(re.findall(r"(\w+)\s*:", m.group(1)))
    check("STATE_TEXT 有內容", len(known) >= 4, str(sorted(known)))

    cfg = config_module.Config.load()
    for raw in ("IDLE", "RECORDING", "PROCESSING", "INSERTING", "ERROR"):
        st = ui_server.Status(cfg)
        st.on_state(raw)
        got = st.snapshot()["state"]
        check(f"{raw} → UI 認得（{got}）", got in known,
              f"得到 {got!r}，STATE_TEXT 只有 {sorted(known)}")


def test_mic_device_not_empty() -> None:
    """`mic_device` 不可以是空字串。

    `app.js` 的判斷是 `s.mic_device != null` —— **空字串會通過**，
    於是畫面顯示成「device 」（後面空的），看起來像壞掉。
    """
    print("\n▍mic_device 不可以是空字串（UI 只在 null 時才省略它）")

    cfg = config_module.Config.load()
    st = ui_server.Status(cfg, platform="macos")
    check("預設值不是空字串（不然 UI 會顯示「device 」）",
          st.snapshot()["mic_device"] != "",
          repr(st.snapshot()["mic_device"]))

    st.mic_device = "系統預設輸入裝置（44100 Hz / 2ch）"
    v = st.snapshot()["mic_device"]
    check("填入真實名稱後仍然不是空字串", v != "", repr(v))
    check("是可序列化的字串", isinstance(v, str), type(v).__name__)


def main() -> int:
    print("=" * 74)
    print("UI ↔ snapshot 欄位契約測試（macOS）")
    print("=" * 74)

    if not UI_JS.is_file():
        print(f"\n  ❌ 找不到 {UI_JS}")
        return 2

    cfg = config_module.Config.load()
    status = ui_server.Status(cfg, platform="macos")
    snap = status.snapshot()

    want = ui_fields()
    print(f"\n  app.js 讀取 {len(want)} 個欄位；snapshot 提供 {len(snap)} 個")

    print("\n▍契約：UI 讀的每個欄位都要存在")
    missing = sorted(want - set(snap))
    check("沒有缺少的欄位", not missing,
          f"缺少 {missing}（UI 會顯示 undefined）" if missing else "全部都在")

    print("\n▍平台欄位：mac 沒有的要誠實回 None，該講的不能靜默")
    # 這三個原本是 Windows 專屬偵測（藍牙中斷、原廠工具、裝置索引）。
    # mac 沒有等價的東西 → None，**不假裝有**。
    for k in ("vendor_warning", "bt_warning"):
        check(f"{k} 存在且為 None（mac 不假裝有警告）",
              k in snap and snap[k] is None, repr(snap.get(k)))

    # ⚠️ 但 `mic_warning` **必須有內容**：macOS 的 AVAudioEngine 只能用
    #    系統預設輸入裝置，而設定頁有個「麥克風優先順序」清單
    #    （那是為 Windows 做的）。那個清單在 mac 上**可編輯但無效** ——
    #    不講的話，使用者會排了半天順序，然後發現完全沒作用。
    #
    #    UI 已經有現成的警告框（收集 mic_warning / bt_warning /
    #    vendor_warning 顯示），所以用那個管道傳，不必改 UI。
    mw = snap.get("mic_warning")
    check("mic_warning 有內容（mac 有一條必須講的限制）",
          "mic_warning" in snap and isinstance(mw, str) and len(mw) > 0,
          repr(mw)[:60] if mw else "None")
    if isinstance(mw, str) and mw:
        check("內容有說出「系統設定」或「系統預設」（可操作的指引）",
              "系統設定" in mw or "系統預設" in mw or "系統" in mw, mw[:40])

    print("\n▍關鍵欄位要有正確的型別（UI 會直接拿去用）")
    types = {
        "state": str, "level": (int, float), "hotkey": str,
        "trigger_mode": str, "engine": str, "model": str,
        "opencc": bool, "config_path": str, "models_dir": str,
        "history": list, "downloads": list, "stats": dict,
    }
    for k, t in types.items():
        v = snap.get(k)
        check(f"{k} 是 {getattr(t, '__name__', t)}", isinstance(v, t),
              f"{type(v).__name__} = {v!r}"[:70])

    print("\n▍stats 的子欄位（UI 的統計面板）")
    for k in ("presses", "inserted", "empty", "failed"):
        check(f"stats.{k} 存在", k in snap.get("stats", {}),
              str(snap.get("stats")))

    print("\n▍update 之後仍然符合契約（不是只有初始狀態對）")
    status.on_state("RECORDING")
    status.level = 0.42
    status.on_result({"text": "測試", "ms": 123})
    snap2 = status.snapshot()
    check("狀態有更新", snap2["state"] == "recording", snap2["state"])
    check("音量有更新", abs(snap2["level"] - 0.42) < 0.01, str(snap2["level"]))
    check("歷史有寫入", len(snap2["history"]) == 1, str(snap2["history"]))
    check("統計有累加", snap2["stats"]["inserted"] == 1, str(snap2["stats"]))
    missing2 = sorted(want - set(snap2))
    check("更新後仍然沒有缺少欄位", not missing2,
          f"缺少 {missing2}" if missing2 else "")

    print("\n▍snapshot 必須可 JSON 序列化（要透過 HTTP 送出去）")
    import json
    try:
        json.dumps(snap2, ensure_ascii=False)
        ok, err = True, ""
    except Exception as exc:                            # noqa: BLE001
        ok, err = False, f"{type(exc).__name__}: {exc}"
    check("可以序列化成 JSON", ok, err)

    test_state_keys_match_ui()
    test_mic_device_not_empty()

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
