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
  const warns = [s.vendor_warning, s.mic_warning, s.bt_warning].filter(Boolean);
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
      還沒有指定麥克風 —— 按下面的「加入」選一支（第一支就是主麥克風）。</div>`;
    return;
  }
  box.innerHTML = micOrder.map((m, i) => `
    <div class="modeopt on" style="display:flex;align-items:center;gap:8px;padding:8px 10px">
      <span class="tag ${i === 0 ? "on" : ""}" style="flex:0 0 auto">${i === 0 ? "主" : "副 " + i}</span>
      <span style="flex:1;word-break:break-all">${esc(m)}</span>
      <button class="ghost sm" data-micup="${i}" ${i === 0 ? "disabled" : ""}>↑</button>
      <button class="ghost sm" data-micdown="${i}" ${i === micOrder.length - 1 ? "disabled" : ""}>↓</button>
      <button class="ghost sm" data-micdel="${i}">✕</button>
    </div>`).join("");

  box.querySelectorAll("[data-micup]").forEach(b => b.onclick = () => {
    const i = +b.dataset.micup;
    [micOrder[i - 1], micOrder[i]] = [micOrder[i], micOrder[i - 1]];
    renderMicOrder();
  });
  box.querySelectorAll("[data-micdown]").forEach(b => b.onclick = () => {
    const i = +b.dataset.micdown;
    [micOrder[i + 1], micOrder[i]] = [micOrder[i], micOrder[i + 1]];
    renderMicOrder();
  });
  box.querySelectorAll("[data-micdel]").forEach(b => b.onclick = () => {
    micOrder.splice(+b.dataset.micdel, 1);
    renderMicOrder();
  });
}

function renderTriggerMode(c) {
  const box = $("triggermode");
  if (!box) return;
  const opts = c.trigger_modes || [];
  const cur = c.trigger_mode || "hold";
  box.innerHTML = opts.map(o => `
    <label class="modeopt${o.value === cur ? " on" : ""}">
      <input type="radio" name="triggermode" value="${esc(o.value)}"${o.value === cur ? " checked" : ""}>
      <span class="t">${esc(o.label)}</span>
      <div class="n">${esc(o.note)}</div>
    </label>`).join("");
  const sync = () => {
    const picked = box.querySelector("input[name=triggermode]:checked");
    box.querySelectorAll(".modeopt").forEach(el => {
      el.classList.toggle("on", el.querySelector("input").checked);
    });
    const row = $("dblrow");
    if (row) row.style.display = (picked && picked.value === "double") ? "block" : "none";
  };
  box.querySelectorAll("input[name=triggermode]").forEach(el => {
    el.addEventListener("change", sync);
  });
  const ms = $("dblms");
  if (ms) ms.value = c.double_tap_ms != null ? c.double_tap_ms : 400;
  sync();
}

async function loadConfig() {
  const c = await api("/api/config");
  configCache = c;
  $("trad").checked = !!c.traditional;
  $("noperiod").checked = c.remove_trailing_period !== false;
  $("mode").value = c.mode || "auto";
  renderMicStream(c);
  renderTriggerMode(c);

  // 錄音鍵：填入目前值與候選清單
  const hk = $("hotkey");
  if (hk) hk.value = c.hotkey || "RightCtrl";
  const dl = $("hotkey-list");
  if (dl) {
    dl.innerHTML = (c.hotkey_presets || []).map(k => `<option value="${esc(k)}"></option>`).join("");
  }

  // 麥克風優先順序
  micOrder = (c.mic_order && c.mic_order.length ? c.mic_order
             : (c.mic_names && c.mic_names.length ? c.mic_names
                : (c.mic_name ? [c.mic_name] : []))).slice();
  renderMicOrder();
  const add = $("micadd");
  if (add) {
    add.innerHTML = (c.devices || []).map(d => `<option>${esc(d.name)}</option>`).join("")
      || "<option>找不到音訊裝置</option>";
  }
  const addBtn = $("micadd-btn");
  if (addBtn) {
    addBtn.onclick = () => {
      const name = ($("micadd").value || "").trim();
      if (!name) return;
      if (micOrder.includes(name)) return;      // 不重複加同一支
      micOrder.push(name);
      renderMicOrder();
    };
  }
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
  const trigPick = document.querySelector("input[name=triggermode]:checked");

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
    hotkey: ($("hotkey").value || "").trim(),
    trigger_mode: trigPick ? trigPick.value : "hold",
    double_tap_ms: parseInt($("dblms").value, 10) || 400,
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
    // 存完重新讀一次，讓畫面（含狀態頁那一行）反映真正生效的值
    setTimeout(() => { loadConfig().catch(() => {}); }, 300);
  } catch (e) {
    // 後端會回具體原因（例如「錄音鍵無法解析」）——要讓使用者看到，
    // 不要只說「儲存失敗」。
    $("saved").textContent = "儲存失敗";
    $("saved").style.color = "var(--warn, #d9534f)";
    alert("儲存失敗：" + e.message);
  }
  setTimeout(() => { $("saved").textContent = ""; $("saved").style.color = "var(--ok)"; }, 3000);
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
