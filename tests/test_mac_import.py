#!/usr/bin/env python3
"""macOS 模組的「可載入性」測試（**不需要 pyobjc，任何平台都能跑**）。

## 為什麼要有這一支

`app/core/mac_*.py` 與 `app/mac_vibetalkie.py` 刻意**不在頂層 import pyobjc** ——
`Quartz` / `AVFoundation` 都是延後到「真的要用了」才 import。
這個性質讓 `MacPttDaemon` 的**狀態機邏輯**可以在 Windows 上測試
（見 `tests/test_mac_trigger.py`）。

但那個性質很脆弱：任何人在 `mac_recorder.py` 頂層加一行 `import Quartz`，
就會讓 mac 的測試在非 macOS 上一次全掛，而且錯誤訊息會指向 pyobjc 而不是
「你加了不該加的 import」。

所以這支測試把它釘住：**載入模組（不呼叫任何原生路徑）** 必須成功。

## 為什麼連 `trigger.py`／`hotkey.py` 的 import 也要掃

它們是**兩個平台共用的那一層**。共用層一旦 import 了 `ctypes` 或 `Quartz`，
「共用」就只是名義上的 —— mac 會在 Windows 專屬的東西上炸掉（反之亦然），
而那正是這次整合要消掉的問題。

執行：python tests/test_mac_import.py
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def imported_names(path: Path) -> set[str]:
    """檔案裡所有 import 的**最上層模組名**（含函式內的延後 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def main() -> int:
    print("=" * 68)
    print("macOS 模組可載入性測試（不需要 pyobjc）")
    print("=" * 68)

    print("\n[1] app/core 的 mac 模組在頂層不碰 pyobjc")
    for name in ("keys", "mac_keylistener", "mac_recorder", "mac_textin", "ui_server"):
        try:
            __import__(name)
            check(f"import {name}", True)
        except Exception as exc:                       # noqa: BLE001
            check(f"import {name}", False, f"{type(exc).__name__}: {exc}")

    print("\n[2] 進入點載入得動（不代表能在本平台跑）")
    spec = importlib.util.spec_from_file_location(
        "mac_entry_probe", ROOT / "app" / "mac_vibetalkie.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)                   # type: ignore[union-attr]
        check("載入 app/mac_vibetalkie.py", True)
        check("MacPttDaemon 類別存在", hasattr(mod, "MacPttDaemon"))
    except Exception as exc:                           # noqa: BLE001
        check("載入 app/mac_vibetalkie.py", False, f"{type(exc).__name__}: {exc}")
        if "-v" in sys.argv:
            traceback.print_exc()

    print("\n[3] 共用層不可以依賴平台原生模組")
    # `ctypes` 在 Windows 是平台層的必需品（`ptt.py` 用它呼叫 winmm），
    # 但**共用層**用了它就代表 mac 也會被拖進 Windows 專屬的路徑。
    banned = {
        "ctypes", "winreg", "msvcrt",
        "Quartz", "AppKit", "AVFoundation", "AVFAudio", "objc", "Foundation",
        "mac_keylistener", "mac_recorder", "mac_textin",
        "record_wav", "keycode_logger", "recorder", "textin", "ptt",
        "vibetalkie", "mac_vibetalkie",
    }
    # ⚠️ `config.py` 住在 `app/`（不是 `app/core/`）—— 這份清單要照實際的
    #    目錄結構寫，不要照「想像中的結構」。寫錯的症狀是「測試說檔案不存在」，
    #    而檔案明明在（實際踩到）。
    for rel in ("app/core/trigger.py", "app/core/hotkey.py", "app/config.py"):
        path = ROOT / rel
        if not path.exists():
            check(f"{rel} 存在", False, "檔案不存在")
            continue
        bad = sorted(imported_names(path) & banned)
        check(f"{rel} 沒有平台原生相依", not bad, str(bad) if bad else "")

    print("\n[4] `trigger.py` 是**平台無關**的（不得出現 Win32／CG 常數）")
    tpath = ROOT / "app" / "core" / "trigger.py"
    if tpath.exists():
        text = tpath.read_text(encoding="utf-8")
        # 只掃「程式碼」，不掃註解與 docstring —— 註解裡提到 WM_KEYDOWN
        # 是正當的（說明為什麼某條規則存在），不能因此判它不平台無關。
        code_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        suspects = [tok for tok in ("WM_KEYDOWN", "WM_SYSKEYDOWN", "RI_KEY_E0",
                                    "RAWKEYBOARD", "windll", "kCGEvent", "CGEventTap")
                    if tok in code]
        check("沒有 Win32／CG 常數", not suspects,
              f"可疑：{suspects}" if suspects else "")
    else:
        check("app/core/trigger.py 存在", False, "檔案不存在（Phase C 會建立）")

    print("\n" + "=" * 68)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
