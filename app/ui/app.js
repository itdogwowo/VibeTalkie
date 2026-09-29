// VibeTalkie UI 邏輯
//
// 純靜態檔案，改完重新整理即可，不需要任何建置步驟。
// 刻意與 index.html 分開：HTML 裡沒有 inline script，
// 免得非瀏覽器行程（例如 PowerShell）下載含 JS 的網頁時踩到
// Cortex XDR 的 amsi_malicious_js_activity 規則（實測踩過）。
//
// 資料流：
//   1. /api/status   每 500ms 輪詢（輕量：狀態、音量、計數、下載進度）
//   2. /api/models   只在載入 / 手動重整 / 動作後抓（官方清單有 499 筆，不進輪詢）
//   3. 下載進度由 (1) 帶回來，前端合併進 (2) 的清單再重繪

const $ = id => document.getElementById(id);

const STATE_TEXT = {
  idle:       ["待命", "按住錄音鍵說話"],
  recording:  ["錄音中…", "放開錄音鍵結束"],
  processing: ["辨識中…", "正在跑本機模型"],
  inserting:  ["注入中…", "寫入前景視窗"],
  error:      ["發生錯誤", "請看下方訊息"],
};

const MODEL_STATE = {
  ready: "已下載", absent: "未下載", queued: "排隊中",
  downloading: "下載中", extracting: "解壓縮中",
  error: "失敗", cancelled: "已取消",
};

const BUSY = ["queued", "downloading", "extracting"];

let lastCount = -1;
let catalog = [];          // /api/models 的結果
let catMeta = null;
let downloads = {};        // name -> 進度（來自 /api/status）
let configCache = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = "HTTP " + r.status;
    try { const j = await r.json(); msg = j.message || j.error || msg; } catch (e) {}
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

function esc(t) {
  return String(t == null ? "" : t).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtMb(mb) {
  if (!mb) return "—";
  return mb >= 1000 ? (mb / 1000).toFixed(2) + " GB" : Math.round(mb) + " MB";
}

// ---------------------------------------------------------------- 分頁

function showTab(name) {
  document.querySelectorAll(".tab").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".page").forEach(p =>
    p.classList.toggle("active", p.dataset.page === name));
  try { location.hash = name; } catch (e) {}
}

document.querySelectorAll(".tab").forEach(b => {
  b.onclick = () => showTab(b.dataset.tab);
});

// ---------------------------------------------------------------- 狀態

function setDot(state) {
  $("dot").className = "dot " + state;
  $("dot2").className = "dot " + state;
}

function render(s) {
  lastStatus = s;              // 錄音鍵狀態列會用到（見 renderHotkeyLive）
  // 後端有沒有 `hotkeys` 欄位？沒有的話就是舊版程式還在跑 ——
  // 實測踩到：UI 改了、後端沒重啟，症狀是「按了完全沒反應」而且查不出原因。
  window.__vtHotkeysSeen = Array.isArray(s.hotkeys);
  const [name, meta] = STATE_TEXT[s.state] || ["未知", s.state];
  $("state").textContent = name;
  $("statemeta").textContent = s.error || meta;
  setDot(s.state);

  // 目前實際生效的設定。
  // 為什麼要顯示「設定要 X / 實際跑 Y」：實測踩過「設定改了、模型沒換」——
  // 使用者聽到的是舊模型的效果，卻以為是模型本身不好，白白誤判一整輪。
  const want = s.model_wanted || "", got = s.model_loaded || "";
  $("model-now").textContent = s.model_mismatch
    ? `（設定要 ${want}，實際跑 ${got}）`
    : (got || want || "—");
  $("model-now").style.color = s.model_mismatch ? "var(--warn, #d9534f)" : "";

  $("micstream-now").textContent = micStreamLabel(s.mic_stream);
  const openTxt = s.mic_open === true ? "開著"
    : (s.mic_open === false ? "已關閉" : "—");
  $("mic-open").textContent =
    (s.mic_device != null ? openTxt + "　device " + s.mic_device : openTxt);

  // 目標麥克風在不在線（藍牙省電休眠時會消失）。醒來時程式會自動切回，
  // 所以這裡只是讓使用者知道「現在錄的是哪一支」，不必自己處理。
  const on = s.target_online;
  $("target-online").textContent = on === true ? "在線"
    : (on === false ? "離線（省電休眠？醒來會自動切回）" : "—");
  $("target-online").style.color =
    on === false ? "var(--warn, #d9534f)" : "";

  $("counters").textContent =
    `按 ${s.presses} · 成功 ${s.inserted} · 空 ${s.empty} · 失敗 ${s.failed}`;
  renderHotkeyLive(s);
  $("m-engine").textContent = s.engine || "—";
  $("m-active").textContent = s.model || "—";
  // 切換模型後最想知道的就是「有沒有生效」——直接顯示，不要讓使用者自己比對
  const mm = $("m-mismatch-row");
  if (mm) {
    if (s.model_mismatch) {
      mm.style.display = "flex";
      $("m-mismatch").textContent = `設定要 ${s.model_wanted}，實際跑 ${s.model_loaded}`;
    } else {
      mm.style.display = "none";
    }
  }
  $("a-engine").textContent = s.engine || "—";
  $("a-path").textContent = s.models_dir || "—";
  $("a-config").textContent = s.config_path || "—";
  $("a-opencc").textContent = s.opencc ? "可用" : "未安裝（無法輸出繁體）";

  $("lat-total").textContent = s.latency_ms != null ? s.latency_ms + " ms" : "—";
  $("lat-asr").textContent = s.asr_ms != null ? s.asr_ms + " ms" : "—";
  $("lat-paste").textContent = s.paste_ms != null ? s.paste_ms + " ms" : "—";

  $("level").style.width = Math.round((s.level || 0) * 100) + "%";

  if (s.history && s.history.length !== lastCount) {
    lastCount = s.history.length;
    $("log").innerHTML = s.history.length
      ? s.history.map(h => `<div class="entry">
           <div class="txt">${esc(h.text)}</div>
           <div class="meta">${esc(h.time)} · ${h.ms} ms · ${h.chars} 字</div>
         </div>`).join("")
      : '<div class="empty">還沒有紀錄 —— 按住錄音鍵說一句話試試</div>';
  }

  // 警告
  // `hotkey_warning` 也要進來 —— 錄音鍵設定壞掉（寫了看不懂的字串）時，
  // 症狀是「按了完全沒反應」，使用者最需要看到的就是這一行。
  const warns = [s.vendor_warning, s.mic_warning, s.bt_warning,
                 s.hotkey_warning].filter(Boolean);
  $("warnbox").style.display = warns.length ? "block" : "none";
  $("warnbox").innerHTML = warns.map(w => `<div>⚠️ ${esc(w)}</div>`).join("");

  // 下載進度：合併進清單再重繪（不重抓 499 筆）
  const next = {};
  (s.downloads || []).forEach(d => { next[d.name] = d; });
  const changed = JSON.stringify(next) !== JSON.stringify(downloads);
  downloads = next;
  if (changed || Object.keys(downloads).length) renderModels();
  if (changed && !Object.keys(downloads).length) refreshModels();
}

async function tick() {
  try {
    render(await api("/api/status"));
  } catch (e) {
    $("state").textContent = "連不上本機服務";
    $("statemeta").textContent = "請確認 VibeTalkie 還在執行";
    setDot("error");
  }
}

// ---------------------------------------------------------------- 模型

function merged(m) {
  const d = downloads[m.name];
  if (d && BUSY.includes(d.state)) return { ...m, state: d.state, download: d };
  return m;
}

