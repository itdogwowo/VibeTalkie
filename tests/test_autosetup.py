#!/usr/bin/env python3
"""相依自動安裝的測試。

## 為什麼要測這個

`app/core/autosetup.py` 會在啟動時**自動下載並安裝東西**，這是它存在的理由
（原本要使用者一項一項自己裝）。但「會自動裝東西」也代表寫錯的代价很高：

  · 判斷錯 → 明明裝好了卻重裝一次（每次啟動都下載 300 MB）
  · 判斷錯 → 明明沒裝卻說裝好了，然後在 import 時才爆（更難查）
  · `--no-install` 失效 → 在公司電腦上偷偷連外網下載

所以這裡驗證的是**判斷邏輯**，不是真的去裝東西。

## 怎麼測

不碰真實環境：用假的套件清單（故意放一個不存在的模組名），
驗證「偵測 → 回報 → 不亂裝」這條路徑。

比對依賴展開（`DEPENDENCIES`）也要測 —— `fetch_wheels.py` **不會**自動解析
依賴，少列一個的症狀是「裝完了但 import 時說缺 objc」。

執行：python tests/test_autosetup.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

# ⚠️ 用**正常的 import**，不要用 spec_from_file_location。
#
# 實際踩到：測試用 spec_from_file_location 載入 autosetup，會建立一個
# **獨立於 sys.modules 的實例**。而 `mac_vibetalkie.py` 裡的
# `import autosetup` 拿到的是 `sys.modules["autosetup"]` —— 兩者是不同物件。
# 於是測試 patch 了 A 的 PACKAGES，程式讀的是 B 的，測試就永遠測不到東西
# （症狀：patch 完全沒作用，回傳值跟沒 patch 一樣）。
import autosetup  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def test_detection() -> None:
    print("\n▍偵測：缺的才回報，裝好的不要重裝")

    real = autosetup.missing_packages()
    check("回傳的是 (模組名, 套件名) 配對",
          all(isinstance(t, tuple) and len(t) == 2 for t in real),
          str(real[:2]))

    # ⚠️ 這一條是「不要每次啟動都重裝 300 MB」的保證。
    #    如果 import 判斷寫錯（例如少了 invalidate_caches），
    #    已經裝好的會被判成缺的。
    for module, _ in real:
        try:
            __import__(module)
        except ImportError:
            continue
        check(f"{module} 已可 import，不該被判為缺少", False,
              "判斷邏輯有問題")
        return
    check("已安裝的套件不會被判為缺少", True,
          f"目前缺 {len(real)} 項" if real else "全齊")

    # 假裝缺一個不存在的模組
    original = dict(autosetup.PACKAGES)
    autosetup.PACKAGES.clear()
    autosetup.PACKAGES["完全不存在的模組xyz"] = ("no-such-package-xyz", lambda m: None)
    try:
        fake = autosetup.missing_packages()
        check("不存在的模組會被偵測到",
              fake == [("完全不存在的模組xyz", "no-such-package-xyz")], str(fake))
    finally:
        autosetup.PACKAGES.clear()
        autosetup.PACKAGES.update(original)


def test_probe_catches_broken_install() -> None:
    """**裝了但壞掉**要被抓到 —— 這是 probe 存在的理由。

    ## 為什麼重要

    實際的失敗模式：套件 import 成功，但傳遞依賴缺了，**用到才爆**。
    例如 `opencc-python-reimplemented` 需要 `pkg_resources`（setuptools）。

    只檢查 import 的做法會把它判成「已就緒」，然後在**使用者第一次
    講話時**才失敗 —— 最糟的時機，而且錯誤訊息通常指向別的地方。

    所以每個相依都配一個 probe（真的動用它）。這個測試驗證那條路徑。
    """
    print("\n▍probe：抓出「已安裝但不能用」")

    real = autosetup.PACKAGES["numpy"]

    def broken(m):
        raise ModuleNotFoundError("No module named 'pkg_resources'")

    autosetup.PACKAGES["numpy"] = (real[0], broken)
    try:
        ok, why = autosetup.check_one("numpy")
        check("check_one 回報不可用", not ok, why)
        check("原因有指出是「已安裝但無法使用」", "無法使用" in why, why)

        missing = autosetup.missing_packages()
        check("missing_packages 會把它列為缺少（→ 觸發修復）",
              any(m[0] == "numpy" for m in missing), str(missing))
    finally:
        autosetup.PACKAGES["numpy"] = real

    # 對照：光看 import 是抓不到的
    import importlib
    importable = True
    try:
        importlib.import_module("numpy")
    except ImportError:
        importable = False
    check("對照組：numpy 本身 import 得到（所以舊做法會漏掉）", importable)

    print("\n▍每個相依都要有 probe（不能有人只檢查 import）")
    for module, (pkg, probe) in autosetup.PACKAGES.items():
        check(f"{module} 有 probe", callable(probe), f"{pkg}")

    print("\n▍diagnose() 要能列出所有相依的狀態")
    diag = autosetup.diagnose()
    check("數量與 PACKAGES 一致", len(diag) == len(autosetup.PACKAGES),
          f"{len(diag)} vs {len(autosetup.PACKAGES)}")
    check("每項都是 (模組, bool, 原因)",
          all(len(d) == 3 and isinstance(d[1], bool) for d in diag), str(diag[:1]))


def test_no_install_path() -> None:
    print("\n▍--no-install：只回報，不下載")

    # 用假的「缺一個不存在的套件」，並確認 install 根本不會被呼叫
    called = {"install": False}
    real_install = autosetup.install

    def spy_install(*a, **kw):
        called["install"] = True
        return True

    import importlib.util as _iu
    mvd_spec = _iu.spec_from_file_location(
        "mvd", ROOT / "app" / "mac_vibetalkie.py")
    try:
        mvd = _iu.module_from_spec(mvd_spec)
        mvd_spec.loader.exec_module(mvd)                         # type: ignore
    except ImportError as exc:
        print(f"  ⏭  跳過（載入 mac_vibetalkie 失敗：{exc}）")
        return

    # ⚠️ 兩個模組必須是同一個物件（見檔頭的說明）。
    #    這裡明確斷言，否則測試會「安靜地測不到東西」。
    if mvd.autosetup is not autosetup:
        print("  ❌ mac_vibetalkie 的 autosetup 與測試的不是同一個物件")
        print("     這樣 patch 不會生效 —— 測試會假通過")
        failures.append("autosetup 模組實例不一致")
        return

    autosetup.install = spy_install
    original_pkgs = dict(autosetup.PACKAGES)
    autosetup.PACKAGES.clear()
    autosetup.PACKAGES["完全不存在的模組xyz"] = ("no-such-package-xyz", lambda m: None)
    try:
        ok = mvd.ensure_dependencies(auto=False)
        check("auto=False 時回傳 False", ok is False)
        check("auto=False 時**不會**嘗試安裝", called["install"] is False,
              "公司電腦／離線環境絕對不能偷偷連外網")
    finally:
        autosetup.PACKAGES.clear()
        autosetup.PACKAGES.update(original_pkgs)
        autosetup.install = real_install


def test_dependency_expansion() -> None:
    print("\n▍依賴展開（fetch_wheels 不會自動解析依賴）")

    # pyobjc 的 framework 套件需要 pyobjc-core 與 Cocoa 先裝好。
    # 少列一個的症狀是「裝完了但 import 時說缺 objc」。
    for pkg, deps in autosetup.DEPENDENCIES.items():
        check(f"{pkg} 有列依賴", len(deps) >= 2, str(deps))
        check(f"{pkg} 的依賴包含 pyobjc-core", "pyobjc-core" in deps,
              "缺 pyobjc-core 時 framework 套件裝不起來")

    # 每個 key 自己也要在依賴清單裡（不然裝了依賴卻沒裝本體）
    for pkg, deps in autosetup.DEPENDENCIES.items():
        check(f"{pkg} 自己也清單內", pkg in deps, str(deps))


def test_module_package_mapping() -> None:
    print("\n▍模組名 ↔ PyPI 套件名（這兩者常常不一樣）")

    # 這幾個是實際踩過的對照：import 名稱與套件名稱不同
    expected = {
        "Quartz": "pyobjc-framework-Quartz",
        "AppKit": "pyobjc-framework-Cocoa",
        "AVFoundation": "pyobjc-framework-AVFoundation",
        "opencc": "opencc-python-reimplemented",
    }
    for module, pkg in expected.items():
        # ⚠️ PACKAGES 的值是 (PyPI 名, probe) 的 tuple ——
        #    probe 是為了抓「裝了但不能用」（見 test_probe_catches_broken_install）
        entry = autosetup.PACKAGES.get(module)
        got = entry[0] if isinstance(entry, tuple) else entry
        check(f"{module} → {pkg}", got == pkg, str(got))


def test_third_party_path() -> None:
    print("\n▍安裝位置：third_party/（不污染系統 Python）")

    check("THIRD_PARTY 指向 repo 底下",
          str(autosetup.THIRD_PARTY).endswith("third_party"),
          str(autosetup.THIRD_PARTY))
    check("THIRD_PARTY 在 .gitignore 內（不會被 commit）",
          "third_party/" in (ROOT / ".gitignore").read_text(encoding="utf-8"),
          "裝進版控會讓 repo 爆炸")


def test_model_check() -> None:
    print("\n▍模型偵測")

    # 已存在的模型不該觸發下載
    existing = None
    models_dir = ROOT / "models"
    if models_dir.is_dir():
        for d in models_dir.iterdir():
            if d.is_dir() and (d / "tokens.txt").exists():
                existing = d.name
                break
    if existing:
        p = autosetup.ensure_model(existing)
        check(f"已存在的模型直接回傳路徑（{existing}）", p is not None, str(p))
    else:
        print("  ⏭  沒有已下載的模型可測")


def main() -> int:
    print("=" * 74)
    print("相依自動安裝測試")
    print("=" * 74)
    print(f"  Python {sys.version.split()[0]}　{sys.platform}")

    test_detection()
    test_probe_catches_broken_install()
    test_dependency_expansion()
    test_module_package_mapping()
    test_third_party_path()
    test_no_install_path()
    test_model_check()

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
