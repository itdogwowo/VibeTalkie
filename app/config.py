#!/usr/bin/env python3
"""VibeTalkie 設定檔（TOML）。

沿用 `docs/plan.md` §9 的 `config.toml` 形狀，但只保留**實際有作用**的欄位
（YAGNI：沒實作的選項不要寫進設定檔，否則使用者會以為它有效）。

讀取用標準函式庫 `tomllib`（3.11+）。寫入則自己寫一個最小的序列化器
—— `tomllib` 只能讀不能寫，而為了寫 TOML 再裝一個相依不值得。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.toml"
EXAMPLE_PATH = ROOT / "config.example.toml"

DEFAULT_MODEL_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"

# 麥克風串流開啟時機的三個選項。
#
# ⚠️ 這裡的描述**必須反映實測**，不要憑推理寫。
# 實際踩到的教訓（很重要，別重犯）：
#   我原本根據「端點狀態變成 UNPLUGGED」推論 `session` 模式會讓耳機沒聲音，
#   就把那句寫進選項說明。**使用者一測就推翻了** —— 他選 session 之後
#   零中斷、而且整段都聽得到聲音。端點狀態會騙人，**聽感才是事實**。
#
# 目前的事實（使用者實測，兩次一致）：
#   · per_press    每按一次開一次串流 → 每段錄音斷一次（耳機重連約 6–8 秒）
#   · session      串流從第一次按下就一直開著 → **零中斷，且聲音不受影響**
#   · idle_timeout 放開後等逾時才關 → 每段錄音仍會斷（除非句間停頓短於逾時）
MIC_STREAM_OPTIONS: list[dict] = [
    {
        "value": "per_press",
        "label": "每次按下才開（預設）",
        "note": "每次錄音都會讓藍牙耳機的播放斷一次（實測重連要 6–8 秒）。"
                "只有在「不想讓程式一直佔著麥克風」時才選這個。",
    },
    {
        "value": "session",
        "label": "開著不關（建議：零中斷）★",
        "note": "第一次按下之後麥克風串流就一直開著，之後每段錄音都不會再中斷，"
                "而且播放正常。實測（使用者聽感 + 端點監看各兩次）：零中斷、"
                "聲音全程都在。缺點是麥克風一直處於開啟狀態。",
    },
    {
        "value": "idle_timeout",
        "label": "放開後等幾秒才關",
        "note": "想折衷時用：連續講幾句不會被斷，停下來超過設定秒數後就關閉串流。"
                "注意這個值必須比你講話的停頓長，否則每段錄音還是會各斷一次。"
                "實測 2 秒太短（等於沒效果）。",
    },
]

MIC_STREAM_VALUES = tuple(o["value"] for o in MIC_STREAM_OPTIONS)

# idle_timeout_s 的合理範圍。
#
# ⚠️ 下限刻意訂在 5 秒（不是 1 秒）：實測踩到使用者把它設成 2 秒 ——
# 那比講話的句間停頓還短，於是**每次放開都會斷一次**，行為幾乎等同
# `per_press`，但畫面上看起來「我明明選了中間路線」。
# 這個值必須比「你講話時的停頓」長才有意義，而正常對話的停頓很少低於 5 秒。
IDLE_TIMEOUT_MIN = 5.0
IDLE_TIMEOUT_MAX = 120.0


@dataclass
class Config:
    # [device]
    device_index: int = 1
    mic_name: str = ""                  # 僅供顯示；藍牙重新配對後 index 可能變
    hotkey: str = "RightCtrl"           # 裝置原生送出的鍵，不可亂改（見 hardware.md §3.3）

    # [asr]
    engine: str = "sherpa-onnx"
    model_dir: str = DEFAULT_MODEL_DIR
    language: str = "zh"
    threads: int = 2

    # [output]
    mode: str = "auto"                  # auto | paste | type
    traditional: bool = True            # SenseVoice 輸出簡體 → 台灣要繁體
    restore_clipboard: bool = True
    remove_trailing_period: bool = True  # 模型會在結尾補句號，輸入時很礙事

    # [bluetooth] 麥克風串流的開啟時機
    #
    # 為什麼這是使用者選項而不是我們決定：藍牙的 A2DP（播放）與 SCO（麥克風）
    # 在同一顆無線電上互斥，**開麥克風就會把耳機踢掉**（實測，見
    # docs/hardware.md §2.2–2.5）。實測數據：
    #   · 每個 session 一次 vs 每次按下都斷 → 中斷次數差距很大
    #   · 但常開期間耳機全程沒聲音
    # 這是「播放」與「手感」的取捨，只有使用者知道自己比較在意哪個。
    mic_stream: str = "per_press"       # per_press | session | idle_timeout
    idle_timeout_s: float = 7.0         # 僅 idle_timeout 模式使用


    # [privacy]
    delete_audio_after: bool = True     # 音訊只在記憶體，不落地

    # [ui]
    port: int = 8756
    open_browser: bool = True

    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------ 載入
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        if not path.exists():
            cfg = cls()
            cfg.save(path)              # 第一次執行就產生一份可編輯的設定檔
            return cfg
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        values, extra = {}, {}
        for section, items in raw.items():
            if not isinstance(items, dict):
                extra[section] = items
                continue
            for key, val in items.items():
                if key in known:
                    values[key] = val
                else:
                    extra[f"{section}.{key}"] = val
        cfg = cls(**{k: v for k, v in values.items() if k in known})
        cfg.extra = extra
        return cfg

    # ------------------------------------------------------------ 儲存
    def save(self, path: Path | None = None) -> Path:
        path = path or CONFIG_PATH
        path.write_text(self.to_toml(), encoding="utf-8")
        return path

    def to_toml(self) -> str:
        d = asdict(self)
        d.pop("extra", None)
        sections = {
            "device": ("device_index", "mic_name", "hotkey"),
            "asr": ("engine", "model_dir", "language", "threads"),
            "output": ("mode", "traditional", "restore_clipboard",
                       "remove_trailing_period"),
            "bluetooth": ("mic_stream", "idle_timeout_s"),
            "privacy": ("delete_audio_after",),
            "ui": ("port", "open_browser"),
        }
        out = [
            "# VibeTalkie 設定檔",
            "# 這個檔案由程式自動產生；改完存檔後重新啟動 VibeTalkie 生效。",
            "",
        ]
        for sec, keys in sections.items():
            out.append(f"[{sec}]")
            for k in keys:
                out.append(f"{k} = {_toml_value(d[k])}")
            out.append("")
        return "\n".join(out)

    def public(self) -> dict:
        """給 UI 看的欄位（不含路徑等內部細節）。"""
        return {
            "device_index": self.device_index,
            "mic_name": self.mic_name,
            "mode": self.mode,
            "traditional": self.traditional,
            "remove_trailing_period": self.remove_trailing_period,
            "engine": self.engine,
            "language": self.language,
            "mic_stream": self.mic_stream,
            "idle_timeout_s": self.idle_timeout_s,
            "mic_stream_options": MIC_STREAM_OPTIONS,
        }


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


EXAMPLE = """\
# VibeTalkie 設定範例（複製成 config.toml 即可使用）
# 程式第一次啟動會自動產生一份 config.toml，通常不需要手動複製。

[device]
device_index = 1
mic_name = ""
hotkey = "RightCtrl"

[asr]
engine = "sherpa-onnx"
model_dir = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
language = "zh"
threads = 2

[output]
mode = "auto"
traditional = true
restore_clipboard = true
remove_trailing_period = true

[bluetooth]
# 麥克風串流何時開啟：per_press | session | idle_timeout
# 開麥克風會讓藍牙耳機的播放中斷（藍牙無線電的硬限制，實測見 docs/hardware.md §2.2）
#   per_press    每次按下才開、放開就關        → 每段錄音斷一次（耳機重連 6–8 秒）
#   session      第一次按下後就一直開著        → **零中斷，播放正常**（建議）
#   idle_timeout 放開後等 idle_timeout_s 秒才關 → 逾時要比講話停頓長才有效
mic_stream = "per_press"
idle_timeout_s = 7.0

[privacy]
delete_audio_after = true

[ui]
port = 8756
open_browser = true
"""


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = Config.load()
    print(f"設定檔：{CONFIG_PATH}")
    print(cfg.to_toml())
