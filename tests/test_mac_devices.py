#!/usr/bin/env python3
"""音訊裝置列舉的測試（macOS）。

## 為什麼要測這個

設定頁有一個「麥克風優先順序」清單，它的下拉選單來自
`/api/config` 的 `devices`。第一版只回**一個**「系統預設輸入裝置」，
於是使用者說「**裝置現在沒有列出來**」。

要列得出來，就得真的列舉 —— 而列舉的解析很容易寫錯，而且錯了
**不會拋錯、只會少幾支或名稱不對**：

  · 取樣率的鍵是 `Current SampleRate`（**沒有空格**），
    寫成 `Current Sample Rate` 就永遠是 0
  · 清單**同時包含輸出裝置**（喇叭、HDMI），要按 `Input Channels` 過濾
  · 但 某些虛擬音訊裝置**真的有** `Input Channels`
    → 只能靠 `Transport: Virtual` 分辨
  · 「預設裝置」要看 `Default Input Device: Yes`，不是取第一支

這些都是實測踩到的，所以用測試釘住。

執行：python tests/test_mac_devices.py
（需要 pyobjc 才能問 AVAudioEngine；沒有就跳過需要它的部分）
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "core"))
sys.path.insert(0, str(ROOT / "third_party"))

import _console  # noqa: E402  # 測試輸出一律 UTF-8（Windows 管線下預設是 cp950）

_console.setup()

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def skip(name: str, why: str) -> None:
    print(f"  ⏭  {name}  — {why}")


def main() -> int:
    print("=" * 74)
    print("音訊裝置列舉測試（macOS）")
    print("=" * 74)

    if sys.platform != "darwin":
        print(f"\n  ⏭  這個測試只適用 macOS（目前 {sys.platform}）")
        return 0

    try:
        import mac_recorder
    except ImportError as exc:
        print(f"\n  ❌ 載入不到 mac_recorder：{exc}")
        return 2

    print("\n▍基本形狀")
    devs = mac_recorder.list_input_devices(force=True)
    check("回傳 list", isinstance(devs, list), type(devs).__name__)
    check("至少有一支麥克風（不然無法錄音）", len(devs) >= 1, f"{len(devs)} 支")

    if devs:
        d = devs[0]
        for k in ("index", "name", "channels", "is_default"):
            check(f"每支裝置有 {k!r}", k in d, f"鍵：{sorted(d.keys())}")
        check("index 是 0,1,2… 連續",
              [x["index"] for x in devs] == list(range(len(devs))),
              str([x["index"] for x in devs]))
        check("每支都有名字（不是空的）",
              all((x.get("name") or "").strip() for x in devs),
              str([x.get("name") for x in devs]))
        check("每支都有 > 0 個輸入通道",
              all((x.get("channels") or 0) > 0 for x in devs),
              str([x.get("channels") for x in devs]))

        # ⚠️ 取樣率的鍵很容易寫錯（實際踩到）
        rates = [x.get("rate") or 0 for x in devs]
        check("至少有一支回報了取樣率（鍵名要對）", any(r > 0 for r in rates),
              f"rate={rates} ← 全 0 代表鍵名寫錯了")

    print("\n▍預設裝置：最多只能有一個")
    defaults = [x for x in devs if x.get("is_default")]
    check("最多一個預設", len(defaults) <= 1,
          f"{len(defaults)} 支被標成預設：{[x['name'] for x in defaults]}")

    print("\n▍虛擬裝置要被排除（不然混進麥克風清單）")
    # 實測：某些虛擬音訊裝置也有 Input Channels，
    # 所以「有沒有輸入通道」分不出來，要靠 Transport。
    all_devs = mac_recorder.list_input_devices(force=True, include_virtual=True)
    virtual = [x for x in all_devs
               if (x.get("transport") or "").lower() == "virtual"]
    if virtual:
        check(f"有 {len(virtual)} 支虛擬裝置被正確識別",
              all((x.get("transport") or "").lower() == "virtual"
                  for x in virtual))
        names_default = {x["name"] for x in devs}
        leaked = [x["name"] for x in virtual if x["name"] in names_default]
        check("虛擬裝置沒有出現在預設清單裡", not leaked, str(leaked))
    else:
        skip("虛擬裝置過濾", "這台機器上沒有 Transport=Virtual 的裝置")

    print("\n▍快取：第二次要快很多（裝置清單不會常常變）")
    import time
    t0 = time.perf_counter()
    mac_recorder.list_input_devices(force=True)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    mac_recorder.list_input_devices()
    warm = time.perf_counter() - t0
    check("有快取（第二次 < 第一次的 1/10）", warm < max(cold / 10, 0.01),
          f"冷 {cold*1000:.0f}ms → 暖 {warm*1000:.2f}ms")

    print("\n▍UI 需要這些欄位（設定頁的下拉選單）")
    cfg_devs = [{"index": x["index"], "name": x["name"]} for x in devs]
    check("可以轉成 UI 要的 {index, name}",
          all(set(x) == {"index", "name"} for x in cfg_devs), str(cfg_devs[:2]))

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
