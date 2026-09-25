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

### T3 — 鍵碼（最重要的一項）

```powershell
# 先看裝置暴露了哪些 HID collection
python tools/p0/keycode_logger.py --list-devices --filter 00001124

# 監聽 30 秒
python tools/p0/keycode_logger.py --seconds 30
```

> 藍牙 HID 裝置的 interface path **不含裝置名稱**，只有 HID UUID，
> 所以用品牌名過濾抓不到 → 請用 `--filter 00001124`。

#### 嚴格協定（第一次擷取失敗的原因）

第一次擷取時有其他打字混入，導致無法判斷哪個事件是「按鈕」。
請照這個協定做：

1. 執行上面的監聽指令，**讓游標停在終端機視窗**。
2. **手離開鍵盤，不要打任何字。**
3. 只按裝置按鈕 **3 次**：每次**按住約 1 秒**再放開，間隔約 2 秒。
4. 等它自己結束（或按 Ctrl+C 提前結束 —— 但這會多一組 Ctrl 事件，可忽略）。

結束後工具會自動印出**摘要（已折疊自動重複）**。
判準：**你按 3 次，摘要就該顯示 3 次按下**。是 3 就定案；不是 3 就代表協定被污染。

要重新分析舊紀錄（不必重錄）：

```powershell
python tools/p0/keycode_logger.py --analyze artifacts/t3-keycodes.jsonl
```

**怎麼讀結果：**

| 看到的東西 | 意義 | 熱鍵層要用的 API |
|---|---|---|
| `[rawkb]` 事件，`page=1 usage=6` | 按鈕是**標準鍵盤鍵** | 一般全域熱鍵即可（`global-hotkey` / `pynput`） |
| `[rawhid]` 事件，`page=12 usage=1` | 按鈕在 **Consumer Control** | 一般熱鍵**可能失效** → 需 Raw Input |
| 只有 `[rawhid]` 事件，其他 page | 按鈕在**廠商自訂 collection** | 標準鍵盤 API **完全看不到** → 只能用 HID API |
| 完全沒有事件 | 按鈕不是 HID，或走 SPP/廠商通道 | 需重新設計架構（見 hardware.md 的架構分支） |
| 鍵碼是 `VK_CONTROL` / `VK_SHIFT` / `VK_MENU` | 按鈕是**修飾鍵** | 三棧的現成熱鍵函式庫**都不支援**，且與 `Ctrl+V` 注入衝突 |

**記錄重點（回填 `hardware.md`）：** 按下與放開是否都收得到？
按鈕在哪個 collection？鍵碼是什麼？按下次數是否等於實際次數？


### T4 — 誰在打字？（**P0 已改寫**）

原本的問法是「裝置會不會自己打字」。P0 已確認真正的答案在
[`../../docs/hardware.md`](../../docs/hardware.md) §8：
**不是裝置，是原廠工具「閃電說」在打字，而且它會注入按鍵。**

所以 T4 拆成兩件必測的事：

#### T4a — 按鍵會不會漏進前景視窗（**架構關鍵**）

1. 開記事本，隨便打幾個字，把游標放在中間
2. 按裝置的 **Backspace** 按鈕一次
3. 按 **Enter** 按鈕一次
4. 按 **方向鍵** 一次

| 結果 | 意義 |
|---|---|
| 記事本的字被刪掉／換行／游標移動 | ❌ **按鍵會漏** → 熱鍵層**必須抑制按鍵**（見 `AGENTS.md` §8.2） |
| 記事本完全不動 | ✅ 有東西已經吃掉按鍵（很可能是閃電說）→ 仍需確認關閉它之後的行為 |

#### T4b — 原廠工具的角色（**決定性實驗**）

先關閉原廠工具（含自動啟動）：

```powershell
Stop-Process -Name shandianshuo -Force
Remove-ItemProperty -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run' -Name '闪电说'
```

然後重跑 T3，每顆按鈕各按 3 次，比較鍵碼：

| 結果 | 意義 | 對計畫的影響 |
|---|---|---|
| 鍵碼與 `hardware.md` §3.3 **相同** | 按鈕是**裝置原生 HID** | ✅ 原架構可行 |
| 按鈕**完全沒反應** | 按鈕靠閃電說經 SPP 實作 | ❌ 「全域熱鍵」路線不成立，需自行實作裝置協議 |
| 鍵碼**不同** | 閃電說有重綁定 | 以原生鍵碼為準，原廠綁定視為衝突來源 |

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
