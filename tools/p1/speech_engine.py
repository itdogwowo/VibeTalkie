#!/usr/bin/env python3
"""可插拔的 ASR 引擎介面。

目前有兩個實作：

  SenseVoiceEngine  ✅ 可用（本地 sherpa-onnx + SenseVoice int8）
  ApiEngine         ⬜ **佔位**，尚未實作（雲端 API）

設計原則（依 `docs/plan.md` §10）：
  - 介面只做「音訊 → 文字」。**不做** LLM 整理、改寫、翻譯。
  - v1 預設全本地；雲端引擎只是留位置，不是功能。
  - 引擎不得自行做網路請求，除非使用者明確選了雲端引擎。

⚠️ 簡繁問題：SenseVoice 輸出**簡體中文**。
   台灣使用者要繁體，必須在 ASR 之後過一道 `s2t` 轉換。
   這件事由 `postprocess` 負責，**不要**讓每個引擎各做一次。
"""

from __future__ import annotations

import struct
import sys
import time
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# 本機的 pip 是壞的（見 tools/p1/fetch_wheels.py 的說明），套件放在 third_party/。
# 自己加進 sys.path，讓使用者不必手動設 PYTHONPATH。
sys.path.insert(0, str(ROOT / "third_party"))


class EngineNotConfigured(RuntimeError):
    """引擎存在但還沒設定好（例如 API 引擎沒有金鑰）。"""


class EngineUnavailable(RuntimeError):
    """引擎在這台機器上根本不能用（例如缺少模型檔或原生函式庫）。"""


@dataclass
class TranscriptResult:
    text: str
    engine: str
    duration_s: float
    latency_ms: float
    language: str | None = None
    raw_text: str | None = None          # 後處理前的原始輸出
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def rtf(self) -> float | None:
        return self.latency_ms / 1000.0 / self.duration_s if self.duration_s else None


# ---------------------------------------------------------------- 音訊

def pcm_to_samples(pcm: bytes) -> list[float]:
    """PCM16 little-endian bytes → float 樣本 [-1, 1]。

    錄音路徑拿到的是 bytes，檔案路徑拿到的是 float list。
    先前只有檔案路徑做轉換，導致**麥克風路徑直接餵 bytes 給 sherpa-onnx**：
        TypeError: accept_waveform(): incompatible function arguments
    兩條路徑都必須經過這裡，才不會再次分岔。
    """
    n = len(pcm) // 2
    if n == 0:
        return []
    s = struct.unpack(f"<{n}h", pcm[:n * 2])
    return [v / 32768.0 for v in s]


def read_wav(path: str | Path) -> tuple[list[float], int]:
    """讀 16-bit PCM WAV → (float 樣本 [-1,1], 取樣率)。立體聲會混成單聲道。"""
    with wave.open(str(path), "rb") as wf:
        rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"只支援 16-bit WAV，{path} 是 {width * 8}-bit")
    if ch == 2:
        n = len(raw) // 2
        s = struct.unpack(f"<{n}h", raw[:n * 2])
        s = [(s[i] + s[i + 1]) / 2 for i in range(0, n - 1, 2)]
        return [v / 32768.0 for v in s], rate
    return pcm_to_samples(raw), rate


# ---------------------------------------------------------------- 介面

class SpeechEngine(ABC):
    """所有 ASR 引擎的共通介面。"""

    name: str = "base"
    is_local: bool = True

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """回傳 (能不能用, 原因)。不能用的時候原因要能直接顯示給使用者。"""

    @abstractmethod
    def transcribe(self, samples: list[float], sample_rate: int,
                   language: str | None = None) -> TranscriptResult:
        """音訊 → 文字。實作不得做後處理（簡繁、熱詞），那些由呼叫端負責。"""

    # -- 便利方法：直接吃檔案 --
    def transcribe_file(self, path: str | Path, language: str | None = None) -> TranscriptResult:
        samples, rate = read_wav(path)
        return self.transcribe(samples, rate, language)

    def warmup(self) -> None:
        """可選：預先載入模型，避免第一次呼叫特別慢。"""


# ---------------------------------------------------------------- 本地引擎

