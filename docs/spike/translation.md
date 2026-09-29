# Spike：中文／粵語 → 英文 翻譯可行性（實測）

- **狀態：** 🔬 一次性可行性探測（**未進入產品計畫**，見文末「這份文件不代表決定」）
- **執行平台：** macOS arm64 / CPU 推論（M 系列）
- **目的：** 回答一個具體問題 ——「既然 ASR 已經通了，離翻譯還有多遠？」
- **結論（一句話）：** 演算法上很近，**架構上要新增一整套推論堆疊**，
  而真正的阻擋點是記憶體與「不做翻譯」這條產品線。

> ⚠️ **本文件不改變 `AGENTS.md` §1「v1 不做翻譯」的決定。**
> 這是把成本量出來，讓那個決定可以在有數字的情況下被重新檢視。

---

## 1. 實驗設計

### 測試語料（5 句，粵語口語）

用 macOS 內建粵語語音合成（`say -v Sinji`）產生，
再用 `afconvert` 轉成 **16 kHz / mono / 16-bit**（與專案錄音格式一致）。

| 編號 | 粵語口語（ground truth） | 正確英文 |
|---|---|---|
| c1 | 我哋而家去食飯，你想唔想一齊去？ | We're going to eat now. Do you want to come along? |
| c2 | 呢個問題我唔係好清楚，不如你問下阿明。 | I'm not clear on this. Why don't you ask Ah Ming? |
| c3 | 聽日朝早九點開會，記得帶埋份文件。 | The meeting is at 9 tomorrow morning — remember to bring the documents. |
| c4 | 佢話尋日已經寄咗封信出去喇。 | He said he already mailed the letter yesterday. |
| c5 | 唔好意思，我遲到咗，因為塞車塞得好犀利。 | Sorry I'm late — the traffic was terrible. |

**為什麼用 TTS 而不是真人錄音：** 本 repo 是公開的，
`AGENTS.md` §7 禁止 commit 真實錄音。TTS 的可重現性也更高。
**代價：** TTS 沒有真實的環境噪音、語速變化、吞音，數字會偏樂觀（見 §5）。

### 受測方法

| 方法 | 路徑 | 說明 |
|---|---|---|
| **一步** | 音訊 → 英文 | Whisper 內建 `task=translate` |
| **兩段** | 音訊 → 中文文字 → 英文 | ASR + 文字翻譯模型 |

### 環境與模型

| 項目 | 版本／大小 |
|---|---|
| whisper.cpp | v1.9.4 原始碼自行編譯（`-DGGML_METAL=OFF`，純 CPU） |
| Whisper 模型 | `ggml-small` 487 MB、`ggml-medium` 1.5 GB |
| 翻譯模型 | `Xenova/opus-mt-zh-en` ONNX 量化（encoder 52.9 + decoder 59.8 + with_past 56.6 = **169.3 MB**；含 tokenizer 172.7 MB） |
| 推論引擎 | onnxruntime 1.30.0（CPUExecutionProvider） |
| Tokenizer | transformers 5.17 + sentencepiece 0.2.2 |

> **⚠️ 為什麼特別註明 CPU：** 第一次量測時 Metal 後端初始化花了
> **24.8 秒編譯 shader**，讓每句都變成 25 秒 —— 那個數字與推論無關。
> 即使加了 `--no-gpu`，Metal 函式庫仍在初始化時編譯。
> **必須用 `-DGGML_METAL=OFF` 重編**才量得到真實推論時間。
> 專案的 sherpa-onnx 走 ONNX CPU 路徑，所以 CPU 數字反而更有參考價值。

---

## 2. 實驗一（一步）：Whisper 的 `task=translate`

`docs/hardware.md` 已確認 sherpa-onnx 的 Whisper 支援 `whisper-task=translate`。
本次直接量它的實際表現。

### 2.1 🔴 最重要的發現：Whisper 不認得「粵語」這個語言

```
whisper_full_with_state: auto-detected language: zh (p = 0.979066)
```

即使輸入是**純粵語**，Whisper 的語言偵測回報的是 **`zh`（信心 0.98）**，
不是 `yue`。強制指定 `yue` 的結果是災難性的：

| 設定 | c1 | c3 | c5 |
|---|---|---|---|
| `auto` | Now we are going to eat. Do you want to go together? | 9.00 am tomorrow morning, remember to bring your documents. | I am sorry, I was late, because the traffic was very bad. |
| **`yue`（強制）** | We are going to eat. Do you want to go to eat together? | **`() () () () () () () () () () () () () ()`** | **`"""""""""`** |