function modelCard(m) {
  const st = m.state;
  const busy = BUSY.includes(st);
  const dl = m.download || {};

  let tags = "";
  if (m.active) tags += '<span class="tag on">使用中</span>';
  if (st === "ready" && !m.active) tags += '<span class="tag">已下載</span>';
  if (!m.supported) tags += '<span class="tag no">不支援</span>';
  if (busy) tags += `<span class="tag rec">${MODEL_STATE[st]}</span>`;

  let action = "";
  if (m.active) {
    action = '<span class="size">目前使用中</span>';
  } else if (st === "ready") {
    action = `<button class="sm" data-select="${esc(m.name)}">切換使用</button>`;
  } else if (busy) {
    const sp = dl.speed_mbps ? `　${dl.speed_mbps} MB/s` : "";
    action = `<span class="size">${dl.downloaded_mb || 0} / ${dl.total_mb || "?"} MB${sp}</span>
              <button class="sm ghost" data-cancel="${esc(m.name)}">取消</button>`;
  } else if (m.supported && m.asset) {
    action = `<button class="sm" data-download="${esc(m.asset)}">下載 ${fmtMb(m.size_mb)}</button>`;
  } else {
    action = '<span class="size">無法下載</span>';
  }

  let bar = "";
  if (busy) {
    const pct = st === "extracting" ? 100 : (dl.percent || 0);
    bar = `<div class="pbar"><i style="width:${pct}%"></i></div>`;
  }

  const reason = !m.supported && m.reason
    ? `<div class="desc" style="color:var(--warn)">✕ ${esc(m.reason)}</div>` : "";
  const err = st === "error" && dl.error
    ? `<div class="desc" style="color:var(--err)">⚠️ ${esc(dl.error)}</div>` : "";
  const metaLine = [m.family, m.langs].filter(Boolean).join("　·　");

  return `<div class="model${m.supported ? "" : " dim"}">
    <div class="info">
      <div class="name">${esc(m.name)} ${tags}</div>
      <div class="desc">${esc(metaLine)}${metaLine ? "　·　" : ""}${fmtMb(m.size_mb)}</div>
      ${reason}${err}${bar}
    </div>
    <div class="side">${action}</div>
  </div>`;
}

function renderModels() {
  const q = ($("q").value || "").trim().toLowerCase();
  const onlyOk = $("only-ok").checked;

  let list = catalog.map(merged);
  if (onlyOk) list = list.filter(m => m.supported || m.state === "ready" || m.active);
  if (q) {
    list = list.filter(m =>
      (m.name + " " + (m.family || "") + " " + (m.langs || "")).toLowerCase().includes(q));
  }

  const box = $("models");
  if (!list.length) {
    box.innerHTML = `<div class="empty">沒有符合的模型${onlyOk ? "（試著取消「只顯示可用」）" : ""}</div>`;
  } else {
    // 使用中／已下載的排最前面，其餘依大小
    list.sort((a, b) => {
      const rank = x => x.active ? 0 : (x.state === "ready" ? 1 : (BUSY.includes(x.state) ? 2 : 3));
      return rank(a) - rank(b) || (a.size_mb - b.size_mb);
    });
    const shown = list.slice(0, 150);
    box.innerHTML = shown.map(modelCard).join("")
      + (list.length > shown.length
        ? `<div class="hint" style="text-align:center;margin:12px 0 0">
             只顯示前 ${shown.length} 筆（共 ${list.length}）—— 用搜尋縮小範圍</div>` : "");
  }

  box.querySelectorAll("[data-download]").forEach(b => {
    b.onclick = () => modelAction("/api/models/download", b.dataset.download, b);
  });
  box.querySelectorAll("[data-cancel]").forEach(b => {
    b.onclick = () => modelAction("/api/models/cancel", b.dataset.cancel, b, true);
  });
  box.querySelectorAll("[data-select]").forEach(b => {
    b.onclick = () => modelAction("/api/models/select", b.dataset.select, b);
  });
}

function renderCatMeta() {
  if (!catMeta) return;
  const src = { github: "已從 GitHub 更新", cache: "使用快取",
                "stale-cache": "使用過期快取（連線失敗）",
                builtin: "內建清單（連線失敗）" }[catMeta.source] || catMeta.source;
  const age = catMeta.age_hours != null ? `，${catMeta.age_hours.toFixed(1)} 小時前` : "";
  const err = catMeta.error ? `　⚠️ ${catMeta.error}` : "";
  $("cat-meta").textContent =
    `官方清單共 ${catMeta.total} 筆，其中 ${catMeta.supported} 筆可用。${src}${age}${err}`;
}

async function modelAction(path, name, btn, quiet) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const r = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!quiet) {
      $("saved").textContent = r.message || "已送出";
      setTimeout(() => { $("saved").textContent = ""; }, 2500);
    }
  } catch (e) {
    alert(e.message || "操作失敗");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    refreshModels();
  }
}

async function refreshModels(force) {
  try {
    const r = await api("/api/models" + (force ? "?refresh=1" : ""));
    catalog = r.models || [];
    catMeta = r;
    renderCatMeta();
    renderModels();
  } catch (e) {
    $("cat-meta").textContent = "取不到模型清單：" + e.message;
  }
}

$("q").oninput = renderModels;
$("only-ok").onchange = renderModels;
$("refresh").onclick = () => {
  $("refresh").disabled = true;
  $("refresh").textContent = "更新中…";
  refreshModels(true).finally(() => {
    $("refresh").disabled = false;
    $("refresh").textContent = "重新整理清單";
  });
};

// ---------------------------------------------------------------- 設定

// 麥克風串流模式：選項與說明全部由後端提供（config.py 的 MIC_STREAM_OPTIONS），
// UI 不自己寫一份，避免兩邊的說明漂移。
function micStreamLabel(value) {
  if (!value) return "—";
  const opts = (configCache && configCache.mic_stream_options) || [];
  const hit = opts.find(o => o.value === value);
  return hit ? hit.label : value;
}

function renderMicStream(c) {
  const box = $("micstream");
  const opts = c.mic_stream_options || [];
  const cur = c.mic_stream || "per_press";
  box.innerHTML = opts.map((o, i) => `
    <label class="modeopt${o.value === cur ? " on" : ""}">
      <input type="radio" name="micstream" value="${esc(o.value)}"${o.value === cur ? " checked" : ""}>
      <span class="t">${esc(o.label)}</span>
      <div class="n">${esc(o.note)}</div>
    </label>`).join("") || `<div class="hint">
      ⚠️ 讀不到模式選項（後端沒有回 mic_stream_options）。<br>
      你正在跑的可能是舊版程式 —— 請關掉 VibeTalkie 再重新啟動。
    </div>`;
  if (!opts.length) {
    // 沒有選項就沒東西可選。明講原因，不要讓設定頁看起來正常但其實壞了。
    $("idlerow").style.display = "none";
    $("idlehint").style.display = "none";
    return;
  }

  const sync = () => {
    const picked = box.querySelector("input[name=micstream]:checked");
    box.querySelectorAll(".modeopt").forEach(el => {
      el.classList.toggle("on", el.querySelector("input").checked);
    });
    $("idlerow").style.display = (picked && picked.value === "idle_timeout") ? "flex" : "none";
    $("idlehint").style.display = (picked && picked.value === "idle_timeout") ? "block" : "none";
  };
  box.querySelectorAll("input[name=micstream]").forEach(el => {
    el.addEventListener("change", sync);
  });
  $("idlesecs").value = Math.round(c.idle_timeout_s != null ? c.idle_timeout_s : 7);
  sync();
}

// 麥克風優先順序：主 → 副，可上移／下移／移除。
// 為什麼是清單而不是單選：藍牙麥克風會休眠、會斷線。有順序就能
// 「主的不在就用副的、主的一回來就自動換回」，不必手動切。
let micOrder = [];

function renderMicOrder() {
  const box = $("micorder");
  if (!box) return;
  if (!micOrder.length) {
    box.innerHTML = `<div class="hint" style="margin:0">
      （清單是空的 —— 還沒有指定任何麥克風）<br>
      下面選一支，再按「加入清單」；<b>第一支就是主麥克風</b>。</div>`;
    return;
  }
  box.innerHTML = micOrder.map((m, i) => `
    <div class="modeopt on" style="display:flex;align-items:center;gap:8px;padding:8px 10px">
      <span class="tag ${i === 0 ? "on" : ""}" style="flex:0 0 auto">${i === 0 ? "主" : "副 " + i}</span>
      <span style="flex:1;word-break:break-all">${esc(m)}</span>
      <button class="ghost sm" data-micup="${i}" ${i === 0 ? "disabled" : ""}
              title="往上移（更優先）">↑</button>
      <button class="ghost sm" data-micdown="${i}" ${i === micOrder.length - 1 ? "disabled" : ""}
              title="往下移">↓</button>
      <button class="ghost sm" data-micdel="${i}" title="從清單移除">✕</button>
    </div>`).join("");

  box.querySelectorAll("[data-micup]").forEach(b => b.onclick = () => {
    const i = +b.dataset.micup;
    [micOrder[i - 1], micOrder[i]] = [micOrder[i], micOrder[i - 1]];
    renderMicOrder();
    markUnsaved();
    renderMicNote(`「${micOrder[i - 1]}」移到第 ${i} 順位`);
  });
  box.querySelectorAll("[data-micdown]").forEach(b => b.onclick = () => {
    const i = +b.dataset.micdown;
    [micOrder[i + 1], micOrder[i]] = [micOrder[i], micOrder[i + 1]];
    renderMicOrder();
    markUnsaved();
    renderMicNote(`「${micOrder[i + 1]}」移到第 ${i + 2} 順位`);
  });
  box.querySelectorAll("[data-micdel]").forEach(b => b.onclick = () => {
    const i = +b.dataset.micdel;
    const gone = micOrder.splice(i, 1)[0];
    renderMicOrder();
    markUnsaved();
    renderMicNote(micOrder.length
      ? `已移除「${gone}」—— 記得按「儲存設定」。`
      : `已移除「${gone}」—— 現在沒有任何麥克風，請再選一支加入。`);
  });
}

