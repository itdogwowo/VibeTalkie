#!/usr/bin/env python3
"""wheel 平台選擇的測試：三個平台分支都要對。

## 為什麼要測這個

`tools/p1/fetch_wheels.py` 是「缺套件時的最後一條路」——當 `pip` 壞掉時
（本專案實測會壞），它是唯一能把套件裝起來的管道。

而它原本的 `PLATFORM_PRIORITY` 是**寫死的 Windows 清單**：

    PLATFORM_PRIORITY = ("win_amd64", "win32", "any")

在 macOS 上的後果是 `pick_wheel()` 永遠回 `None` —— 症狀是
「明明 PyPI 上有 wheel，工具卻說找不到」。這個 bug 之所以能活這麼久，
是因為**沒有人測過非 Windows 的分支**。

所以這個測試的重點是：**每一個平台分支都要驗證**，
而且參數要能注入（`platform_patterns(sys_platform, machine, bits)`），
否則在 mac 上開發時，Windows 分支就沒人看得到。

執行：python tests/test_wheel_platform.py
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

spec = importlib.util.spec_from_file_location(
    "fw", ROOT / "tools" / "p1" / "fetch_wheels.py")
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)                                      # type: ignore

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def rank(plat: str, patterns: tuple[str, ...]) -> int | None:
    """複製 `wheel_rank` 的比對方式（用正則 fullmatch）。"""
    for i, p in enumerate(patterns):
        if re.fullmatch(p, plat):
            return i
    return None


def test_current_platform() -> None:
    print("\n▍目前平台（實際會用到的分支）")

    missing = fw.pick_wheel(fw.pypi_json("sherpa-onnx")) is None
    check("sherpa-onnx 選得到 wheel", not missing,
          "選不到 = 這個工具在目前平台上等於廢掉")

    # ⚠️ `pyobjc-framework-Quartz` **只有 macOS 的 wheel**。
    #
    #    這一條原本直接 `pick_wheel(pypi_json("pyobjc-framework-Quartz"))` ——
    #    也就是用「**目前平台**」的規則去挑。在 Windows / Linux 上跑時，
    #    macOS 的 wheel 會被平台規則全部濾掉 → 永遠回 None → 測試永遠是紅的。
    #
    #    實測：這條在 `origin/mac` 上就是紅的，而且會被誤以為是整合造成的。
    #    修法是用 `patterns=` 明確指定「用 macOS 的規則挑」—— 這樣三個平台
    #    都能驗同一件事（PyPI 上有 Quartz 的 macOS wheel，而且挑選邏輯正確）。
    darwin = fw.platform_patterns("darwin", "arm64", 64)
    q = fw.pick_wheel(fw.pypi_json("pyobjc-framework-Quartz"), patterns=darwin)
    check("pyobjc-framework-Quartz 選得到 wheel（用 macOS 規則挑）",
          q is not None, q["filename"] if q else "❌ None")

    if q:
        fn = q["filename"]
        # 選到別的平台的 wheel 會「裝得起來但 import 才爆」，極難查
        if "win_amd64" in fn or "win32" in fn or "manylinux" in fn \
                or "musllinux" in fn:
            check("不會選到 Windows/Linux 的 wheel", False, fn)
        else:
            check("不會選到 Windows/Linux 的 wheel", True, fn)
        # 明確指定 macOS 規則時，一定要挑到 macOS 的 wheel
        check("macOS 規則挑到的是 macOS wheel", "macosx" in fn, fn)

    if sys.platform == "win32":
        check("Windows 規則**不會**挑到 macOS 的 wheel（pyobjc 在 Windows 無 wheel）",
              fw.pick_wheel(fw.pypi_json("pyobjc-framework-Quartz")) is None,
              "挑到 macOS wheel = 會裝起來但 import 才爆")


def test_macos_branches() -> None:
    print("\n▍macOS（arm64 / x86_64）")

    arm = fw.platform_patterns("darwin", "arm64", 64)
    check("arm64：arm64 wheel 最優先", rank("macosx_11_0_arm64", arm) == 0)
    check("arm64：universal2 次之", rank("macosx_10_15_universal2", arm) == 1)
    check("arm64：**不接受** x86_64 專用 wheel",
          rank("macosx_10_9_x86_64", arm) is None,
          "接受了會裝到不能執行的二進位")
    check("arm64：純 Python（any）仍可用", rank("any", arm) is not None)

    x86 = fw.platform_patterns("darwin", "x86_64", 64)
    check("x86_64：x86_64 wheel 最優先", rank("macosx_10_9_x86_64", x86) == 0)
    check("x86_64：**不接受** arm64 專用 wheel",
          rank("macosx_11_0_arm64", x86) is None)


def test_windows_branches() -> None:
    print("\n▍Windows（64 / 32 位元）——**這是原本就存在的行為，不可以改變**")

    w64 = fw.platform_patterns("win32", "AMD64", 64)
    check("64 位元：win_amd64 最優先", rank("win_amd64", w64) == 0)
    check("64 位元：win32 次之", rank("win32", w64) == 1)
    check("64 位元：any 最後", rank("any", w64) == 2)
    check("64 位元：優先序與修改前一致",
          w64 == ("win_amd64", "win32", "any"), str(w64))

    w32 = fw.platform_patterns("win32", "x86", 32)
    check("32 位元：win32 最優先", rank("win32", w32) == 0)
    check("32 位元：不會選 win_amd64", rank("win_amd64", w32) == 1,
          "排在後面（非首選）")


def test_linux_branches() -> None:
    print("\n▍Linux")

    lin = fw.platform_patterns("linux", "x86_64", 64)
    check("manylinux 最優先", rank("manylinux_2_17_x86_64", lin) == 0)
    check("musllinux 次之", rank("musllinux_1_1_x86_64", lin) == 1)
    check("純 linux 標籤也可", rank("linux_x86_64", lin) is not None)
    check("any 可用", rank("any", lin) is not None)


def test_python_tags() -> None:
    print("\n▍Python 版本標籤（原本也寫死成 cp314…）")

    tags = fw._python_tags()                                      # noqa: SLF001
    cur = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    check("第一個 tag 就是目前解譯器版本", tags[0] == cur,
          f"{tags[0]}（目前 {cur}）")
    check("包含 abi3 與 py3（純 Python 套件要用）",
          "abi3" in tags and "py3" in tags)
    check("不會推薦比目前更新的版本",
          all(int(t[2:]) <= sys.version_info[0] * 100 + sys.version_info[1]
              for t in tags if t.startswith("cp")),
          "推薦更新的版本會抓到不能用的 wheel")


def main() -> int:
    print("=" * 74)
    print("wheel 平台選擇測試")
    print("=" * 74)
    print(f"  目前平台：{sys.platform}　Python {sys.version.split()[0]}")

    test_current_platform()
    test_macos_branches()
    test_windows_branches()
    test_linux_branches()
    test_python_tags()

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
