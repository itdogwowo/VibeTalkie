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

# 監聽 30 秒，期間在裝置上按按鈕 3 次（每次按一下就放開）
python tools/p0/keycode_logger.py --seconds 30
```

> 藍牙 HID 裝置的 interface path **不含裝置名稱**，只有 HID UUID，
> 所以用品牌名過濾抓不到 → 請用 `--filter 00001124`。

**怎麼讀結果：**

| 看到的東西 | 意義 | 熱鍵層要用的 API |
|---|---|---|
| `[rawkb]` 事件，`dev_usage=6` | 按鈕是**標準鍵盤鍵** | 一般全域熱鍵即可（`global-hotkey` / `pynput`） |
| `[rawkb]` 事件，`dev_usage=1` 且 `page=12` | 按鈕在 **Consumer Control** | 一般熱鍵**可能失效** → 需 Raw Input |
| 只有 `[rawhid]` 事件 | 按鈕在**廠商自訂 collection** | 標準鍵盤 API **完全看不到** → 只能用 HID API |
| 完全沒有事件 | 按鈕不是 HID，或走 SPP/廠商通道 | 需重新設計架構（見 hardware.md 的架構分支） |

**記錄重點（回填 `hardware.md`）：**
按下與放開是否都收得到？鍵碼是 `VK_*` 還是 consumer usage？
event 是 `rawkb` 還是 `rawhid`？

### T4 — 裝置會不會自行打字（手動）

1. 開記事本
2. 把游標放進記事本
3. 按住裝置按鈕說一句話、放開
4. **觀察記事本是否自己冒出文字**

| 結果 | 意義 |
|---|---|
| 沒有冒出任何文字 | ✅ 裝置只是 HID 鍵盤 + 麥克風，照原架構走 |
| 冒出文字 | ❌ 裝置內建辨識 → 架構需改為透傳/監聽模式 |

---

## 產出物

| 路徑 | 內容 | 是否 commit |
|---|---|---|
| `artifacts/t2-*.wav` | 真實錄音 | ❌ **絕不**（已 gitignore） |
| `artifacts/t3-keycodes.jsonl` | 原始按鍵事件 | ❌ **絕不**（已 gitignore） |
| `docs/hardware.md` | **只放去識別化的結論** | ✅ |

**隱私：** 錄音內容、裝置 MAC、其他藍牙裝置名稱都不得進入版控。詳見 `AGENTS.md` §7。