// 麥克風清單的即時提示（放在清單下方，跟錄音鍵那張卡一致的做法）
function renderMicNote(text) {
  const box = $("micnote");
  if (box) box.textContent = text || "";
}

// ------------------------------------------------- 錄音鍵（多組）與按鍵錄製
//
// 為什麼要有「錄製」：原本只是一個文字框，使用者得自己知道鍵名怎麼寫
// （`RightCtrl`？`RCtrl`？`Ctrl+Alt+R`？）。打錯字的症狀是「按了沒反應」，
// 而且畫面上完全看不出來 —— 所以直接讓他按一次那顆鍵。
//
// ⚠️ 錄製只回答兩件事：**哪顆鍵**、**是不是組合鍵**（要不要按住修飾鍵）。
// 「要怎麼觸發」（按住／按一下／雙擊）是**使用方式**，用每一列的 chip 選 ——
// 不要在錄製時猜。先前做過手勢判斷（長按＝hold、短按＝toggle、連兩下＝double），
// 使用者的回饋很直接：錄製時不需要判斷，只要判斷是不是組合就好。
// 猜測還有一個本質問題：短按一下既可能是「切換」也可能只是「雙擊的第一下」，
// 當下根本無法分辨，硬猜就會猜錯。
//
// ⚠️ 抓的是**瀏覽器**的按鍵事件，所以：
//   · 這頁一定要有焦點（沒有焦點時按鍵會被前景程式吃掉）
//   · 修飾鍵看 event.keyCode，左右側看 event.location（1=左 2=右）
//   · 錄到的字串一律是 `hotkey.py` 認得的寫法 —— 同一套命名
function modifierName(code, keyCode, loc) {
  if (keyCode === 16) return loc === 2 ? "RightShift" : "LeftShift";
  if (keyCode === 17) return loc === 2 ? "RightCtrl" : "LeftCtrl";
  if (keyCode === 18) return loc === 2 ? "RightAlt" : "LeftAlt";
  if (keyCode === 91 || keyCode === 92) return code === "MetaRight" ? "RWin" : "LWin";
  return null;
}

// event.code → 鍵名。用**實體位置**而不是 event.key，因為：
//   · 中文輸入法／注音會讓 event.key 變成「Process」之類的怪值
//   · 不同鍵盤配置的符號位置不一樣，位置才是熱鍵真正綁定的東西
const CODE_NAMES = {
  Backquote: "Backtick", Minus: "Minus", Equal: "Equals",
  BracketLeft: "LeftBracket", BracketRight: "RightBracket",
  Backslash: "Backslash", Semicolon: "Semicolon", Quote: "Quote",
  Comma: "Comma", Period: "Period", Slash: "Slash",
  Space: "Space", Enter: "Enter", NumpadEnter: "NumpadEnter",
  Tab: "Tab", Escape: "Esc", Backspace: "Backspace", Delete: "Delete",
  Insert: "Insert", Home: "Home", End: "End",
  PageUp: "PageUp", PageDown: "PageDown", CapsLock: "CapsLock",
  ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
  PrintScreen: "PrintScreen", ScrollLock: "ScrollLock", Pause: "Pause",
  NumLock: "NumLock", ContextMenu: "Apps",
  NumpadAdd: "NumpadAdd", NumpadSubtract: "NumpadSubtract",
  NumpadMultiply: "NumpadMultiply", NumpadDivide: "NumpadDivide",
  NumpadDecimal: "NumpadDecimal",
};

function keyNameFromCode(code, keyCode) {
  if (CODE_NAMES[code]) return CODE_NAMES[code];
  if (/^Key[A-Z]$/.test(code || "")) return code.slice(3);        // KeyR → R
  if (/^Digit[0-9]$/.test(code || "")) return code.slice(5);      // Digit3 → 3
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code || "")) return code;   // F9
  if (/^Numpad[0-9]$/.test(code || "")) return code;              // Numpad0
  // 這顆鍵沒有對應的鍵名（hotkey.py 認不得）→ 回 null，由呼叫端明講，
  // 不要硬掰一個名字出來（那會變成「存得下去但永遠不觸發」）。
  return null;
}

// 每一筆是 {text, key, mode}：
//   text = 設定檔裡的字串（`F9@double`）—— 存回後端用這個
//   key  = 只有按鍵的部分（`F9`）—— 顯示與比對用
//   mode = 這一組自己的觸發方式（hold / toggle / double）
//
// ⚠️ 模式是**每一組各自**的（後端格式 `@模式`）。為什麼不做成全域：
// 真實情境是「麥克風按住說話 ＋ 鍵盤 F9 雙擊」，全域一個值做不到。
let hotkeysList = [];
let capturing = false;
let captureBox = null;
let captureDone = false;          // 這一次錄製已經定案（只認第一次完成的來源）
let pendingSpec = null;           // 已錄到的按鍵（字串），放開後才加入清單
let commitTimer = 0;              // 使用者一直壓著不放時的保險
let heldMods = new Set();
let flashIndex = -1;              // 剛加入的那一列要閃一下
let lastHotkeyErr = null;         // 後端回報的解析錯誤
let lastHotkeyNote = null;        // 前端自己的提示（已加入／已取消…）
let lastStatus = null;            // 最近一次 /api/status 的快照

const COMMIT_FALLBACK_MS = 1500;

// 觸發方式的舊標籤（只剩提示文字在用；兩邊的行為各自有 START_MODES / END_MODES）
const MODE_LABELS = { hold: "按住說話", toggle: "按一下切換", double: "雙擊" };

// 顯示用的鍵名。後端的 label 會把它變成「Ctrl（限定右側）」那種人話，
// 這裡只需要把 `rightctrl` / `ctrl-alt-r` 這種輸入整理成一致的寫法。
const PRETTY = {
  rightctrl: "RightCtrl", leftctrl: "LeftCtrl", ctrl: "Ctrl", control: "Ctrl",
  rightshift: "RightShift", leftshift: "LeftShift", shift: "Shift",
  rightalt: "RightAlt", leftalt: "LeftAlt", alt: "Alt", win: "Win",
  space: "Space", enter: "Enter", return: "Enter", tab: "Tab", esc: "Esc",
  escape: "Esc", backspace: "Backspace", delete: "Delete", insert: "Insert",
  home: "Home", end: "End", pageup: "PageUp", pagedown: "PageDown",
  up: "Up", down: "Down", left: "Left", right: "Right",
  capslock: "CapsLock", backtick: "Backtick", scrolllock: "ScrollLock",
  pause: "Pause", printscreen: "PrintScreen", numlock: "NumLock", apps: "Apps",
  semicolon: "Semicolon", quote: "Quote", comma: "Comma", period: "Period",
  slash: "Slash", backslash: "Backslash", minus: "Minus", equals: "Equals",
  leftbracket: "LeftBracket", rightbracket: "RightBracket",
  mouse4: "Mouse4", mouse5: "Mouse5",
};

function prettySpec(text) {
  return String(text || "").split("+").map(tok => {
    const k = tok.trim().toLowerCase();
    return PRETTY[k] || tok.trim();
  }).join("+");
}

// ---- 設定檔字串的組裝與拆解 ----
//
// 格式（單一真相來源是 app/core/hotkey.py，這裡只是它的鏡像）：
//   `F9`                    開始 F9（行為用預設）
//   `F9@double`             開始是「連按兩下開始」
//   `F9,Esc`                開始 F9、結束 Esc（Esc 鬆開才停 ← 預設）
//   `F9,Esc@toggle`         同上，但 Esc「再按一下」才停
//   `F9@double,Esc@toggle`  兩邊都指定
//
// ⚠️ `,` 是配對、`+` 是組合鍵；而**行為一律放在最後**（`@` 之後，依序對應
// 開始、結束）。第一版把 `@` 貼在各自的鍵名後面（`F9@double,Esc@hold`），
// 解析時要來回特判，結果連最基本的 `F9,Esc` 配對都被誤殺（實測踩到）。
const MODE_SEP = "@";
const PAIR_SEP = ",";
// 開始鍵下拉選單裡「去錄一顆新鍵」的那一項（不是真的按鍵，選到就開錄製）
const RECORD_OPTION = "🎧 錄製新按鍵…";
const START_NOTE = { RightCtrl: "（藍牙裝置的錄音鍵）" };

