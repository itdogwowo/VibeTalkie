#!/usr/bin/env python3
"""量測「開麥克風時藍牙耳機的播放會不會中斷」。

## 為什麼要量這個

使用者回報：每次開始／停止錄音，耳機的聲音都會斷一下。

藍牙的已知限制：耳機聽音樂走 **A2DP**（高品質、單向輸出），
但一開麥克風就得切成 **HFP/HSP**（雙向、單聲道、低品質）。
多數耳機**無法同時**維持兩者，所以切換時播放會中斷。

**這不是程式的 bug，但它決定了產品設計：**
  · 若「保持串流常開」以換取低延遲 → 耳機會卡在 HFP，播放品質永久變差
  · 若「每次用完就關」 → 每次按下都要重新建立（實測 0.7–1.2 秒），
    開頭那句話會不見
兩個都不能無腦選，所以要有數據。

## 這支工具做什麼

在「開麥克風前 / 開著的時候 / 關掉之後」三個時間點，
快照所有音訊端點的名稱與狀態，然後 diff。

切換 profile 的典型特徵是：某個 **render（輸出）** 端點從 ACTIVE 變成
NOTPRESENT／UNPLUGGED，同時冒出另一個 render 端點（HFP 的）。

用法:
    python tools/p1/measure_bt_profile_switch.py
    python tools/p1/measure_bt_profile_switch.py --device 1 --hold 4
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "p0"))

from record_wav import setup_console  # noqa: E402
from recorder import Capture  # noqa: E402

import winreg  # noqa: E402

STATE = {
    1: "ACTIVE",
    2: "DISABLED",
    4: "NOTPRESENT",
    8: "UNPLUGGED",
    268435457: "DISABLED(0x10000001)",
    536870916: "UNPLUGGED?",
}

BASE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
PROP_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PROP_FRIENDLY = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"


def _prop(props, key) -> str:
    try:
        v = props.get(key)
    except Exception:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-16-le", "replace").rstrip("\x00").strip()
    return str(v or "").strip()


def snapshot() -> dict[str, str]:
    """{識別字: "名稱 [狀態]"}。識別字用端點 GUID，才能跨時間比對。"""
    out = {}
    for direction in ("Render", "Capture"):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{BASE}\{direction}")
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, guid) as k:
                        state = winreg.QueryValueEx(k, "DeviceState")[0]
                    with winreg.OpenKey(root, rf"{guid}\Properties") as pk:
                        props = {n: winreg.QueryValueEx(pk, n)[0]
                                 for n in (PROP_DESC, PROP_FRIENDLY)
                                 if _exists(pk, n)}
                    name = _prop(props, PROP_DESC) or "(無名稱)"
                    desc = _prop(props, PROP_FRIENDLY)
                    label = f"{direction[:3]} {name}" + (f" / {desc}" if desc else "")
                    out[guid] = f"{label} [{STATE.get(state, hex(state))}]"
                except OSError:
                    continue
    return out


def _exists(key, name) -> bool:
    try:
        winreg.QueryValueEx(key, name)
        return True
    except OSError:
        return False


def diff(a: dict, b: dict) -> list[str]:
    lines = []
    for guid, val in b.items():
        if guid not in a:
            lines.append(f"  + 出現  {val}")
        elif a[guid] != val:
            lines.append(f"  ~ 改變  {val}\n          （原本 {a[guid]}）")
    for guid, val in a.items():
        if guid not in b:
            lines.append(f"  - 消失  {val}")
    return lines


def monitor(device: int, hold: float, poll: float = 0.1) -> tuple[dict, list[str], dict]:
    """開著麥克風並持續輪詢端點狀態，回傳 (開啟前的快照, 轉變事件, 時間統計)。

    ⚠️ registry 的 DeviceState **不是即時的**。實測：開著串流時讀到 UNPLUGGED，
    關掉之後又自己回到 ACTIVE —— 那是音訊服務還沒把狀態寫回去，不是真的斷線。
    所以這裡改成**持續輪詢並記錄每一次轉變**，用轉變的方向與時序判斷。

    時間統計會明確分開「擷取開始→中斷」與「擷取結束→恢復」兩段，
    因為使用者感受到的是這兩段的長度。
    """
    before = snapshot()
    events: list[str] = []
    prev = before
    t0 = time.time()
    timing: dict = {}

    def watch(mark: bool = False) -> None:
        nonlocal prev
        cur = snapshot()
        for guid, val in cur.items():
            old = prev.get(guid)
            if old != val:
                rel = time.time() - t0
                tag = "（已放開）" if mark else ""
                events.append(f"  t+{rel:5.2f}s {tag}{val}"
                              + (f"   ← 原本 {old}" if old else ""))
        prev = cur

    cap = Capture(device, rate=16000, max_seconds=hold + 6)
    cap.__enter__()
    timing["t_open"] = time.time() - t0
    try:
        while time.time() - t0 < hold:
            time.sleep(poll)
            watch()
    finally:
        cap.__exit__(None, None, None)
    timing["t_close"] = time.time() - t0

    # 放開之後追蹤到耳機回來（或逾時）
    deadline = time.time() + 6.0
    while time.time() < deadline:
        time.sleep(poll)
        watch(mark=True)

    timing["events"] = list(events)
    return before, events, timing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量測開麥克風時藍牙播放是否中斷")
    parser.add_argument("--device", type=int, default=None,
                        help="要開的錄音裝置索引；不指定就測全部藍牙裝置")
    parser.add_argument("--hold", type=float, default=3.0, help="開著幾秒")
    args = parser.parse_args(argv)
    setup_console()

    from record_wav import list_devices
    devs = [(i, n.strip()) for i, n, *_ in list_devices()]
    print("=" * 78)
    print("開麥克風 vs 藍牙播放中斷 —— 端點狀態轉變量測")
    print("=" * 78)
    print("\n本機錄音裝置：")
    for i, n in devs:
        print(f"  [{i}] {n}")

    targets = ([args.device] if args.device is not None
               else [i for i, n in devs if "Headset" in n or "Hands-Fre" in n])

    for dev in targets:
        name = next((n for i, n in devs if i == dev), f"device {dev}")
        print("\n" + "=" * 78)
        print(f"[裝置 {dev}] {name}　開 {args.hold:g} 秒")
        print("=" * 78)
        try:
            before, events, timing = monitor(dev, args.hold)
        except Exception as exc:
            print(f"  ❌ 失敗：{type(exc).__name__}: {exc}")
            continue
        print(f"\n  擷取區間：開 t+{timing['t_open']:.2f}s → 關 t+{timing['t_close']:.2f}s"
              f"（按住 {args.hold:g} 秒）")
        if events:
            print("\n  偵測到的端點狀態轉變：")
            for e in events:
                print(e)
            # 把兩段延遲單獨算出來 —— 使用者感覺到的就是這兩段
            first_cut = next((e for e in events if "UNPLUGGED" in e and "已放開" not in e), None)
            first_back = next((e for e in events
                               if "已放開" in e and "[ACTIVE]" in e), None)
            print()
            if first_cut:
                t = float(first_cut.split("t+")[1].split("s")[0])
                print(f"  按下 → 中斷：約 {(t - timing['t_open']) * 1000:.0f} ms")
            if first_back:
                t = float(first_back.split("t+")[1].split("s")[0])
                print(f"  放開 → 恢復：約 {(t - timing['t_close']) * 1000:.0f} ms"
                      f"　← 這就是你聽到「放開後又斷一下」的長度")
        else:
            print("\n  期間沒有任何端點狀態轉變。")

    print("\n" + "=" * 78)
    print("判讀")
    print("=" * 78)
    print("  · 若某個**其他**藍牙裝置的 render 端點在開麥克風時變 UNPLUGGED、")
    print("    放開後才回來 → 開麥克風把那個裝置整個踢掉。")
    print("    ⚠️ 關鍵：干擾的**不是**「同一支耳機同時當麥克風和喇叭」，")
    print("       而是同一顆藍牙控制器上的資源衝突 —— 實測 AI_VOICE_MAX 錄音")
    print("       會踢掉 vivo TWS，兩者是不同實體裝置。")
    print("  · 若換成 USB 麥克風完全沒有轉變 → 那就用 USB 麥克風，")
    print("    這是唯一能根治的辦法（藍牙協定限制，程式修不掉）。")
    print("  · 沒有轉變也可能是 registry 來不及更新 —— 所以本工具用")
    print("    **連續輪詢抓轉變**，而不是只比對頭尾兩張快照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
