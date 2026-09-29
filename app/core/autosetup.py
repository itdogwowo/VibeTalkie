#!/usr/bin/env python3
"""缺什麼就自己裝：Python 相依套件 ＋ ASR 模型。

## 為什麼需要這個

原本的體驗是「缺什麼就報錯停住，使用者自己去裝」：

    ❌ 載入不到 AVFoundation（pyobjc）：No module named 'AVFoundation'
       請安裝：pip install pyobjc-framework-AVFoundation

執行一次要跑好幾輪（先裝 A → 再啟動 → 缺 B → 再裝 B…），
而且 `pip install` 在這台機器上本來就可能壞掉
（見 `tools/p1/fetch_wheels.py` 的說明），使用者還得先知道那支工具的存在。

**這個模組把那段補起來：啟動時檢查、缺了就抓、抓完直接用。**

## 兩種安裝路徑

| 情況 | 做法 |
|---|---|
| `pip` 可用 | 用 `pip install --target third_party/`（尊重使用者的環境） |
| `pip` 壞掉（本專案實測） | 退回 `tools/p1/fetch_wheels.py`：自己抓 wheel 解開 |

兩條路都裝到 **`third_party/`**，不污染使用者的 site-packages ——
與 `speech_engine.py` 既有做法一致（它已經把 `third_party` 加進 `sys.path`）。

## 模型

模型（163 MB）也一併處理：呼叫既有的 `tools/p1/fetch_model.py`，
不下載到暫存目錄，而是直接進 `models/`（daemon 預期的地方）。

## 設計原則

· **講清楚要做什麼**：安裝前先列出「缺哪些、準備裝什麼」，不靜默下載。
· **失敗要能繼續看到原因**：不吞錯誤，把最後幾行輸出帶上來。
· **`--no-install` 可以關掉**：公司電腦／離線環境需要這個開關。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIRD_PARTY = ROOT / "third_party"


def ensure_third_party_on_path() -> None:
    """把 third_party/ 加進 sys.path（若還沒有的話）。"""
    p = str(THIRD_PARTY)
    if p not in sys.path:
        sys.path.insert(0, p)


def _probe_sherpa_onnx(m) -> None:
    # 只是拿到類別就好，不要真的建模型（那要幾百 MB）
    assert m.OfflineRecognizer is not None


def _probe_numpy(m) -> None:
    import numpy as np
    assert int(np.array([1, 2]).sum()) == 3


def _probe_opencc(m) -> None:
    # ⚠️ 這一個**必須真的轉一次字**，不能只 import。
    #    `opencc-python-reimplemented` import 時不會碰到資料檔，
    #    但轉換時需要 `pkg_resources`（setuptools）——
    #    少了它的症狀是「import 成功，第一次辨識才炸」。
    assert m.OpenCC("s2t").convert("测试") == "測試"


def _probe_quartz(m) -> None:
    assert m.CGEventTapCreate is not None
    assert m.kCGSessionEventTap is not None


def _probe_appkit(m) -> None:
    # 真的碰一下剪貼簿 —— 這是它唯一的用途
    assert m.NSPasteboard.generalPasteboard() is not None


def _probe_avfoundation(m) -> None:
    # 建一個 engine 就好，不要真的開麥克風（那需要權限）
    assert m.AVAudioEngine.alloc().init() is not None


# 模組名 → PyPI 套件名 ＋「怎麼確認它真的能用」。
#
# ⚠️ **一定要真的動用它，不能只 import。**
#
# 實際的失敗模式：套件 import 成功，但傳遞依賴缺了，用到才爆 ——
# 例如 `opencc-python-reimplemented` 需要 `pkg_resources`（setuptools）。
# 只檢查 import 的話，那種情況會被判成「已就緒」，然後在使用者
# 第一次講話時才失敗 —— 最糟的時機。
#
# 所以每個項目都配一個 probe。probe 失敗 = 視為缺少 → 觸發安裝/修復。
PACKAGES: dict[str, tuple[str, object]] = {
    "sherpa_onnx": ("sherpa-onnx", _probe_sherpa_onnx),
    "numpy": ("numpy", _probe_numpy),
    "opencc": ("opencc-python-reimplemented", _probe_opencc),
    "Quartz": ("pyobjc-framework-Quartz", _probe_quartz),
    "AppKit": ("pyobjc-framework-Cocoa", _probe_appkit),
    "AVFoundation": ("pyobjc-framework-AVFoundation", _probe_avfoundation),
}

# pyobjc 的套件彼此依賴。`fetch_wheels.py` **不會**自動解析依賴，
# 所以要手動列出來 —— 少一個的症狀是「裝完了但 import 時說缺 objc」。
DEPENDENCIES: dict[str, list[str]] = {
    "pyobjc-framework-Cocoa": ["pyobjc-core", "pyobjc-framework-Cocoa"],
    "pyobjc-framework-Quartz": ["pyobjc-core", "pyobjc-framework-Cocoa",
                                "pyobjc-framework-Quartz"],
    "pyobjc-framework-AVFoundation": ["pyobjc-core", "pyobjc-framework-Cocoa",
                                      "pyobjc-framework-AVFoundation"],
}


def check_one(module: str) -> tuple[bool, str]:
    """檢查單一模組。回傳 (可用, 原因)。

    **分三層判斷**，因為失敗原因不同、該給的訊息也不同：

        1. import 不到              → 沒裝
        2. import 得到但 probe 失敗  → 裝了但壞掉（通常是傳遞依賴缺失）
        3. 兩者都過                  → 可用

    第 2 種最陰險：它會通過「只檢查 import」的檢查，然後在**使用者
    第一次講話時**才炸 —— 那是最糟的失敗時機，而且訊息通常指向
    一個與安裝無關的地方。
    """
    ensure_third_party_on_path()
    spec = PACKAGES.get(module)
    if spec is None:
        return False, "不在相依清單裡"
    _pkg, probe = spec
    try:
        m = importlib.import_module(module)
    except ImportError as exc:
        return False, f"未安裝（{exc}）"
    # ⚠️ import 成功**不代表能用** —— 一定要真的動用它（見上面的說明）
    try:
        probe(m)                                          # type: ignore[operator]
    except Exception as exc:                              # noqa: BLE001
        return False, f"已安裝但無法使用：{type(exc).__name__}: {exc}"
    return True, "ok"


def missing_packages() -> list[tuple[str, str]]:
    """回傳 [(模組名, PyPI 名)]，包含**現在不能用的**。

    判斷標準是「probe 能不能跑過」，不是「import 得到嗎」。
    """
    out: list[tuple[str, str]] = []
    for module, (pkg, _probe) in PACKAGES.items():
        ok, _why = check_one(module)
        if not ok:
            out.append((module, pkg))
    return out


def diagnose() -> list[tuple[str, bool, str]]:
    """完整診斷：每個相依的狀態與原因（給 `--check` 顯示用）。"""
    return [(m, *check_one(m)) for m in PACKAGES]


def _pip_works() -> bool:
    """先確認 pip 能不能用。

    本專案實測 `pip install` 會在 `pip-unpack-*.whl.metadata` 撞到
    PermissionError，所以**不能假設它可用** —— 要真的試一次才知道。
    """
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:                                  # noqa: BLE001
        return False


def _install_with_pip(packages: list[str]) -> tuple[bool, str]:
    """用 pip 裝到 third_party/。回傳 (成功, 訊息)。"""
    cmd = [sys.executable, "-m", "pip", "install", "--quiet",
           "--target", str(THIRD_PARTY), *packages]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return False, "pip 逾時（900 秒）"
    except Exception as exc:                           # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if r.returncode == 0:
        return True, "pip 安裝成功"
    tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-6:])
    return False, tail or f"pip 回傳 {r.returncode}"


def _install_with_fetch_wheels(packages: list[str]) -> tuple[bool, str]:
    """退回 `tools/p1/fetch_wheels.py`（自己抓 wheel 解開）。"""
    script = ROOT / "tools" / "p1" / "fetch_wheels.py"
    if not script.is_file():
        return False, f"找不到 {script}"
    try:
        r = subprocess.run(
            [sys.executable, str(script), *packages],
            capture_output=True, text=True, timeout=900, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return False, "fetch_wheels 逾時（900 秒）"
    except Exception as exc:                           # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if r.returncode == 0:
        return True, "fetch_wheels 安裝成功"
    tail = "\n".join((r.stdout or r.stderr or "").strip().splitlines()[-8:])
    return False, tail or f"fetch_wheels 回傳 {r.returncode}"


def install(packages: list[tuple[str, str]], verbose: bool = True) -> bool:
    """安裝缺少的套件。回傳「全部就緒」。

    `packages` 是 `missing_packages()` 的輸出格式（模組名, PyPI 名）。
    """
    if not packages:
        return True

    names = [pkg for _, pkg in packages]
    # 展開依賴（fetch_wheels 不會自動解析）
    expanded: list[str] = []
    for n in names:
        for d in DEPENDENCIES.get(n, [n]):
            if d not in expanded:
                expanded.append(d)

    print("\n" + "=" * 66)
    print("  需要安裝缺少的套件")
    print("=" * 66)
    for module, pkg in packages:
        print(f"  · {module:14s} ← {pkg}")
    if expanded != names:
        print(f"\n  （含依賴：{', '.join(expanded)}）")
    print(f"\n  安裝位置：{THIRD_PARTY}")
    print("  （裝在這裡而不是系統 Python，避免污染你的環境）")
    print()

    ok = False
    if _pip_works():
        print("  用 pip 安裝…")
        ok, msg = _install_with_pip(expanded)
        if not ok:
            print(f"  ⚠️ pip 失敗：{msg}")
            print("     改用 fetch_wheels.py（自己抓 wheel，繞過 pip）…")
    else:
        print("  pip 不可用，用 fetch_wheels.py（自己抓 wheel）…")

    if not ok:
        ok, msg = _install_with_fetch_wheels(expanded)

    print(f"  {'✅ ' + msg if ok else '❌ ' + msg}")

    if not ok:
        print("\n  手動安裝方式：")
        print(f"    python3 -m pip install --target third_party "
              f"{' '.join(expanded)}")
        print(f"    # 或")
        print(f"    python3 tools/p1/fetch_wheels.py {' '.join(expanded)}")
        return False

    # ⚠️ 裝完之後要把 import 快取清掉，否則同一個行程裡
    #    剛剛才失敗的 import 會被記住，還是 import 不到。
    #    （實測：不清的話「裝成功了但下一步說找不到」。）
    importlib.invalidate_caches()
    ensure_third_party_on_path()

    # 逐一驗證**真的能用** —— 不要只信「安裝指令回傳 0」，
    # 也不要只檢查 import 得到（見 check_one 的三層判斷）。
    still: list[tuple[str, str]] = []
    for module, _pkg in packages:
        ok, why = check_one(module)
        if not ok:
            still.append((module, why))
    if still:
        print("\n  ⚠️ 安裝指令成功，但這些仍然不能用：")
        for module, why in still:
            print(f"     · {module}：{why}")
        print("     可能是 wheel 與 Python 版本不相容，或傳遞依賴缺漏。")
        return False
    print("  ✅ 全部就緒")
    return True


def ensure_model(model_dir: str, verbose: bool = True) -> Path | None:
    """確認模型在 `models/<model_dir>`；不在就下載。回傳路徑或 None。"""
    target = ROOT / "models" / model_dir
    if (target / "model.int8.onnx").exists() or (target / "model.onnx").exists():
        return target
    if target.is_dir() and (target / "tokens.txt").exists():
        return target

    script = ROOT / "tools" / "p1" / "fetch_model.py"
    if not script.is_file():
        print(f"  ❌ 找不到 {script}")
        return None

    print("\n" + "=" * 66)
    print("  需要下載 ASR 模型")
    print("=" * 66)
    print(f"  {model_dir}")
    print(f"  → {target}")
    print("  （約 160 MB，只需下載一次）\n")

    try:
        r = subprocess.run([sys.executable, str(script), "--get", model_dir],
                           text=True, timeout=1800, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        print("  ❌ 下載逾時")
        return None
    except Exception as exc:                           # noqa: BLE001
        print(f"  ❌ 下載失敗：{type(exc).__name__}: {exc}")
        return None

    if r.returncode != 0:
        print(f"  ❌ 下載失敗（結束碼 {r.returncode}）")
        return None
    if (target / "model.int8.onnx").exists() or (target / "model.onnx").exists():
        print("  ✅ 模型就緒")
        return target
    print(f"  ❌ 下載指令成功，但 {target} 裡沒有模型檔")
    return None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="檢查／補齊 VibeTalkie 的相依")
    ap.add_argument("--check", action="store_true", help="只檢查，不安裝")
    ap.add_argument("--model", default=None, help="順便確認這個模型")
    args = ap.parse_args()

    missing = missing_packages()
    print("=" * 66)
    print("  相依檢查")
    print("=" * 66)
    if not missing:
        print("  ✅ Python 套件都齊了")
    else:
        for module, pkg in missing:
            print(f"  ❌ 缺 {module}（套件：{pkg}）")
        if not args.check:
            install(missing)

    if args.model:
        p = ensure_model(args.model)
        print(f"  模型：{p if p else '❌ 未就緒'}")
