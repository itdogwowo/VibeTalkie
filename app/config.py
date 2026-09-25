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
            "output": ("mode", "traditional", "restore_clipboard"),
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
            "engine": self.engine,
            "language": self.language,
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