class SenseVoiceEngine(SpeechEngine):
    """本地 sherpa-onnx 引擎。

    名字保留為 SenseVoiceEngine（既有程式與測試都引用它），但實際上
    支援多種模型種類，由 `kind` 決定用哪個 sherpa-onnx 工廠：

        sense_voice  有標點、多語言（預設）
        paraformer   純中文，速度快，**不產生標點**

    可以用 `kind="auto"` 讓它從模型目錄名自己判斷。
    """

    name = "sherpa-onnx"
    is_local = True

    DEFAULT_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"

    def __init__(self, model_dir: str | Path | None = None, threads: int = 2,
                 use_itn: bool = True, kind: str = "auto"):
        self.model_dir = Path(model_dir) if model_dir else ROOT / "models" / self.DEFAULT_DIR
        self.threads = threads
        self.use_itn = use_itn
        self.kind = kind
        self._rec = None
        self._load_ms = 0.0
        self._resolved_kind: str | None = None
        self._load_error: str | None = None

    # -- 模型種類 --
    def resolved_kind(self) -> str:
        if self._resolved_kind:
            return self._resolved_kind
        k = self.kind
        if k == "auto":
            n = self.model_dir.name.lower()
            if "paraformer" in n:
                k = "paraformer"
            elif "whisper" in n:
                k = "whisper"
            else:
                k = "sense_voice"
        self._resolved_kind = k
        return k

    def describe(self) -> str:
        return f"{self.model_dir.name}（{self.resolved_kind()}）"

    def is_available(self) -> tuple[bool, str]:
        if not self.model_dir.is_dir():
            return False, (f"找不到模型目錄 {self.model_dir}。"
                           f"可在設定介面下載，或用 "
                           f"python tools/p1/fetch_model.py --get {self.DEFAULT_DIR}")
        if not self._model_file():
            return False, f"{self.model_dir} 內沒有 model.onnx / model.int8.onnx"
        if not (self.model_dir / "tokens.txt").exists():
            return False, f"{self.model_dir} 內沒有 tokens.txt"
        if self.resolved_kind() == "whisper":
            return False, "Whisper 模型尚未支援（目前支援 sense_voice / paraformer）"
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False, ("載入不到 sherpa_onnx。本機請用 "
                           "python tools/p1/fetch_wheels.py sherpa-onnx 安裝。")
        return True, "ok"

    def _model_file(self) -> Path | None:
        for n in ("model.int8.onnx", "model.onnx"):
            p = self.model_dir / n
            if p.exists():
                return p
        return None

    def _build(self):
        import sherpa_onnx  # type: ignore
        common = dict(
            model=str(self._model_file()),
            tokens=str(self.model_dir / "tokens.txt"),
            num_threads=self.threads,
            debug=False,
        )
        if self.resolved_kind() == "paraformer":
            # Paraformer 沒有 use_itn / language，特徵是 80 維 fbank
            return sherpa_onnx.OfflineRecognizer.from_paraformer(
                **common, sample_rate=16000, feature_dim=80,
                decoding_method="greedy_search")
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            **common, use_itn=self.use_itn, language="auto")

    def _ensure_loaded(self) -> None:
        if self._rec is not None:
            return
        if self._load_error:                       # 之前載入失敗過，不要一直重試
            raise EngineUnavailable(self._load_error)
        ok, why = self.is_available()
        if not ok:
            raise EngineUnavailable(why)
        t0 = time.perf_counter()
        try:
            self._rec = self._build()
        except Exception as exc:
            self._load_error = f"載入模型失敗（{self.resolved_kind()}）：{exc}"
            raise EngineUnavailable(self._load_error) from exc
        self._load_ms = (time.perf_counter() - t0) * 1000

    # -- 切換模型 --
    def reload(self, model_dir: str | Path) -> None:
        """換成另一個模型並重新載入。

        呼叫端必須確認**目前沒有在錄音**（狀態為 IDLE）——
        推論中把 recognizer 換掉會讓正在進行的辨識拿到半個狀態。
        """
        self.model_dir = Path(model_dir)
        self._rec = None
        self._resolved_kind = None
        self._load_error = None
        self._ensure_loaded()          # 立刻載入，失敗會馬上拋錯而不是等到下一句話

    def warmup(self) -> None:
        self._ensure_loaded()

    def transcribe(self, samples: list[float], sample_rate: int,
                   language: str | None = None) -> TranscriptResult:
        self._ensure_loaded()
        assert self._rec is not None
        t0 = time.perf_counter()
        stream = self._rec.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(stream)
        latency = (time.perf_counter() - t0) * 1000
        return TranscriptResult(
            text=stream.result.text.strip(),
            engine=self.name,
            duration_s=len(samples) / sample_rate if sample_rate else 0.0,
            latency_ms=latency,
            language=language,
            meta={"model_load_ms": round(self._load_ms, 1),
                  "model_dir": self.model_dir.name},
        )


# ---------------------------------------------------------------- API 佔位

