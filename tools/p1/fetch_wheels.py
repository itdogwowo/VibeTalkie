#!/usr/bin/env python3
"""不安裝 pip 套件，直接把 wheel 解開到 third_party/。

為什麼需要這個？
    本機的沙箱環境下，`pip install` / `pip download` 都會失敗：
        PermissionError: [Errno 13] Permission denied:
            ...\\pip-unpack-xxxx\\<pkg>.whl.metadata
    連把 pip 的 TEMP 指到工作區內也一樣。但單純用 Python 寫檔完全正常，
    網路也正常（PyPI 查得到、wheel 抓得到）。

    wheel 本質上只是一個 zip 檔，所以繞過 pip 的安裝流程、
    自己下載 + 驗證 sha256 + 解壓縮，就能達成一樣的效果。

用法:
    python tools/p1/fetch_wheels.py sherpa-onnx
    python tools/p1/fetch_wheels.py --info sherpa-onnx      # 只看版本與依賴
    python tools/p1/fetch_wheels.py --list                  # 已解開的套件

解開後把 third_party/ 加進 PYTHONPATH 即可：
    set PYTHONPATH=%CD%\\third_party
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import sysconfig
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIRD_PARTY = ROOT / "third_party"
WHEEL_CACHE = ROOT / "wheels"


def _python_tags() -> tuple[str, ...]:
    """目前解譯器可用的 wheel python tag，由新到舊。

    ⚠️ 原本是寫死的 `("cp314", "cp313", …)`。寫死的問題有兩個：
      1. 換 Python 版本就要改程式碼（而且忘了改的症狀是「什麼都裝不起來」）
      2. 它不反映**實際在跑的那個解譯器** —— 用 3.10 跑卻優先挑 cp314，
         結果是下載了不能用的 wheel，錯誤訊息還指向別的地方
    """
    major, minor = sys.version_info[:2]
    tags = [f"cp{major}{minor}"]
    # 同一個 major 的較舊版本也可以（wheel 有向下相容的慣例）
    for m in range(minor - 1, 5, -1):
        tags.append(f"cp{major}{m}")
    tags += ["abi3", "py3", "py2.py3"]
    return tuple(tags)


def platform_patterns(sys_platform: str | None = None,
                      machine: str | None = None,
                      bits: int | None = None) -> tuple[str, ...]:
    """本平台可接受的 wheel 平台標籤，由最適配到最通用。

    ⚠️ **原本寫死 `("win_amd64", "win32", "any")`** —— 那讓這個工具
       在 macOS / Linux 上永遠選不到 wheel（`pick_wheel` 回 None），
       症狀是「明明有 wheel 卻說找不到」。現在依實際平台與架構決定。

    回傳的是**正則片段**（不是純字串），因為 macOS 的標籤長這樣：
        macosx_11_0_arm64 / macosx_10_9_x86_64 / macosx_10_15_universal2
    版本號與架構都必須彈性比對，不能用 `endswith` 硬猜。

    三個參數都可以注入，**目的是讓每一個平台的分支都測得到** ——
    否則「在 mac 上開發、Windows 分支沒人驗證」就會變成下一次的驚喜。
    """
    import platform as _platform

    plat = sys_platform if sys_platform is not None else sys.platform
    mach = (machine if machine is not None else _platform.machine()).lower()
    nbits = bits if bits is not None else (64 if sys.maxsize > 2**32 else 32)

    if plat == "darwin":
        arch = {"arm64": "arm64", "aarch64": "arm64",
                "x86_64": "x86_64"}.get(mach, mach)
        return (
            rf"macosx_\d+_\d+_{arch}",      # 最適配：指定 macOS 版本 + 本機架構
            r"macosx_\d+_\d+_universal2",   # 通用二進位（arm64 + x86_64）
            r"macosx_\d+_\d+_universal",
            r"macosx_\d+_\d+_intel",
            "any",
        )
    if plat == "win32":
        bitted = "win_amd64" if nbits == 64 else "win32"
        other = "win32" if bitted == "win_amd64" else "win_amd64"
        return (bitted, other, "any")
    # Linux 與其他
    return (
        rf"manylinux\d*_\d+_\d+_{mach}",
        rf"musllinux_\d+_\d+_{mach}",
        rf"linux_{mach}",
        "any",
    )


TAG_PRIORITY = _python_tags()
PLATFORM_PATTERNS = platform_patterns()
# 相容舊名稱（原本是純字串 tuple，現在是正則；保留給外部引用）
PLATFORM_PRIORITY = PLATFORM_PATTERNS


def setup_console() -> None:
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


def pypi_json(name: str) -> dict:
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def wheel_rank(filename: str) -> tuple[int, int] | None:
    """回傳 (python 標籤優先序, 平台優先序)；不適用的回 None。"""
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    pythons, _abi, plat = parts[-3], parts[-2], parts[-1]

    py_rank = None
    for i, tag in enumerate(TAG_PRIORITY):
        if tag in pythons.split("."):
            py_rank = i
            break
    if py_rank is None:
        return None

    plat_rank = None
    for i, pat in enumerate(PLATFORM_PATTERNS):
        # ⚠️ 用正則比對而不是 `plat == tag or plat.endswith(tag)`：
        #    macOS 的標籤是 `macosx_11_0_arm64` 這種形式，
        #    版本號與架構都必須彈性比對，硬猜會選錯架構的 wheel
        #    （選錯的症狀是裝得起來但 import 時才爆，而且訊息看不懂）。
        if re.fullmatch(pat, plat):
            plat_rank = i
            break
    if plat_rank is None:
        return None
    return py_rank, plat_rank


def pick_wheel(data: dict) -> dict | None:
    candidates = []
    for f in data["urls"]:
        rank = wheel_rank(f["filename"])
        if rank is not None:
            candidates.append((rank, f))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def download(url: str, dest: Path, sha256: str | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256:
        if hashlib.sha256(dest.read_bytes()).hexdigest() == sha256:
            print(f"  已快取：{dest.name}")
            return
    print(f"  下載 {dest.name} …")
    with urllib.request.urlopen(url, timeout=300) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    if sha256:
        got = hashlib.sha256(dest.read_bytes()).hexdigest()
        if got != sha256:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"sha256 不符：預期 {sha256}，實際 {got}")
        print("  sha256 ✅")


def extract(wheel: Path, target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(wheel) as zf:
        for member in zf.namelist():
            # wheel 的 .data/ 目錄放的是 scripts/purelib 等，這裡不需要
            if ".data/" in member:
                continue
            zf.extract(member, target)
            count += 1
    return count


def installed_version(name: str) -> str | None:
    for dist in THIRD_PARTY.glob(f"{name.replace('-', '_')}-*.dist-info/METADATA"):
        for line in dist.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return None


def cmd_list() -> int:
    if not THIRD_PARTY.exists():
        print("third_party/ 不存在（尚未安裝任何套件）")
        return 0
    found = sorted({p.name.split("-")[0] for p in THIRD_PARTY.glob("*.dist-info")})
    if not found:
        print("third_party/ 內沒有 dist-info")
        return 0
    for n in found:
        print(f"  {n:<22} {installed_version(n)}")
    return 0


def cmd_info(names: list[str]) -> int:
    for name in names:
        data = pypi_json(name)
        wheel = pick_wheel(data)
        print(f"{name}: 最新 {data['info']['version']}")
        print(f"  選用 wheel: {wheel['filename'] if wheel else '(找不到相容 wheel)'}")
        if wheel:
            print(f"  大小: {wheel['size'] / 1e6:.1f} MB")
        reqs = data["info"].get("requires_dist") or []
        print(f"  依賴: {', '.join(reqs) if reqs else '(無)'}")
    return 0


def cmd_install(names: list[str], force: bool) -> int:
    print(f"目標目錄: {THIRD_PARTY}")
    print(f"解譯器標籤: {sysconfig.get_config_var('EXT_SUFFIX')} "
          f"({platform.python_version()}, {platform.machine()})")
    print()
    pending = list(names)
    done: set[str] = set()
    failed = False

    while pending:
        name = pending.pop(0)
        if name in done:
            continue
        done.add(name)

        current = installed_version(name)
        if current and not force:
            print(f"✅ {name} 已安裝（{current}），略過（--force 可強制重裝）")
            continue

        print(f"→ {name}")
        try:
            data = pypi_json(name)
        except Exception as exc:
            print(f"  ❌ 查詢 PyPI 失敗：{exc}")
            failed = True
            continue

        wheel = pick_wheel(data)
        if not wheel:
            print(f"  ❌ 找不到相容此平台的 wheel（純源碼套件請自行處理）")
            failed = True
            continue

        dest = WHEEL_CACHE / wheel["filename"]
        try:
            download(wheel["url"], dest, wheel.get("digests", {}).get("sha256"))
            n = extract(dest, THIRD_PARTY)
            print(f"  解開 {n} 個檔案 → version {data['info']['version']}")
        except Exception as exc:
            print(f"  ❌ 失敗：{exc}")
            failed = True
            continue

        # 依賴：只挑必要（不含 extra）
        reqs = data["info"].get("requires_dist") or []
        for req in reqs:
            if "extra ==" in req:
                continue
            dep = req.split(";")[0].strip()
            for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
                dep = dep.split(sep)[0]
            dep = dep.strip()
            if dep and dep.lower() not in done:
                print(f"  （依賴）{dep}")
                pending.append(dep)

    print()
    if failed:
        print("⚠️ 有套件未完成，請看上面訊息。")
        return 1
    print("完成。使用方式：")
    print(f'  $env:PYTHONPATH = "{THIRD_PARTY}"')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下載 wheel 並解開到 third_party/（繞過 pip）")
    parser.add_argument("packages", nargs="*", help="套件名稱")
    parser.add_argument("--info", action="store_true", help="只顯示版本與依賴，不安裝")
    parser.add_argument("--list", action="store_true", help="列出已解開的套件")
    parser.add_argument("--force", action="store_true", help="已安裝也重新解開")
    args = parser.parse_args(argv)
    setup_console()

    if args.list:
        return cmd_list()
    if not args.packages:
        parser.error("請指定套件名稱，或用 --list")
    if args.info:
        return cmd_info(args.packages)
    return cmd_install(args.packages, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