**→ 不要指定 `language=yue`。** 那個 token 沒有對應的訓練資料，
輸出會直接崩成重複的標點。

### 2.2 純辨識（`transcribe`）—— 它在內部先「翻成書面中文」

`task=transcribe`、語言 auto 的輸出：

| 編號 | 輸出 | 觀察 |
|---|---|---|
| c1 | 我們現在去吃飯,你想不想一起去? | 口語→書面 |
| c2 | 這個問題我不是很清楚,不如你問問阿明。 | 口語→書面 |
| c3 | 明天早上九點開會,記得帶**埋**份文件。 | ⚠️ 粵語語素殘留 |
| c4 | 他說昨天已經寄了封信出去啦! | 口語→書面 |
| c5 | 不好意思,我遲到了,因為塞車塞得很厲害! | 口語→書面 |

**關鍵觀察：** Whisper 對粵語**不是辨識，是翻譯** ——
它把「我哋」「唔係」「聽日」「尋日」直接改寫成書面中文。
換句話說，**Whisper 內部已經在做「粵→中」這一段**（略過不談好壞）。
但 c3 漏了「埋」沒轉（`帶埋` 應為 `帶`），代表這個內部轉換不完整。

### 2.3 一步翻譯的品質與延遲

| 模型 | 平均 encode | 平均 decode | **平均總延遲** | 品質（5 句） |
|---|---|---|---|---|
| small | 1136 ms | 13 ms | **1650 ms** | 5 句可用 |
| medium | 3154 ms | 16 ms | **4367 ms** | 4 句可用，1 句錯譯 |

**逐句品質（一步）：**

| 編號 | small（`auto`） | medium（`auto`） | 正確英文要點 |
|---|---|---|---|
| c1 | Now we are going to eat. Do you want to go together? | we are going to have dinner, do you want to join us? | ✅ 兩者皆可 |
| c2 | I am not very clear about this question. Why don't you ask Ah Ming? | I am not clear about this question, you may ask Mr. Ah Ming. | ✅ 兩者皆可 |
| c3 | 9.00 am tomorrow morning, remember to bring your documents. | tomorrow morning at 9 am, remember to bring your documents with you! | ✅ 兩者皆可 |
| c4 | He said he had sent a message yesterday. | **she** said that **she** had sent out a letter yesterday. | ❌ **medium 把「佢」譯成 she** |
| c5 | sorry, I got late, because **the car was so dirty**. | sorry, I am late, because the traffic jam is very severe. | ❌ small 錯譯「塞車」 |

**⚠️ 反直覺但重要的結果：較大的模型（medium）在這一組上並沒有更好。**
medium 在 c4 出現性別錯譯（佢→she），small 在 c5 把「塞車」聽成「車很髒」。
**在 5 句的樣本上不能斷言哪個模型較強**，但可以斷言：
**「加大模型」不是解決翻譯品質的可靠手段。**

### 2.4 🔑 附帶量到的最佳化：語言偵測要花半秒

Whisper 的 `auto` 會**額外跑一趟 encode 做語言偵測**。實測代價：

| 設定 | 平均 encode | 平均總延遲 |
|---|---|---|
| `auto`（要偵測） | 1136 ms | 1650 ms |
| `zh`（直接指定） | **583 ms** | **1138 ms** |
| **省下** | **553 ms** | **512 ms/句** |

**品質沒有差別**（B2 與 B4 輸出逐字相同）。
**→ 這是免費的 500 ms。** 對任何固定語言的部署，都應該明寫語言而非用 auto。

> 📌 這條對**現有產品也有用**：即使不做翻譯，
> 「已知使用者講中文」就不該讓模型每句重新猜一次。

---

## 3. 實驗二（兩段）：ASR → 文字翻譯模型

### 3.1 候選調查（先確認有沒有現成的）

| 模型 | 存在 | 大小 | 備註 |
|---|---|---|---|
| `Helsinki-NLP/opus-mt-zh-en` | ✅ | 312 MB (PyTorch) | 中→英，成熟 |
| **`Helsinki-NLP/opus-mt-yue-en`** | ❌ **HTTP 401** | — | **沒有現成的粵→英模型** |
| `facebook/nllb-200-distilled-600M` | ✅ | 2.46 GB | 多語，太大 |
| `Xenova/opus-mt-zh-en`（ONNX 量化） | ✅ | **169.3 MB** | 本次採用 |

**🔴 沒有 `yue-en`。** 所以兩段式**不能**「粵語 ASR → 直接翻譯」，
必須先經過**書面中文**這一站：