// 兩邊的行為清單（**下拉選單**，實測回饋：「開始和結束都有他自己的行為，
// 下拉選單，而且開始和結束的行為會不一樣，結束有鬆開行為」）。
// 語意：開始＝怎麼開始；結束＝怎麼結束（hold 就是「鬆開」）。
const START_MODES = [
  { value: "hold", label: "按一下開始" },
  { value: "double", label: "連按兩下開始" },
];
const END_MODES = [
  { value: "hold", label: "鬆開才停" },
  { value: "toggle", label: "再按一下停" },
  { value: "double", label: "連按兩下停" },
];

// ⚠️ 正式寫法：行為**一律擠在結尾**（`@` 之後，依序＝開始、結束）。
// `A@m1,B@m2` 這種「各自標在自己的鍵名後面」的寫法後端也吃得下（使用者
// 很自然會那樣寫），但**輸出只採一種**，否則存一次就換一種寫法、很難比對。
// `~` 前綴＝**停用**（保留設定但不觸發）—— 見 enabled 欄位。
const DISABLED_PREFIX = "~";

function entryText(key, mode, endKey, endMode, enabled) {
  let text = endKey ? key + PAIR_SEP + endKey : key;
  if (endKey && endMode && endMode !== "hold") {
    // 結束行為不是預設值 → 兩個行為都要寫（第一個可能是預設的 hold，
    // 少了它後端會把唯一的行為當成「開始行為」）
    text += MODE_SEP + (mode || "hold") + MODE_SEP + endMode;
  } else if (mode && mode !== "hold") {
    text += MODE_SEP + mode;
  }
  return (enabled === false ? DISABLED_PREFIX : "") + text;
}

// 把後端給的清單轉成內部結構：{key, endKey, mode, endMode, enabled, text}
// 後端有 `key_modes` 就直接用（避免兩邊各切一次 `@` 而漂移）。
function parseEntries(c) {
  const modes = c.key_modes || {};
  return (c.hotkeys || (c.hotkey ? [c.hotkey] : [])).map(raw => {
    const text0 = String(raw || "").trim();
    const enabled = !text0.startsWith(DISABLED_PREFIX);
    const text = enabled ? text0 : text0.slice(DISABLED_PREFIX.length).trim();
    const at = text.indexOf(MODE_SEP);
    const body = at >= 0 ? text.slice(0, at) : text;
    const suffix = at >= 0 ? text.slice(at + 1).split(MODE_SEP) : [];
    const comma = body.indexOf(PAIR_SEP);
    const key = prettySpec(comma >= 0 ? body.slice(0, comma) : body);
    const endKey = comma >= 0 ? prettySpec(body.slice(comma + 1)) : "";
    const known = modes[text] || null;
    let mode = known ? known.start
      : (suffix.length === 2 ? suffix[0] : (endKey ? "hold" : suffix[0])) || "hold";
    let endMode = known ? known.end
      : (suffix.length === 2 ? suffix[1] : (endKey ? suffix[0] : "")) || "";
    if (!START_MODES.some(m => m.value === mode)) mode = "hold";
    if (endKey && !END_MODES.some(m => m.value === endMode)) endMode = "hold";
    if (!endKey) endMode = "";
    return { key, endKey, mode, endMode, enabled,
             text: entryText(key, mode, endKey, endMode, enabled) };
  });
}

// 狀態列的文字：由 `renderHotkeyLive()` 統一繪製。
// ⚠️ 為什麼訊息要**記住**而不是直接寫進 DOM：狀態頁每 500ms 輪詢一次，
// 直接寫的訊息會在 0.5 秒後被輪詢重繪蓋掉 —— 使用者根本來不及看。
function renderHotkeyStatus(err, note) {
  lastHotkeyErr = err || null;
  lastHotkeyNote = note || null;
  if (!capturing) renderHotkeyLive(lastStatus || {});
}

