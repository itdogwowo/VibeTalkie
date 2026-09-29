#!/usr/bin/env python3
"""平台無關的觸發狀態機：按鍵事件 → 「開始錄音／停止錄音」。

## 為什麼要這一層（而不是兩個平台各寫一份）

Windows 版（`app/core/ptt.py`）與 macOS 版（`app/mac_vibetalkie.py`）
原本**各有一份**觸發邏輯。第一份只有「一顆鍵 + 一個全域模式」，
第二份只有「單一顆鍵 + 三個 if」—— 然後 main 這一邊長出了

  · 多組錄音鍵同時生效
  · 開始鍵與結束鍵可以是不同顆（`F9,Esc`）
  · 開始與結束**各自**有自己的行為（hold／toggle／double）
  · 每一組可以單獨停用（`~` 前綴）
  · 「測試」模式（只聽、不錄、不注入）

而 mac 那一邊**一項都沒有**。兩份實作一定會漂移，所以把與 OS 無關的
部分全部搬到這裡，兩個 daemon 共用同一顆引擎。

## 邊界（改這個檔案之前先讀）

**這裡不做任何平台判斷。** 沒有 `ctypes`、沒有 `Quartz`、沒有 `WM_KEYDOWN`、
沒有 `E0` 旗標的解讀（那個由平台層做完再傳進來）。

    平台層的責任                     本模組的責任
    ─────────────────────────────    ─────────────────────────────
    鍵碼 → canonical 名稱            「這顆鍵命中哪一組綁定」
    取得修飾鍵狀態                    修飾鍵狀態機（自己維護）
    判斷來源裝置是否允許（Windows）    裝置政策只是**照做**（`Event.device_ok`）
    真的開／關麥克風、跑 ASR          什麼時候該開、什麼時候該關
    注入文字                          回報「收到什麼鍵」（測試模式）

平台層提供的只有兩個回呼：`on_start(label)` 與 `on_finish(pressed)`，
以及一個 `get_bindings()`。**引擎不匯入 `ptt`／`recorder`／`mac_*`。**

## 事件的形狀（兩個平台都要轉成這個）

    Event(key="f9", down=True)
    Event(key="rightctrl", down=True)          # 側別寫在名稱裡
    Event(key="ctrl", down=True, e0=True)      # Windows：通用 VK + E0 旗標

側別由 `hotkey.side_of()` 判斷 —— **名稱優先於旗標**（mac 的 keycode
本身就分左右，名稱比旗標精確）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import hotkey

# 測試模式的秒數下限。為什麼要有：UI 送 0.5 秒時，使用者還沒把手指移到
# 按鍵上就已經結束了，看起來像「測試功能壞了」。
MIN_TEST_SECONDS = 3.0
# 測試模式回報最近幾筆（UI 只顯示最後幾筆，不必留整串）
TEST_HISTORY = 5


def token_of(name: str, e0: bool = False, by_name: str | None = None) -> str:
    """事件的 canonical token（含側別）：`("ctrl", e0=True)` → `"rightctrl"`。

    `main_down` 用這個當 key —— **不能用 VK**：同一個事件可能是通用 VK
    （`0x11` + E0）也可能是專用 VK（`0xA3`），兩者 VK 不同但是**同一顆鍵**，
    用 VK 會讓「按住右 Ctrl 不放」的自動重複漏掉一半。
    """
    side = hotkey.side_of(name, by_name, e0)
    return f"{side}{name}" if side else name


@dataclass(frozen=True)
class Event:
    """一個**平台無關**的按鍵事件。"""

    key: str                        # canonical 名稱（可帶側別：rightctrl）
    down: bool
    mods: frozenset[str] = frozenset()   # 事件**之前**已按住的修飾鍵（小寫）
    e0: bool = False                # Windows 的右側旗標（mac 恆為 False）
    device: str | None = None       # 來源裝置標籤（**只給 log 用**，mac 恆為 None）
    # ⚠️ 裝置**政策**的判斷留在平台層（Windows 才知道 `hDevice` 與 device_filter），
    #    平台層算完之後把結論放在這裡。mac 恆為 True —— macOS 做不到裝置辨識
    #    （AGENTS.md §8.7 已實測四個候選欄位全部相同），所以那裡沒有政策可言。
    device_ok: bool = True
    autorepeat: bool = False        # 按住產生的重複事件


class TriggerEngine:
    """多組錄音鍵的觸發狀態機（平台無關）。

    ## 狀態歸屬

    引擎擁有**所有觸發狀態**：`capturing`／`mods_down`／`tap_at`／`main_down`／
    `pressed`。daemon **不要再存第二份** —— 兩份的症狀是「一邊更新、一邊沒更新」，
    而且通常只在某個平台的某個模式下才看得出來。
    """

    def __init__(self,
                 get_bindings: Callable[[], "hotkey.HotkeyBindings"],
                 on_start: Callable[[str], None],
                 on_finish: Callable[[bool], None],
                 double_window: float = 0.4,
                 debug: bool = False):
        """
        `get_bindings`：回傳目前生效的 `HotkeyBindings`（每次事件都問一次，
                       所以改設定不必重啟）。
        `on_start(label)`：開始錄音（`label` 只用於 log／除錯輸出）。
        `on_finish(pressed)`：停止錄音。`pressed` 是「使用者此刻還按著嗎」——
                       Windows 的自動重錄要靠它判斷（放開後重錄會錄到環境音）。
        `double_window`：double 模式的兩下間隔上限（秒）。
        """
        self._get_bindings = get_bindings
        self._on_start = on_start
        self._on_finish = on_finish
        self.double_window = float(double_window)
        self.debug = debug

        # ------------------------------------------------ 觸發狀態
        self.capturing = False
        # 目前按住的修飾鍵（**小寫通用名稱**）。Raw Input / CGEventTap 都只送
        # 單一事件，不告訴我們「現在 Ctrl 有沒有按住」，所以自己維護。
        self.mods_down: set[str] = set()
        # 「等待第二下」的時間戳，**以規格為 key**（每一組雙擊鍵各自獨立）。
        # ⚠️ 用單一變數的話，兩顆雙擊鍵輪流各按一下就會被誤判成雙擊。
        self.tap_at: dict = {}
        # 目前按下的主鍵（canonical token 集合）—— 用來吃掉作業系統的
        # **自動重複**（按住不放時每 ~30ms 一次 keydown）。
        # ⚠️ 沒有這一層：按住一顆 double 鍵不放會落在配對視窗內，
        #    被當成「連按兩下」→ 反而開始錄音（實測踩到）。
        self.main_down: set[str] = set()
        # 主鍵現在是不是按著（**跨「開始錄音 → 放開」兩個時間點**都要看得到，
        # 因為自動重錄是在「放開的瞬間」才決定的）
        self.pressed = False
        # 結束鍵的狀態（見 `feed`）：
        #   `_end_armed`  ＝結束鍵按下了（行為是「鬆開才停」）→ keyup 收尾
        #   `_end_pressed`＝結束鍵剛觸發過（其他行為，已經在 down 收尾）
        # 兩者都是為了讓 **keyup 不會重複收尾**（放開不該再停一次）。
        self._end_armed = False
        self._end_pressed = False
        # 「測試這個組合」模式（見 `start_key_test`）
        self._test_until = 0.0
        self._test_which = ""
        self._test_hits: list[dict] = []

    # ------------------------------------------------ 查詢

    def _bindings(self) -> "hotkey.HotkeyBindings":
        return self._get_bindings()

    def allow_any_device(self) -> bool:
        """按鍵設定是否允許來自任何裝置。

        判準：**有任一組**錄音鍵「不是裝置原生那顆」時，就代表使用者想用
        別的來源（鍵盤、滑鼠側鍵…）。這種情況不該再要求裝置符合
        `device_filter` —— 否則「完全脫離硬體」做不到。
        """
        return self._bindings().accepts_any_device

    def end_owner(self, key, e0: bool = False):
        """這顆鍵是不是某一組的**結束鍵**？回傳那一組的開始規格。

        配對語法：`F9,Esc` ＝ 開始 F9、結束 Esc（見 `hotkey.parse_binding`）。
        ⚠️ 只看**啟用中**的組合（停用的不該有反應，含它的結束鍵）。
        """
        b = self._bindings()
        for spec in b.active:
            end = b.end_of(spec)
            if end is None:
                continue
            if hotkey.spec_hit(end, key, e0) and hotkey.mods_ok(end, self.mods_down):
                return spec
        return None

    def label_of(self, spec) -> str:
        return self._bindings().label_of(spec)

    # ------------------------------------------------ 測試這個組合

    def start_key_test(self, seconds: float = 20.0, which: str = "") -> dict:
        """開始「測試」：聽按鍵，但**不錄音、不注入**，只回報收到什麼。

        為什麼要做（實測回饋：「除了沒有測試之外其他基本上都像是我想要的」）：
        設定好一顆鍵之後，使用者唯一的驗證方式是「按下去看有沒有反應」——
        但那會真的開始錄音、注入文字到他正在打的地方。
        所以需要一個**只聽不做**的模式。

        `which`＝只測這一組的開始鍵（空字串＝測全部）。
        """
        self._test_until = time.monotonic() + max(MIN_TEST_SECONDS, float(seconds))
        self._test_which = which
        self._test_hits = []
        print(f"  🧪 測試模式：請按你要測的按鍵（{seconds:.0f} 秒內有效；"
              f"不會錄音）", flush=True)
        return {"ok": True, "seconds": seconds, "which": which}

    def cancel_key_test(self) -> None:
        self._test_until = 0.0
        self._test_hits = []
        self._test_which = ""

    def test_active(self) -> bool:
        """測試模式現在有效嗎？

        ⚠️ **一定要比較 `time.monotonic()`，不能只看時間戳有沒有值** ——
        過期後那個時間戳還留著，只判斷「有值」會讓錄音功能**永遠失效**
        （實測踩到，而且是那種「按了完全沒反應」的最糟症狀）。
        """
        return bool(self._test_until) and time.monotonic() < self._test_until

    def _note_test_hit(self, ev: Event, hits: list, token: str) -> None:
        """測試模式下收到一個事件 → 記下來（給 UI 顯示）。"""
        if not ev.down:
            return
        b = self._bindings()
        # 這顆鍵的「角色」：開始鍵？結束鍵？還是不相關？
        roles: list[str] = []
        for spec in b.active:
            if hotkey.spec_hit(spec, ev.key, ev.e0) and hotkey.mods_ok(spec, self.mods_down):
                roles.append(f"開始鍵（{b.label_of(spec)}）")
            end = b.end_of(spec)
            if end is not None and hotkey.spec_hit(end, ev.key, ev.e0) \
                    and hotkey.mods_ok(end, self.mods_down):
                roles.append(f"結束鍵（{b.label_of(spec)}）")
        if not hits and not roles:
            # 沒命中的鍵也報出來 —— 「按了沒反應」時最需要知道的就是
            # 「有沒有收到」，而不是只有「沒有」。
            roles = ["不相關的鍵（不會觸發）"]
        self._test_hits.append({
            "key": ev.key, "vk": ev.key, "e0": ev.e0, "down": ev.down,
            "mods": sorted(self.mods_down),
            "roles": roles,
            "at": time.strftime("%H:%M:%S"),
        })
        if self.debug:
            print(f"  🧪 測試收到 {ev.key} → {roles}", flush=True)

    def test_state(self) -> dict:
        """測試模式的狀態（給 UI 輪詢）。"""
        now = time.monotonic()
        running = bool(self._test_until) and now < self._test_until
        if self._test_until and not running:
            self._test_until = 0.0            # 逾時自動結束
        return {
            "running": running,
            "remaining": round(max(0.0, self._test_until - now), 1) if running else 0.0,
            "which": self._test_which,
            "hits": list(self._test_hits)[-TEST_HISTORY:],
        }

    # ------------------------------------------------ 事件入口

    def _update_mods(self, ev: Event) -> None:
        """修飾鍵狀態機。

        ⚠️ 存的是**通用小寫名稱**（`"ctrl"`），不是 `"rightctrl"` ——
        側別由事件的 `key` 決定（`spec_hit(spec, "rightctrl")` 判成右邊）。
        存側別名稱的話，`Ctrl+Alt+R` 這種組合鍵在 mac 上永遠不吻合
        （mac 送的是 `ctrl`／`leftctrl` 混用）。
        """
        name, _side = hotkey._key_name_and_side(ev.key)      # noqa: SLF001
        if name not in hotkey._MODIFIER_NAMES:               # noqa: SLF001
            return
        if ev.down:
            self.mods_down.add(name)
        else:
            self.mods_down.discard(name)

    def feed(self, ev: Event) -> None:
        """一個按鍵事件進來。**這裡不做任何平台判斷。**

        順序與 `ptt._handle_key()` 原本的順序**完全一致**（改了會壞事）：

          1. 修飾鍵狀態先更新（主鍵的判定要用）
          2. 自動重複直接丟掉（double 模式靠這條才不會被誤判成連點）
          3. 算出命中哪幾組
          4. 測試模式：只回報、不觸發
          5. 結束鍵（**優先於**開始行為）
          6. 起點：裝置政策 → 自動重複 → 交給該組自己的行為
        """
        token = token_of(ev.key, ev.e0)
        self._update_mods(ev)
        if ev.autorepeat:
            return

        mods = frozenset(self.mods_down)
        hits = [s for s in self._bindings().active
                if hotkey.spec_hit(s, ev.key, ev.e0) and hotkey.mods_ok(s, mods)]

        # 「測試這個組合」模式：只回報「有沒有收到」，**絕不錄音**。
        # 判斷要用 `test_active()`（它會比較時間），不能只看時間戳有沒有值。
        if self.test_active():
            self._note_test_hit(ev, hits, token)
            if self._test_hits:
                return

        # ⚠️ **結束鍵優先於行為**（實測回饋：「也可以接受按鍵不一樣做
        # 開始和結束的配對」）。使用者在設定裡明確寫了結束鍵（`F9,Esc`）時，
        # 就以那顆鍵為準 —— 這時「開始行為」只是開始的方式，不再決定怎麼收尾。
        # 結束鍵自己有行為（`F9,Esc@hold`＝**鬆開**才停、`@toggle`＝再按一下停）。
        if not hits and self.capturing:
            owner = self.end_owner(ev.key, ev.e0)
            if owner is not None:
                end_mode = self._bindings().end_mode_of(owner)
                if ev.down:
                    if end_mode == hotkey.MODE_HOLD:
                        # 「鬆開才停」→ 先記下來，等 keyup 再收尾。
                        # ⚠️ 按下就停的話，使用者按 Escape 的瞬間錄音就斷了 ——
                        #    但他可能還想多講半句。
                        self._end_armed = True
                        if self.debug:
                            print(f"  · 結束鍵按下（{self.label_of(owner)}）"
                                  f"→ 等鬆開才停")
                    else:
                        self._end_pressed = True
                        if self.debug:
                            print(f"  · 結束鍵 {self.label_of(owner)} → 停止錄音")
                        self._finish_explicit()
                elif self._end_armed or self._end_pressed:
                    self._end_armed = False
                    self._end_pressed = False
                    if self.debug:
                        print("  · 結束鍵鬆開 → 停止錄音")
                    self._finish_explicit()
                return

        if not hits:
            self.main_down.discard(token)   # 沒命中的鍵不該留在按下集合裡
            return
        spec = hits[0]

        # 被拒絕的時候要說得出原因，否則只能看到「按了沒反應」。
        # 實測踩過：送合成的 Ctrl 進來時 make=0、裝置無名、沒有 E0，
        # 結果整個事件被靜默丟棄，完全查不出為什麼。
        #
        # ⚠️ 裝置篩選只在「用裝置原生鍵」時才有意義（政策由平台層算好放進
        #    `ev.device_ok`）。使用者若刻意把錄音鍵改成別的鍵（完全脫離硬體），
        #    平台層就會回 True。
        if not ev.device_ok:
            if self.debug:
                print(f"  · 忽略 {ev.device} 的 {spec.label}（不是目標裝置）")
            return

        # 作業系統的自動重複（按住不放時每 ~30ms 一次 keydown）要吃掉。
        if ev.down:
            if token in self.main_down:
                if self.debug:
                    print(f"  · 忽略自動重複 {spec.label}")
                return
            self.main_down.add(token)
        else:
            self.main_down.discard(token)

        if self.debug:
            print(f"  · 接受 {spec.label}（{self._bindings().mode_of(spec)}）"
                  f"{'DOWN' if ev.down else 'UP  '} dev={ev.device} "
                  f"capturing={self.capturing}")

        if ev.down:
            self._on_main_down(spec, spec.label)
        else:
            self._on_main_up(spec)

    def tick(self) -> None:
        """double 模式的計時器：超過配對視窗就清掉「等待第二下」的狀態。

        ⚠️ 必須由主迴圈定期呼叫。沒有它，double 模式會永遠配不到第二下。
        ⚠️ 待配對的狀態是**每一組各記一份**（`tap_at`）。
        """
        now = time.monotonic()
        for spec, at in list(self.tap_at.items()):
            if at and (now - at) > self.double_window:
                self.tap_at[spec] = 0.0

    # ------------------------------------------------ 行為

    def _toggle_capture(self, label: str) -> None:
        """toggle / double 模式的「開始 ↔ 停止」。

        ⚠️ 停止時要把 `pressed` 清掉：這兩種模式的錄音是「按一下開始」，
        使用者早就放開了，若留著 True，之後的自動重錄會以為他還按著，
        變成對著空氣重錄一段（而且錄到的是環境音）。
        """
        if self.capturing:
            self.capturing = False
            self.pressed = False
            self._on_finish(False)
        else:
            self.capturing = True
            self.pressed = False        # 按一下就放開了，不是「按著」
            self._on_start(label)

    def _on_main_down(self, spec, label: str) -> None:
        """主鍵按下。**依這一組自己的觸發方式**決定要做什麼。

        ⚠️ 為什麼模式是「每一組」而不是全域：實測回饋「無法錄製雙擊」——
        真實情境是「藍牙麥克風按住說話 ＋ 鍵盤 F9 雙擊」。
        全域一個模式做不到，而兩組各自的狀態也必須分開記（`tap_at`）。
        """
        mode = self._bindings().mode_of(spec)
        if mode == hotkey.MODE_TOGGLE:
            self._toggle_capture(label)
            return
        if mode == hotkey.MODE_DOUBLE:
            # 單擊不動作，等第二下（模仿 macOS 的聽寫手勢）
            now = time.monotonic()
            last = self.tap_at.get(spec, 0.0)
            if last and (now - last) <= self.double_window:
                self.tap_at[spec] = 0.0        # 用掉這次配對
                self._toggle_capture(label)
            else:
                self.tap_at[spec] = now
            return
        # hold（預設）：按住就開始
        if not self.capturing:
            self.capturing = True
            self.pressed = True
            self._on_start(label)

    def _on_main_up(self, spec) -> None:
        """主鍵放開。

        ⚠️ **只有 `hold` 模式該在這裡動作。** toggle / double 是「按下才算
        一次」，若放開也處理，按一下就會「開始＋馬上停」——
        實測被測試抓到（第一下按完 `capturing` 又變回 False）。

        ⚠️ **而且設定了結束鍵時也不能在放開時停**：`F9,Escape` 的意思是
        「按 F9 開始、按 Escape 結束」，F9 放開只代表「開始鍵按完了」。
        這條規則讓「用同一顆鍵的組合鍵當開始鍵」也說得通
        （`Ctrl+Alt+R` 放開 R 不該結束錄音）。
        """
        b = self._bindings()
        if b.end_of(spec) is not None:
            return                      # 收尾交給結束鍵
        if self._bindings().mode_of(spec) != hotkey.MODE_HOLD:
            return                      # toggle / double：放開不算一次
        if self.capturing:
            was_pressed = self.pressed
            self.capturing = False
            self.pressed = False
            self._on_finish(was_pressed)

    def _finish_explicit(self) -> None:
        """由**明確的結束鍵**收尾（不是靠開始行為，也不是靠放開開始鍵）。"""
        self.capturing = False
        # ⚠️ 使用者不是靠放開結束的 → `pressed=False`，不可以自動重錄
        #    （那會錄到他放開之後的環境音）。
        self.pressed = False
        self._end_armed = False
        self._end_pressed = False
        self._on_finish(False)


def engine_for_test(specs, modes=None, ends=None, end_modes=None, enabled=None,
                    double_window: float = 0.4, debug: bool = False,
                    on_start=None, on_finish=None) -> TriggerEngine:
    """測試與工具的便利建構子：直接吃設定字串清單。

    正式路徑**不要**用它 —— daemon 的 `_bindings()` 有自己的快取與
    「設定改了要重讀」邏輯（見 `ptt._bindings()`）。
    """
    bind, err = hotkey.bindings(list(specs))
    if err:
        print(f"  ⚠️ {err}")
    if modes or ends or end_modes or enabled:
        bind = hotkey.HotkeyBindings(
            list(bind.specs),
            list(modes) if modes else list(bind.modes),
            list(ends) if ends else list(bind.ends),
            list(end_modes) if end_modes else list(bind.end_modes),
            list(enabled) if enabled else list(bind.enabled))
    started: list[str] = []
    finished: list[bool] = []
    return TriggerEngine(
        get_bindings=lambda: bind,
        on_start=on_start or (lambda label: started.append(label)),
        on_finish=on_finish or (lambda pressed: finished.append(pressed)),
        double_window=double_window, debug=debug)
