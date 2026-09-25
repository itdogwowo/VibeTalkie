#!/usr/bin/env python3
"""下載 sherpa-onnx 的 ASR 模型到 models/（models/ 已 gitignore）。

模型放在 GitHub release `asr-models`，每個都是 .tar.bz2。
用 GitHub API 查詢可下載的檔案與大小，再挑選下載，避免盲抓。

用法:
    python tools/p1/fetch_model.py --list sense-voice     # 列出符合關鍵字的模型
    python tools/p1/fetch_model.py --list paraformer
    python tools/p1/fetch_model.py --get sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models"
CACHE = ROOT / "model_downloads"

RELEASE_API = "https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/asr-models"


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


def _get_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "vibetalkie-p1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def list_assets() -> list[dict]:
    """列出 release 的所有資產。

    注意：`/releases/tags/<tag>` 回傳的是**單一 release 物件**（不是陣列），
    而且它內嵌的 assets 有數量上限。要拿完整清單必須用
    `/releases/<id>/assets`，那個才有分頁。
    """
    release = _get_json(RELEASE_API)
    if not isinstance(release, dict) or "id" not in release:
        raise RuntimeError(f"GitHub API 回應不如預期：{release!r}")

    assets: list[dict] = []
    page = 1
    while True:
        batch = _get_json(
            f"https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/"
            f"{release['id']}/assets?per_page=100&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        assets.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 40:
            break
    return assets


def cmd_list(keyword: str) -> int:
    assets = list_assets()
    print(f"release asr-models 共有 {len(assets)} 個檔案\n")
    hits = [a for a in assets if keyword.lower() in a["name"].lower()
            and a["name"].endswith(".tar.bz2")]
    if not hits:
        print(f"沒有符合 '{keyword}' 的模型。")
        return 1
    hits.sort(key=lambda a: a["size"])
    for a in hits:
        print(f"  {a['size'] / 1e6:>8.1f} MB  {a['name']}")
    print(f"\n合計 {len(hits)} 個。用 --get <檔名去副檔名> 下載。")
    return 0


def cmd_get(name: str) -> int:
    target_dir = name.replace(".tar.bz2", "")
    if (MODELS / target_dir).exists():
        print(f"✅ 已存在：models/{target_dir}（要重抓請先刪除）")
        return 0

    assets = list_assets()
    match = next((a for a in assets if a["name"] == f"{target_dir}.tar.bz2"), None)
    if not match:
        print(f"❌ 找不到 {target_dir}.tar.bz2，請先用 --list 確認名稱。")
        return 1

    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / match["name"]
    print(f"下載 {match['name']}（{match['size'] / 1e6:.1f} MB）…")
    with urllib.request.urlopen(match["browser_download_url"], timeout=1800) as resp, \
            archive.open("wb") as fh:
        shutil.copyfileobj(resp, fh)

    got = archive.stat().st_size
    if got != match["size"]:
        print(f"⚠️ 大小不符：預期 {match['size']}，實際 {got}")

    MODELS.mkdir(parents=True, exist_ok=True)
    print("解壓縮…")
    with tarfile.open(archive, "r:bz2") as tf:
        tf.extractall(MODELS)

    print(f"\n✅ models/{target_dir}")
    for p in sorted((MODELS / target_dir).rglob("*")):
        if p.is_file():
            print(f"   {p.stat().st_size / 1e6:>8.2f} MB  {p.relative_to(MODELS / target_dir)}")
    archive.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下載 sherpa-onnx ASR 模型")
    parser.add_argument("--list", metavar="KEYWORD", help="列出符合關鍵字的模型與大小")
    parser.add_argument("--get", metavar="NAME", help="下載並解壓縮指定模型")
    args = parser.parse_args(argv)
    setup_console()

    if args.list:
        return cmd_list(args.list)
    if args.get:
        return cmd_get(args.get)
    parser.error("請用 --list <關鍵字> 或 --get <模型名>")


if __name__ == "__main__":
    raise SystemExit(main())
