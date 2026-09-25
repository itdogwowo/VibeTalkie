# P0 硬體驗證工具

目的：把 [`docs/hardware.md`](../../docs/hardware.md) §4 的四個假設從「推測」變成「量測結果」。
**在這些結果定案之前，不要開始寫正式產品程式碼。**

全部工具都**不需要 pip install**（只用 Python 標準函式庫 + ctypes + 已安裝的 numpy）。
只支援 Windows。

---

## 對應關係

| 測試 | 假設 | 工具 |
|---|---|---|
| T1 | 麥克風以標準藍牙音訊輸入裝置出現 | `enumerate_audio.ps1` / `record_wav.py --list` |
| T2 | 支援 16 kHz 或可被重採樣 | `record_wav.py --probe` + **實際錄音的頻譜分析** |
| T3 | 按鍵是 HID 鍵盤，送得出鍵碼 | `keycode_logger.py` |
| T4 | 裝置本身不會自行打字 | 手動（見下方） |

---

## 執行順序

### T1 — 列舉裝置

```powershell
pwsh -File tools/p0/enumerate_audio.ps1
pwsh -File tools/p0/enumerate_audio.ps1 -Filter AI_VOICE
python tools/p0/record_wav.py --list
```

**通過條件：** 看得到目標麥克風，且 `StateText = ACTIVE`。

> 藍牙 HFP 端點**只在裝置連線時存在**。裝置關機就不會出現，這不是 bug。

### T2 — 取樣率與**真實頻寬**

```powershell
# 1) 看驅動接受哪些取樣率（注意：接受 ≠ 真的有那個頻寬）
python tools/p0/record_wav.py --probe --device 1

# 2) 真正重要的：實際錄音，讓工具做頻譜分析
python tools/p0/record_wav.py --device 1 --seconds 6 --rate 16000
```

錄音時請做兩件事：

1. 說一句中文（例：「今天天氣很好」）
2. **拉長音發「ㄙ～～」或噓聲**

第二步是關鍵。「ㄙ」含大量高頻，而 8 kHz 窄頻連結**不可能**傳遞 4 kHz 以上的能量。
所以：

| 頻譜結果 | 意義 |
|---|---|
| 能量只到 ~4 kHz | 底層是 **8 kHz 窄頻（CVSD）** → 中文辨識率會受影響 |
| 能量一路到 8 kHz（Nyquist） | 來源**沒有**被 8 kHz 限制 → 至少是 16 kHz 寬頻 |

⚠️ **為什麼不能只看 `--probe`：** Windows 藍牙音訊堆疊會把 8 kHz 窄頻連結
重採樣成你要求的任何取樣率。所以要 48 kHz 它也「接受」，
但資料裡 4 kHz 以上全是空的。**只有頻譜分析能分辨。**

### T3 — 鍵碼（✅ 已完成，結果見 `hardware.md` §3.2）

```powershell
# 先看裝置暴露了哪些 HID collection
python tools/p0/keycode_logger.py --list-devices --filter 00001124

# 監聽 30 秒
python tools/p0/keycode_logger.py --seconds 30
```

> 藍牙 HID 裝置的 interface path **不含裝置名稱**，只有 HID UUID，
> 所以用品牌名過濾抓不到 → 請用 `--filter 00001124`。

#### 嚴格協定

1. **先關閉原廠工具**「閃電說」（見 T4b），以取得裝置原生行為。
2. 執行監聽指令，**讓游標停在終端機視窗**。
3. **手離開鍵盤，不要打任何字。**
4. 每顆按鈕各按 **3 次**（按住約 1 秒再放開，間隔約 2 秒）。
5. ⚠️ **擷取開始前，確認沒有任何裝置按鍵是按住狀態** ——
   Raw Input 只回報**狀態變化**，若開始時鍵已按住，會漏掉初始的 DOWN
   （這是第一次擷取「幽靈 Ctrl」的真正原因）。

結束後工具會自動印出**摘要（已折疊自動重複）**。
判準：**每顆按鈕按 3 次，摘要就該顯示 3 次按下。**

要重新分析舊紀錄（不必重錄）：`--analyze artifacts/t3-keycodes.jsonl`

