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

    # 閒置關閉（要用 idle_timeout 模式才會發生 —— session 模式不該被關）
    d2 = make_daemon("idle_timeout")
    FakeCapture.instances.clear()
    d2._start_recording("t")
    cap2 = FakeCapture.instances[-1]
    d2._set_state("IDLE")
    d2._idle_at = time.monotonic() - 100
    d2._tick_idle(7.0)
    check("idle_timeout 逾時後關掉串流", cap2.closed)
    check("關掉後 daemon 不再握著它", d2._cap is None)


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


def test_session_never_closed_by_idle() -> None:
    """⚠️ `session` 模式**不可以**被閒置計時器關掉。

    實際踩到的 bug：`_tick_idle()` 沒有檢查模式，只檢查「有串流、是 IDLE、
    過了多久」。於是 `session`（名叫「開著不關」）在放開 `idle_timeout_s`
    秒之後照樣被關 —— 因為 `_finish_recording()` 在常駐模式下**也會**
    設定 `_idle_at`。

    症狀很誤導：`session` 看起來「有時生效、有時不生效」，因為它只在
    「按下 → 放開 → 逾時內」這段時間才真的常駐。使用者早先測到的
    「零中斷」是真的（他當時連續操作），**但一停下來交談就破功**。
    """
    print("\n[9] session 模式不可被閒置計時器關掉（實際踩到的 bug）")
    d = make_daemon("session", idle_timeout_s=1.0)   # 逾時刻意設超短
    FakeCapture.instances.clear()

    d._start_recording("t")
    cap = FakeCapture.instances[-1]
    d._set_state("IDLE")
    # 模擬「放開後閒置很久」——_finish_recording 在常駐模式會設 _idle_at
    d._idle_at = time.monotonic() - 100

    d._tick_idle(1.0)
    check("session 模式下，閒置再久都不關串流", not cap.closed,
          "若這裡失敗，就是 _tick_idle 沒檢查模式")
    check("串流仍被握著", d._cap is cap)

    # 對照組：同一組狀態在 idle_timeout 模式就**應該**被關
    d2 = make_daemon("idle_timeout", idle_timeout_s=1.0)
    FakeCapture.instances.clear()
    d2._start_recording("t")
    cap2 = FakeCapture.instances[-1]
    d2._set_state("IDLE")
    d2._idle_at = time.monotonic() - 100
    d2._tick_idle(1.0)
    check("對照：idle_timeout 模式逾時要關（避免修過頭）", cap2.closed)


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


def test_early_return_keeps_stream() -> None:
    """⚠️ 常駐模式的關鍵陷阱：early return 不能讓串流「消失」。

    實際踩到的 bug（上線後才被使用者發現，極難查）：
    `_finish_recording()` 原本先把 `self._cap` 清成 None，而「保留串流」
    的還原寫在**後面**。於是只要走到後面的 early return
    ——「完全沒收到音訊」或「錄太短」——串流就再也沒被指定回來：

      · session 模式從此永久失效（`mic_open` 一直是 False）
      · `_idle_at` 也沒設定 → 連閒置逾時都不會去關它
      · 而畫面與 UI 完全正常，只有「耳機怎麼又開始斷」這個症狀

    觸發條件很普通：**一次沒收到音訊的按下就夠了。**

    這個測試用「餵 0 音訊」重現那條路徑。
    """
    print("\n[8] early return 不可讓常駐串流消失（實際踩到的 bug）")
    d = make_daemon("session")
    FakeCapture.instances.clear()

    # 第一次按下：正常錄到音訊
    d._start_recording("第 1 次")
    cap = FakeCapture.instances[-1]
    cap.feed(2000)
    d._finish_recording()
    check("正常錄音後串流仍在", d._cap is cap, f"_cap={d._cap!r}")

    # 第二次按下：**完全沒收到音訊**（藍牙 SCO 有時就是這樣）
    d._start_recording("第 2 次")
    same = FakeCapture.instances[-1]
    check("第二次沿用同一個串流", same is cap)
    # 不餵任何資料 → _cap_seen 之後是空的
    same.data.clear()
    d._finish_recording()
    check("⚠️ 沒收到音訊之後，串流**仍然保留**（這裡是 bug 的位置）",
          d._cap is not None, f"_cap={d._cap!r}")

    # 第三次按下：錄太短
    d._start_recording("第 3 次")
    three = FakeCapture.instances[-1]
    three.feed(320)                       # 0.01 秒 < min_s
    d._finish_recording()
    check("錄太短之後，串流仍然保留", d._cap is not None, f"_cap={d._cap!r}")

    # 第四次按下：要能正常運作（串流沒有壞掉）
    d._start_recording("第 4 次")
    four = FakeCapture.instances[-1]
    check("第四次要沿用同一個串流（沒有被重開）", four is cap,
          f"共開了 {len(FakeCapture.instances)} 個串流")
    check("全程只開過一個串流", len(FakeCapture.instances) == 1,
          f"{len(FakeCapture.instances)} 個")