function renderHotkeys() {
  const box = $("hotkeys");
  if (!box) return;
  if (!hotkeysList.length) {
    box.innerHTML = `<div class="hint" style="margin:0">
      ⚠️ 一個錄音鍵都沒有 —— 按什麼都不會開始錄音。<br>
      按下面的「🎧 錄製按鍵」直接按一次你要用的鍵即可。</div>`;
    return;
  }
  // 一列＝**一組配對**，只有兩個區域（實測回饋：「你現在設定了三個區域，
  // 實質兩個區域就足夠了」）：
  //   ┌ 開始 ─────────────┬ 結束 ─────────────┐
  //   │ F9       [🎧 錄製] │ Esc      [🎧 錄製] │
  //   │ 行為 [按一下開始▾] │ 行為 [鬆開才停▾]  │
  //   └───────────────────┴───────────────────┘
  // 兩邊**各有自己的行為下拉**：開始是「怎麼開始」，結束是「怎麼結束」
  // （結束的 hold 就是「鬆開」）。
  const ends = ["", ...(((configCache && configCache.hotkey_end_presets) || []))];
  hotkeysList.forEach(h => { if (h.endKey && !ends.includes(h.endKey)) ends.push(h.endKey); });
  box.innerHTML = hotkeysList.map((h, i) => `
    <div class="hkrow modeopt on${i === flashIndex ? " flash" : ""}${
      h.enabled ? "" : " off"}">
      <div class="hkcell">
        <div class="hkhead"><span class="tag ${i === 0 ? "on" : ""}">${
          i === 0 ? "主要" : "#" + (i + 1)}</span> 開始
          <label class="switch hksw" title="停用後這一組完全不會觸發（設定保留）">
            <input type="checkbox" data-hkon="${i}"${h.enabled ? " checked" : ""}>
            <span>${h.enabled ? "啟用" : "停用"}</span></label>
          <button class="ghost sm hktest" data-hktest="${i}"
                  title="只聽不錄：按一下那顆鍵，這裡會回報有沒有收到">測試</button>
        </div>
        <div class="hkval kv">${esc(h.key)}</div>
        <div class="hkacts">
          <button class="ghost sm" data-hkrec="${i}" title="用錄製的方式換掉開始鍵">🎧 錄製</button>
          <select class="hkkey" data-hkstart="${i}"
                  title="換掉這顆開始鍵（會直接改這一列，不是新增）">
            ${(() => {
              const opts = ((configCache && configCache.hotkey_presets) || []).slice();
              if (!opts.some(k => k.toLowerCase() === h.key.toLowerCase())) opts.push(h.key);
              return opts.map(k => `<option value="${esc(k)}"${
                k.toLowerCase() === h.key.toLowerCase() ? " selected" : ""}>${
                esc(k)}${START_NOTE[k] ? "　" + esc(START_NOTE[k]) : ""}</option>`).join("");
            })()}
          </select>
        </div>
        <div class="hkacts hkacts-mode">
          <span class="hklabel">行為</span>
          <select data-hkmode="${i}">
            ${START_MODES.map(m => `<option value="${m.value}"${
              h.mode === m.value ? " selected" : ""}>${esc(m.label)}</option>`).join("")}
          </select>
        </div>
      </div>

      <div class="hkcell">
        <div class="hkhead">結束</div>
        <div class="hkval kv${h.endKey ? "" : " dim"}">${
          h.endKey ? esc(h.endKey) : "（沒有指定）"}</div>
        <div class="hkacts">
          <button class="ghost sm" data-hkrecend="${i}"
                  title="錄一顆專門用來結束的鍵">🎧 錄製</button>
          <select class="hkkey" data-hkend="${i}" title="也可以從清單挑一個">
            ${ends.map(k => `<option value="${esc(k)}"${
              (h.endKey || "").toLowerCase() === k.toLowerCase() ? " selected" : ""}>${
              k ? esc(k) : "沒有（放開開始鍵就停）"}</option>`).join("")}
          </select>
        </div>
        <div class="hkacts hkacts-mode">
          <span class="hklabel">行為</span>
          <select data-hkendmode="${i}" ${h.endKey ? "" : "disabled"}>
            ${END_MODES.map(m => `<option value="${m.value}"${
              h.endMode === m.value ? " selected" : ""}>${esc(m.label)}</option>`).join("")}
          </select>
        </div>
      </div>

      <div class="hkcell hkcell-ops">
        <button class="ghost sm" data-hkup="${i}" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="ghost sm" data-hkdown="${i}" ${
          i === hotkeysList.length - 1 ? "disabled" : ""}>↓</button>
        <button class="ghost sm" data-hkdel="${i}">✕</button>
      </div>
    </div>`).join("");

  // 啟用／停用（多組同時生效的前提下，暫時關掉某一組而不刪掉它）
  box.querySelectorAll("[data-hkon]").forEach(cb => cb.onchange = () => {
    const i = +cb.dataset.hkon;
    const row = hotkeysList[i];
    if (!row) return;
    row.enabled = cb.checked;
    row.text = entryText(row.key, row.mode, row.endKey, row.endMode, row.enabled);
    renderHotkeys();
    markUnsaved();
    const on = hotkeysList.filter(x => x.enabled).length;
    renderHotkeyStatus(null, row.enabled
      ? `「${row.key}」已啟用（目前 ${on} 組生效中）—— 記得按「儲存設定」。`
      : `「${row.key}」已停用（目前 ${on} 組生效中）—— 記得按「儲存設定」。`);
  });
  // 測試：只聽不錄
  box.querySelectorAll("[data-hktest]").forEach(b => b.onclick = async () => {
    const i = +b.dataset.hktest;
    const row = hotkeysList[i];
    if (!row) return;
    try {
      await api("/api/test-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ which: row.key, seconds: 20 }),
      });
      renderHotkeyStatus(null,
        `🧪 測試中：請按「${row.key}」${row.endKey ? `或「${row.endKey}」` : ""}`
        + `—— 只會回報有沒有收到，不會錄音。結果會顯示在下面。`);
    } catch (e) {
      renderHotkeyStatus(`測試無法開始：${e.message}`, null);
    }
  });

  // 開始的行為
  box.querySelectorAll("[data-hkmode]").forEach(sel => sel.onchange = () => {
    const i = +sel.dataset.hkmode;
    const row = hotkeysList[i];
    if (!row || row.mode === sel.value) return;
    row.mode = sel.value;
    row.text = entryText(row.key, row.mode, row.endKey, row.endMode, row.enabled);
    renderHotkeys();
    markUnsaved();
    renderHotkeyStatus(null, `「${row.key}」的開始行為改成「${
      START_MODES.find(m => m.value === row.mode).label}」—— 記得按「儲存設定」。`);
  });
  // 結束的行為（鬆開／再按一下／連按兩下）
  box.querySelectorAll("[data-hkendmode]").forEach(sel => sel.onchange = () => {
    const i = +sel.dataset.hkendmode;
    const row = hotkeysList[i];
    if (!row || !row.endKey || row.endMode === sel.value) return;
    row.endMode = sel.value;
    row.text = entryText(row.key, row.mode, row.endKey, row.endMode, row.enabled);
    renderHotkeys();
    markUnsaved();
    renderHotkeyStatus(null, `「${row.endKey}」的結束行為改成「${
      END_MODES.find(m => m.value === row.endMode).label}」—— 記得按「儲存設定」。`);
  });
  box.querySelectorAll("[data-hkstart]").forEach(sel => sel.onchange = () => {
    const i = +sel.dataset.hkstart;
    const row = hotkeysList[i];
    if (!row) return;
    const v = sel.value;
    if (v.toLowerCase() === row.key.toLowerCase()) return;
    const old = row.key;
    row.key = v;
    row.text = entryText(row.key, row.mode, row.endKey, row.endMode, row.enabled);
    renderHotkeys();
    markUnsaved();
    renderHotkeyStatus(null, `開始鍵改成「${v}」（原本 ${old}）`
      + `—— 記得按「儲存設定」。`);
  });
  box.querySelectorAll("[data-hkend]").forEach(sel => sel.onchange = () => {
    const i = +sel.dataset.hkend;
    const row = hotkeysList[i];
    if (!row) return;
    row.endKey = sel.value;                     // "" ＝ 沒有結束鍵
    if (!row.endKey) row.endMode = "";          // 沒有結束鍵就沒有結束行為
    else if (!row.endMode) row.endMode = "hold";
    row.text = entryText(row.key, row.mode, row.endKey, row.endMode, row.enabled);
    renderHotkeys();
    markUnsaved();
    renderHotkeyStatus(null, row.endKey
      ? `「${row.key}」改成按「${row.endKey}」結束 —— 記得按「儲存設定」。`
      : `「${row.key}」改成「放開就停」（沒有結束鍵）—— 記得按「儲存設定」。`);
  });
  box.querySelectorAll("[data-hkrec]").forEach(b => b.onclick = () => {
    startCapture(+b.dataset.hkrec, "start");    // 只換開始鍵
  });
  // 「結束」區域自己的錄製鈕 —— 錄到的鍵直接配到這一列的結束
  box.querySelectorAll("[data-hkrecend]").forEach(b => b.onclick = () => {
    startCapture(+b.dataset.hkrecend, "end");
  });
  box.querySelectorAll("[data-hkup]").forEach(b => b.onclick = () => {
    const i = +b.dataset.hkup;
    [hotkeysList[i - 1], hotkeysList[i]] = [hotkeysList[i], hotkeysList[i - 1]];
    renderHotkeys();
    markUnsaved();
  });
  box.querySelectorAll("[data-hkdown]").forEach(b => b.onclick = () => {
    const i = +b.dataset.hkdown;
    [hotkeysList[i + 1], hotkeysList[i]] = [hotkeysList[i], hotkeysList[i + 1]];
    renderHotkeys();
    markUnsaved();
  });
  box.querySelectorAll("[data-hkdel]").forEach(b => b.onclick = () => {
    hotkeysList.splice(+b.dataset.hkdel, 1);
    renderHotkeys();
    markUnsaved();
  });
  flashIndex = -1;
}

// 錄製面板上的即時顯示：「現在按住什麼、錄到什麼」。
// 為什麼一定要有：使用者按了鍵卻沒看到任何變化時，會以為沒收到，
// 然後再按一次 —— 而那次才是被錄到的。
function hotkeySpec(main, mods) {
  const held = (mods || []).filter(m => m !== main);
  return (held.length ? held.join("+") + "+" : "") + main;
}

function hotkeyReadout(main, mods) {
  const el = captureBox && captureBox.querySelector(".captured");
  if (!el) return;
  if (!main) {
    el.innerHTML = `<span style="color:var(--dim)">等待按鍵…${
      mods.length ? "（目前按住 " + esc(mods.join("+")) + "）" : ""}</span>`;
    return;
  }
  const spec = hotkeySpec(main, mods);
  el.innerHTML = `錄到：<b class="kv">${esc(spec)}</b>`
    + (spec.includes("+")
        ? `　<b>組合鍵</b>（必須按住 ${esc(spec.split("+").slice(0, -1).join("+"))}）`
        : `　<span style="color:var(--dim)">單一按鍵</span>`);
}

// 使用者按住主鍵時，那些修飾鍵要寫進規格裡？（主鍵自己不算修飾鍵）
function heldModifierNames(e) {
  const mods = [];
  if (e.ctrlKey) mods.push("Ctrl");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  if (e.metaKey) mods.push("Win");
  return mods;
}

// 錄製面板（三個入口，全部走同一個面板）：
//   startCapture()              新增一組：錄到之後選「當開始鍵」或「當結束鍵」
//   startCapture(i, "start")    換掉第 i 列的**開始**鍵
//   startCapture(i, "end")      設定第 i 列的**結束**鍵（兩個區域各自錄製）
let captureTarget = null;         // null＝新增；數字＝那一列
let captureWhat = "new";          // "new" | "start" | "end"
let captureStage = "key";         // "key"＝等按鍵；"choose"＝等使用者選開始/結束

