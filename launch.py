#!/usr/bin/env python3
"""VibeTalkie 跨平台啟動器（雙擊可用）。

各平台的雙擊入口都只是薄薄一層，實際檢查都在這裡：

    Windows  啟動 VibeTalkie.cmd
    macOS    啟動 VibeTalkie.command
    Linux    VibeTalkie.desktop

啟動前會先檢查下面這些，有問題就**講清楚原因並停住**（雙擊的視窗不會一閃就消失）：

    1. Python 版本（需要 3.11+，因為用到 tomllib）
       → 版本不對時會**自動找一個合適的並重新執行自己**（見 find_python）
    2. 平台是否支援
    3. 相依套件是否就緒（sherpa-onnx，見 tools/p1/fetch_wheels.py）
    4. ASR 模型是否存在（見 tools/p1/fetch_model.py）

⚠️ 絕對不要改成「隱藏視窗 + 脫離父行程」的啟動方式。
   那個模式會被 EDR 當成惡意行為（見 AGENTS.md §8.5），而且沒有必要。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_PY = (3, 11)
MODEL_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"

# 目前只有 Windows 實作（Raw Input / 剪貼簿 / waveIn 都是 Win32）。
# 這不是「還沒測試」，是真的還沒寫 —— 所以明確說出來。
SUPPORTED = {"win32": "Windows"}
PLANNED = {"darwin": "macOS", "linux": "Linux"}


def setup_console() -> None:
    """把主控台切成 UTF-8。

    沒有這一步的話，Windows 主控台預設是 cp950，而我們的訊息裡有
    `✗`（U+2717）這種 cp950 編不了的字符 → 不是顯示亂碼而已，
    是**直接 UnicodeEncodeError 崩潰**，錯誤訊息反而看不到。
    其他工具（tools/p0、tools/p1）都有這一段，啟動器先前漏了。
    """
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def pause_if_double_clicked() -> None:
    """雙擊啟動時視窗會一閃就關，看不到錯誤。有互動終端機就不要停。"""
    if sys.stdin and sys.stdin.isatty():
        return
    try:
        input("\n按 Enter 關閉…")
    except Exception:
        pass


def fail(title: str, *lines: str) -> int:
    say()
    say("=" * 66)
    say(f"  ✗ {title}")
    say("=" * 66)
    for line in lines:
        say(f"  {line}")
    say()
    pause_if_double_clicked()
    return 1


def check_python() -> str | None:
    if sys.version_info < MIN_PY:
        return (f"需要 Python {MIN_PY[0]}.{MIN_PY[1]} 以上，"
                f"目前是 {sys.version.split()[0]}（tomllib 需要 3.11+）")
    return None


# ---------------------------------------------------------------- 找 Python

# 重新執行自己時設的哨兵。**沒有它會無限迴圈**：
# 換了 Python 之後若那個版本也不合格，就會再找一次、再執行一次。
_REEXEC_FLAG = "VIBETALKIE_REEXEC"


def _python_version(exe: str) -> tuple[int, int] | None:
    """問一個執行檔「你是第幾版」。不是 Python 或跑不動就回 None。"""
    try:
        out = subprocess.run(
            [exe, "-c", "import sys;print('%d %d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    parts = out.stdout.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _candidates() -> list[str]:
    """所有「可能的」Python 執行檔，去重後回傳。

    使用者機器上常常同時有 conda、pyenv、Homebrew、系統內建等多套 Python，
    而 PATH 最前面的那個不一定是對的版本 —— 這正是實際踩到的情況：

        (base) 環境的 conda python3 = 3.10.10  →  雙擊直接被判版本不符

    ⚠️ **不要只加「已知路徑」**：那等於把使用者的安裝方式寫死。
    一律先用 PATH 掃（尊重使用者的環境），再補上各平台的常見位置當備援。
    """
    names = (["python3.14", "python3.13", "python3.12", "python3.11", "python3", "python"]
             if os.name != "nt" else ["python.exe", "python3.exe"])
    found: list[str] = []
    for n in names:
        p = shutil.which(n)
        if p:
            found.append(p)
    # 目前這個解譯器本身也要列入（可能是使用者刻意指定的虛擬環境）
    found.append(sys.executable)
    # 常見但不在 PATH 上的位置（macOS 的 framework build 最容易漏掉）
    extra = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
             "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"]
    found.extend(p for p in extra if os.path.exists(p))

    out: list[str] = []
    seen: set[str] = set()
    for p in found:
        try:
            key = os.path.realpath(p)     # 同一顆用不同路徑指到時只留一個
        except OSError:
            key = p
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_python() -> str | None:
    """找一個版本合格的 Python 執行檔，找不到回 None。

    **挑最舊的合格版本，不是最新的。** 這是刻意的：

        專案要 3.11+，而最新版（例如 3.14）常常還沒有相依套件的 wheel。
        挑最新會把使用者推進「版本夠新但套件裝不起來」的死路。

    合格的定義是「執行檔真的跑得起來而且 ≥ MIN_PY」，
    不是「檔名看起來像」—— 檔名會騙人。
    """
    ok: list[tuple[tuple[int, int], str]] = []
    for exe in _candidates():
        ver = _python_version(exe)
        if ver and ver >= MIN_PY:
            ok.append((ver, exe))
    if not ok:
        return None
    ok.sort(key=lambda t: t[0])          # 版本由小到大 → 取最舊的合格版本
    return ok[0][1]


def reexec_if_python_too_old(script: str | Path | None = None) -> int | None:
    """目前的 Python 太舊就換一個，重新執行自己。回傳結束碼或 None（不用換）。

    `script`：要重新執行的檔案。預設是 `launch.py` 自己；
    其他進入點（例如 `app/mac_vibetalkie.py`）可以指定自己 ——
    否則會把使用者丟回啟動器，而啟動器在 macOS 上只會說「尚未支援」。

    為什麼用「重新執行自己」而不是「在 shell 入口換 Python」：
      `.cmd` / `.command` / `.desktop` 三個入口都只是薄殼，
      把邏輯放在這裡，三個平台一起受益，也不必在 shell 裡重寫一遍。
    """
    if check_python() is None:
        return None
    if os.environ.get(_REEXEC_FLAG):     # 已經換過一次就不再換，避免無限迴圈
        return None
    better = find_python()
    if not better:
        return None
    # 找到的就是自己 → 沒必要重跑（也避免無窮迴圈）
    try:
        if os.path.realpath(better) == os.path.realpath(sys.executable):
            return None
    except OSError:
        return None

    target = Path(script).resolve() if script else Path(__file__).resolve()
    ver = _python_version(better) or ("?", "?")
    say(f"  目前的 Python {sys.version.split()[0]} 太舊"
        f"（需要 {MIN_PY[0]}.{MIN_PY[1]}+），改用 {ver[0]}.{ver[1]} 重新啟動…")
    say(f"  {better}")
    say()
    env = dict(os.environ, **{_REEXEC_FLAG: "1"})
    try:
        # ⚠️ execv 而不是 subprocess：同一個行程直接換掉，
        # 不留下「父行程已結束但子行程還在跑」的狀態。
        os.execve(better, [better, str(target), *sys.argv[1:]], env)
    except OSError as exc:
        say(f"  ⚠️ 換不過去（{exc}），改用目前這個繼續。")
        say()
        return None


def check_platform() -> str | None:
    if sys.platform in SUPPORTED:
        return None
    name = PLANNED.get(sys.platform, sys.platform)
    return f"{name} 尚未支援"


def check_deps() -> str | None:
    tp = ROOT / "third_party"
    if str(tp) not in sys.path:
        sys.path.insert(0, str(tp))
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return ("載入不到 sherpa_onnx")
    return None


def check_model() -> str | None:
    d = ROOT / "models" / MODEL_NAME
    if not d.is_dir():
        return f"找不到模型：models/{MODEL_NAME}"
    if not ((d / "model.int8.onnx").exists() or (d / "model.onnx").exists()):
        return f"模型目錄不完整：{d}"
    return None


def main() -> int:
    setup_console()

    # ⚠️ 這一步必須在印出任何東西**之前**：如果換了 Python，
    # 下面的版本號與平台資訊才是最後真正在跑的那一個，
    # 否則使用者會看到「兩個 Python 版本」的困惑輸出。
    if (rc := reexec_if_python_too_old()) is not None:
        return rc

    say("=" * 66)
    say("  VibeTalkie 啟動器")
    say("=" * 66)
    say(f"  Python {sys.version.split()[0]}　平台 {sys.platform}")
    say()

    if (err := check_python()):
        # 走到這裡代表「找不到任何合格的 Python」—— 連自動換都換不了。
        found = [f"{v[0]}.{v[1]}　{p}" for p in _candidates()
                 if (v := _python_version(p))]
        listing = ["系統上找到的 Python："] if found else ["系統上找不到任何 Python。"]
        listing += [f"  · {f}" for f in found]
        return fail("Python 版本不符", err, "", *listing, "",
                    "請安裝 Python 3.11 以上：https://www.python.org/downloads/")

    if (err := check_platform()):
        return fail(
            "這個平台還沒實作", err, "",
            "VibeTalkie 目前只有 Windows 實作，因為下列模組直接使用 Win32 API：",
            "  · 全域熱鍵（Raw Input + 裝置身分 + E0 旗標）",
            "  · 文字注入（剪貼簿 + SendInput）",
            "  · 錄音（winmm waveIn）",
            "",
            "macOS / Linux 的對應實作尚未開始。",
            "架構與模組邊界已經定義好，見 docs/architecture.md §6。")

    if (err := check_deps()):
        return fail("相依套件未就緒", err, "",
                    "請執行（會自行下載 wheel 並解開，不需要 pip）：",
                    "  python tools/p1/fetch_wheels.py sherpa-onnx",
                    "",
                    "註：本機的 pip 可能無法使用，這個工具就是為了繞過它。")

    if (err := check_model()):
        return fail("ASR 模型未就緒", err, "",
                    "請執行（約 163 MB，會下載並解壓到 models/）：",
                    f"  python tools/p1/fetch_model.py --get {MODEL_NAME}")

    say("  檢查通過，啟動中…")
    say()
    app = ROOT / "app" / "vibetalkie.py"
    if not app.exists():
        return fail("找不到主程式", str(app))

    try:
        return subprocess.call([sys.executable, str(app), *sys.argv[1:]])
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        return fail("啟動失敗", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
