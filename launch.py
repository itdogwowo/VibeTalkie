#!/usr/bin/env python3
"""VibeTalkie 跨平台啟動器（雙擊可用）。

各平台的雙擊入口都只是薄薄一層，實際檢查都在這裡：

    Windows  啟動 VibeTalkie.cmd
    macOS    啟動 VibeTalkie.command
    Linux    VibeTalkie.desktop

啟動前會先檢查下面這些，有問題就**講清楚原因並停住**（雙擊的視窗不會一閃就消失）：

    1. Python 版本（需要 3.11+，因為用到 tomllib）
    2. 平台是否支援
    3. 相依套件是否就緒（sherpa-onnx，見 tools/p1/fetch_wheels.py）
    4. ASR 模型是否存在（見 tools/p1/fetch_model.py）

⚠️ 絕對不要改成「隱藏視窗 + 脫離父行程」的啟動方式。
   那個模式會被 EDR 當成惡意行為（見 AGENTS.md §8.5），而且沒有必要。
"""

from __future__ import annotations

import os
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
    say("=" * 66)
    say("  VibeTalkie 啟動器")
    say("=" * 66)
    say(f"  Python {sys.version.split()[0]}　平台 {sys.platform}")
    say()

    if (err := check_python()):
        return fail("Python 版本不符", err, "",
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