class ApiEngine(SpeechEngine):
    """雲端 ASR —— **佔位，尚未實作**。

    刻意保持「可用但會明確報錯」的狀態，而不是默默假裝成功。
    這樣介面與設定欄位可以先定下來，之後接哪一家都不用改呼叫端。

    要實作時需要補的東西（見 TODO）：
        1. 音訊格式轉換：多數雲端 API 要 16 kHz mono、PCM16 或指定容器（wav/webm/ogg）
        2. 認證：API key 從環境變數或設定檔讀，**絕對不要寫進版控**
        3. 請求與重試：逾時、429/5xx 退避、最大音長切段
        4. 隱私：使用者必須明確選用才會發出請求；UI 要標示「音訊會上傳」
        5. 串流/非串流：非串流實作簡單，先做非串流
    """

    name = "api"
    is_local = False

    # 未來要接的服務；現在只是列出來，沒有任何實作
    PROVIDERS = ("openai", "azure", "deepgram", "aliyun", "volcengine", "custom")

    def __init__(self, provider: str = "custom", endpoint: str | None = None,
                 api_key: str | None = None, model: str | None = None):
        self.provider = provider
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model

    def is_available(self) -> tuple[bool, str]:
        if not self.endpoint:
            return False, "API 引擎尚未設定 endpoint（此引擎目前是佔位實作）"
        if not self.api_key:
            return False, "API 引擎尚未設定 API key"
        return False, ("API 引擎尚未實作（endpoint 與 key 已設定，但還沒有請求邏輯）。"
                       "v1 請使用 --engine sherpa-onnx。")

    def transcribe(self, samples: list[float], sample_rate: int,
                   language: str | None = None) -> TranscriptResult:
        ok, why = self.is_available()
        raise EngineNotConfigured(
            f"ApiEngine 尚未實作：{why}\n"
            f"   provider={self.provider!r} endpoint={'已設定' if self.endpoint else '未設定'} "
            f"api_key={'已設定' if self.api_key else '未設定'}\n"
            f"   本機可用引擎：sherpa-onnx"
        )


# ---------------------------------------------------------------- 後處理

_S2T: Any = None
_S2T_TRIED = False


def to_traditional(text: str) -> str:
    """簡體 → 繁體。沒有 OpenCC 時原樣回傳（呼叫端可用 has_opencc() 提示）。"""
    global _S2T, _S2T_TRIED
    if not _S2T_TRIED:
        _S2T_TRIED = True
        try:
            from opencc import OpenCC  # type: ignore
            _S2T = OpenCC("s2t")
        except Exception:
            _S2T = None
    return _S2T.convert(text) if _S2T is not None else text


def has_opencc() -> bool:
    to_traditional("测试")          # 觸發一次載入
    return _S2T is not None


# 「句號」不只一種寫法 —— 實際會遇到的至少這四種：
#   。 U+3002 全形句號（中文最常用，模型輸出的就是這個）
#   ． U+FF0E 全形句點
#   .  U+002E 半形句點
#   ｡ U+FF61 半形日文句號
# 所以用字元集合處理，而不是比對單一字元。
TRAILING_PERIODS = "。．.｡"


def strip_trailing_period(text: str) -> str:
    """移除結尾的句號（含連續多個、多種寫法、句號後面夾空白）。

    使用者需求：講完一句話模型會在結尾補句號，輸入時還要自己刪很麻煩。
    只處理**結尾**，不動句中的標點。

    ⚠️ 順序很重要：必須「先清空白、再清句號」並**交替進行**。
    只做一次 `rstrip(句號).rstrip()` 的話，`"你好。 "` 會變成 `"你好。"` ——
    空白去掉了、句號卻還在（因為句號不在字串尾端）。
    """
    if not text:
        return text
    out = text
    while True:
        # 先清空白，再清句號；句號後面可能還有空白，所以要交替到穩定為止
        nxt = out.rstrip().rstrip(TRAILING_PERIODS)
        if nxt == out:
            return out
        out = nxt


# ---------------------------------------------------------------- 註冊表

ENGINES: dict[str, type[SpeechEngine]] = {
    "sherpa-onnx": SenseVoiceEngine,
    "api": ApiEngine,
}


def build_engine(name: str, **kwargs) -> SpeechEngine:
    """建立引擎。

    只把該引擎**簽章真的有**的選項傳進去。
    CLI 的 `--threads` 這種旗標是全引擎共用的，但只有本地引擎用得到；
    硬傳給 ApiEngine 會直接 TypeError（實際踩過）。
    """
    import inspect

    if name not in ENGINES:
        raise KeyError(f"未知引擎 {name!r}；可用：{', '.join(ENGINES)}")
    cls = ENGINES[name]
    params = inspect.signature(cls.__init__).parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return cls(**kwargs)
    accepted = {k: v for k, v in kwargs.items() if k in params}
    return cls(**accepted)


def engine_status() -> list[tuple[str, bool, str]]:
    out = []
    for key, cls in ENGINES.items():
        try:
            eng = cls()
        except Exception as exc:                       # 建構子就不行
            out.append((key, False, f"初始化失敗：{exc}"))
            continue
        ok, why = eng.is_available()
        out.append((key, ok, why))
    return out