```
粵語語音 → [ASR] → 粵語/中文文字 → [口語轉書面] → 書面中文 → [Opus-MT] → 英文
```

好消息是**中間那一站專案已經有了**：`speech_engine.to_traditional()`（OpenCC）
就是同一個接縫，再加一張粵語虛詞表即可。詳見 `docs/architecture.md` §1 的後處理鏈。

### 3.2 🔴 sherpa-onnx 沒有任何翻譯介面

查證 k2-fsa/sherpa-onnx 的 Python 模組清單：

```
__init__.py  cli.py  display.py  keyword_spotter.py
offline_recognizer.py  online_recognizer.py  utils.py
```

**沒有 translator / translation 模組。** 只有 ASR（offline/online）、
關鍵詞偵測（keyword_spotter）、以及 `cli`。

→ **兩段式的第二段必須自帶推論引擎。** 這不是「加一個模型檔」，
而是「加一整套推論堆疊」：

| 新增元件 | 為什麼非有不可 |
|---|---|
| onnxruntime（或等效） | 專案目前完全靠 sherpa-onnx 內部處理，沒有直接的 ORT 依賴 |
| Tokenizer 執行期 | Marian 用 SentencePiece；需要 `sentencepiece` + tokenizer 定義 |
| Beam search / 解碼迴圈 | 要自己寫（本次實作約 60 行） |

### 3.3 實測結果

翻譯模型：`Xenova/opus-mt-zh-en` 量化 ONNX，貪婪解碼，純 CPU。

| 編號 | 輸入（＝Whisper 的 ASR 輸出） | 輸出 | 評價 |
|---|---|---|---|
| c1 | 我們現在去吃飯，你想不想一起去？ | We're going to dinner now. You want to come with us? | ✅ 道地 |
| c2 | 這個問題我不是很清楚，不如你問問阿明。 | I'm not sure about this. Why don't you ask Ming? | ✅ 道地 |
| c3 | 明天早上九點開會，記得帶埋份文件。 | Tomorrow morning at 9:00, bring a copy of the document. | ✅ 可用 |
| c4 | 他說昨天已經寄了封信出去啦！ | He said he sent a letter yesterday! | ✅ **性別正確** |
| c5 | 不好意思，我遲到了，因為塞車塞得很厲害！ | Sorry I'm late, because the traffic is really heavy! | ✅ **正確** |

**延遲：**

| 指標 | 實測 |
|---|---|
| 三顆模型 + tokenizer 載入 | 1424 ms（一次性） |
| **平均翻譯延遲** | **309 ms/句**（最快 232、最慢 490） |

**⚠️ 兩段式在品質上明顯勝過一步式：**
c4 的「佢」→ **He**（一步式 medium 譯成 she）、
c5 的「塞車」→ **traffic is really heavy**（一步式 small 譯成 car was so dirty）。
**而且快得多**（309 ms vs 4367 ms）。

### 3.4 整合後的總延遲估算

| 階段 | 延遲 | 來源 |
|---|---|---|
| 粵語 ASR（Paraformer-yue） | 70 ms | `hardware.md` §7.1.2 實測 |
| 口語→書面（OpenCC + 詞表） | ~5 ms | 既有管線 |
| 中→英翻譯（Opus-MT ONNX） | 309 ms | 本次實測 |
| **合計（放開 → 英文出現）** | **~384 ms** | |
| 專案門檻（`plan.md`） | 1500 ms | **✅ 過關，餘裕約 4 倍** |

---

## 4. 距離的四個層次

| 層次 | 距離 | 說明 |
|---|---|---|
| **演算法** | 🟢 **近** | 384 ms 總延遲，門檻是 1500 ms |
| **架構** | 🟡 **中** | sherpa-onnx 沒有翻譯介面 → 要新增 ORT + tokenizer + 解碼迴圈 |
| **資源** | 🔴 **遠** | 見下 |
| **產品決定** | 🔴 **未決定** | `AGENTS.md` §1 明列 v1 不做翻譯 |

### 4.1 🔴 資源：這才是真正的阻擋點

| 項目 | 數值 |
|---|---|
| 翻譯模型檔（量化 ONNX ×3） | **169.3 MB** |
| 載入後 RSS（僅翻譯三顆，Python） | **375 MB** |
| 現有 `ptt.py` 閒置工作集 | 340.5 MB（**已超標 300 MB**） |
| 加翻譯後估計 | **約 440 MB 以上** |