function startCapture(target, what) {
  if (capturing) return;
  const box = $("hotkey-rec");
  if (!box) return;
  capturing = true;
  captureDone = false;
  pendingSpec = null;
  captureTarget = (typeof target === "number") ? target : null;
  captureWhat = captureTarget == null ? "new" : (what || "start");
  captureStage = "key";
  heldMods = new Set();
  box.style.display = "block";
  box.className = "hotkeyrec on";
  const row = captureTarget != null ? hotkeysList[captureTarget] : null;
  const aimed = row
    ? (captureWhat === "end"
        ? `（設定「${row.key}」的<b>結束</b>鍵）`
        : `（換掉「${row.key}」的<b>開始</b>鍵）`)
    : "";
  box.innerHTML = `
    <div class="t">🎧 錄製中 ${aimed} —— 直接按你要用的那顆鍵</div>
    <div class="n">
      要組合鍵就先按住修飾鍵再按主鍵（例如 Ctrl+Alt，再按 R）。<br>
      ${row
        ? (captureWhat === "end"
            ? `只會改到<b>結束</b>這一格；開始鍵（${esc(row.key)}）不變。`
            : `只會改到<b>開始</b>這一格；結束鍵（${esc(row.endKey || "自動")}）不變。`)
        : `錄到之後可以選：這顆當<b>開始鍵</b>（新的一組），或當<b>結束鍵</b>`
          + `（配到最近錄的那一顆上）。`}<br>
      ⚠️ 錄的是<b>這一頁收到的按鍵</b>：請確認這個瀏覽器視窗有焦點。
      <b>這裡每一顆鍵都會被當成按鍵</b>（連 Esc 也是 —— 它是最常用的結束鍵），
      要取消請按下面的「取消」。<br>
      按錯了嗎？直接再按一次別顆鍵就會換掉。
    </div>
    <div class="captured"></div>
    <div style="margin-top:8px"><button class="ghost sm" data-cap="cancel">取消</button></div>`;
  captureBox = box;
  box.querySelector('[data-cap="cancel"]').onclick = () => cancelCapture("已取消。");
  const el = box.querySelector(".captured");
  if (el) el.innerHTML = `<span style="color:var(--dim)">等待按鍵…</span>`;
}

function endCapture() {
  capturing = false;
  captureBox = null;
  pendingSpec = null;
  captureTarget = null;
  captureWhat = "new";
  captureStage = "key";
  heldMods = new Set();
  if (commitTimer) { window.clearTimeout(commitTimer); commitTimer = 0; }
  const box = $("hotkey-rec");
  if (box) { box.className = "hotkeyrec"; box.style.display = "none"; }
}

function cancelCapture(msg) {
  if (!capturing) return;
  endCapture();
  renderHotkeyStatus(null, msg || "已取消錄製。");
}

// ---- 錄製：只認「按鍵」與「是不是組合」，**不猜觸發方式** ----
//
// ⚠️ 這裡刻意不做手勢判斷（曾經做過，是錯的）：
//   · 錄一個鍵只該回答一個問題：**哪顆鍵**（以及要不要修飾鍵）
//   · 「雙擊」是**使用方式**，不是按鍵的一部分 —— 錄的時候怎麼按，
//     跟之後要怎麼用是兩件事。使用者要的是「錄 F9」，不是「錄 F9 的按法」。
//   · 猜錯會很誤導（短按一下到底是「切換」還是「雙擊的前半」？無法分辨）
// 所以觸發方式交給每一列的 chip 選，預設「按住說話」。
function finalizeCapture(spec) {
  if (captureDone) return;
  captureDone = true;
  pendingSpec = spec;
  const combo = spec.includes("+");
  // 已經指定要改哪一格（開始／結束）→ 直接套用，不必問
  if (captureTarget != null && hotkeysList[captureTarget]) {
    const row = hotkeysList[captureTarget];
    if (captureWhat === "end") {
      const old = row.endKey;
      row.endKey = prettySpec(spec);
      if (!row.endMode) row.endMode = "hold";   // 新配到的結束鍵預設「鬆開才停」
      row.text = entryText(row.key, row.mode, row.endKey, row.endMode, row.enabled);
      showCaptured(`<b style="color:var(--ok)">✓ 結束鍵改成 <span class="kv">${
        esc(row.endKey)}</span></b>　（開始鍵：${esc(row.key)}）`);
      flashIndex = captureTarget;
      commitTimer = window.setTimeout(() => {
        endCapture();
        renderHotkeys();
        markUnsaved();
        renderHotkeyStatus(null, `「${row.key}」的結束鍵改成「${row.endKey}」`
          + `（原本 ${old || "沒有"}）—— 記得按「儲存設定」。`);
      }, 420);
      return;
    }
    const old = row.key;
    row.key = prettySpec(spec);
    row.text = entryText(row.key, row.mode, row.endKey, row.endMode, row.enabled);
    showCaptured(`<b style="color:var(--ok)">✓ 開始鍵改成 <span class="kv">${
      esc(row.key)}</span></b>　（結束鍵：${esc(row.endKey || "沒有")}）`);
    flashIndex = captureTarget;
    commitTimer = window.setTimeout(() => {
      endCapture();
      renderHotkeys();
      markUnsaved();
      renderHotkeyStatus(null, `「${old}」的開始鍵改成「${row.key}」`
        + `—— 記得按「儲存設定」。`);
    }, 420);
    return;
  }
  // 新增：讓使用者選這顆要當「開始」還是「結束」
  captureStage = "choose";
  // 面板外層那個「取消」要收起來 —— 這裡已經有自己的取消按鈕，
  // 留著會出現兩個一模一樣的按鈕（實測看到）。
  const outerCancel = captureBox && captureBox.querySelector(':scope > div > [data-cap="cancel"]');
  if (outerCancel) outerCancel.style.display = "none";
  const held = pendingSpec;
  showCaptured(`
    <div style="margin:4px 0 6px"><b style="color:var(--ok)">✓ 錄到 <span class="kv">${
      esc(held)}</span></b>　${combo ? "組合鍵" : "單一按鍵"}
      　<span style="color:var(--dim)">（按錯了嗎？直接再按一次別顆鍵就會換掉）</span></div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <button class="sm" data-cap="start">當<b>開始</b>鍵（新的一組）</button>
      <button class="sm ghost" data-cap="end" ${hotkeysList.length ? "" : "disabled"}>
        當<b>結束</b>鍵${hotkeysList.length ? `（配到「${esc(hotkeysList[hotkeysList.length - 1].key)}」）` : ""}</button>
      <button class="sm ghost" data-cap="cancel">取消</button>
    </div>`);
  const el = captureBox && captureBox.querySelector(".captured");
  if (el) {
    el.querySelectorAll("[data-cap]").forEach(b => b.onclick = () => {
      const act = b.dataset.cap;
      if (act === "cancel") { cancelCapture("已取消。"); return; }
      if (act === "end") { commitCapture("end"); return; }
      commitCapture("start");
    });
  }
}
function showCaptured(html) {
  const el = captureBox && captureBox.querySelector(".captured");
  if (el) el.innerHTML = html;
}

function commitCapture(as) {
  if (commitTimer) { window.clearTimeout(commitTimer); commitTimer = 0; }
  const spec = prettySpec(pendingSpec || "");
  const target = captureTarget;
  if (target == null && !as) return;        // 等使用者選「開始」或「結束」
  endCapture();
  if (!spec) return;

  // 當「結束」鍵 → 配到清單最後一組（也就是剛錄的那一顆）
  if (as === "end") {
    const row = hotkeysList[hotkeysList.length - 1];
    if (!row) return;
    if (row.endKey && row.endKey.toLowerCase() === spec.toLowerCase()) {
      renderHotkeyStatus(null, `「${row.key}」的結束鍵本來就是「${spec}」。`);
      return;
    }
    row.endKey = spec;
    if (!row.endMode) row.endMode = "hold";     // 預設「鬆開才停」
    row.text = entryText(row.key, row.mode, row.endKey, row.endMode, row.enabled);
    flashIndex = hotkeysList.length - 1;
    renderHotkeys();
    markUnsaved();
    renderHotkeyStatus(null, `配對完成：「${row.key}」開始、「${spec}」結束`
      + `（預設鬆開才停，可在「結束」那一欄改）—— 記得按「儲存設定」。`);
    return;
  }

  // 大小寫不敏感的去重：`f9` 與 `F9` 是同一組（後端也會再去重一次，
  // 但這裡先擋掉才不會出現「看起來一樣的兩列」）。
  const hit = hotkeysList.findIndex(h => h.key.toLowerCase() === spec.toLowerCase());
  if (hit >= 0) {
    flashIndex = hit;
    renderHotkeys();
    renderHotkeyStatus(null, `「${spec}」已經在清單裡了（沒有重複加入）。`);
    return;
  }
  hotkeysList.push({ key: spec, endKey: "", mode: "hold", endMode: "",
                     text: entryText(spec, "hold", "", "", true) });
  flashIndex = hotkeysList.length - 1;
  renderHotkeys();
  renderHotkeyStatus(null, `已加入「${spec}」—— 開始行為預設「按一下開始、`
    + `放開就停」；要換成雙擊、或指定結束鍵，都在那一列的下拉選單選。`);
  markUnsaved();
}