#### 實測結果（7 顆按鈕）

| 按鈕 | 鍵 | 備註 |
|---|---|---|
| 上／下／左／右 | `VK_UP` / `VK_DOWN` / `VK_LEFT` / `VK_RIGHT` | 標準鍵盤鍵 |
| 確認／返回 | `VK_RETURN` / `VK_BACK` | 標準鍵盤鍵 |
| **🎤 錄音** | **`VK_RCONTROL`（Right Ctrl）** | `make=29`，Raw Input 用 `E0` 旗標區分左右 |

**沒有任何 consumer control 或廠商自訂 collection 事件**，也沒有軟體注入。

#### 判讀對照表（保留供未來換裝置時使用）

| 看到的東西 | 意義 | 熱鍵層要用的 API |
|---|---|---|
| `[rawkb]` 事件，`page=1 usage=6` | 按鈕是**標準鍵盤鍵** | 一般全域熱鍵即可 |
| `[rawhid]` 事件，`page=12 usage=1` | 按鈕在 **Consumer Control** | 一般熱鍵**可能失效** → 需 Raw Input |
| 只有 `[rawhid]` 事件，其他 page | 按鈕在**廠商自訂 collection** | 標準鍵盤 API **完全看不到** → 只能用 HID API |
| 完全沒有事件 | 按鈕不是 HID，或走 SPP/廠商通道 | 需重新設計架構 |
| 鍵碼是 `VK_CONTROL` / `VK_SHIFT` / `VK_MENU` | 按鈕是**修飾鍵** | 現成熱鍵函式庫**不支援單獨修飾鍵**，且與 `Ctrl+V` 注入有競態 |



### T4 — 行為驗證（**P0 已改寫結論**）

原本的問法是「裝置會不會自己打字」。P0 已確認答案：
**裝置只送按鍵、不會自己打字**；會打字的是原廠工具（`hardware.md` §8）。

#### T4a — 導覽鍵「應該」要漏進前景視窗（確認用，**不是問題**）

> ⚠️ 這一項先前被判定為「架構關鍵風險」，**已更正**。方向鍵／Enter／Backspace
> 漏進前景視窗是**正確行為** —— 那是使用者刻意要的操作，**不該抑制**。

1. 開記事本，打幾個字，游標放中間
2. 按裝置的 **Backspace** 按鈕一次 → **應該要刪掉一個字**（正確）
3. 按 **方向鍵** → **游標應該要移動**（正確）

| 結果 | 意義 |
|---|---|
| 按鍵有正常作用 | ✅ 正確 —— 導覽鍵**不要**在熱鍵層吃掉 |
| 按鍵完全沒作用 | ⚠️ 有東西吃掉了按鍵（很可能是閃電說）→ 關閉它之後應恢復 |

#### T4b — 原廠工具的角色（**決定性實驗**）

先關閉原廠工具（含自動啟動）：

```powershell
Stop-Process -Name shandianshuo -Force
Remove-ItemProperty -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run' -Name '闪电说'
```

然後重跑 T3，每顆按鈕各按 3 次。

> ✅ **此實驗已於 2026-09 執行完畢：** 關閉後錄音鍵**仍然**送出 Right Ctrl
> （`injected=False`）→ **裝置原生行為，不依賴原廠工具。**
> 結論見 `hardware.md` §3.2–3.3。

**要復原自動啟動**（測試完請自行決定是否還原）：

```powershell
Set-ItemProperty -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run' `
  -Name '闪电说' -Value "$env:LOCALAPPDATA\Shandianshuo\shandianshuo.exe --autostart"
```



---

## 產出物

| 路徑 | 內容 | 是否 commit |
|---|---|---|
| `artifacts/t2-*.wav` | 真實錄音 | ❌ **絕不**（已 gitignore） |
| `artifacts/t3-keycodes.jsonl` | 原始按鍵事件 | ❌ **絕不**（已 gitignore） |
| `docs/hardware.md` | **只放去識別化的結論** | ✅ |

**隱私：** 錄音內容、裝置 MAC、其他藍牙裝置名稱都不得進入版控。詳見 `AGENTS.md` §7。
