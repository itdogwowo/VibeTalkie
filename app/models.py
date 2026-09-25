#!/usr/bin/env python3
"""離線模型的下載與管理。

設計重點：

1. **目錄是寫死的，不是每次去打 GitHub API。**
   GitHub 的 release 有 499 個檔案，匿名 API 有速率限制，
   而且要打兩次才拿得到清單。寫死的目錄可以離線顯示、反應快，
   而且每一筆的說明（有沒有標點、適合什麼語言）是我們自己確認過的。

2. **下載在背景執行緒，UI 不會卡住。**
   模型 78–234 MB，用主執行緒下載會讓整個介面停住。

3. **下載一次就是一次明確的使用者動作。**
   產品的承諾是「零強制上傳」—— 那講的是**音訊**。
   下載模型是往外抓檔案，方向相反，但 UI 必須講清楚，
   否則使用者會以為它偷偷連網。

4. 下載來源是 sherpa-onnx 官方 release（k2-fsa/sherpa-onnx），
   `tools/p1/fetch_model.py` 已經驗證過這條路可用。
"""

from __future__ import annotations

import json
import shutil
import tarfile
import threading
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
DOWNLOADS_DIR = MODELS_DIR / ".downloads"

RELEASE_TAG = "asr-models"
URL_TEMPLATE = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                f"{RELEASE_TAG}/{{name}}.tar.bz2")


@dataclass(frozen=True)
class ModelSpec:
    name: str                # 同時是 release 檔名（去 .tar.bz2）與解壓後的目錄名
    title: str               # UI 顯示名稱
    kind: str                # sense_voice | paraformer — 決定要用哪個 sherpa-onnx 工廠
    size_mb: float           # 實測大小，用於顯示
    langs: str
    note: str
    recommended: bool = False


# 目錄內容由 `tools/p1/fetch_model.py --list <關鍵字>` 查證後寫入。
# 要新增模型時：先跑那個工具確認檔名與大小，再加進來。
CATALOG: list[ModelSpec] = [
    ModelSpec(
        name="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        title="SenseVoice 多語言（int8）",
        kind="sense_voice",
        size_mb=163.0,
        langs="中文・英文・日文・韓文・粵語",
        note="預設。有標點與數字正規化，速度快。輸出為簡體，台灣使用需經簡→繁轉換。",
        recommended=True,
    ),
    ModelSpec(
        name="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
        title="SenseVoice 多語言（int8，2025 新版）",
        kind="sense_voice",
        size_mb=165.8,
        langs="中文・英文・日文・韓文・粵語",
        note="與預設同系列的新版權重。若預設辨識不理想可試這個。",
    ),
    ModelSpec(
        name="sherpa-onnx-paraformer-zh-small-2024-03-09",
        title="Paraformer 中文（小型）",
        kind="paraformer",
        size_mb=77.9,
        langs="中文",
        note="最小、最快。⚠️ 不產生標點，輸出也會是簡體。適合只求快的情境。",
    ),
    ModelSpec(
        name="sherpa-onnx-paraformer-zh-int8-2025-10-07",
        title="Paraformer 中文（int8）",
        kind="paraformer",
        size_mb=228.3,
        langs="中文",
        note="純中文辨識通常較強。⚠️ 不產生標點。",
    ),
]


def spec(name: str) -> ModelSpec | None:
    return next((m for m in CATALOG if m.name == name), None)


def _guess_kind(name: str) -> str:
    """從目錄名猜測模型種類（給手動放進 models/ 的模型用）。"""
    n = name.lower()
    if "paraformer" in n:
        return "paraformer"
    if "sense-voice" in n or "sensevoice" in n:
        return "sense_voice"
    if "whisper" in n:
        return "whisper"
    return "sense_voice"        # 保守預設，失敗時會回報錯誤而不是亂猜成功


# ---------------------------------------------------------------- 本機狀態

def model_dir(name: str) -> Path:
    return MODELS_DIR / name


def is_ready(name: str) -> bool:
    """目錄存在，而且有模型檔與 tokens。"""
    d = model_dir(name)
    if not d.is_dir():
        return False
    has_model = (d / "model.onnx").exists() or (d / "model.int8.onnx").exists()
    return has_model and (d / "tokens.txt").exists()


def model_file(name: str) -> Path | None:
    d = model_dir(name)
    for f in ("model.int8.onnx", "model.onnx"):
        if (d / f).exists():
            return d / f
    return None


def disk_usage_mb(name: str) -> float:
    d = model_dir(name)
    if not d.is_dir():
        return 0.0
    total = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
    return round(total / 1e6, 1)


def installed_models() -> list[str]:
    """models/ 底下所有「看起來可用」的目錄（包含目錄外的自訂模型）。"""
    if not MODELS_DIR.is_dir():
        return []
    out = []
    for d in sorted(MODELS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if (d / "tokens.txt").exists() and (
                (d / "model.onnx").exists() or (d / "model.int8.onnx").exists()):
            out.append(d.name)
    return out


# ---------------------------------------------------------------- 下載管理

class Download:
    """單一模型的下載狀態。"""

    def __init__(self, name: str):
        self.name = name
        self.state = "queued"      # queued|downloading|extracting|ready|error|cancelled
        self.downloaded = 0
        self.total = 0
        self.error: str | None = None
        self.started = time.time()
        self.speed_bps = 0.0
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def percent(self) -> float:
        if self.state == "extracting":
            return 100.0
        if not self.total:
            return 0.0
        return round(self.downloaded / self.total * 100, 1)

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "percent": self.percent,
            "downloaded_mb": round(self.downloaded / 1e6, 1),
            "total_mb": round(self.total / 1e6, 1) if self.total else None,
            "speed_mbps": round(self.speed_bps / 1e6, 2) if self.speed_bps else None,
            "error": self.error,
        }


