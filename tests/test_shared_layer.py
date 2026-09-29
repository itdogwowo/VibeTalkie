#!/usr/bin/env python3
"""共用層的釘子：**同一條規則只能有一份實作**（任何平台都能跑）。

## 為什麼要有這一支

這個專案反覆踩到同一種 bug —— 同一條規則寫在兩個地方，後來**只改了一份**：

| 規則 | 漂移的症狀 |
|---|---|
| 熱鍵去重（`config.apply_hotkeys_patch` vs `hotkey.bindings`） | UI 存得進去，**重啟後少一組**（`10c17d3`） |
| 鍵名轉換（`keys.py` vs `hotkey.py`） | 「同一顆鍵在 mac 命中、在 Windows 不命中」 |
| 狀態值大小寫（`Status.on_state` 兩平台各一份） | 共用同一份 UI，**只有一個平台**顯示「未知」 |
| 啟動防護（`vibetalkie.py` vs `ui_server.py`） | 兩個實例互相覆蓋 `config.toml` →「設定存了又變回去」 |

**文件規範擋不住這件事**（AGENTS.md 已經有很多「不要各寫一份」的註解，
還是多寫了一份），所以用程式釘住：**定義只能出現在唯一允許的檔案裡。**

    tests/test_launch_guard.py  驗「行為對不對」（真的開行程佔 port）
    tests/test_shared_layer.py  驗「實作只有一份」（AST 掃定義）← 本檔
    兩支一起看才完整：行為對但兩份實作，下一次改動就會漂移。

## 這不是在禁止重構

把規則搬到別的檔案是允許的 —— 只要**同時**改這裡的 `SHARED` 表。
那一步是刻意的摩擦：搬家的時候你得說明「為什麼新位置才是對的」。

執行：python tests/test_shared_layer.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

failures: list[str] = []

# 名稱 → (唯一允許的定義檔, 為什麼是它)
SHARED: dict[str, tuple[str, str]] = {
    "AlreadyRunning": ("app/core/launch_guard.py",
                       "啟動防護：兩個進入點都要用同一顆例外"),
    "port_in_use": ("app/core/launch_guard.py",
                    "啟動防護：綁定探測只有一種正確寫法"),
    "pick_port": ("app/core/launch_guard.py",
                  "啟動防護：span 預設值就是「不要靜默讓位」這條規則本身"),
    "explain": ("app/core/launch_guard.py",
                "啟動防護：訊息文字兩平台共用，平台差異只是參數"),
    "start_key_text": ("app/config.py",
                       "設定 ↔ UI 的字串來回，兩個平台都要一致"),
    "split_binding": ("app/core/hotkey.py",
                      "`開始[,結束][@行為]` 的切法只能有一套"),
    "parse_binding": ("app/core/hotkey.py",
                      "規格解析：打錯字要回 400，不能靜默吃掉"),
    "binding_text": ("app/core/hotkey.py",
                     "寫回設定檔的格式（存一次不能換一種寫法）"),
    "_key_name_and_side": ("app/core/hotkey.py",
                           "鍵名 ↔ 側別：`keys.py` 只能轉呼叫，不能自己一份"),
}

# 進入點 → 它一定要用的共用層模組
MUST_IMPORT: dict[str, str] = {
    "app/vibetalkie.py": "launch_guard",
    "app/core/ui_server.py": "launch_guard",
    "app/mac_vibetalkie.py": "launch_guard",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def definitions(path: Path) -> set[str]:
    """這個檔案裡**定義**了哪些函式／類別（含巢狀，因為巢狀也算一份實作）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
    return found


def imported_modules(path: Path) -> set[str]:
    """這個檔案 import 了哪些**最上層模組名**（含函式內的延後 import）。"""
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
    print("共用層：同一條規則只能有一份實作")
    print("=" * 68)

    sources = sorted((ROOT / "app").rglob("*.py"))
    where: dict[str, set[str]] = {}
    for path in sources:
        rel = path.relative_to(ROOT).as_posix()
        for name in definitions(path):
            where.setdefault(name, set()).add(rel)

    print(f"\n[1] 已共用的規則，定義只能有一份（掃 {len(sources)} 個檔案）")
    for name, (owner, why) in SHARED.items():
        found = where.get(name, set())
        check(f"{name} → {owner}", found == {owner},
              f"實際：{sorted(found) or '找不到'}"
              + ("" if found == {owner} else f"（{why}）"))

    print("\n[2] 進入點真的用共用層（不是自己再寫一份）")
    for rel, module in MUST_IMPORT.items():
        path = ROOT / rel
        if not path.exists():
            check(f"{rel} 存在", False, "檔案不存在")
            continue
        check(f"{rel} import {module}", module in imported_modules(path))

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
