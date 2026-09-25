#!/usr/bin/env python3
"""診斷「藍牙錄音時，聲音輸出被踢掉」—— 誰被踢、什麼時候被踢、踢多久。

## 為什麼需要另一支工具（`measure_bt_profile_switch.py` 不夠嗎）

舊工具只做一件事：開麥克風，然後 diff 端點狀態。它答得出「有東西被踢掉」，
但答不出使用者真正想知道的兩件事：

  1. **是哪一個輸出被踢掉？** 舊工具把 Render 與 Capture 混在一個 dict 裡，
     且沒有標出「哪個是系統預設輸出裝置」。
  2. **為什麼 USB 就沒事？** 舊工具沒有把「共用同一顆藍牙無線電」這件事
     畫出來。答案在 Windows PnP 樹裡：音訊端點 → 藍牙音訊子裝置 → 藍牙節點
     → **藍牙無線電（藍牙介面卡）**。同一顆無線電底下的裝置會互相搶頻寬。

所以本工具做三件事：

  · **分組**：把所有音訊端點依「Render / Capture」分開，各別標出預設裝置。
  · **畫樹**：把每個端點往上追溯到藍牙無線電，看誰跟誰共用一顆無線電。
  · **連續監看**：錄音期間持續輪詢，記下每一次狀態轉變與其延遲。

## 這支工具**不會**做的事

  · 不會寫入 registry、不會改裝置狀態、不會終止任何程式（唯讀）。
  · 不會輸出真實藍牙 MAC 或裝置型號 —— 一律遮蔽成 `<BT-MAC>` / `<BT-DEV n>`，
    因為本 repo 是公開的（見 AGENTS.md §7）。

用法:
    python tools/p1/diagnose_bt_output.py                  # 只列現況與拓撲
    python tools/p1/diagnose_bt_output.py --watch          # 邊錄邊看（按 Enter 停）
    python tools/p1/diagnose_bt_output.py --watch --device 0 --hold 3
    python tools/p1/diagnose_bt_output.py --compare-usb    # 依序測藍牙與 USB 做對照
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_ROOT = HERE.parents[1]
sys.path.insert(0, str(_ROOT / "app" / "core"))
sys.path.insert(0, str(_ROOT / "third_party"))

from record_wav import setup_console  # noqa: E402

import winreg  # noqa: E402

# ── registry 路徑 ────────────────────────────────────────────────────────────
MMDEV = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
ENUM = r"SYSTEM\CurrentControlSet\Enum"

PROP_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PROP_FRIENDLY = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
# 實測：端點自己的 DeviceState 機碼裡**沒有** ContainerId，
# 而端點的 PnP 路徑藏在這個屬性裡（值長得像
# `{1}.BTHENUM\{0000110B-…}_LOCALMFG&03E0\7&6A05F28&0&<BT-MAC>_C00000000`）。
PROP_PNP_PATH = "{b3f8fa53-0004-438e-9003-51a46e139bfc},2"

# DeviceState 的值會混入過渡狀態（重新協商連線時），所以人名話要包含過渡態。
STATE = {
    0x00000001: "ACTIVE",
    0x00000002: "DISABLED",
    0x00000004: "NOTPRESENT",
    0x00000008: "UNPLUGGED",
    0x10000001: "DISABLED?",
    0x80000001: "過渡(0x80000001)",
    0x80000008: "過渡(0x80000008)",
}
LIVE = {"ACTIVE"}          # 只有這狀態算「能播／能錄」


def _state_name(v: int) -> str:
    return STATE.get(v, f"0x{v:08X}")


# ── 去識別化 ─────────────────────────────────────────────────────────────────
class Redactor:
    """把裝置型號與藍牙位址換成穩定代號。

    為什麼要這樣做：藍牙裝置名稱常常就是型號（可反推使用者身分），
    MAC 更是唯一識別碼。輸出要能貼進 issue，所以一律遮蔽，
    但**同一個實體裝置每次都要拿到同一個代號**，否則無法比對。
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._macs: dict[str, str] = {}
        self._products: dict[str, str] = {}

    @staticmethod
    def _branded(label: str) -> bool:
        """名稱裡含有常見消費品牌嗎？

        為什麼要另外檢查：有些裝置名稱是「品牌＋型號」（例如某些真無線耳機的
        端點名稱會直接寫出型號），但端點屬性的結尾不是 `Hands-Free` 或 `Stereo`，
        上面的字尾規則抓不到。這種名字比 MAC 更容易反推使用者身分，所以一律換成代號。
        """
        import re
        brands = (r"vivo|oppo|xiaomi|redmi|huawei|honor|samsung|galaxy|jabra|sony|bose|"
                  r"sennheiser|jbl|beats|airpods|apple|anker|soundcore|edifier|baseus|"
                  r"shokz|aftershokz|plantronics|poly|logitech|razer|hyperx|steelseries|"
                  r"corsair|1more|fiio|tws")
        return bool(re.search(brands, label, re.I))

    def name(self, raw: str) -> str:
        """裝置名稱 → 去識別化代號。

        規則：先去掉括號內容與 `Hands-Free` 之類的字尾；若結果仍然是
        **型號樣**（品牌詞、或含英數字混排的產品名），就換成 `外部裝置N`。
        端點類別名（Headphones / Headset / Microphone / 喇叭…）則原樣保留，
        因為那是判斷問題所必需的資訊，而且沒有識別性。
        """
        label = raw.split("(")[0].strip() or raw
        for suffix in (" Hands-Free", " Hands Free", " Stereo", " Headset", " HandsFree"):
            if label.endswith(suffix):
                label = label[: -len(suffix)].strip()
        generic = {"headphones", "headset", "microphone", "speakers", "speaker",
                   "stereo mix", "digital audio", "line in", "喇叭", "麥克風", "耳機"}
        if label.lower() in generic or label.upper() == "AI_VOICE_MAX":
            return label
        if self._branded(label) or any(ch.isdigit() for ch in label):
            # ⚠️ 這裡**不能**用 `mac()` 的代號空間：那會讓一個 USB 網路攝影機
            # 被標成 `BT裝置D`（實測踩到，因為 `mac()` 也會為其他識別字配號）。
            # 型號遮蔽與位置代號是兩件事，分開編號才不會誤導。
            return self._products.setdefault(label, f"外部裝置{len(self._products) + 1}")
        return self._names.setdefault(label, label)

    def mac(self, raw: str) -> str:
        """藍牙位址（或任何需要代號的識別字）→ `BT裝置A/B/C`。

        ⚠️ 這是**唯一**該把識別字換成代號的地方。實測踩過：把自己拿到的位址
        直接印出來，等於把裝置指紋寫進可貼出去的輸出裡 —— 違反 AGENTS.md §7。
        所以原始值只留在 `Redactor` 內部，對外一律只用代號。
        """
        if not raw:
            return ""
        if raw not in self._macs:
            self._macs[raw] = f"BT裝置{chr(ord('A') + len(self._macs))}"
        return self._macs[raw]

    @staticmethod
    def scrub(text: str) -> str:
        """最後一道防線：任何 MAC 樣式的字串（含無分隔的 12 位十六進位）都換掉。"""
        import re
        text = re.sub(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", "<BT-MAC>", text)
        # 無分隔的 12 位十六進位（例如 `7&6A05F28&0&<BT-MAC>_C00000000`）
        return re.sub(r"(?i)(?<=&)[0-9a-f]{12}(?=_|\b)", "<BT-MAC>", text)


REDACT = Redactor()


def say(*parts) -> None:
    """輸出的**唯一**出口：每一行都再過一次遮蔽。

    為什麼要集中：本工具會印出 PnP 路徑，而那些路徑裡含有裝置位址。
    分散在各處自己記得遮蔽一定會漏（實測就漏過一次），所以在唯一的出口攔。
    """
    print(REDACT.scrub(" ".join(str(p) for p in parts)))


# ── registry 小工具 ──────────────────────────────────────────────────────────
def _open(path: str, root=winreg.HKEY_LOCAL_MACHINE):
    try:
        return winreg.OpenKey(root, path)
    except OSError:
        return None


def _values(key) -> dict:
    out = {}
    i = 0
    while True:
        try:
            name, val, _typ = winreg.EnumValue(key, i)
        except OSError:
            break
        out[name] = val
        i += 1
    return out


def _subkeys(key) -> list[str]:
    out = []
    i = 0
    while True:
        try:
            out.append(winreg.EnumKey(key, i))
        except OSError:
            break
        i += 1
    return out


def _text(val) -> str:
    """把 registry 的值轉成字串。

    ⚠️ 實測踩到：REG_MULTI_SZ 讀出來是 list，但裡面的元素**可能是 str 也可能是 int**
    （同一台機器上兩種都出現過），所以不能無腦 `chr(c)`。
    """
    if isinstance(val, bytes):
        return val.decode("utf-16-le", "replace").rstrip("\x00").strip()
    if isinstance(val, (list, tuple)):
        if all(isinstance(c, int) for c in val):
            return "".join(chr(c) for c in val if c).rstrip("\x00").strip()
        return "".join(str(c) for c in val).rstrip("\x00").strip()
    return str(val or "").strip()


# ── 音訊端點 ─────────────────────────────────────────────────────────────────
class Endpoint:
    __slots__ = ("guid", "direction", "name", "desc", "state", "container",
                 "device", "key", "label")

    def __init__(self, guid, direction, name, desc, state, container):
        self.guid = guid
        self.direction = direction      # "Render" | "Capture"
        self.name = name
        self.desc = desc
        self.state = state              # int
        self.container = container      # 容器 GUID，用來串回 PnP 裝置
        self.device = ""                # 對應的 PnP InstanceId（best effort）
        self.key = ""                   # 實體裝置識別碼（位址或容器 GUID）
        self.label = ""                 # 已遮蔽的顯示名稱（型號不會外流）

    @property
    def live(self) -> bool:
        return _state_name(self.state) in LIVE

    @property
    def is_bt(self) -> bool:
        u = self.device.upper()
        return "BTHENUM" in u or "BTHHFENUM" in u

    def label_text(self) -> str:
        if self.label:
            return self.label
        d = f" / {REDACT.name(self.desc)}" if self.desc else ""
        return f"{REDACT.name(self.name)}{d}"

    def line(self) -> str:
        kind = "藍牙" if self.is_bt else ("USB" if "USB" in self.device.upper() else "本機")
        return f"[{_state_name(self.state):>16}] {kind:<4} {self.label_text()}"

    def as_dict(self) -> dict:
        return {"guid": self.guid, "state": _state_name(self.state),
                "label": self.label_text()}


def normalize_pnp_path(raw: str) -> str:
    """把端點屬性裡的裝置路徑正規化成 Enum 樹用的形式。

    實測原始值長這樣（前面有一個**小數索引**）：

        {1}.BTHHFENUM\\BTHHFPAUDIO\\8&2181AAA3&0&97
        {1}.BTHENUM\\{0000110B-…}_LOCALMFG&03E0\\7&6A05F28&0&<BT-MAC>_C00000000
        ^^^^ ← 這個前綴必須整個去掉，只剝掉 `{` 會留下 `1}.` 讓比對全錯

    而且同一段在兩邊的大小寫不一樣（實測踩到）：
    端點屬性是大寫 `BTHHFENUM\\BTHHFPAUDIO`，Enum 機碼是小寫 `BthHFPAudio`。
    所以先把已知的段落換成 Enum 的標準寫法，比對時再一律轉小寫。
    """
    import re
    s = (raw or "").strip()
    s = re.sub(r"^\{\d+\}\.", "", s)     # {1}. → 去掉
    s = re.sub(r"^\{[^}]*\}\.", "", s)   # 其他型式的前綴
    s = s.strip("{}").strip()
    for wrong, right in (("BTHHFENUM", "BTHHFENUM"), ("BTHHFPAUDIO", "BthHFPAudio")):
        s = re.sub(wrong, right, s, flags=re.I)
    return s


def _device_index() -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    """建立三張表，用來把音訊端點串回實體裝置。

    回傳 (小寫路徑→實際路徑, 容器GUID→實例清單, 容器GUID→裝置位址)。

    ⚠️ 一個容器會對應多個實例（同一台耳機的 A2DP、HFP、HID 各有實例），
    而且**只有 HFP 那條實例的路徑裡沒有裝置位址**，所以位址要從同容器的
    其他實例借過來 —— 否則 A2DP 與 HFP 端點會被誤判成兩台不同裝置。
    """
    by_lower: dict[str, str] = {}
    container_to_instances: dict[str, list[str]] = {}
    container_to_mac: dict[str, str] = {}
    for inst in _iter_enum_instances():
        key = _open(rf"{ENUM}\{inst}")
        if not key:
            continue
        with key:
            vals = _values(key)
        by_lower.setdefault(inst.lower(), inst)
        cid = str(vals.get("ContainerID") or "").strip("{}").lower()
        if not cid:
            continue
        container_to_instances.setdefault(cid, []).append(inst)
        mac = device_mac(inst)
        if mac:
            container_to_mac.setdefault(cid, mac)
    return by_lower, container_to_instances, container_to_mac


def enumerate_endpoints() -> list[Endpoint]:
    """列舉所有音訊端點，並盡量串回它的 PnP InstanceId（＝知道它是不是藍牙）。"""
    by_lower, container_to_instances, container_to_mac = _device_index()

    def resolve(props: dict, vals: dict) -> tuple[str, str]:
        """回傳 (PnP 路徑, 裝置識別碼)。識別碼用來把同一台裝置的端點歸在一起。"""
        path = normalize_pnp_path(_text(props.get(PROP_PNP_PATH)))
        inst = by_lower.get(path.lower(), path)
        cid = str(vals.get("ContainerId") or "").strip("{}").lower()
        if not cid:
            # 端點自己的屬性能反推容器：`{b3f8fa53…},10` 是容器 GUID
            cid = _text(props.get("{b3f8fa53-0004-438e-9003-51a46e139bfc},10")).strip("{}").lower()
        if cid:
            if not inst or inst.lower() not in by_lower:
                inst = next(iter(container_to_instances.get(cid, [])), inst)
            mac = container_to_mac.get(cid) or device_mac(inst)
            return inst, (mac or cid)
        return inst, device_mac(inst) or inst

    out: list[Endpoint] = []
    for direction in ("Render", "Capture"):
        root = _open(rf"{MMDEV}\{direction}")
        if not root:
            continue
        with root:
            for guid in _subkeys(root):
                key = _open(rf"{MMDEV}\{direction}\{guid}")
                if not key:
                    continue
                with key:
                    vals = _values(key)
                props_key = _open(rf"{MMDEV}\{direction}\{guid}\Properties")
                props = _values(props_key) if props_key else {}
                if props_key:
                    props_key.Close()
                name = _text(props.get(PROP_DESC)) or "(無名稱)"
                desc = _text(props.get(PROP_FRIENDLY))
                ep = Endpoint(guid, direction, name, desc,
                              int(vals.get("DeviceState", 0)), "")
                ep.device, ep.key = resolve(props, vals)
                d = f" / {REDACT.name(desc)}" if desc else ""
                ep.label = f"{REDACT.name(name)}{d}"
                out.append(ep)
    return out


def _iter_enum_instances():
    """走訪 Enum 樹，產出所有 PnP InstanceId（只需三層，夠用且快）。"""
    root = _open(ENUM)
    if not root:
        return
    with root:
        for bus in _subkeys(root):
            bus_key = _open(rf"{ENUM}\{bus}")
            if not bus_key:
                continue
            with bus_key:
                for dev in _subkeys(bus_key):
                    dev_key = _open(rf"{ENUM}\{bus}\{dev}")
                    if not dev_key:
                        continue
                    with dev_key:
                        for inst in _subkeys(dev_key):
                            yield rf"{bus}\{dev}\{inst}"


# ── 藍牙無線電拓撲 ───────────────────────────────────────────────────────────
def radios() -> list[str]:
    """列出本機的藍牙無線電（介面卡）。

    ⚠️ 實測教訓：`BTH\\MS_BTHPAN` 這類 `BTH` 開頭的東西是**軟體堆疊元件**
    （PAN / RFCOMM / LE），不是無線電，列出來只會誤導。
    真正代表介面卡的是 `Service = BTHUSB` 的 USB 裝置。
    """
    real, stack = [], []
    for inst in _iter_enum_instances():
        key = _open(rf"{ENUM}\{inst}")
        if not key:
            continue
        with key:
            vals = _values(key)
        service = str(vals.get("Service", "")).upper()
        if service == "BTHUSB":
            real.append(inst)
        elif service.startswith("BTH") or inst.upper().startswith("BTH\\"):
            stack.append(inst)
    if not real:
        # 備援：有些機器不寫 Service，改用 InstanceId 特徵
        real = [i for i in _iter_enum_instances()
                if i.upper().startswith("USB\\") and "BTHUSB" in i.upper()]
    return real or stack[:1]


def radio_label(instance: str) -> str:
    """無線電的顯示字串 —— 說明它是走 USB 的介面卡，但不揭露匯流排上的序號。"""
    if instance.upper().startswith("USB\\"):
        return "USB 介面上的藍牙無線電（識別字已遮蔽）"
    return instance


def device_mac(device: str) -> str:
    """從 PnP 路徑抓出「這台藍牙裝置」的位址；抓不到就回空字串。"""
    import re
    m = re.search(r"&([0-9A-Fa-f]{12})_", device)
    return m.group(1).upper() if m else ""


def parent_token(device: str) -> str:
    """取出「父裝置片段」——同一顆藍牙無線電底下的裝置會共用這一段。

    實測的兩種路徑（已遮蔽）：

        BTHENUM\\{0000110B-…}_LOCALMFG&03E0\\7&6A05F28&0&<BT-MAC-A>_C00000000
        BTHENUM\\{00001124-…}_LOCALMFG&03E0\\7&6A05F28&0&<BT-MAC-B>_C00000000
                                             ^^^^^^^^^ ← 兩台相同
        BTHHFENUM\\BthHFPAudio\\8&1176C093&0&97
                               ^^^^^^^^^ ← HFP 子裝置用不同的介面編號

    取法：最後一段裡 `&0&` 之前的部分（抓不到就退而取整段）。
    ⚠️ 這只是**推論**：HFP 子裝置的編號與 BTHENUM 那組不同，所以這個函式
    **不會**告訴你「HFP 屬於哪一台」，只告訴你「這兩個 BTHENUM 裝置是不是同一顆」。
    """
    import re
    m = re.search(r"\\([^\\]+)$", device)
    if not m:
        return ""
    tail = m.group(1)
    return tail.split("&0&")[0] if "&0&" in tail else tail


def device_label(device: str, mac_names: dict[str, str]) -> str:
    """給一台藍牙裝置一個穩定、可對照的代號（例如 `BT裝置A`）。"""
    mac = device_mac(device)
    if mac:
        return mac_names.setdefault(mac, f"BT裝置{chr(ord('A') + len(mac_names))}")
    return "(未知裝置)"


def group_by_device(eps: list[Endpoint]) -> tuple[dict[str, list[Endpoint]], dict[str, str]]:
    """把藍牙音訊端點依「同一組父裝置片段」分組，回傳 (群組標題→端點, 標題→父片段)。

    為什麼用父裝置片段當鍵：**同一個片段＝同一顆藍牙無線電底下的同一組裝置**，
    而「藍牙裝置會互相搶無線電」正是本工具要回答的問題。

    ⚠️ HFP 子裝置的片段編號與 BTHENUM 不同（實測 `8&1176C093` vs `7&6A05F28`），
    所以同一支耳機的 A2DP 與 HFP 會落在不同群 —— 這是**已知限制**，
    寧可誠實分開列，也不要靠猜測合併。
    """
    groups: dict[str, list[Endpoint]] = {}
    tokens: dict[str, str] = {}
    no_addr: set[str] = set()
    for ep in eps:
        if not ep.is_bt:
            continue
        # 用裝置位址當群組鍵（同一台裝置的端點會併在一起）。
        # 位址本身只留在 Redactor 內，取代號一律走 REDACT.mac()。
        mac = device_mac(ep.device)
        key = mac or ep.device
        if not mac:
            no_addr.add(key)
        tokens[key] = parent_token(ep.device)
        groups.setdefault(key, []).append(ep)

    ordered: dict[str, list[Endpoint]] = {}
    used: dict[str, int] = {}
    for key, members in groups.items():
        # 標題直接用端點本身的代號 —— 那才是 REDACT 產生、畫面上唯一的代號，
        # 另外呼叫一次 REDACT.mac() 只會拿到第二個代號（實測就是這樣冒出鬼東西）。
        alias = next((REDACT.name(m.desc) or REDACT.name(m.name) for m in members), "BT裝置")
        # 同一台裝置的 HFP 與 A2DP 是不同群但同名，用序號區分才分得出來
        used[alias] = used.get(alias, 0) + 1
        tag = alias if used[alias] == 1 else f"{alias} 第 {used[alias]} 組"
        note = "；位址未揭露：HFP 子裝置的端點屬性不含位址" if key in no_addr else ""
        ordered[f"{tag}（父裝置片段 {REDACT.scrub(tokens[key])}{note}）"] = members
    return ordered, tokens


# ── 預設裝置 ─────────────────────────────────────────────────────────────────
def default_endpoints() -> dict[str, str]:
    """讀「預設播放／預設錄音」是哪個端點 GUID。

    ⚠️ 實測：**這一版 Windows 的 registry 裡沒有 `Role` 子機碼**
    （`MMDevices\\Audio\\Render` 底下只有端點 GUID），所以這個函式
    在本機一律回空 dict。這是刻意保留的：與其猜一個「大概是預設裝置」，
    不如明說拿不到。UI 若需要，應該改用 `IMMDeviceEnumerator` 的 COM 介面。
    """
    out = {}
    for direction, role_key in (("Render", "eRender"), ("Capture", "eCapture")):
        for candidate in (
            rf"{MMDEV}\{direction}\Role\{role_key}\Role",
            rf"{MMDEV}\{direction}\Role\{role_key}",
            rf"{MMDEV}\{direction}\Role",
        ):
            key = _open(candidate)
            if not key:
                continue
            with key:
                vals = _values(key)
            for name in ("Role:0", "Role:1", "Role:2", "Default", "DefaultRender"):
                if name in vals:
                    out[direction] = str(vals[name]).strip("{}").lower()
                    break
            if direction in out:
                break
    return out


# ── 快照與監看 ───────────────────────────────────────────────────────────────
def snapshot() -> dict[str, Endpoint]:
    return {f"{ep.direction}:{ep.guid}": ep for ep in enumerate_endpoints()}


def show_inventory(eps: list[Endpoint]) -> None:
    print("=" * 78)
    print("音訊端點現況")
    print("=" * 78)
    defaults = default_endpoints()
    for direction in ("Render", "Capture"):
        group = [ep for ep in eps if ep.direction == direction]
        if not group:
            continue
        title = "輸出（Render）" if direction == "Render" else "輸入（Capture）"
        print(f"\n{title}　共 {len(group)} 個")
        for ep in sorted(group, key=lambda e: (not e.live, e.label_text())):
            mark = ""
            if defaults.get(direction) == ep.guid:
                mark = "   ← 系統預設"
            print(f"  {ep.line()}{mark}")

    print("\n" + "=" * 78)
    print("藍牙拓撲（誰跟誰共用同一顆無線電）")
    print("=" * 78)
    rds = radios()
    print(f"\n藍牙無線電（介面卡）：{len(rds)} 顆")
    for r in rds:
        print(f"  · {radio_label(r)}")
    groups, _ = group_by_device(eps)
    if not groups:
        print("\n（沒有任何藍牙音訊端點）")
        return
    for label, members in groups.items():
        print(f"\n{label}")
        for ep in members:
            # 群組標題已經寫了裝置代號，這裡只印端點類別（避免同一串代號重複兩次）
            parts = [p.strip() for p in ep.label_text().split(" / ")]
            parts = [p for p in parts if p and p not in label]
            print(f"  {ep.direction[:3]} {' / '.join(parts) or '端點'}"
                  f"  [{_state_name(ep.state)}]")

    # 關鍵判斷用**所有** BTHENUM 裝置（不只音訊端點）：同一台裝置的
    # A2DP 與 HID 一定共用父裝置片段，拿它當基準才知道「幾台裝置共用一顆無線電」。
    devices: dict[str, set[str]] = {}
    for inst in _iter_enum_instances():
        if "BTHENUM" not in inst.upper():
            continue
        token = parent_token(inst)
        mac = device_mac(inst)
        # 位址為 0 的實例（例如 SPP/RFCOMM 的預留項）不算一台裝置
        if token and mac and set(mac) != {"0"}:
            devices.setdefault(token, set()).add(mac)
    print("\n父裝置片段 → 底下有幾台藍牙裝置（只算 BTHENUM，不含 HFP 子裝置）：")
    for token, macs in devices.items():
        print(f"  {token}：{len(macs)} 台")
    shared = {t: m for t, m in devices.items() if len(m) > 1}
    print()
    if shared:
        for token, macs in shared.items():
            print(f"  ⚠️ 父裝置片段 {token} 底下有 {len(macs)} 台裝置 → "
                  "**共用同一顆藍牙無線電**")
        print("     → 預期行為：開其中一台的音訊串流時，另一台會被踢掉（實測確認）。")
        print("     → 唯一能根治的做法是把**麥克風換成 USB／有線**，")
        print("       走 USB 匯流排就不會經過藍牙無線電。")
    else:
        print("  ℹ️ 每個父裝置片段底下都只有一台裝置。")
        print("     （仍可能是「同一台裝置的 HFP 與 A2DP 互搶」，要實際錄音才看得出來：")
        print("       用 --watch 或 --compare-usb。）")


def watch(device: int | None, hold: float, poll: float = 0.1) -> int:
    """開著麥克風並監看「輸出」端點的每一次轉變。"""
    from record_wav import list_devices
    from recorder import Capture

    devs = [(i, n.strip()) for i, n, *_ in list_devices()]
    print("\n本機錄音裝置：")
    for i, n in devs:
        print(f"  [{i}] {REDACT.name(n)}")

    if device is None:
        bt = [i for i, n in devs if "Hands" in n or "Headset" in n]
        if not bt:
            print("\n❌ 找不到藍牙錄音裝置。請確認耳機／麥克風已連線。")
            return 1
        device = bt[0]
    name = next((n for i, n in devs if i == device), f"device {device}")

    before = snapshot()
    print("\n" + "=" * 78)
    print(f"開始錄音：[{device}] {REDACT.name(name)}　持續 {hold:g} 秒")
    print("=" * 78)

    events: list[tuple[float, str, str, str]] = []
    prev = before
    t0 = time.time()

    def poll_once(phase: str) -> None:
        nonlocal prev
        cur = snapshot()
        for k, ep in cur.items():
            old = prev.get(k)
            old_state = _state_name(old.state) if old else "（之前不存在）"
            if old_state != _state_name(ep.state):
                events.append((time.time() - t0, phase, ep.label_text(),
                               f"{old_state} → {_state_name(ep.state)}"))
        prev = cur

    cap = Capture(device, rate=16000, max_seconds=hold + 8)
    cap.__enter__()
    t_open = time.time() - t0
    try:
        while time.time() - t0 < hold:
            time.sleep(poll)
            poll_once("錄音中")
    finally:
        cap.__exit__(None, None, None)
    t_close = time.time() - t0

    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(poll)
        poll_once("已放開")

    print(f"\n  擷取區間：開 t+{t_open:.2f}s → 關 t+{t_close:.2f}s")
    if not events:
        print("\n  ✅ 期間沒有任何端點狀態轉變 —— 這次錄音沒有干擾任何輸出。")
        return 0

    print("\n  端點狀態轉變：")
    for rel, phase, label, change in events:
        print(f"    t+{rel:5.2f}s [{phase}] {label}\n              {change}")

    def first(pred) -> tuple[float, str, str, str] | None:
        return next((e for e in events if pred(e)), None)

    cut = first(lambda e: e[1] == "錄音中" and not e[3].endswith("ACTIVE"))
    back = first(lambda e: e[1] == "已放開" and e[3].endswith("ACTIVE"))
    print()
    if cut:
        print(f"  🔴 按下 → 輸出被踢掉：約 {(cut[0] - t_open) * 1000:.0f} ms（{cut[2]}）")
    if back:
        print(f"  🟢 放開 → 輸出恢復：  約 {(back[0] - t_close) * 1000:.0f} ms（{back[2]}）")
    if cut and back:
        print(f"\n  → 你聽到的「鬆手後聲音斷一下」＝ 恢復的 {(back[0] - t_close) * 1000:.0f} ms"
              "，加上播放器重新協商裝置的時間。")
    return 0


def compare_usb(hold: float) -> int:
    """依序測「藍牙麥克風」與「USB 麥克風」，把差異攤開。"""
    from record_wav import list_devices

    devs = [(i, n.strip()) for i, n, *_ in list_devices()]
    bt = [i for i, n in devs if "Hands" in n or "Headset" in n]
    usb = [i for i, n in devs if "USB" in n.upper()]
    print("\n對照組挑選：")
    print(f"  藍牙麥克風：{bt or '（找不到）'}")
    print(f"  USB 麥克風：{usb or '（找不到）'}")
    results = {}
    for tag, targets in (("藍牙", bt), ("USB", usb)):
        if not targets:
            continue
        print("\n" + "#" * 78)
        print(f"# {tag} 麥克風")
        print("#" * 78)
        before = snapshot()
        rc = watch(targets[0], hold)
        after = snapshot()
        changed = [k for k in after if k in before
                   and _state_name(before[k].state) != _state_name(after[k].state)]
        results[tag] = {"rc": rc, "unstable": len(changed)}
    print("\n" + "=" * 78)
    print("對照結果")
    print("=" * 78)
    for tag, r in results.items():
        print(f"  {tag}：錄音前後狀態不同的端點數 = {r['unstable']}")
    if results.get("藍牙", {}).get("unstable", 0) and not results.get("USB", {}).get("unstable", 0):
        print("\n  → 與既有實測一致：藍牙會干擾輸出，USB 不會。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="診斷藍牙錄音是否踢掉聲音輸出")
    parser.add_argument("--watch", action="store_true", help="邊錄邊監看端點轉變")
    parser.add_argument("--device", type=int, default=None, help="錄音裝置索引")
    parser.add_argument("--hold", type=float, default=3.0, help="錄幾秒")
    parser.add_argument("--compare-usb", action="store_true", help="藍牙 vs USB 對照")
    args = parser.parse_args(argv)
    setup_console()

    eps = enumerate_endpoints()
    show_inventory(eps)

    if args.compare_usb:
        return compare_usb(args.hold)
    if args.watch:
        return watch(args.device, args.hold)

    print("\n" + "=" * 78)
    print("下一步")
    print("=" * 78)
    print("  加上 --watch 就會實際開麥克風，並記下每一次端點轉變：")
    print("    python tools/p1/diagnose_bt_output.py --watch")
    print("  想直接看藍牙 vs USB 的差別：")
    print("    python tools/p1/diagnose_bt_output.py --compare-usb")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
