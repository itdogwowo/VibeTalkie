#!/usr/bin/env python3
"""常駐程式的觸發行為測試：hold / toggle / double ＋設定熱重載。

## 為什麼要測這個

`trigger_mode` 與 `hotkey` 是使用者**每天都會碰**的兩個設定，而它們的
錯誤症狀全部是「按了沒反應」或「行為跟我想的不一樣」——沒有例外、沒有訊息：

  · hold    按住開始、放開停止 → 放開後若沒停，會一直錄到 max_s
  · toggle  按一下開始、再按一下停 → 若把「開始」也當成「停」，就永遠錄不了
  · double  連兩下才動作 → 單擊**不可以**有任何反應

還有一條更隱蔽的：**改 config.toml 之後要不要重啟？**
`architecture.md` §10 的驗收標準寫「設定重開後仍生效」，但更基本的是
「改了就生效」——不然使用者會以為設定壞了。

## 怎麼測

不碰真的麥克風與真的按鍵：把 `recorder.MacCapture` 換成假的，
然後直接餵 `KeyEvent` 給 `_handle_key()`。這樣可以精確控制時序，
包括 double 模式的時間窗。

需要 pyobjc（因為要載入 mac_vibetalkie，它會 import 三個 mac 模組）。

執行：PYTHONPATH=<pyobjc 目錄> python3 tests/test_mac_trigger.py
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    print("=" * 74)
    print("常駐程式觸發行為測試（hold / toggle / double ＋熱重載）")
    print("=" * 74)

    try:
        import Quartz  # noqa: F401
    except ImportError as exc:
        print(f"\n  ❌ 需要 pyobjc：{exc}")
        return 2

    spec = importlib.util.spec_from_file_location(
        "mvd", ROOT / "app" / "mac_vibetalkie.py")
    mvd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mvd)                                  # type: ignore

    import config as config_module
    import mac_keylistener as keylistener
    import mac_recorder as recorder

    # ---- 假麥克風：記錄被呼叫幾次，不碰硬體 ----
    class FakeCapture:
        opened = 0
        closed = 0

        def __init__(self, *a, **kw):
            self.error = None
            self._entered = False

        def __enter__(self):
            FakeCapture.opened += 1
            self._entered = True
            return self

        def __exit__(self, *exc):
            FakeCapture.closed += 1
            self._entered = False

        def recorded(self) -> bytes:
            # 回傳「夠長但不是真的語音」的資料；這些測試只看狀態機，
            # 不看辨識結果（辨識由 test_mac_e2e.py 驗證）。
            return b"\x00\x00" * 16000 * 3

        def full(self) -> bool:
            return False

        @property
        def done(self) -> bool:
            return not self._entered

        def poll_level(self) -> float:
            return 0.0

    recorder.MacCapture = FakeCapture                     # type: ignore[misc]

    def new_daemon(hotkey: str = "F9", mode: str = "hold",
                   double_ms: int = 400):
        cfg = config_module.Config()
        cfg.hotkey = hotkey
        cfg.trigger_mode = mode
        cfg.double_tap_ms = double_ms
        d = mvd.MacPttDaemon(cfg, dry_run=True, debug=False)
        d._mods_down = set()                              # noqa: SLF001
        # 跳過真的引擎：這一組測試只驗狀態機
        d.engine = _StubEngine()
        return d

    class _StubEngine:
        def transcribe(self, samples, rate, language=None):
            class R:
                text = ""
            return R()

    def send(d, key: str, down: bool):
        d._handle_key(keylistener.KeyEvent(key=key, down=down))   # noqa: SLF001

    # ------------------------------------------------ hold
    print("\n▍hold：按住開始、放開停止")
    FakeCapture.opened = FakeCapture.closed = 0
    d = new_daemon(mode="hold")
    send(d, "f9", True)
    check("按下 → RECORDING", d.state == "RECORDING", d.state)
    check("麥克風被開啟一次", FakeCapture.opened == 1, str(FakeCapture.opened))
    check("_pressed 標記為 True", d._pressed is True)          # noqa: SLF001

    # 按住時的重複事件不可以重新開始錄音
    send(d, "f9", True)
    check("按住的重複事件不重啟錄音", FakeCapture.opened == 1,
          f"opened={FakeCapture.opened}")

    send(d, "f9", False)
    check("放開 → 離開 RECORDING", d.state != "RECORDING", d.state)
    check("_pressed 重設", d._pressed is False)               # noqa: SLF001

    # ------------------------------------------------ toggle
    print("\n▍toggle：按一下開始、再按一下停")
    FakeCapture.opened = FakeCapture.closed = 0
    d = new_daemon(mode="toggle")
    send(d, "f9", True)
    check("第一下 → RECORDING", d.state == "RECORDING", d.state)
    send(d, "f9", False)                       # 放開不該停止（toggle 的關鍵）
    check("放開**不**停止（否則永遠錄不了）", d.state == "RECORDING", d.state)
    send(d, "f9", True)
    check("第二下 → 停止", d.state != "RECORDING", d.state)

    # ------------------------------------------------ double
    print("\n▍double：連兩下才動作（單擊必須毫無反應）")
    FakeCapture.opened = FakeCapture.closed = 0
    d = new_daemon(mode="double", double_ms=400)
    send(d, "f9", True)
    send(d, "f9", False)
    check("單擊 → **不**開始錄音（AGENTS 的關鍵要求）",
          FakeCapture.opened == 0 and d.state == "IDLE",
          f"opened={FakeCapture.opened} state={d.state}")

    send(d, "f9", True)                        # 第二下（在時間窗內）
    send(d, "f9", False)
    check("雙擊 → 開始錄音", d.state == "RECORDING", d.state)

    # 再雙擊一次 → 停止
    send(d, "f9", True)
    send(d, "f9", False)
    send(d, "f9", True)
    send(d, "f9", False)
    check("再雙擊 → 停止", d.state != "RECORDING", d.state)

    # 超過時間窗的兩下不算雙擊
    FakeCapture.opened = 0
    d2 = new_daemon(mode="double", double_ms=50)
    send(d2, "f9", True)
    send(d2, "f9", False)
    time.sleep(0.12)                           # 超過 50 ms
    send(d2, "f9", True)
    send(d2, "f9", False)
    check("兩下間隔超過時間窗 → 不算雙擊（不啟動）",
          d2.state == "IDLE" and FakeCapture.opened == 0,
          f"opened={FakeCapture.opened} state={d2.state}")

    # ------------------------------------------------ 熱重載
    print("\n▍設定熱重載：改 hotkey 之後要不要重啟？")
    d = new_daemon(hotkey="F9", mode="hold")
    check("F9 設定：f9 觸發", d._spec_matches(                     # noqa: SLF001
        keylistener.KeyEvent(key="f9", down=True)))
    check("F9 設定：f8 不觸發", not d._spec_matches(               # noqa: SLF001
        keylistener.KeyEvent(key="f8", down=True)))

    # 模擬使用者在 UI / 編輯器改了設定
    d.cfg.hotkey = "Ctrl+Alt+R"
    check("改成 Ctrl+Alt+R：f9 不再觸發", not d._spec_matches(      # noqa: SLF001
        keylistener.KeyEvent(key="f9", down=True)))
    d._mods_down = {"ctrl", "alt"}                                 # noqa: SLF001
    check("改成 Ctrl+Alt+R：ctrl+alt+r 觸發", d._spec_matches(      # noqa: SLF001
        keylistener.KeyEvent(key="r", down=True)),
        "改設定後不需重啟 —— 若這裡失敗，代表設定被快取住了")

    # 壞掉的設定值要退回預設，而且要能說出原因（不能靜默）
    print("\n▍壞掉的設定值")
    d = new_daemon(hotkey="這不是按鍵", mode="hold")
    sp = d.spec()
    check("無法解析時退回預設（RightCtrl）", sp is not None and sp.raw != "這不是按鍵",
          f"raw={sp.raw!r}")
    check("退回後仍然可運作（rightctrl 觸發）", d._spec_matches(   # noqa: SLF001
        keylistener.KeyEvent(key="rightctrl", down=True)))

    # 空字串也要安全
    d = new_daemon(hotkey="", mode="hold")
    check("空字串 → 退回預設而不是崩潰", d.spec() is not None)

    # ------------------------------------------------ 誤觸保護
    print("\n▍誤觸保護（太短不辨識）")
    # ⚠️ 這裡的門檻要設得比假麥克風的長度**大**才測得到。
    #    第一版設 0.2 秒，但 FakeCapture 固定回傳 3 秒音，
    #    所以永遠走不到「太短」那條分支，測試變成假通過。
    d = new_daemon(mode="hold")
    d.min_s = 5.0                              # > 假麥克風的 3 秒
    before = dict(d.stats)
    send(d, "f9", True)
    send(d, "f9", False)
    check("音訊太短 → 不辨識、不注入",
          d.stats["inserted"] == before["inserted"]
          and d.stats["too_short"] > before.get("too_short", 0),
          str(d.stats))

    print("\n" + "=" * 74)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
