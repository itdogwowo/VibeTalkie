#!/usr/bin/env python3
"""自動更新的模型清單（從 sherpa-onnx 官方 release 取得）。

## 資料來源

    https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/asr-models
    https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/{id}/assets?per_page=100&page=N
    下載：https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/<name>.tar.bz2

k2-fsa/sherpa-onnx 是 sherpa-onnx 的官方上游。這個 release 目前約 499 個檔案。

## 為什麼一定要分類，不能直接列出來

實際統計（455 個壓縮檔）：

    166  平台特化（RK3566/68/76、RK3562/88、Ascend NPU、Qualcomm QNN）
         → 板上 NPU 格式，一般 PC 載不起來
    123  串流辨識模型 → OfflineRecognizer 不支援（我們是「按完再辨識」）
     若干 VAD / TTS / 關鍵詞 / 語者 / 測試音檔 → 根本不是語音辨識
    ~198 離線 ASR，但分散在 20 個不同家族

直接列 499 筆會讓使用者無從選擇，而且會下載到根本不能用的東西。

## 關於「支援」的誠實原則

`sherpaonnx.OfflineRecognizer` 有 20 種 from_* 載入方式，每種要的檔案組合不同。
**只有我們真的實作且驗證過的家族才會標成可下載**，其餘明確標「尚未支援」並說明原因。
不要為了讓清單看起來豐富而假裝什麼都能跑 —— 那會讓使用者下載 200 MB 後才發現載不起來。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
CACHE_PATH = MODELS_DIR / ".catalog.json"

API = "https://api.github.com/repos/k2-fsa/sherpa-onnx"
RELEASE_TAG = "asr-models"
URL_TEMPLATE = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                f"{RELEASE_TAG}/{{name}}")
CACHE_TTL_HOURS = 24

# 平台特化：板上 NPU 格式，PC 不能用
PLATFORM_PAT = re.compile(
    r"(?:^|-)("
    r"rk35\d\d|rk356\d|ascend|qnn|axera|horizon|spacemit"
    r")(?:-|$)")

# 不是語音辨識
NON_ASR_PAT = re.compile(
    r"(vad|tts|kws|keyword|speaker|diarization|punctuation|audio-tagging|"
    r"speech-enhancement|source-separation|test-wavs|lib(rknn|nn)|"
    r"silero|streaming-zipformer-vad)")

# 串流模型（要用 OnlineRecognizer，跟我們的按鍵模式不合）
STREAMING_PAT = re.compile(r"streaming")

# 家族 → (kind, 中文名, 是否需要多檔案)
# kind 對應 sherpa_onnx.OfflineRecognizer 的 factory
#
# ⚠️ 順序即優先權：**specific 的必須排在 general 前面**。
#    實際踩過：`sherpa-onnx-sense-voice-funasr-nano-int8` 同時含 "sense-voice"
#    與 "funasr-nano"，因為 sense_voice 排在前面而被判成可用 ——
#    但它是 FunASR Nano 家族，載入方式不同，使用者會下載 187 MB 才發現載不起來。
FAMILIES: list[tuple[re.Pattern, str, str, bool]] = [
    # --- 先比對複合名稱（含多個家族關鍵字的）---
    (re.compile(r"funasr.*nano|nano.*funasr"), "funasr_nano",     "FunASR Nano",  False),
    (re.compile(r"nemo.*canary|canary"),       "nemo_canary",     "NeMo Canary",  True),
    (re.compile(r"zipformer.*ctc|ctc.*zipformer"), "zipformer_ctc", "Zipformer CTC", False),
    (re.compile(r"fire-?red.*ctc"),            "fire_red_asr_ctc", "FireRedASR CTC", False),
    # --- 再比對單一家族 ---
    (re.compile(r"sense-?voice"),              "sense_voice",       "SenseVoice",   False),
    (re.compile(r"paraformer"),                "paraformer",        "Paraformer",   False),
    (re.compile(r"whisper"),                   "whisper",           "Whisper",      True),
    (re.compile(r"moonshine"),                 "moonshine",         "Moonshine",    True),
    (re.compile(r"dolphin"),                   "dolphin_ctc",       "Dolphin",      False),
    (re.compile(r"telespeech"),                "telespeech_ctc",    "TeleSpeech",   False),
    (re.compile(r"medasr"),                    "medasr_ctc",        "MedASR",       False),
    (re.compile(r"omnilingual"),               "omnilingual_asr_ctc", "Omnilingual", False),
    (re.compile(r"nemo|parakeet"),             "nemo_ctc",          "NeMo CTC",     False),
    (re.compile(r"(wenetspeech|wenet)"),       "wenet_ctc",         "WeNet",        False),
    (re.compile(r"fire-?red"),                 "fire_red_asr",      "FireRedASR",   True),
    (re.compile(r"qwen3"),                     "qwen3_asr",         "Qwen3 ASR",    True),
    (re.compile(r"tdnn"),                      "tdnn_ctc",          "TDNN",         False),
    (re.compile(r"zipformer|transducer|conformer"), "transducer",  "Transducer",   True),
]

# **只有這些是我們實作且驗證過的**。其餘一律標「尚未支援」。
SUPPORTED_FAMILIES = {
    "sense_voice", "paraformer",
}

# 語言線索（只做粗略標示，不假裝精確）
LANG_HINTS = [
    (re.compile(r"-zh-|-zh\.|chinese|wenetspeech|paraformer-zh|telespeech-.*zh"),
     "中文"),
    (re.compile(r"-en-|-en\.|english|librispeech|tedlium"), "英文"),
    (re.compile(r"-ja-|-ja\.|japanese|reazonspeech"), "日文"),
    (re.compile(r"-ko-|-ko\.|korean|zeroth"), "韓文"),
    (re.compile(r"-yue-|cantonese"), "粵語"),
    (re.compile(r"-ru-|russian"), "俄文"),
    (re.compile(r"-vi-|vietnamese"), "越南文"),
    (re.compile(r"-fr-|french"), "法文"),
    (re.compile(r"-de-|german"), "德文"),
    (re.compile(r"-es-|spanish"), "西班牙文"),
    (re.compile(r"multi-?lang|omnilingual"), "多語言"),
]


def _guess_langs(name: str) -> str:
    low = name.lower()
    hits = [label for pat, label in LANG_HINTS if pat.search(low)]
    # 去重並保序
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return "・".join(out) if out else ""


def classify(name: str, size: int) -> dict:
    """判斷一個資產能不能用、屬於哪個家族。"""
    low = name.lower()

    if not name.endswith((".tar.bz2", ".zip")):
        return {"family": None, "supported": False,
                "reason": "不是模型壓縮檔", "langs": ""}

    if PLATFORM_PAT.search(low):
        m = PLATFORM_PAT.search(low)
        return {"family": None, "supported": False,
                "reason": f"給開發板／NPU（{m.group(1)}）的格式，一般電腦載不起來",
                "langs": ""}

    if NON_ASR_PAT.search(low):
        return {"family": None, "supported": False,
                "reason": "不是語音辨識模型（VAD／TTS／關鍵詞／語者等）", "langs": ""}

    if STREAMING_PAT.search(low):
        return {"family": None, "supported": False,
                "reason": "串流模型；本工具是「放開後辨識」，不吃串流模型", "langs": ""}

    family = None
    for pat, kind, _label, _multi in FAMILIES:
        if pat.search(low):
            family = kind
            break

    langs = _guess_langs(name)
    size_mb = round(size / 1e6, 1)

    if family is None:
        return {"family": None, "supported": False,
                "reason": "無法判斷模型種類", "langs": langs, "size_mb": size_mb}

    if family not in SUPPORTED_FAMILIES:
        return {"family": family, "supported": False,
                "reason": f"{family} 家族的載入方式尚未實作與驗證",
                "langs": langs, "size_mb": size_mb}

    return {"family": family, "supported": True, "reason": "",
            "langs": langs, "size_mb": size_mb}


# ---------------------------------------------------------------- 抓取 + 快取

def _api(url: str) -> dict | list:
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "VibeTalkie/0.1"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def fetch_remote() -> list[dict]:
    """從 GitHub 抓完整資產清單（分頁）。約 6 次請求。"""
    rel = _api(f"{API}/releases/tags/{RELEASE_TAG}")
    if not isinstance(rel, dict) or "id" not in rel:
        raise RuntimeError(f"GitHub 回應不如預期：{str(rel)[:200]}")
    out: list[dict] = []
    page = 1
    while True:
        batch = _api(f"{API}/releases/{rel['id']}/assets"
                     f"?per_page=100&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 100:
            break
        page += 1
        if page > 40:
            break
    return [{"name": a["name"], "size": a["size"],
             "url": a.get("browser_download_url") or URL_TEMPLATE.format(name=a["name"])}
            for a in out]


def load_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def cache_age_hours() -> float | None:
    data = load_cache()
    if not data or "fetched_at" not in data:
        return None
    return (time.time() - data["fetched_at"]) / 3600.0


def build_index(force: bool = False) -> dict:
    """取得索引。優先順序：新鮮快取 → GitHub → 過期快取 → 內建目錄。

    離線或 GitHub 掛掉時**仍然要能顯示清單**，所以每一層都有退路。
    """
    data = load_cache()
    age = cache_age_hours()
    src = "cache"

    if not force and data and age is not None and age < CACHE_TTL_HOURS:
        return {"assets": data["assets"], "source": "cache", "age_hours": age}

    try:
        assets = fetch_remote()
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(
            {"fetched_at": time.time(), "assets": assets},
            ensure_ascii=False), encoding="utf-8")
        return {"assets": assets, "source": "github", "age_hours": 0.0}
    except Exception as exc:
        if data:
            return {"assets": data["assets"], "source": "stale-cache",
                    "age_hours": age, "error": f"{type(exc).__name__}: {exc}"}
        return {"assets": builtin_assets(), "source": "builtin", "age_hours": None,
                "error": f"{type(exc).__name__}: {exc}"}


def builtin_assets() -> list[dict]:
    """最後退路：離線也能顯示一份最小清單（就是我們驗證過的那幾個）。"""
    from models import CATALOG
    return [{"name": f"{m.name}.tar.bz2", "size": int(m.size_mb * 1e6),
             "url": URL_TEMPLATE.format(name=f"{m.name}.tar.bz2")}
            for m in CATALOG]


def catalog(force: bool = False, only_supported: bool = False) -> dict:
    """給 UI 用的完整清單（含分類）。"""
    idx = build_index(force)
    models = []
    for a in idx["assets"]:
        base = a["name"]
        for suffix in (".tar.bz2", ".zip"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        c = classify(a["name"], a["size"])
        models.append({
            "name": base,
            "asset": a["name"],
            "size_mb": round(a["size"] / 1e6, 1),
            "family": c["family"],
            "supported": c["supported"],
            "reason": c["reason"],
            "langs": c["langs"],
            "url": a["url"],
        })
    models.sort(key=lambda m: (not m["supported"], m["size_mb"]))
    if only_supported:
        models = [m for m in models if m["supported"]]
    return {
        "models": models,
        "source": idx["source"],
        "age_hours": idx.get("age_hours"),
        "error": idx.get("error"),
        "total": len(idx["assets"]),
        "supported": sum(1 for m in models if m["supported"]),
    }
