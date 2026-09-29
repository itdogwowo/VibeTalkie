#!/usr/bin/env python3
"""觸發引擎（`app/core/trigger.py`）的契約測試 —— **不經過任何 daemon**。

## 為什麼要有一支「直接測引擎」的測試

`tests/test_trigger.py` 走的是 `PttDaemon`（Windows 路徑），
`tests/test_mac_trigger.py` 走 `MacPttDaemon`（mac 路徑）。兩支都在測
**同一個引擎**，但它們各自還夾帶了平台層的東西。

引擎自己的介面契約需要獨立釘住，理由有三個：

  1. **`Event` 的形狀是兩個平台的接縫。** mac 傳名稱（`"rightctrl"`）、
     Windows 傳名稱 + `E0` 旗標；兩種都要命中同一組規格。
     這條如果有錯，症狀是「Windows 正常、mac 按了沒反應」。
  2. **引擎不得依賴平台。** `tests/test_mac_import.py` 用 AST 掃 import，
     這裡則從**行為**面確認：只餵 `Event` 就能跑完整套觸發。
  3. **重構期間的守門員。** 把狀態機從 `ptt.py` 搬過來的時候，
     這一支綠了才代表搬對了（`test_trigger.py` 同時也是綠的才算）。

執行：python tests/test_trigger_engine.py
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

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

import hotkey  # noqa: E402
import trigger  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


class Rig:
    """把引擎接上兩個記錄用的回呼（引擎**只**需要這兩個）。"""

    def __init__(self, specs, double_window: float = 0.4, **kw):
        # ⚠️ 不能斷言 `err is None`：`bindings()` 對「全部停用」也會回報訊息
        #    （那是**該出現**的警告，見 [9]），而停用的組合正是要測的情境之一。
        self.bind, self.bind_error = hotkey.bindings(list(specs), default_mode="hold")
        self.started: list[str] = []
        self.finished: list[bool] = []
        self.eng = trigger.TriggerEngine(
            get_bindings=lambda: self.bind,
            on_start=lambda label: self.started.append(label),
            on_finish=lambda pressed: self.finished.append(pressed),
            double_window=double_window, **kw)

    # ---- 便利動作 ----
    def down(self, key: str, **kw) -> None:
        self.eng.feed(trigger.Event(key=key, down=True, **kw))

    def up(self, key: str, **kw) -> None:
        self.eng.feed(trigger.Event(key=key, down=False, **kw))

    @property
    def recording(self) -> bool:
        return self.eng.capturing


def main() -> int:
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:                                  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                              # noqa: BLE001
            pass

    print("=" * 68)
    print("觸發引擎契約（平台無關，直接餵 Event）")
    print("=" * 68)

    print("\n[1] Event 的兩種形狀都要命中（平台接縫）")
    r = Rig(["RightCtrl"])
    r.down("rightctrl")                       # mac：名稱自帶側別
    check("mac 形狀（key=rightctrl）→ 開始", r.recording is True)
    r.up("rightctrl")
    check("mac 形狀 → 放開就停", r.recording is False)

    r.down("ctrl", e0=True)                   # Windows：通用名稱 + E0
    check("Windows 形狀（ctrl + e0）→ 開始", r.recording is True)
    r.up("ctrl", e0=True)
    check("Windows 形狀 → 放開就停", r.recording is False)

    r.down("ctrl", e0=False)                  # 左 Ctrl 不該觸發
    check("左 Ctrl（e0=False）不觸發", r.recording is False)

    print("\n[2] 側別：三態（None＝兩側都算）")
    r2 = Rig(["Ctrl"])
    r2.down("ctrl", e0=True)
    check("沒寫側別 → 右邊也命中", r2.recording is True)
    r2.up("ctrl", e0=True)
    r2.down("ctrl", e0=False)
    check("沒寫側別 → 左邊也命中", r2.recording is True)
    r2.up("ctrl", e0=False)

    r3 = Rig(["LeftCtrl"])
    r3.down("ctrl", e0=True)
    check("限定左側 → 右邊不命中（實測踩過的 bug）", r3.recording is False)
    r3.down("ctrl", e0=False)
    check("限定左側 → 左邊命中", r3.recording is True)

    print("\n[3] 多組同時生效（不是取代）")
    r4 = Rig(["RightCtrl", "F9"])
    r4.down("rightctrl")
    check("第一組可用", r4.recording is True)
    r4.up("rightctrl")
    r4.down("f9")
    check("第二組也可用", r4.recording is True)
    r4.up("f9")
    r4.down("a")
    check("沒設定的鍵不觸發", r4.recording is False)
    check("開始回呼被呼叫兩次", len(r4.started) == 2, str(r4.started))

    print("\n[4] 組合鍵：修飾鍵要剛好符合（不多也不少）")
    r5 = Rig(["Ctrl+Alt+R"])
    r5.down("r")
    check("沒按修飾鍵 → 不觸發", r5.recording is False)
    r5.down("ctrl")
    r5.down("alt")
    r5.down("r")
    check("ctrl+alt 按住後按 r → 觸發", r5.recording is True)
    r5.up("r")
    check("放開 r → 停", r5.recording is False)
    r5.up("ctrl")
    r5.up("alt")
    check("修飾鍵狀態清乾淨", r5.eng.mods_down == set(), str(r5.eng.mods_down))

    r6 = Rig(["Ctrl+Alt+R"])
    r6.down("ctrl")
    r6.down("alt")
    r6.down("shift")                          # 多餘的修飾鍵
    r6.down("r")
    check("多按一顆修飾鍵 → 不觸發（避免搶到別的快捷鍵）", r6.recording is False)

    print("\n[5] 自動重複要被吃掉（double 模式的關鍵）")
    r7 = Rig(["F9@double"])
    for _ in range(6):
        r7.down("f9")
    check("連送 6 次 keydown（未放手）→ 不算連點", r7.recording is False)
    r7.up("f9")
    r7.down("f9")
    r7.up("f9")
    check("真正的第二下才觸發", r7.recording is True)

    r7b = Rig(["F9@double"])
    # ⚠️ 序列要精確：**只有 autorepeat 事件**不可以被算成「點一下」。
    #    中間若夾一次正常 keydown，那本來就是一下（會被記錄）。
    r7b.down("f9", autorepeat=True)
    r7b.down("f9", autorepeat=True)
    check("autorepeat=True 直接丟掉（不進配對）", r7b.recording is False)
    check("配對狀態完全沒被建立", not r7b.eng.tap_at, str(r7b.eng.tap_at))
    r7b.down("f9")                            # 真正的一下
    check("真正的一下才會進配對", bool(r7b.eng.tap_at), str(r7b.eng.tap_at))
    r7b.up("f9")
    r7b.down("f9", autorepeat=True)           # 重複事件不該算成第二下
    check("重複事件不算第二下（不會觸發）", r7b.recording is False)
    r7b.down("f9")                            # 真正的第二下
    check("真正的第二下才觸發", r7b.recording is True, str(r7b.started))
    print("\n[6] tick()：配對窗過期要清掉")
    r8 = Rig(["F9@double"], double_window=0.15)
    r8.down("f9")
    r8.up("f9")
    check("第一下之後有配對記錄", bool(r8.eng.tap_at), str(r8.eng.tap_at))
    time.sleep(0.2)
    r8.eng.tick()
    check("逾時後清掉", not any(r8.eng.tap_at.values()), str(r8.eng.tap_at))
    r8.down("f9")
    r8.up("f9")
    check("清掉後再按一下不會誤判成雙擊", r8.recording is False)

    print("\n[7] 兩顆 double 鍵的配對狀態各自獨立")
    r9 = Rig(["F9@double", "F8@double"])
    r9.down("f9")
    r9.up("f9")
    r9.down("f8")
    r9.up("f8")
    check("F9 一下 + F8 一下 ≠ 雙擊", r9.recording is False)

    print("\n[8] 開始／結束可以是不同顆鍵")
    r10 = Rig(["F9,Esc"])
    r10.down("f9")
    check("按 F9 → 開始", r10.recording is True)
    r10.up("f9")
    check("放開 F9 **不會**結束（結束由 Esc 負責）", r10.recording is True)
    r10.down("esc")
    check("Esc 按下時還沒停（hold＝鬆開才停）", r10.recording is True)
    r10.up("esc")
    check("鬆開 Esc → 停", r10.recording is False)
    check("明確結束鍵的收尾回報 pressed=False（不可自動重錄）",
          r10.finished[-1] is False, str(r10.finished))

    r11 = Rig(["F9,Esc@toggle"])
    r11.down("f9")
    r11.up("f9")
    r11.down("esc")
    check("結束行為 toggle → Esc 一按下就停", r11.recording is False)
    r11.up("esc")
    check("鬆開不會再停一次", len(r11.finished) == 1, str(r11.finished))

    print("\n[9] 停用的組合完全不理（含它的結束鍵）")
    r12 = Rig(["~F9,Esc", "F8"])
    r12.down("f8")
    check("F8 開始", r12.recording is True)
    r12.down("esc")
    r12.up("esc")
    check("停用那一組的 Esc 不會停掉 F8 的錄音", r12.recording is True)
    r12.up("f8")
    check("放開 F8 才停", r12.recording is False)

    r13 = Rig(["~F9", "~F8"])
    r13.down("f9")
    check("停用的 F9 不觸發", r13.recording is False)
    check("沒有任何一組生效中", len(r13.bind.active) == 0, str(len(r13.bind.active)))

    print("\n[10] 裝置政策：引擎只照做（判斷在平台層）")
    r14 = Rig(["F9"])
    r14.down("f9", device_ok=False, device="USB鍵盤")
    check("device_ok=False → 不觸發", r14.recording is False)
    r14.down("f9", device_ok=True, device="BT-HID")
    check("device_ok=True → 觸發", r14.recording is True)
    r14.up("f9", device_ok=True)
    check("device 標籤不影響狀態機", r14.recording is False)

    print("\n[11] hold 的 on_finish 要帶對 pressed（自動重錄靠它）")
    r15 = Rig(["F9"])
    r15.down("f9")
    r15.up("f9")
    check("按住再放開 → pressed=True", r15.finished == [True], str(r15.finished))
    r15.down("f9")
    r15.eng._finish_explicit()                # 模擬被結束鍵收尾
    check("明確收尾 → pressed=False", r15.finished[-1] is False, str(r15.finished))

    print("\n[12] toggle 模式：放開不可以翻轉（否則永遠錄不了）")
    r16 = Rig(["F9@toggle"])
    r16.down("f9")
    check("第一下按下 → 開始", r16.recording is True)
    r16.up("f9")
    check("第一下放開 → 仍在錄", r16.recording is True)
    r16.down("f9")
    check("第二下按下 → 停止", r16.recording is False)
    r16.up("f9")
    r16.down("f9")
    check("第三下按下 → 又開始", r16.recording is True)

    print("\n[13] 測試模式：只回報、絕不觸發")
    r17 = Rig(["RightCtrl", "F9,Esc"])
    st = r17.eng.start_key_test(5.0, "F9")
    check("可以啟動", st.get("ok") is True, str(st))
    check("狀態回報 running", r17.eng.test_state()["running"] is True)
    r17.down("f9")
    r17.down("esc")
    r17.down("a")
    check("測試期間完全沒有開始錄音", r17.started == [], str(r17.started))
    hits = r17.eng.test_state()["hits"]
    check("收到三個事件（含不相關的鍵）", len(hits) == 3, str(len(hits)))
    check("F9 被認成開始鍵", "開始鍵" in hits[0]["roles"][0], str(hits[0]["roles"]))
    check("Esc 被認成結束鍵", "結束鍵" in hits[1]["roles"][0], str(hits[1]["roles"]))
    check("沒設定的鍵也回報「不相關」",
          "不相關的鍵" in hits[2]["roles"][0], str(hits[2]["roles"]))
    r17.eng.cancel_key_test()
    check("可以取消", r17.eng.test_state()["running"] is False)

    print("\n  （過期後行為要恢復 —— 這是「按了完全沒反應」的地雷）")
    r18 = Rig(["F9"])
    r18.eng.start_key_test(3.0, "")
    r18.eng._test_until = time.monotonic() - 0.1     # 模擬過期
    check("逾時自動結束", r18.eng.test_state()["running"] is False)
    r18.down("f9")
    check("恢復正常後 F9 會開始錄音", r18.recording is True, str(r18.started))

    r19 = Rig(["F9"])
    r19.eng.start_key_test(0.5, "")
    check("秒數下限 3 秒（避免還沒按就結束）",
          r19.eng.test_state()["remaining"] > 2.5,
          str(r19.eng.test_state()["remaining"]))
    r19.eng.cancel_key_test()

    print("\n[14] token_of()：同一顆鍵的不同來源要收斂成同一個 token")
    check("通用名稱 + e0 → rightctrl", trigger.token_of("ctrl", True) == "rightctrl",
          trigger.token_of("ctrl", True))
    check("專用名稱 → rightctrl", trigger.token_of("rightctrl") == "rightctrl",
          trigger.token_of("rightctrl"))
    check("非修飾鍵沒有側別", trigger.token_of("f9", True) == "f9",
          trigger.token_of("f9", True))

    print("\n" + "=" * 68)
    if failures:
        print(f"❌ {len(failures)} 項失敗：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