> **兩個數字看起來不成比例（169 MB 檔案 vs 375 MB RSS），原因有二：**
> (1) 375 MB 是**含 Python 直譯器 + numpy + onnxruntime** 的整個行程，
> 不是模型本身的淨增量；(2) ONNX Runtime 的 arena 配置會保留比權重更多的記憶體。
> **「模型檔案多大」不等於「記憶體增加多少」** —— 這一項必須實測，不能從檔案大小推。

`docs/spike/python.md` 已記錄「閒置 340.5 MB 未達標」。
**再加翻譯只會讓這一項更遠。**

### 4.2 🔴 離線承諾會被迫二選一

`plan.md` 驗收標準第 5 條：「無網路環境下（本地模型）全功能可用」。

- Opus-MT 量化後 164 MB → **可以全離線**，但記憶體代價如上
- 換成雲端翻譯 API → 延遲與品質都更好，但**直接違反這一條**

---

## 5. 誠實的限制（不要把這份數字當成最終結論）

1. **語料是 TTS，不是真人。** `say -v Sinji` 沒有環境噪音、
   沒有語速變化、沒有吞音。真實情境的 ASR 錯誤率會更高，
   而 ASR 錯誤會被翻譯**放大**（錯的字翻成錯的英文）。
2. **只有 5 句。** 足夠看出趨勢，不足以排名模型。
   特別是「small vs medium 哪個好」**不應**從這 5 句下結論。
3. **平台不同。** 本次是 macOS arm64，產品主要目標是 Windows。
   絕對延遲會不同（但 `hardware.md` §7.1.2 顯示該專案的
   Windows 機器上 ASR 反而更快，RTF 0.014–0.019）。
4. **Opus-MT 訓練在書面中文上**，不是口語粵語。
   所以「粵語口語 → 書面中文」那一段的品質**直接決定**最終英文品質，
   而本次是用 Whisper 已經整理過的書面中文去測的 —— **這一段的損失沒有被量到。**
5. **貪婪解碼，不是 beam search。** 品質是下限，beam 會更好但更慢。

---

## 6. 若要往下走：建議路徑

1. **走兩段式，不要走一步式。** 一步式（Whisper translate）
   品質較差、慢 10 倍以上，且會繞過專案既有的
   簡繁轉換與熱詞管線（`docs/architecture.md` §3 `asr` 的「熱詞替換表」）。
2. **保留 Whisper translate 當備援**（萬一 ASR 品質不可用時的對照組）。
3. **口語→書面那一段要當成獨立工項**，不是附帶品 ——
   它的品質上限決定了最終英文品質（見 §5 第 4 點）。
4. **明寫語言，不要用 auto**：無論做不做翻譯，這條都該套用（§2.4）。
5. **先量再決定**：把「翻譯後閒置記憶體」與「真實錄音的翻譯品質」
   量出來，再回來重新檢視 300 MB 門檻與「不做翻譯」這條線。

---

## 7. 可重現性：踩到的坑

| 坑 | 症狀 | 解法 |
|---|---|---|
| Metal shader 編譯 | 每句 25 秒（與推論無關） | `cmake -DGGML_METAL=OFF` 重編 |
| `-np` 抑制 timing | 所有計時變成 `?` | 不要用 `-np` |
| merged decoder 不吃零長度快取 | `Reshape` 失敗（dimension with value zero） | 改用 `decoder_model` + `decoder_with_past_model` 兩顆 |
| 依順序 `zip` 輸出與輸入 | `Required inputs ... are missing` | **依名字精確配對**（24 個 present vs 12 個 past 輸入，`zip` 會靜默截斷） |
| `timeout` 指令 | macOS 沒有 | 用別的方式限制時間 |

**實作腳本**（本次量測用，未進版控）：
`/tmp/vt-eval/mt_translate.py`（Opus-MT ONNX 貪婪解碼 + 延遲量測）、
`/tmp/vt-eval/run_final2.sh`（Whisper 矩陣）。
若日後要做，這些應重寫為 `tools/p1/` 底下的正式工具。

---

## 8. 這份文件不代表決定

`AGENTS.md` §1 明列 v1 不做翻譯。本文件只是把**成本量出來**：

- 演算法與延遲：**比預期近很多**（384 ms，門檻 1500 ms）
- 記憶體：**比預期遠**（+169 MB 檔案、+375 MB RSS，而現況已超標）
- 產品：**這是使用者的決定，不是 AI 的**

要走這條路，需要的是產品決策（可能要開新的 ADR），不是把程式碼塞進 `app/`。