class ModelManager:
    """管理模型下載。一次只跑一個，避免塞爆頻寬與磁碟。"""

    def __init__(self, on_change=None):
        self._lock = threading.Lock()
        self._downloads: dict[str, Download] = {}
        self._queue: list[str] = []
        self._worker: threading.Thread | None = None
        self.on_change = on_change          # 可選回呼，狀態變動時通知

    # -- 對外查詢 --
    def list_models(self, active: str) -> list[dict]:
        with self._lock:
            dl = {k: v.snapshot() for k, v in self._downloads.items()}
        out = []
        for m in CATALOG:
            d = asdict(m)
            info = dl.get(m.name)
            if info and info["state"] in ("queued", "downloading", "extracting"):
                d["state"] = info["state"]
            elif is_ready(m.name):
                d["state"] = "ready"
            elif info and info["state"] == "error":
                d["state"] = "error"
            else:
                d["state"] = "absent"
            d["download"] = info
            d["active"] = (m.name == active)
            d["installed_mb"] = disk_usage_mb(m.name) if d["state"] == "ready" else 0.0
            out.append(d)

        # 不在目錄裡、但使用者自己放進 models/ 的目錄也要看得到
        known = {m.name for m in CATALOG}
        for extra in installed_models():
            if extra in known:
                continue
            out.append({
                "name": extra, "title": extra, "kind": _guess_kind(extra),
                "size_mb": 0.0, "langs": "", "note": "手動放置的模型",
                "recommended": False, "state": "ready", "download": None,
                "active": extra == active, "installed_mb": disk_usage_mb(extra),
            })
        return out

    def active_downloads(self) -> list[dict]:
        with self._lock:
            return [v.snapshot() for v in self._downloads.values()
                    if v.state in ("queued", "downloading", "extracting")]

    # -- 動作 --
    def start(self, name: str) -> tuple[bool, str]:
        if spec(name) is None and name not in installed_models():
            # 允許下載目錄外的名字（例如使用者從別處抄來的），但仍要防目錄跳脫
            if "/" in name or "\\" in name or ".." in name:
                return False, "模型名稱不合法"
        if is_ready(name):
            return False, "這個模型已經下載好了"
        with self._lock:
            cur = self._downloads.get(name)
            if cur and cur.state in ("queued", "downloading", "extracting"):
                return False, "這個模型正在下載中"
            self._downloads[name] = Download(name)
            self._queue.append(name)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True,
                                                name="model-dl")
                self._worker.start()
        self._notify()
        return True, "已排入下載"

    def cancel(self, name: str) -> tuple[bool, str]:
        with self._lock:
            d = self._downloads.get(name)
        if not d or d.state not in ("queued", "downloading", "extracting"):
            return False, "沒有正在進行的下載"
        d.cancel()
        return True, "已要求取消"

    def retry(self, name: str) -> tuple[bool, str]:
        with self._lock:
            if name in self._downloads:
                del self._downloads[name]
        return self.start(name)

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # -- 背景工作 --
    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                name = self._queue.pop(0)
                d = self._downloads.get(name)
            if d is None:
                continue
            try:
                self._fetch(name, d)
            except _Cancelled:
                d.state = "cancelled"
                d.error = None
                (DOWNLOADS_DIR / f"{name}.tar.bz2").unlink(missing_ok=True)
            except Exception as exc:                       # 下載不可讓行程崩潰
                d.state = "error"
                d.error = f"{type(exc).__name__}: {exc}"
                (DOWNLOADS_DIR / f"{name}.tar.bz2").unlink(missing_ok=True)
            self._notify()

    def _fetch(self, name: str, d: Download) -> None:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        archive = DOWNLOADS_DIR / f"{name}.tar.bz2"
        url = URL_TEMPLATE.format(name=name)

        # 1) 下載
        d.state = "downloading"
        self._notify()
        req = urllib.request.Request(url, headers={"User-Agent": "VibeTalkie/0.1"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=60) as resp, archive.open("wb") as fh:
            try:
                d.total = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                d.total = 0
            while True:
                if d.cancelled:
                    raise _Cancelled()
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                d.downloaded += len(chunk)
                elapsed = max(time.time() - t0, 1e-6)
                d.speed_bps = d.downloaded / elapsed

        # 2) 解壓（tar.bz2 解壓時沒什麼可回報的，直接標記狀態）
        if d.cancelled:
            raise _Cancelled()
        d.state = "extracting"
        self._notify()
        target = model_dir(name)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        with tarfile.open(archive, "r:bz2") as tf:
            # filter="data" 擋掉絕對路徑／跳脫，3.12+ 支援
            try:
                tf.extractall(MODELS_DIR, filter="data")
            except TypeError:
                tf.extractall(MODELS_DIR)          # 舊版 Python 沒有 filter

        archive.unlink(missing_ok=True)

        if not is_ready(name):
            raise RuntimeError(f"解壓後仍找不到模型檔：{target}")
        d.state = "ready"
        d.downloaded = d.total or d.downloaded


class _Cancelled(Exception):
    pass