// ⚠️ 這兩個監聽器**永遠掛著**，只在自己是錄製狀態時動作 ——
// 用 `{once:true}` 的寫法會在「按 Esc 取消」時留下沒被消費的監聽器。
function onCaptureKeyDown(e) {
  if (!capturing) return;
  // 已經在等「開始/結束」的選擇時，再按一顆鍵就是「換成這一顆」
  // （使用者按錯鍵時最直覺的補救方式）。
  if (captureDone && captureStage !== "choose") return;
  if (pendingSpec && captureStage === "choose") {
    e.preventDefault();
    e.stopPropagation();
    captureStage = "key";
    pendingSpec = null;
    captureDone = false;
    heldMods = new Set();
  } else if (pendingSpec) {
    e.preventDefault();
    e.stopPropagation();
    return;
  }

  // ⚠️ 為什麼 Esc **不當**「取消」：Escape 是最常用的**結束鍵**。
  // 只要 Esc 代表取消，「用 Esc 結束錄音」這個最基本的需求就永遠錄不出來
  // （實測踩到：按 Esc 想錄它，結果錄製被取消）。
  // 所以取消改用面板上的「取消」按鈕 —— 按鍵的語意保持單純：錄到的就是按鍵。
  e.preventDefault();
  e.stopPropagation();

  const mod = modifierName(e.code, e.keyCode, e.location);
  if (mod) {
    if (!e.repeat) heldMods.add(mod);
    hotkeyReadout(mod, [mod]);            // 先顯示，等放開才算完成
    return;                               // ← 單獨一顆修飾鍵就靠 keyup 收尾
  }
  const name = keyNameFromCode(e.code, e.keyCode);
  if (!name) {
    hotkeyReadout(`無法辨識（code=${e.code || "?"}）`, heldModifierNames(e));
    return;
  }
  // 主鍵按下就錄到了（修飾鍵是「當下按住的那些」）——
  // 不等 keyup，是為了讓組合鍵的修飾鍵能被正確記錄下來。
  finalizeCapture(hotkeySpec(name, heldModifierNames(e)));
}

function onCaptureKeyUp(e) {
  if (!capturing) return;
  if (captureDone && captureStage !== "choose") return;
  const mod = modifierName(e.code, e.keyCode, e.location);
  if (!mod) return;
  e.preventDefault();
  e.stopPropagation();
  heldMods.delete(mod);
  // 最後放開的修飾鍵（例如單獨一顆 RightCtrl）才算「按下這顆鍵」
  if (heldMods.size === 0 && !pendingSpec) finalizeCapture(mod);
}

// 錄製中若視窗失去焦點（跑去點別的程式），提醒他焦點跑掉了。
// 不主動取消 —— 他可能只是去別的地方確認一下，回來還要繼續錄。
window.addEventListener("blur", () => {
  if (!capturing) return;
  if (captureDone && captureStage !== "choose") return;
  hotkeyReadout("視窗失去焦點 —— 請點回這一頁再按鍵", [...heldMods]);
});

window.addEventListener("keydown", onCaptureKeyDown, true);
window.addEventListener("keyup", onCaptureKeyUp, true);

function markUnsaved() {
  const b = $("save");
  if (!b) return;
  b.textContent = "儲存設定（有未存的變更）";
  b.style.outline = "2px solid var(--warn, #d29922)";
}

function clearSaveHint() {
  const b = $("save");
  if (!b) return;
  b.textContent = "儲存設定";
  b.style.outline = "";
}

// 錄音鍵卡片：清單 + 「錄製按鍵」。
//
// 為什麼要做成清單而不是一個文字框：實測回饋「應該要能錄製、而且不該只有一個」。
// 兩件事其實是同一個問題 —— 使用者不想知道鍵名的寫法（`RightCtrl`？
// `RCtrl`？`Ctrl+Alt+R`？），打錯字的症狀是「按了完全沒反應」。
//
// ⚠️ 為什麼**拿掉候選下拉選單**：既然每一顆鍵都能直接錄，下拉就只是多一個
// 要維護的鍵名清單（而且它只有 10 個常見鍵，清單外的鍵還是得手打）。
function renderHotkeyCard(c) {
  hotkeysList = parseEntries(c);
  renderHotkeys();

  const rec = $("hotkey-record");
  if (rec) rec.onclick = startCapture;
  if (!capturing) renderHotkeyStatus(c.hotkey_error || null, null);
  clearSaveHint();
}

// 狀態列的即時資訊：設定檔說的 vs 程式**現在真正在用的**。
// 為什麼要顯示這個：實測踩過「設定改了、程式沒換」——使用者按了沒反應，
// 卻以為是自己按錯鍵。兩邊不一致時要**明講**，不要讓他猜。
function renderHotkeyLive(s) {
  if (capturing) return;                  // 錄製中不要覆蓋提示文字
  const st = $("hotkey-status");
  if (!st) return;
  const live = s.hotkeys || [];
  const enabled = s.key_enabled || live.map(() => true);
  const onCount = enabled.filter(Boolean).length;
  const bits = [];
  if (live.length) {
    bits.push(`程式現在生效的錄音鍵：<span class="kv">${
      live.map(esc).join("</span> 或 <span class=\"kv\">")}</span>`
      + `（共 ${live.length} 組，其中 <b>${onCount} 組啟用中</b>）`);
    if (onCount === 0) {
      bits.push('<span style="color:var(--warn,#d9534f)">⚠️ 全部停用了 —— '
        + '按什麼都不會開始錄音</span>');
    }
  } else {
    bits.push('<span style="color:var(--warn,#d9534f)">⚠️ 程式目前沒有任何錄音鍵生效</span>');
    // 後端是舊版（沒有 hotkeys 欄位）→ 光看畫面會以為「設定被吃掉了」。
    // 實測踩到：UI 改了、後端沒重啟，症狀是「按了完全沒反應」。
    if (window.__vtHotkeysSeen === false) {
      bits.push('<span style="color:var(--warn,#d29922)">你正在跑的可能是舊版程式 —— '
        + '請關掉 VibeTalkie 再重新啟動。</span>');
    }
  }
  const mine = hotkeysList.map(h => h.text);
  if (live.length && JSON.stringify(live) !== JSON.stringify(mine)) {
    bits.push(`<span style="color:var(--warn,#d29922)">⚠️ 這一頁還沒生效 —— `
      + `目前存檔的是 <span class="kv">${mine.map(esc).join("、") || "（空）"}</span>，`
      + `按下面的「儲存設定」才會套用（或程式需要重啟）。</span>`);
  }
  const err = lastHotkeyErr || s.hotkey_error;
  if (err) {
    bits.push(`<span style="color:var(--warn,#d9534f)">⚠️ ${esc(err)}</span>`);
  }
  // 測試模式（只聽不錄）：把「有沒有收到那顆鍵」直接報出來 ——
  // 「按了沒反應」時最需要知道的就是這個（是沒收到，還是被邏輯吃掉）。
  const t = s.key_test;
  if (t && t.running) {
    const hits = t.hits || [];
    const last = hits.length ? hits[hits.length - 1] : null;
    bits.push(`<span style="color:var(--accent)">🧪 測試中（剩 ${t.remaining}s，`
      + `不會錄音）</span>　請按你要測的鍵…`);
    if (last) {
      const unrelated = last.roles.some(r => r.startsWith("不相關"));
      const mark = unrelated ? "✗" : "✓";
      const color = unrelated ? "var(--warn,#d29922)" : "var(--ok)";
      bits.push(`<span style="color:${color}">${mark} 收到 <span class="kv">${
        esc(vkName(last.vk))}</span>${
        last.mods && last.mods.length ? `（按住 ${esc(last.mods.join("+"))}）` : ""}`
        + ` → ${esc(last.roles.join("、"))}</span>`);
      if (hits.length > 1) {
        bits.push(`<span style="color:var(--dim)">（前 ${hits.length - 1} 次：${
          hits.slice(0, -1).map(x => esc(vkName(x.vk))).join("、")}）</span>`);
      }
    } else {
      bits.push(`<span style="color:var(--dim)">還沒收到任何按鍵</span>`);
    }
  }
  if (lastHotkeyNote) bits.push(esc(lastHotkeyNote));
  st.innerHTML = bits.join("<br>");
}

// VK → 顯示名稱（只在測試回報用；後端的 label 才是正式來源，
// 但測試訊息要顯示**使用者實際按下的那顆鍵**，那顆鍵可能還沒被設定過）。
const VK_NAMES = {
  0x11: "Ctrl", 0xA2: "LeftCtrl", 0xA3: "RightCtrl",
  0x10: "Shift", 0xA0: "LeftShift", 0xA1: "RightShift",
  0x12: "Alt", 0xA4: "LeftAlt", 0xA5: "RightAlt",
  0x5B: "LeftWin", 0x5C: "RightWin", 0x20: "Space", 0x0D: "Enter",
  0x1B: "Esc", 0x09: "Tab", 0x08: "Backspace", 0x2E: "Delete",
  0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
};

