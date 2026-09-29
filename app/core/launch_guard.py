#!/usr/bin/env python3
"""啟動防護：同一個 port 已經有 VibeTalkie 在跑，就**拒絕啟動第二個**。

## 為什麼要獨立成一個模組（而不是各進入點自己一份）

`app/vibetalkie.py`（Windows 進入點）與 `app/core/ui_server.py`（macOS 的
設定頁面）本來**各有一份** `pick_port()` / `port_in_use()` / `AlreadyRunning`。
兩份一開始一樣，後來只改了一份 —— 那正是這個專案反覆踩到的**漂移**：

    同一個 bug 有兩份實作 → 只修一份的症狀是
    「UI 存得進去，重啟後少一組」（hotkey 去重，`10c17d3`）
    「mac 命中、Windows 不命中」（鍵名轉換，`hotkey._key_name_and_side`）
    「只有一個平台顯示『未知』」（狀態值大小寫，`Status.on_state`）

所以用 `app/core/trigger.py` 處理過的同一招：**規則放共用層、平台差異當參數**。

    · 規則（`AlreadyRunning` / `port_in_use` / `pick_port`）→ 這裡
    · 訊息**講什麼**（`explain()`）→ 這裡（兩個平台同一個說法）
    · 「怎麼關掉舊的」→ 呼叫端傳進來（Windows 會教工作管理員，macOS 是 ⌘Q）

`tests/test_shared_layer.py` 用 AST 釘住「這條規則只能有一份實作」。

## 為什麼要有這個防護（實測踩到，症狀是「設定一直被還原」）

原本的 `pick_port()` 是「從 preferred 起算 20 號裡找一個空的」，找不到就直接
回傳 preferred。於是使用者再啟動一次時，第二個行程**靜默地**換一個 port 起來，
他完全不知道。後果不只是「兩個視窗」：

  · 兩個行程**共用同一個 `config.toml`**，各自握一份記憶體
  · `cfg.save()` 有 5 處（啟動、換麥克風、換模型、UI 儲存…），每次都是**整個檔案重寫**
  · 於是舊行程會把它記憶體裡的舊值蓋回去

實測證據（同一台機器，兩個行程都活著）：

    磁碟：        model_dir = "sherpa-onnx-paraformer-…"、mic_stream = "session"
    舊行程記憶體：model_dir = ""（空）、mic_stream = "per_press"

使用者的感受是「我存了，過一陣子又變回去」，而且**檔案永遠看起來是對的**。
所以寧可拒絕啟動，也不要默默開第二個。
"""

from __future__ import annotations

import socket

__all__ = ["AlreadyRunning", "explain", "pick_port", "port_in_use"]


class AlreadyRunning(Exception):
    """已經有另一個 VibeTalkie 在用這個 port。"""


def port_in_use(port: int) -> bool:
    """這個 port 有東西在 listen 嗎？（用**綁定探測**，不是 `connect_ex`）

    ⚠️ 兩者的差別是實際踩到的 bug：`connect_ex` 問的是「連得上嗎」，
    所以對「已綁定但還沒開始 accept」或防火牆丟 RST 的 port 會回「沒人用」
    → 於是回傳一個根本用不到的 port。綁定探測問的才是我們真正要問的問題：
    「我綁得上嗎？」

    ⚠️ `SO_REUSEADDR` **不要設** —— 設了會讓「已被佔用」的 port 也綁得上，
    這個函式就永遠回 False（Windows 與 Linux 的行為不同，不可依賴）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def pick_port(preferred: int, span: int = 1) -> int:
    """取得要用的 port。**被佔用就丟 `AlreadyRunning`，不自動漂流。**

    ## ⚠️ 為什麼 `span` 預設是 1（這是一個實測踩到的錯誤設計）

    第一版是「往後找 20 號」，理由是「被佔用時讓位比較方便」。那個設計讓
    防護**完全失效**：

        port_in_use(8756) → True        # 偵測正確：舊實例在跑
        pick_port(8756)   → 8757        # 但 8757 是空的 → 讓位 → 靜默啟動第二個

    真實情境就是這樣：舊實例佔著 8756，8757 當然是空的。於是使用者
    **每次重複啟動都會成功**，接著兩個行程開始互相覆蓋 `config.toml`。

    （測試之所以沒抓到，是因為它只佔住 8756 一號，而 `span=20` 的邏輯
    向後讓位就通過了。**測試要照真實情境設計，不是照實作設計。**）

    ## 想刻意跑第二個

    明確指定 `--port` 就是「我要那個位置」：那個 port 被佔用時**也是**
    `AlreadyRunning`（誠實報錯，而不是偷偷換一個）。

    `span > 1` 只在**確定要讓位**的場合才傳（目前沒有這種呼叫端）。
    """
    for p in range(preferred, preferred + span):
        if not port_in_use(p):
            return p
    raise AlreadyRunning(
        f"port {preferred} 已被佔用"
        + (f"（{preferred}–{preferred + span - 1} 都滿了）" if span > 1 else ""))


def explain(exc: Exception, *, command: str, close_hints: list[str],
            alt_port: int,
            action: str = "所以這裡刻意拒絕啟動。") -> list[str]:
    """要印給使用者看的訊息（回傳行清單，印出由呼叫端決定）。

    共用層決定**講什麼**：為什麼拒絕、後果是什麼、下一步怎麼做。
    呼叫端決定**平台專屬的那一句**（`close_hints`）—— 那正是三份訊息
    唯一的差別，也是漂移的起點（Windows 教你去工作管理員，macOS 是 ⌘Q）。

    ⚠️ 這裡刻意**不**用 `print`：macOS 的呼叫端在「設定頁面起不來」時
    選擇繼續跑（錄音功能不受影響，所以它傳 `action=` 改掉最後一句），
    Windows 的呼叫端則是結束行程。
    """
    return [
        "",
        "  ⚠️ VibeTalkie 好像已經在執行了。",
        f"     {exc}",
        "",
        "  同時跑兩個會讓**設定互相覆蓋**（各自記一份，存檔時整個寫回），",
        f"  症狀是「設定存了又變回去、模型自己換掉」。{action}",
        "",
        "  請先關掉舊的那一個：",
        *[f"    · {hint}" for hint in close_hints],
        "",
        "  想刻意同時跑兩個（例如測試）請指定不同 port：",
        f"    {command} --port {alt_port}",
    ]