def test_device_reclaim_on_wake() -> None:
    """藍牙麥克風省電休眠後回來時，要**自動**跟上（不必等下一次按下）。

    實際情境（使用者回報）：`AI_VOICE_MAX` 閒置太久會進省電休眠、從系統消失。
    裝置離線期間 `resolve_device()` 會退回設定檔的 `device_index`（別支麥克風），
    這是對的 —— 總比完全不能錄好。但先前的行為是**被動**的：
    裝置回來後要等使用者**下一次按下**才重算，使用者只覺得
    「它醒了，但程式還在用錯的麥克風」，而且沒有任何提示。
    """
    print("\n[10] 目標麥克風醒來要自動跟上")
    online = {"v": True}
    d = ptt_mod.PttDaemon(
        engine=None, device_index=0, dry_run=True,
        cfg_provider=lambda: Cfg("session"), device_provider=lambda: 0,
        target_online_probe=lambda: online["v"],
    )
    # 讓節流不擋住測試
    d._DEVICE_POLL_S = 0.0

    print("\n  （1）目標離線時：不可亂關串流")
    FakeCapture.instances.clear()
    d._start_recording("t")
    cap = FakeCapture.instances[-1]
    d._set_state("IDLE")
    online["v"] = False
    d._tick_device()
    check("離線時不關掉目前（退路的）串流", not cap.closed)
    check("有記下「目標離線」", d._target_seen_online is False)

    print("\n  （2）目標回來時：要放掉退路串流，下次用對的裝置")
    online["v"] = True
    d._tick_device()
    check("回來時放掉退路串流", cap.closed or d._cap is None,
          f"cap.closed={cap.closed} _cap={d._cap!r}")
    check("有記下「目標上線」", d._target_seen_online is True)

    print("\n  （3）錄音中就算目標回來，也不可切掉正在錄的音")
    FakeCapture.instances.clear()
    d2 = ptt_mod.PttDaemon(
        engine=None, device_index=0, dry_run=True,
        cfg_provider=lambda: Cfg("session"), device_provider=lambda: 0,
        target_online_probe=lambda: True,
    )
    d2._DEVICE_POLL_S = 0.0
    d2._target_seen_online = False        # 假裝目標之前是離線的
    d2._start_recording("t")              # 狀態變成 RECORDING
    cap2 = FakeCapture.instances[-1]
    d2._tick_device()
    check("錄音中不關串流", not cap2.closed, f"state={d2.state}")

    print("\n  （4）沒有注入探測函式時不可爆掉（純終端機模式）")
    d3 = make_daemon("session")
    try:
        d3._tick_device()
        check("沒探測函式時安全略過", True)
    except Exception as exc:
        check("沒探測函式時安全略過", False, f"{type(exc).__name__}: {exc}")


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
    test_early_return_keeps_stream()
    test_session_never_closed_by_idle()
    test_device_reclaim_on_wake()

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
