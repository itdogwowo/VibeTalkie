#!/usr/bin/env python3
"""啟動器的 Python 自動尋找測試。

## 為什麼要測這個

`launch.py` 會在使用者的 Python 太舊時**自動換一個並重新執行自己**。
這段邏輯有三個地方一旦寫錯，症狀都很難查：

  · 挑「最新」而不是「最舊的合格版本」
      → 使用者被推進「版本夠新但相依套件還沒有 wheel」的死路
  · 少了防迴圈的哨兵
      → 換了還不合格就再換一次，變成無限重新執行（而且看起來像當掉）
  · 沒擋「找到的就是自己」
      → 對自己 execve 一次，白跑一輪

這三件事都不會拋錯、不會留訊息，只會「行為怪怪的」——
所以用測試把它們釘住，而不是靠下次記得。

## 怎麼測

用**假的 Python 執行檔**（`sys.executable` + `-c` 回報版本）當候選，
所以不管跑測試的機器裝了哪些 Python，結果都一樣。
`os.execve` 會被攔下來，只檢查「本來會執行什麼」，不會真的換掉行程。

## 真實踩到的情境（這個測試要防的就是它）

    (base) conda 環境的 python3 = 3.10.10  →  雙擊直接被判版本不符

conda 的自動啟用把 3.10 放到 PATH 最前面，蓋掉了系統裡的 3.14。
`.command` 只是薄殼（`command -v python3`），所以修在 `launch.py` 裡。

執行：python tests/test_launch_python.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

# launch.py 在 repo 根目錄（不在 app/），所以不能用一般 import
_spec = importlib.util.spec_from_file_location("vt_launch", ROOT / "launch.py")
assert _spec and _spec.loader, "找不到 launch.py"
launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def fake_python(tmp: Path, name: str, version: str | None) -> Path:
    """做一個假的 Python 執行檔。

    version 給 "3.12" 就回報自己 3.12；給 None 則**什麼都不印且失敗**，
    用來模擬「這不是 Python」。建好之後 chmod +x，否則會被當成不能執行。
    """
    p = tmp / name
    if version is None:
        p.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    else:
        major, minor = version.split(".")
        p.write_text(
            "#!/bin/sh\n"
            f"echo '{major} {minor}'\n", encoding="utf-8")
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------- 版本探測

def test_python_version(tmp: Path) -> None:
    print("\n▍版本探測（問一個執行檔「你是第幾版」）")

    ok = fake_python(tmp, "py-ok", "3.12")
    check("正常的 Python → 回版本", launch._python_version(str(ok)) == (3, 12))

    # ⚠️ PATH 上什麼都可能叫 python3。回傳非 0 或亂印都要安全地回 None，
    #    不能讓啟動器因為一個壞掉的執行檔就崩潰。
    bad = fake_python(tmp, "py-fail", None)
    check("執行失敗的檔案 → None", launch._python_version(str(bad)) is None)
    check("不存在的路徑 → None",
          launch._python_version(str(tmp / "根本沒有這個檔")) is None)

    junk = tmp / "py-junk"
    junk.write_text("#!/bin/sh\necho '我不是版本'\n", encoding="utf-8")
    junk.chmod(0o755)
    check("輸出不是版本 → None", launch._python_version(str(junk)) is None)


# ---------------------------------------------------------------- 候選清單

def test_candidates() -> None:
    print("\n▍候選清單（使用者機器上常常有好幾套 Python）")

    cands = launch._candidates()
    check("至少找得到一個", len(cands) > 0, f"{len(cands)} 個")

    # 目前這個解譯器一定要在裡面，否則使用者刻意指定的虛擬環境會被忽略
    real = [os.path.realpath(p) for p in cands]
    check("包含目前執行的解譯器",
          os.path.realpath(sys.executable) in real,
          os.path.realpath(sys.executable))

    check("去重過（同一顆用不同路徑指到時只留一個）",
          len(real) == len(set(real)))


# ---------------------------------------------------------------- 挑選邏輯

def test_find_python_prefers_oldest(tmp: Path) -> None:
    print("\n▍挑選邏輯（要挑最舊的合格版本，不是最新的）")

    # 三個都合格 → 必須挑 3.11
    three = [fake_python(tmp, "p313", "3.13"),
             fake_python(tmp, "p311", "3.11"),
             fake_python(tmp, "p312", "3.12")]
    with mock.patch.object(launch, "_candidates", return_value=[str(p) for p in three]):
        got = launch.find_python()
    check("3.11/3.12/3.13 都在 → 挑 3.11（不是 3.13）",
          got == str(three[1]), str(got))

    # ⚠️ 這條是整個檔案的重點：最新版常常還沒有相依套件的 wheel
    check("明確不是挑最新的",
          got != str(three[0]), "若挑 3.13 代表邏輯被改成「挑最新」")

    # 太舊的要跳過
    mixed = [fake_python(tmp, "m310", "3.10"),
             fake_python(tmp, "m308", "3.8"),
             fake_python(tmp, "m312", "3.12")]
    with mock.patch.object(launch, "_candidates", return_value=[str(p) for p in mixed]):
        got = launch.find_python()
    check("3.10/3.8 太舊 → 跳過，挑 3.12", got == str(mixed[2]), str(got))

    # 全部不合格 → None（上層才能報「請安裝 Python 3.11+」）
    only_old = [fake_python(tmp, "o310", "3.10"), fake_python(tmp, "o309", "3.9")]
    with mock.patch.object(launch, "_candidates", return_value=[str(p) for p in only_old]):
        check("全部都太舊 → None", launch.find_python() is None)

    # 都不是 Python → None，而且要安靜地回 None 而不是拋錯
    junk = [fake_python(tmp, "j1", None), fake_python(tmp, "j2", None)]
    with mock.patch.object(launch, "_candidates", return_value=[str(p) for p in junk]):
        check("一個都不是 Python → None", launch.find_python() is None)


# ---------------------------------------------------------------- 重新執行

def test_reexec(tmp: Path) -> None:
    print("\n▍重新執行自己（會不會換、會不會無限迴圈）")

    # 先確認這台機器上真的有合格的 Python；沒有就沒必要跑這幾條
    if launch.check_python() is not None:
        print("  ⏭  目前解譯器本身不合格，跳過重新執行的檢查")
        return

    calls: list[tuple] = []

    def spy(*a, **kw):
        calls.append((a, kw))
        return None

    # --- 情境 1：版本太舊，而且有更好的 → 應該換過去 ---
    better = fake_python(tmp, "better", "3.12")
    with mock.patch.object(launch, "check_python", return_value="太舊"), \
         mock.patch.object(launch, "find_python", return_value=str(better)), \
         mock.patch.object(launch, "_python_version", return_value=(3, 12)), \
         mock.patch.object(launch, "say"), \
         mock.patch.dict(os.environ, {}, clear=False), \
         mock.patch.object(os, "execve", spy):
        os.environ.pop(launch._REEXEC_FLAG, None)
        rc = launch.reexec_if_python_too_old()

    check("太舊且有更好的 → 會重新執行", len(calls) == 1,
          f"execve 呼叫 {len(calls)} 次")
    check("回傳 None（代表「已交給新行程」）", rc is None)
    if calls:
        argv = calls[0][0][1]
        check("重新執行的是自己（不是別的腳本）",
              any(str(ROOT / "launch.py") in str(a) for a in argv),
              " ".join(str(a) for a in argv))
        check("有帶哨兵，否則會無限迴圈",
              calls[0][0][2].get(launch._REEXEC_FLAG) == "1")

    # --- 情境 2：哨兵已設 → 不可以再換（這條就是防無限迴圈）---
    calls.clear()
    with mock.patch.object(launch, "check_python", return_value="太舊"), \
         mock.patch.object(launch, "find_python", return_value=str(better)), \
         mock.patch.dict(os.environ, {launch._REEXEC_FLAG: "1"}), \
         mock.patch.object(os, "execve", spy):
        rc = launch.reexec_if_python_too_old()

    check("哨兵已設 → 不再換（防無限迴圈）", len(calls) == 0 and rc is None,
          f"execve 呼叫 {len(calls)} 次")

    # --- 情境 3：找到的就是自己 → 不該對自己 execve ---
    calls.clear()
    with mock.patch.object(launch, "check_python", return_value="太舊"), \
         mock.patch.object(launch, "find_python", return_value=sys.executable), \
         mock.patch.dict(os.environ, {}, clear=False), \
         mock.patch.object(os, "execve", spy):
        os.environ.pop(launch._REEXEC_FLAG, None)
        rc = launch.reexec_if_python_too_old()

    check("找到的是自己 → 不重新執行（白跑一輪）",
          len(calls) == 0 and rc is None, f"execve 呼叫 {len(calls)} 次")

    # --- 情境 4：找不到更好的 → 不換，讓上層去報版本不符 ---
    calls.clear()
    with mock.patch.object(launch, "check_python", return_value="太舊"), \
         mock.patch.object(launch, "find_python", return_value=None), \
         mock.patch.dict(os.environ, {}, clear=False), \
         mock.patch.object(os, "execve", spy):
        os.environ.pop(launch._REEXEC_FLAG, None)
        rc = launch.reexec_if_python_too_old()

    check("找不到更好的 → 不換，回 None 交上層報錯",
          len(calls) == 0 and rc is None)

    # --- 情境 5：版本已經合格 → 什麼都不做 ---
    calls.clear()
    with mock.patch.object(launch, "check_python", return_value=None), \
         mock.patch.object(launch, "find_python") as fp, \
         mock.patch.object(os, "execve", spy):
        rc = launch.reexec_if_python_too_old()

    check("版本合格 → 連找都不找", len(calls) == 0 and rc is None and not fp.called)


# ---------------------------------------------------------------- 契約

def test_entry_scripts_guard_python(tmp: Path) -> None:
    """每個進入點都要自己擋版本太舊 —— **不能只靠 launch.py**。

    ## 為什麼（實際踩到）

    使用者直接跑 `python3 app/mac_vibetalkie.py`（而不是透過啟動器），
    而他的 `python3` 是 conda 的 3.10：

        ModuleNotFoundError: No module named 'tomllib'

    `tomllib` 是 3.11 才進標準庫的。使用者看到的是一個指向 import 的錯誤，
    **完全看不出「你只是用錯 Python」** —— 而 launch.py 明明已經有
    「找合格的 Python 並重新執行」的邏輯，只是被繞過了。

    所以修法是：進入點在最前面（**早於任何需要 3.11+ 的 import**）
    呼叫 `launch.reexec_if_python_too_old(自己)`。

    這個測試用**真的舊解譯器**跑一次，驗證它不會炸 tomllib。
    找不到舊解譯器就跳過（不假裝測過）。
    """
    print("\n▍進入點的版本守衛（不能只靠 launch.py）")

    import subprocess
    entry = ROOT / "app" / "mac_vibetalkie.py"
    if not entry.is_file():
        skip("進入點版本守衛", "找不到 app/mac_vibetalkie.py")
        return

    src = entry.read_text(encoding="utf-8")
    guard_at = src.find("_reexec_with_better_python")
    config_at = src.find("import config as config_module")
    check("mac_vibetalkie.py 有版本守衛的呼叫", guard_at != -1)
    check("守衛**在** import config 之前（順序是關鍵）",
          guard_at != -1 and config_at != -1 and guard_at < config_at,
          f"guard@{guard_at} config@{config_at}")

    # 用真的舊解譯器跑 —— 這是唯一能證明「不會炸 tomllib」的方法。
    #
    # ⚠️ **不要寫死任何人的家目錄路徑。** 這個 repo 是公開的
    #    （AGENTS.md §7），寫死 `/Users/<某人>/...` 等於洩漏使用者名稱。
    #    改成掃 PATH 上所有 python3，挑第一個版本 < 3.11 的。
    old: list[str] = []
    seen: set[str] = set()
    for name in ("python3", "python3.10", "python3.9", "python3.8", "python"):
        p = shutil.which(name)
        if not p:
            continue
        real = os.path.realpath(p)
        if real in seen:
            continue
        seen.add(real)
        ver = _python_version_of(p)
        if ver and ver < (3, 11):
            old.append(p)
    if not old:
        skip("用真實舊解譯器驗證",
             "PATH 上找不到 < 3.11 的 Python（有就測，沒有不假裝測過）")
        return

    exe = old[0]
    r = subprocess.run([exe, str(entry), "--check"], capture_output=True,
                       text=True, timeout=180, cwd=str(ROOT))
    combined = (r.stdout or "") + (r.stderr or "")
    check(f"用 {_python_version_of(exe)} 跑不會炸 tomllib",
          "tomllib" not in combined,
          "出現 tomllib 錯誤 = 守衛沒擋住")
    check("會說明版本太舊並自動換一個",
          "太舊" in combined or "需要 Python 3.11" in combined,
          combined.strip().splitlines()[-1][:60] if combined.strip() else "")


def _python_version_of(exe: str) -> tuple[int, int] | None:
    """直接呼叫另一支解譯器問版本（測試自己的小工具）。"""
    import subprocess
    try:
        r = subprocess.run([exe, "-c", "import sys;print('%d %d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=10)
        a, b = r.stdout.split()
        return int(a), int(b)
    except Exception:                                   # noqa: BLE001
        return None


def test_contract() -> None:
    print("\n▍契約（避免改動時默默破壞）")

    check("MIN_PY 是 3.11（tomllib 的需求）", launch.MIN_PY == (3, 11),
          str(launch.MIN_PY))
    check("哨兵名稱有定義", isinstance(launch._REEXEC_FLAG, str)
          and len(launch._REEXEC_FLAG) > 0, launch._REEXEC_FLAG)
    # 注意：launch.py 仍然把 macOS 列為「尚未支援」——這是**刻意的**，
    # 因為它走的是 `app/vibetalkie.py` 那條 Windows 實作路徑。
    # macOS 有自己的進入點 `app/mac_vibetalkie.py`，
    # 由 `啟動 VibeTalkie.command` 直接呼叫（不經過 launch.py）。
    check("launch.py 仍只支援 Windows（macOS 有自己的進入點）",
          "darwin" not in launch.SUPPORTED)


def main() -> int:
    print("=" * 74)
    print("啟動器：Python 自動尋找測試")
    print("=" * 74)
    print(f"  執行環境：Python {sys.version.split()[0]}　{sys.platform}")

    # ⚠️ **這支測試只在 POSIX（macOS / Linux）上有意義。**
    #
    #    它用 `#!/bin/sh` 腳本 ＋ `chmod +x` 來偽造「舊版 Python 執行檔」，
    #    而 `.sh` 在 Windows 上既不能執行、chmod 也沒有意義。測的內容又是
    #    「`launch.py` 在 macOS 上找不到合格 Python 時重新執行自己」——
    #    `launch.py` 對 Windows 根本不走那條路（它直接支援 win32）。
    #
    #    實測：這支測試在 Windows 上從來沒有通過過（會在 `write_text` 就
    #    `PermissionError`，因為那個路徑在沙箱的暫存區），而且失敗訊息
    #    （一個寫檔錯誤）**完全指向錯的地方** —— 看起來像權限問題或整合
    #    造成的退化，其實只是「這支測試不該在 Windows 上跑」。
    #
    #    直接跳過並講清楚原因，比留著一個看不懂的紅燈誠實。
    if sys.platform == "win32":
        print("\n  ⏭  這支測試只適用 POSIX（macOS / Linux）。")
        print("     它用 `#!/bin/sh` 腳本模擬舊版 Python 執行檔，並驗證")
        print("     `launch.py` 的『找不到合格 Python 就重新執行自己』——")
        print("     那條路徑在 Windows 上不存在（launch.py 直接支援 win32）。")
        print("     請在 macOS 或 Linux 上跑：python tests/test_launch_python.py")
        return 0

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_python_version(tmp)
        test_candidates()
        test_find_python_prefers_oldest(tmp)
        test_reexec(tmp)
        test_entry_scripts_guard_python(tmp)
        test_contract()

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
