#!/usr/bin/env python3
"""麥克風串流三種模式的行為測試。

## 為什麼要測這個

`per_press` / `idle_timeout` / `session` 三種模式的差別**全在 `Capture` 的
開關時機**，而那正是最容易寫錯、又最難用眼睛看出來的地方：

  · 該關沒關 → 藍牙耳機在整個程式執行期間都沒聲音（使用者會以為壞了）
  · 該沿用卻重開 → 每按一次都重新跟無線電協商，中斷次數回到最糟的情況
  · 常駐時沒有記住「上次讀到哪」 → **第二句話會摻進第一句的音訊**（靜默的錯誤！）

最後一項特別危險：它不會報錯，只會辨識出奇怪的文字。

## 怎麼測

不碰真麥克風 —— 注入一個假的 `Capture`，只驗證：
  1. 什麼時候 `__enter__`（開串流）、什麼時候 `__exit__`（關串流）
  2. 常駐模式下有沒有正確地「只取新增的那一段」
  3. idle 逾時的判斷（只在 IDLE 且真的超過時間才關）

執行：python tests/test_mic_stream.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import ptt as ptt_mod  # noqa: E402
from config import MIC_STREAM_OPTIONS, MIC_STREAM_VALUES, Config  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


class FakeCapture:
    """假的擷取物件：記錄自己有沒有被開啟、被餵進多少資料。"""

    instances: list["FakeCapture"] = []

    def __init__(self, device_id, rate=16000, channels=1, bits=16, max_seconds=15.0):
        self.device_id = device_id
        self.rate = rate
        self.max_seconds = max_seconds
        self.opened = False
        self.closed = False
        self.data = bytearray()
        FakeCapture.instances.append(self)

    # -- 介面 --
    def __enter__(self):
        self.opened = True
        return self

    def __exit__(self, *exc):
        self.closed = True

    def recorded(self) -> bytes:
        return bytes(self.data)

    @property
    def done(self) -> bool:
        return False

    # -- 測試用 --
    def feed(self, n_bytes: int) -> None:
        """模擬多錄到 n 個 byte（內容是什麼不重要，只需要長度）。"""
        self.data.extend(bytes(n_bytes))


class Cfg:
    """假的設定物件（`_live()` 只需要這幾個屬性）。"""

    def __init__(self, mic_stream: str, idle_timeout_s: float = 7.0):
        self.mic_stream = mic_stream
        self.idle_timeout_s = idle_timeout_s
        self.traditional = True
        self.remove_trailing_period = True
        self.mode = "auto"


def make_daemon(mic_stream: str, idle_timeout_s: float = 7.0):
    cfg = Cfg(mic_stream, idle_timeout_s)
    d = ptt_mod.PttDaemon(
        engine=None, device_index=0, dry_run=True,
        cfg_provider=lambda: cfg, device_provider=lambda: 0,
    )
    d.cfg = cfg
    return d


def test_config_options() -> None:
    print("\n[1] 設定選項本身")
    check("有三個模式", len(MIC_STREAM_VALUES) == 3, str(MIC_STREAM_VALUES))
    check("預設是 per_press", Config().mic_stream == "per_press")
    check("每個選項都有 label 與 note（UI 要顯示代價）",
          all(o.get("label") and o.get("note") for o in MIC_STREAM_OPTIONS))
    pub = Config().public()
    check("public() 帶 mic_stream", "mic_stream" in pub)
    check("public() 帶選項說明（UI 不自己寫一份）",
          isinstance(pub.get("mic_stream_options"), list) and pub["mic_stream_options"])
    check("public() 帶 idle_timeout_s", "idle_timeout_s" in pub)


def test_per_press() -> None:
    print("\n[2] per_press：每次按下都開一個新的，放開就關")
    d = make_daemon("per_press")
    FakeCapture.instances.clear()

    d._start_recording("t")
    first = FakeCapture.instances[-1]
    check("按下時開了串流", first.opened and not first.closed)
    check("daemon 指向它", d._cap is first)

    d._cap = None                      # 模擬 _finish_recording 取走後的行為
    first.__exit__(None, None, None)

    d._start_recording("t")
    second = FakeCapture.instances[-1]
    check("第二次按下又開了一個新的（不沿用）", second is not first)
    check("總共開了 2 個串流", len(FakeCapture.instances) == 2)


def test_session_reuse_and_slicing() -> None:
    print("\n[3] session：只開一次，且每次只取新增的音訊（不能摻到上一句）")
    d = make_daemon("session")
    FakeCapture.instances.clear()

    d._start_recording("t")
    cap = FakeCapture.instances[-1]
    check("第一次按下開了串流", cap.opened)
    check("串流被記錄成常駐", d._cap is cap)

    # 第一句：餵 1000 bytes 後「放開」
    cap.feed(1000)
    pcm1 = cap.recorded()[d._cap_seen:]
    d._cap = cap
    d._cap_seen = len(cap.recorded())
    d._idle_at = time.monotonic()
    check("第一句拿到 1000 bytes", len(pcm1) == 1000, f"{len(pcm1)}")

    # 第二句：再餵 400 bytes，起點必須是 1000
    d._start_recording("t")
    check("第二次按下**沿用**同一個串流（沒有重開）",
          FakeCapture.instances[-1] is cap and len(FakeCapture.instances) == 1)
    check("_cap_seen 被重設到當下的長度", d._cap_seen == 1000, f"{d._cap_seen}")
    cap.feed(400)
    pcm2 = cap.recorded()[d._cap_seen:]
    check("第二句只拿到新增的 400 bytes（沒有摻到第一句）",
          len(pcm2) == 400, f"{len(pcm2)}")

    # 閒置關閉
    d._set_state("IDLE")
    d._idle_at = time.monotonic() - 100
    d._tick_idle(7.0)
    check("逾時後關掉串流", cap.closed)
    check("關掉後 daemon 不再握著它", d._cap is None)


def test_idle_timeout_does_not_close_too_early() -> None:
    print("\n[4] idle_timeout：不該在錄音中或時間未到就關")
    d = make_daemon("idle_timeout", idle_timeout_s=30.0)
    FakeCapture.instances.clear()
    d._start_recording("t")
    cap = FakeCapture.instances[-1]

    d._set_state("RECORDING")
    d._idle_at = time.monotonic() - 100        # 遠超過逾時
    d._tick_idle(30.0)
    check("錄音中就算逾時也不關（否則會切掉正在錄的音）", not cap.closed)

    d._set_state("IDLE")
    d._idle_at = time.monotonic()               # 剛放開
    d._tick_idle(30.0)
    check("剛放開、時間未到不關", not cap.closed)

    d._idle_at = time.monotonic() - 31
    d._tick_idle(30.0)
    check("逾時且 IDLE 才關", cap.closed)


def test_close_is_idempotent() -> None:
    print("\n[5] 收尾：重複關閉不可爆掉（finally 一定會呼叫）")
    d = make_daemon("session")
    FakeCapture.instances.clear()
    d._start_recording("t")
    d._close_capture()
    d._close_capture()
    d._close_capture()
    cap = FakeCapture.instances[-1]
    check("重複呼叫 _close_capture() 不丟例外", cap.closed)
    check("_cap_seen 被清乾淨", d._cap_seen == 0)


def test_config_file_reload() -> None:
    """手改 config.toml 要能生效 —— 否則會被 UI 的「儲存設定」覆蓋掉。

    實際踩到：使用者在 UI 改成 session，卻聽到跟 idle_timeout 一樣的結果。
    追下去才發現他改的是**檔案**，而 app 只在啟動時讀一次設定。
    更糟的是之後按「儲存設定」時，UI 把表單上的舊值寫回檔案，
    於是他手改的設定就消失了 —— 使用者看到的是「我的設定被吃掉」。
    """
    print("\n[6] 手改 config.toml 要即時生效（不可被儲存設定覆蓋）")
    from config import Config

    # ⚠️ 用專案內的 artifacts/，不要用系統 temp —— 沙箱可能不允許寫那裡
    tmpdir = ROOT / "artifacts"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / "pytest-mic-stream-config.toml"
    cfg = Config()
    cfg.mic_stream = "idle_timeout"
    cfg.save(tmp)

    d = ptt_mod.PttDaemon(
        engine=None, device_index=0, dry_run=True,
        cfg_provider=lambda: cfg, device_provider=lambda: 0,
        config_path=tmp, config_loader=Config.load,
    )

    d._sync_config()
    check("第一次同步後讀到原值", cfg.mic_stream == "idle_timeout", cfg.mic_stream)

    # 使用者手改檔案（模擬外部編輯器）
    cfg2 = Config.load(tmp)
    cfg2.mic_stream = "session"
    cfg2.idle_timeout_s = 33.0
    cfg2.save(tmp)
    # 讓 mtime 一定不同（某些檔案系統解析度是秒）
    import os
    st = tmp.stat()
    os.utime(tmp, (st.st_atime, st.st_mtime + 5))

    d._sync_config()
    check("手改後 mic_stream 被套用", cfg.mic_stream == "session", cfg.mic_stream)
    check("手改後 idle_timeout_s 被套用", cfg.idle_timeout_s == 33.0,
          str(cfg.idle_timeout_s))
    check("物件身分不變（UI 與 daemon 看同一個 cfg）",
          d.cfg_provider() is cfg)

    # 壞檔不可讓程式崩掉
    tmp.write_text("這不是 TOML [[[", encoding="utf-8")
    st = tmp.stat()
    os.utime(tmp, (st.st_atime, st.st_mtime + 10))
    try:
        d._sync_config()
        check("讀到壞檔不丟例外（使用者可能正存到一半）", True)
    except Exception as exc:
        check("讀到壞檔不丟例外（使用者可能正存到一半）", False,
              f"{type(exc).__name__}: {exc}")


def test_model_dir_syncs() -> None:
    """`model_dir` 也必須同步 —— 只換一半比不換更難查。

    實際踩到：使用者把 config.toml 改成粵語專門模型（Paraformer），
    模式與裝置都跟著換了，**但模型沒有** —— 執行中的 app 一直用啟動時
    載入的舊模型。他聽到的是舊模型的效果（尾音字會掉），
    於是判定「換模型沒用」。畫面上完全看不出「只生效一半」。

    這是第三次同類 bug（前兩次是 mic_stream 與 idle_timeout_s），
    所以改成也檢查「同步清單是否涵蓋所有該生效的欄位」。
    """
    print("\n[7] 換模型要真的換（model_dir / language）")
    import os
    import re as _re
    from dataclasses import fields

    from config import Config

    tmp = ROOT / "artifacts" / "pytest-model-sync.toml"
    cfg = Config()
    cfg.model_dir = "AAAA-old-model"
    cfg.save(tmp)

    d = ptt_mod.PttDaemon(
        engine=None, device_index=0, dry_run=True,
        cfg_provider=lambda: cfg, device_provider=lambda: 0,
        config_path=tmp, config_loader=Config.load,
    )
    d._sync_config()

    fresh = Config.load(tmp)
    fresh.model_dir = "BBBB-new-model"
    fresh.language = "yue"
    fresh.save(tmp)
    st = tmp.stat()
    os.utime(tmp, (st.st_atime, st.st_mtime + 5))

    d._sync_config()
    check("model_dir 被同步", cfg.model_dir == "BBBB-new-model", cfg.model_dir)
    check("language 被同步", cfg.language == "yue", cfg.language)

    # 用 dataclass 欄位表檢查：以後新增欄位忘了加進清單就會被抓到
    src = (ROOT / "app" / "core" / "ptt.py").read_text(encoding="utf-8")
    m = _re.search(r"for name in \((.*?)\):", src, _re.S)
    synced = set(_re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()
    # 這些欄位本來就不需要在執行期同步（見各自的註解）
    skip = {"extra", "hotkey", "restore_clipboard", "delete_audio_after",
            "port", "open_browser", "threads"}
    missing = {f.name for f in fields(Config)} - synced - skip
    check("同步清單涵蓋所有「改了就該生效」的設定欄位",
          not missing, f"漏掉：{sorted(missing)}" if missing else "無")


def main() -> int:
    print("=" * 70)
    print("麥克風串流模式測試")
    print("=" * 70)
    ptt_mod.Capture = FakeCapture          # 注入假擷取

    test_config_options()
    test_per_press()
    test_session_reuse_and_slicing()
    test_idle_timeout_does_not_close_too_early()
    test_close_is_idempotent()
    test_config_file_reload()
    test_model_dir_syncs()

    print("\n" + "=" * 70)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