function vkName(vk) {
  if (VK_NAMES[vk]) return VK_NAMES[vk];
  if (vk >= 0x70 && vk <= 0x87) return "F" + (vk - 0x6F);      // F1–F24
  if (vk >= 0x41 && vk <= 0x5A) return String.fromCharCode(vk); // A–Z
  if (vk >= 0x30 && vk <= 0x39) return String.fromCharCode(vk); // 0–9
  return "VK 0x" + vk.toString(16).toUpperCase().padStart(2, "0");
}

// ⚠️ 這裡刻意**不再**畫「觸發方式」的全域選項。
//
// 原本是一個全域單選（按住／切換／雙擊），但實測回饋是「無法錄製雙擊」——
// 因為真實情境是「麥克風按住說話 ＋ 鍵盤 F9 雙擊」，全域一個值做不到。
// 現在觸發方式是**每一顆鍵自己的**（每一列三個 chip），錄製時也會依
// 「你怎麼按」自動判斷，所以全域選項只會造成兩套設定的困惑。
//
// 後端仍保留 `trigger_mode`：它是「清單裡沒寫 `@模式` 時」的後備值
// （給手改 config.toml 的人用），UI 不再需要顯示它。

async function loadConfig() {
  const c = await api("/api/config");
  configCache = c;
  $("trad").checked = !!c.traditional;
  $("noperiod").checked = c.remove_trailing_period !== false;
  $("mode").value = c.mode || "auto";
  renderMicStream(c);
  renderHotkeyCard(c);

  // 麥克風優先順序
  micOrder = (c.mic_order && c.mic_order.length ? c.mic_order
             : (c.mic_names && c.mic_names.length ? c.mic_names
                : (c.mic_name ? [c.mic_name] : []))).slice();
  renderMicOrder();

  // 挑選器（下拉）＋「加入清單」。
  //
  // ⚠️ 實測回饋：「我以為這個我要添加後它是不能添加的，要我選擇後才能夠添加」
  //    —— 問題出在**下拉選單長得像已經加好的項目**（它自動選了第一支），
  //    所以使用者以為「明明已經顯示了」而按加入沒反應（其實是重複）。
  //    修法：加一個明確的**提示選項**當預設、沒選之前按鈕是停用的，
  //    並且選到已經在清單裡的裝置時直接告訴他。
  const add = $("micadd");
  const addBtn = $("micadd-btn");
  const devices = (c.devices || []);
  const syncAdd = () => {
    if (!add || !addBtn) return;
    const name = (add.value || "").trim();
    const dup = micOrder.includes(name);
    addBtn.disabled = !name || dup || !devices.length;
    addBtn.textContent = dup ? "已在清單中" : "加入清單";
  };
  if (add) {
    add.innerHTML = devices.length
      ? `<option value="">請選擇要加入的麥克風…</option>`
        + devices.map(d => `<option value="${esc(d.name)}">${esc(d.name)}</option>`).join("")
      : "<option value=\"\">找不到音訊裝置</option>";
    add.value = devices.length ? "" : "";     // 預設停在提示那一項
    add.onchange = syncAdd;
  }
  if (addBtn) {
    addBtn.onclick = () => {
      const name = ($("micadd").value || "").trim();
      if (!name) return;                        // 停在提示項就不動作
      if (micOrder.includes(name)) { syncAdd(); return; }   // 不重複加同一支
      micOrder.push(name);
      renderMicOrder();
      const p = $("micadd");
      if (p) p.value = "";                      // 加完歸零，避免誤按第二次
      syncAdd();
      markUnsaved();
      renderMicNote(micOrder.length === 1
        ? `已加入「${name}」（主麥克風）—— 記得按「儲存設定」才會生效。`
        : `已加入「${name}」（第 ${micOrder.length} 順位）—— 記得按「儲存設定」才會生效。`);
    };
  }
  syncAdd();
  watchConfigInputs();
}

// 「有未存的變更」提示要涵蓋**所有會進 `body`（儲存內容）的欄位**。
//
// ⚠️ 實測回饋：「我以為這個我要添加後它是不能添加的」—— 那是**加入清單**
// 沒回饋的同類問題。這次一併檢查了其他列表，發現更大的洞：
// **麥克風清單／串流模式／閒置秒數／輸出開關改了都不會提示**，
// 而它們跟錄音鍵一樣只在按「儲存設定」時才寫檔。
// 使用者改了、以為生效了、結果沒有 —— 正是那種最難查的失敗。
//
// 做法：用**委派的 change/input** 掛在設定頁上（不必逐一列舉），
// 之後新增欄位也自動涵蓋。
function watchConfigInputs() {
  const page = document.querySelector('[data-page="settings"]');
  if (!page || page.dataset.watched) return;     // 只掛一次
  page.dataset.watched = "1";
  const onEdit = e => {
    if (!e.target || e.target.id === "micadd") return;   // 挑選器本身不算變更
    markUnsaved();
  };
  page.addEventListener("change", onEdit);
  page.addEventListener("input", onEdit);
}

// 設定載入失敗要**明講**。
// 為什麼：實測踩到 —— 設定頁的所有欄位都是「讀不到就留著預設 HTML」，
// 所以後端掛掉時畫面看起來一切正常，但存下去的是空白表單的預設值。
// 使用者看到的是「我的設定被吃掉了」，卻沒有任何線索。
window.addEventListener("unhandledrejection", e => {
  const msg = (e.reason && e.reason.message) || String(e.reason);
  const box = document.querySelector('[data-page="settings"] .card:last-child .hint');
  if (box) {
    box.innerHTML = `<span style="color:var(--warn,#d9534f)">
      ⚠️ 設定載入失敗：${esc(msg)}<br>
      請重新整理頁面；在你看到正確的模式選項之前，<b>不要按「儲存設定」</b>，
      否則會把預設值寫回設定檔。</span>`;
  }
});

$("save").onclick = async () => {
  const picked = document.querySelector("input[name=micstream]:checked");

  // ⚠️ 實測踩到：「找不到被選取的 radio 就送預設值」會**靜默吃掉使用者的設定**。
  // 使用者手改 config.toml 成 session 之後，只要在這一頁按一次儲存、
  // 而選項還沒渲染完成，就會被蓋回 per_press —— 他看到的現象是
  // 「我改了都沒用」，而且完全沒有錯誤訊息。
  // 所以：讀不到就不送，讓值留在檔案裡。
  if (!picked) {
    $("saved").textContent = "讀不到模式選項，請重新整理後再儲存";
    $("saved").style.color = "var(--warn, #d9534f)";
    setTimeout(() => { $("saved").textContent = ""; $("saved").style.color = "var(--ok)"; },
               4000);
    return;
  }

  const body = {
    traditional: $("trad").checked,
    remove_trailing_period: $("noperiod").checked,
    mode: $("mode").value,
    // 麥克風優先順序（第一個是主）。名稱優先，索引會隨藍牙重連改變。
    mic_names: micOrder.slice(),
    // 錄音鍵：清單（可多組），每一筆可以是 `F9@double`。
    // ⚠️ 空清單會被後端拒絕（400）——那是刻意的：一個鍵都沒有＝按什麼都不會
    // 錄音，不該存得下去。
    hotkeys: hotkeysList.map(h => h.text),
    mic_stream: picked.value,
    idle_timeout_s: parseFloat($("idlesecs").value) || 7,
  };
  try {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("saved").textContent = "已儲存 ✓";
    $("saved").style.color = "var(--ok)";
    clearSaveHint();
    renderHotkeyStatus(null, null);
    // 存完重新讀一次，讓畫面（含狀態頁那一行）反映真正生效的值
    setTimeout(() => { loadConfig().catch(() => {}); }, 300);
  } catch (e) {
    // 後端會回具體原因（例如「錄音鍵無法解析」）——要讓使用者看到，
    // 不要只說「儲存失敗」。
    $("saved").textContent = "儲存失敗";
    $("saved").style.color = "var(--warn, #d9534f)";
    renderHotkeyStatus(`儲存失敗：${e.message}`, null);
    alert("儲存失敗：" + e.message);
    return;                        // 失敗時不要把「已儲存」的提示清掉
  }
  setTimeout(() => { $("saved").textContent = ""; }, 3000);
};

// ---------------------------------------------------------------- 啟動

if (location.hash) {
  const t = location.hash.slice(1);
  if (document.querySelector(`.tab[data-tab="${t}"]`)) showTab(t);
}
loadConfig().catch(() => {});
refreshModels();
tick();
setInterval(tick, 500);
