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

import hotkey   # 觸發方式的格式（`@` 分隔）只有一處定義，見 app/core/hotkey.py

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
# ⚠️ 2026-09 再修正：`per_press` 原本是「按下才開串流、放開就關」，
#   實測結果是**每次按下都要重新協商 SCO**，所以第一段幾乎一定
#   `音訊 0.00s`（使用者：「第一段錄音經常無法成功」）。
#   現在三種模式的差別只剩「串流開著多久」：
#     · 不按下時也開著（待命）→ 第一次按下立刻有音
#   `per_press` 與 `session` 在待命期間**看起來一樣**，差別是前者
#   放開後會把串流關掉（下次按下要重新協商，但至少暖機做過了）。
#   若實測發現還是不夠，就該把 `per_press` 的說明再改一次 —— 以實測為準。
MIC_STREAM_OPTIONS: list[dict] = [
    {
        "value": "per_press",
        "label": "每次按下才開（預設）",
        "note": "放開之後會把麥克風串流關掉，下次按下重新開啟。"
                "⚠️ 已修正：程式啟動時會先暖機，所以第一次按下不會再拿到空音訊。"
                "只有在「不想讓程式一直佔著麥克風」時才選這個 —— "
                "它的缺點是每段錄音之間藍牙耳機的播放會被斷一次。",
    },
    {
        "value": "session",
        "label": "開著不關（建議：零中斷）★",
        "note": "麥克風串流從啟動就一直開著，每段錄音之間都不會中斷，播放正常。"
                "實測（使用者聽感 + 端點監看各兩次）：零中斷、聲音全程都在；"
                "另外實測確認待命期間音訊持續進來（`tools/p1/measure_standby_audio.py`），"
                "所以第一段錄音一定錄得到。缺點是麥克風一直處於開啟狀態。",
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

# 錄音鍵的觸發方式。
#
# 為什麼要做成選項：裝置原生是「按住說話」（hold），但使用者要能完全脫離硬體 ——
# 用鍵盤時，`hold` 會跟「按住某個鍵做別的事」衝突，所以需要其他方式。
#
# ⚠️ 這裡是**全域預設值**，不是唯一開關。正式來源是 `hotkeys` 清單裡每一組
# 自己的模式（`F9@double`）。為什麼：實測回饋「無法錄製雙擊」——
# 真實情境是「藍牙麥克風按住說話 ＋ 鍵盤 F9 雙擊」，兩者必須並存。
TRIGGER_MODES: list[dict] = [
    {
        "value": "hold",
        "label": "按住說話（預設）",
        "note": "按住錄音、放開結束。裝置原生就是這種；用鍵盤時要挑一個"
                "單獨按住不會輸入字元的鍵（例如 RightCtrl、F9）。",
    },
    {
        "value": "toggle",
        "label": "按一下開始／再按一下停止",
        "note": "按一下開始錄音，**再按一下同一個鍵結束**。"
                "不必一直按著。適合講長句，或鍵盤上不好長時間按住的鍵。",
    },
    {
        "value": "double",
        "label": "雙擊（模仿 macOS）",
        "note": "快速連按兩下開始錄音，**再連按兩下結束** —— 開始與結束是"
                "同一個手勢，不必記兩種操作。與其他操作最不衝突（單擊不會觸發）。",
    },
]

TRIGGER_MODES_VALUES = tuple(o["value"] for o in TRIGGER_MODES)

# 給 UI 的常用按鍵（使用者也可以自己打字；實際解析由 hotkey.py 負責）。
# ⚠️ 這裡的每一項都必須能被 hotkey.parse() 解析 —— 有測試釘住。
HOTKEY_PRESETS: list[str] = [
    "RightCtrl", "LeftCtrl", "F8", "F9", "F10", "ScrollLock", "Pause",
    "Ctrl+Alt+R", "Ctrl+Shift+Space", "Alt+`",
]

# 「結束鍵」選單的候選。為什麼跟開始鍵分開一組：
# 結束鍵最常按的是「不會輸入字元、又順手」的鍵（Escape／Enter／F 系列），
# 而且它只需要**單一顆鍵**就夠用（組合鍵當結束鍵反而難按）。
HOTKEY_END_PRESETS: list[str] = [
    "Esc", "Enter", "Space", "F8", "F9", "F10", "ScrollLock", "Pause",
    "RightCtrl", "LeftCtrl",
]


@dataclass
class Config:
    # [device]
    device_index: int = 1
    mic_name: str = ""                  # 舊欄位；保留相容，正式來源是 mic_names
    # 麥克風**優先順序**（第一個是主、其餘是副）。
    # 為什麼要清單而不是單一裝置：藍牙麥克風會省電休眠、也會斷線。
    # 有順序清單就能「主的不在就用副的，主的一回來就自動換回」，
    # 使用者不必手動切換。空清單時退回舊的 mic_name。
    mic_names: list = field(default_factory=list)

    # [trigger] 錄音鍵（可完全自訂，不必綁任何硬體）
    #
    # ⚠️ 為什麼是**清單**：實測回饋 —— 只設一個鍵不夠用。
    # 藍牙麥克風的 RightCtrl 與鍵盤上的 F9 常常要並存（在家／外出、
    # 裝置休眠時），使用者不該為了換鍵進設定頁改來改去。
    hotkeys: list = field(default_factory=list)   # 正式來源，例如 ["RightCtrl","F9@double"]
    hotkey: str = "RightCtrl"           # 舊欄位；只在 hotkeys 為空時當後備
    # 觸發方式：hold＝按住說話、toggle＝按一下開始再按一下停、
    #           double＝雙擊（模仿 macOS 的聽寫快捷鍵）
    # ⚠️ **全域預設值**：清單裡某一組沒寫 `@模式` 時才用它。
    # 每一組自己的模式寫在 `hotkeys` 裡（`F9@double`），因為
    # 「麥克風按住說話 ＋ 鍵盤雙擊」這種組合是常態，全域一個值做不到。
    trigger_mode: str = "hold"
    double_tap_ms: int = 400            # double 模式的兩下間隔上限

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
            "device": ("device_index", "mic_name", "mic_names",
                       "hotkeys", "hotkey", "trigger_mode", "double_tap_ms"),
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
            "mic_names": list(self.mic_names or []),
            "mic_order": self.effective_mic_order(),
            "hotkeys": self.effective_hotkeys(),
            # 舊欄位：**只給開始鍵**（不含 `@模式` 與 `,結束鍵`）——
            # 它只認得單一顆鍵，塞整筆進去的話舊程式會解析失敗。
            "hotkey": self.effective_start_key(),
            "trigger_mode": self.trigger_mode,
            "double_tap_ms": self.double_tap_ms,
            "trigger_modes": TRIGGER_MODES,
            # 每一組的觸發方式（`{規格字串: 模式}`）—— UI 要畫「按住/切換/雙擊」
            # 這排 chips，不想自己揣測 `@模式` 的格式，所以由後端拆好給它。
            "key_modes": self.hotkey_modes(),
            # 每一組的**啟用狀態**（停用的帶 `~` 前綴）。
            # UI 要顯示「N 組生效中」與開關狀態，不想自己切 `~`。
            "key_enabled": self.hotkey_enabled(),
            "hotkey_presets": HOTKEY_PRESETS,
            "hotkey_end_presets": HOTKEY_END_PRESETS,
            "mode": self.mode,
            "traditional": self.traditional,
            "remove_trailing_period": self.remove_trailing_period,
            "engine": self.engine,
            "language": self.language,
            "mic_stream": self.mic_stream,
            "idle_timeout_s": self.idle_timeout_s,
            "mic_stream_options": MIC_STREAM_OPTIONS,
        }

    def effective_mic_order(self) -> list:
        """實際要用的麥克風優先順序。

        新舊欄位並存的關係：`mic_names` 是正式來源；若它是空的
        （舊設定檔、或使用者還沒動過），就退回單一的 `mic_name`。
        這樣舊設定不必遷移也不會壞掉。
        """
        names = [n for n in (self.mic_names or []) if n and n.strip()]
        if names:
            return names
        return [self.mic_name] if (self.mic_name or "").strip() else []

    def effective_hotkeys(self) -> list:
        """實際要用的錄音鍵清單。

        新舊欄位並存的關係（和麥克風同一套路）：`hotkeys` 是正式來源；
        空的就退回舊的單一字串 `hotkey`。舊設定檔因此不必遷移。

        每一筆可以帶觸發方式：`"F9@double"`（沒寫就是全域 `trigger_mode`）。
        """
        keys = [k for k in (self.hotkeys or []) if str(k or "").strip()]
        if keys:
            return keys
        return [self.hotkey] if (self.hotkey or "").strip() else []

    def effective_start_key(self) -> str:
        """第一組的**開始鍵**寫法（給舊欄位 `hotkey` 用）。

        ⚠️ 一定要去掉 `@模式` 與 `,結束鍵`：舊欄位只認得單一顆鍵，
        塞整筆 `"F9,Esc@double"` 進去，舊程式與文件都會看到看不懂的字串。
        """
        keys = self.effective_hotkeys()
        if not keys:
            return ""
        head = str(keys[0]).split(hotkey.BINDING_SEP)[0]   # 先切掉結束鍵
        return head.split(hotkey.MODE_SEP)[0].strip()      # 再切掉模式

    def hotkey_enabled(self) -> list:
        """每一組是否啟用（順序與 `effective_hotkeys()` 一致）。

        `~` 前綴＝停用（見 `app/core/hotkey.py` 的 `DISABLED_PREFIX`）。
        為什麼由後端算：那是 hotkey 模組的知識，UI 不該自己切字串。
        """
        return [not str(k).strip().startswith(hotkey.DISABLED_PREFIX)
                for k in self.effective_hotkeys()]

    def hotkey_modes(self) -> dict:
        """每一組的**兩邊行為**：`{"F9,Esc@toggle": {"start": "hold", "end": "toggle"}}`。

        ⚠️ 為什麼由後端拆好：`@行為`、`,` 配對的格式是
        `app/core/hotkey.py` 的知識，UI 不該自己切字串 ——
        兩邊各切一次就是漂移的開始（實際踩過好幾次）。
        UI 的兩個下拉各自要一個值，所以這裡一次給兩邊。
        """
        out = {}
        for entry in self.effective_hotkeys():
            text = str(entry).strip()
            if not text:
                continue
            try:
                _s, start, _e, end = hotkey.parse_binding(text, self.trigger_mode)
                out[text] = {"start": start, "end": end}
            except hotkey.HotkeyError:
                # 壞掉的字串由 daemon 回報（UI 會顯示 hotkey_warning），
                # 這裡先給一組安全的預設值，不要讓 UI 拿到 None。
                out[text] = {"start": self.trigger_mode, "end": ""}
        return out


def start_key_text(entry: str) -> str:
    """從一筆設定抽出「開始鍵」的寫法（去掉 `@模式` 與 `,結束鍵`）。

    只有舊欄位 `hotkey` 需要這個 —— 它只認得單一顆鍵。

    ⚠️ 順序：`F9,Esc@double` 要先切掉 `@模式` 再切 `,結束鍵`；
    反過來的話 `F9,Esc@double` 會先被 `,` 切掉尾巴，卻留著 `@double`
    （實測就是這個順序寫錯，舊欄位變成 `F9@double`）。

    ⚠️ 放在 `config.py` 而不是 `vibetalkie.py`：兩個平台都要用，
    而兩份的漂移症狀是「舊欄位存成 `F9@double`」—— 那正是上面那個 bug。
    """
    head = str(entry or "").split(hotkey.BINDING_SEP)[0]
    return head.split(hotkey.MODE_SEP)[0].strip()


def apply_hotkeys_patch(cfg, raw) -> str | None:
    """把 UI 送來的 `hotkeys` 清單套進設定。**回傳錯誤訊息，成功回 None。**

    為什麼放在這裡而不是各自的 HTTP handler：兩個平台（Windows 的
    `vibetalkie.py` 與 mac 的 `ui_server.py`）都要用同一套驗證 ——
    各寫一份就會漂移，而漂移的症狀是「同一筆設定在 Windows 存得進去、
    在 mac 被拒」，使用者完全無法理解。

    驗證規則：

      · 每一筆都要 `hotkey.parse_binding()` 過得去
        （開始鍵、結束鍵、**兩邊的行為**都驗）
      · 去重比**解析後**的規格（`F9` 與 `f9` 是同一組）
      · 空清單是**拒絕**，不是「等於沒設」—— 接受的話會變成
        「一個錄音鍵都沒有」→ 按什麼都不會錄音，而畫面看起來儲存成功了
    """
    if not isinstance(raw, list):
        return 'hotkeys 必須是陣列（例如 ["RightCtrl", "F9,Escape"]）'
    keys: list[str] = []
    seen: list = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            start, smode, end, emode = hotkey.parse_binding(
                text, getattr(cfg, "trigger_mode", hotkey.MODE_HOLD))
        except hotkey.HotkeyError as exc:
            return f"錄音鍵無法解析：{exc}"
        # ⚠️ 去重要比**整筆描述**（開始鍵＋開始行為＋結束鍵＋結束行為），
        #    不是只比開始鍵的規格。
        #
        #    實際踩到的 bug：先前只比 `spec`，於是底下這些**合法**的組合
        #    會被當成重複而**靜默丟掉**（使用者只看到「設了兩組，存完只剩
        #    一組」，而且完全沒有錯誤訊息）：
        #
        #        F9@double  ＋  F9,Esc@toggle     （同一顆開始鍵、不同收尾）
        #        F9         ＋  F9,Esc
        #        F9@double  ＋  F9@toggle
        #
        #    但「寫法不同、語意相同」的仍然要去重：`F9`／`f9`、
        #    `Ctrl+Alt+R`／`ctrl-alt-r`／`Alt+Ctrl+R` —— 那些解析後
        #    每一項都一樣，所以整筆描述相等（見 `HotkeySpec` 的
        #    `compare=False` on `raw`）。
        #
        # ⚠️ `enabled` 也要納入：`~F9` 與 `F9,Esc` **不是**重複，
        #    一個是停用、一個是啟用。少了它，`["~F9", "F9,Esc"]` 會被
        #    合併成兩組都停用 —— 使用者明明啟用了一組卻按什麼都沒反應，
        #    而畫面上每一列看起來都正常。
        #    這條規則與 `hotkey.bindings()` 裡的去重**必須一致**，
        #    不一致的症狀是「UI 存得進去，重啟後少一組」。
        sig = (start, smode or hotkey.MODE_HOLD,
               end, emode or hotkey.MODE_HOLD,
               not text.startswith(hotkey.DISABLED_PREFIX))
        if any(sig == s for s in seen):
            continue
        seen.append(sig)
        keys.append(text)
    if not keys:
        return "至少要有一個錄音鍵"
    cfg.hotkeys = keys
    # 兼容舊欄位：只留**開始鍵**（去掉 `@模式` 與 `,結束鍵`）
    cfg.hotkey = start_key_text(keys[0])
    return None


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        # ⚠️ 實測踩到：沒有這個分支時，清單會被 str() 成 `"[]"` ——
        # 寫進 TOML 變成**字串**而不是陣列，讀回來就不是 list 了。
        # `tomllib` 讀到 `mic_names = "[]"` 會給字串，於是
        # `[n for n in "[]"]` 之類的程式碼就會拿到一堆 `[`、`]` 字元。
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


EXAMPLE = """\
# VibeTalkie 設定範例（複製成 config.toml 即可使用）
# 程式第一次啟動會自動產生一份 config.toml，通常不需要手動複製。

[device]
device_index = 1
mic_name = ""
# 錄音鍵可以有很多個（按鍵來源完全自訂，不必綁硬體）。
# 想用鍵盤錄音就填一個不是裝置原生的鍵，例如 F9。
hotkeys = ["RightCtrl"]
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
