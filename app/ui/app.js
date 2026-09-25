// VibeTalkie UI 邏輯
//
// 刻意與 index.html 分開：
//   1. 這是純靜態檔案，改完重新整理即可，**不需要任何建置步驟**
//   2. 分開之後 HTML 裡沒有 inline script。非瀏覽器行程（例如 PowerShell
//      的 Invoke-WebRequest）下載一個內含 inline JS 的網頁，會被
//      Cortex XDR 的 `amsi_malicious_js_activity` 規則盯上（實測踩過）。
//      內容分離可以少掉這一類誤判，也比較好維護。

const $ = id => document.getElementById(id);

const STATE_TEXT = {
  idle:       ["待命", "按住錄音鍵說話"],
  recording:  ["錄音中…", "放開錄音鍵結束"],
  processing: ["辨識中…", "正在跑本機模型"],
  inserting:  ["注入中…", "寫入前景視窗"],
  error:      ["發生錯誤", "請看下方訊息"],
};

let lastCount = -1;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function esc(t) {
  return String(t).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function render(s) {
  const [name, meta] = STATE_TEXT[s.state] || ["未知", s.state];
  $("state").textContent = name;
  $("statemeta").textContent = meta;
  $("dot").className = "dot " + s.state;

  $("counters").textContent =
    `按 ${s.presses} · 成功 ${s.inserted} · 空 ${s.empty} · 失敗 ${s.failed}`;
  $("engine").textContent = s.engine || "—";
  $("model").textContent = s.model || "—";

  if (s.latency_ms) {
    $("latency").textContent =
      `最近一次：放開到文字出現 ${s.latency_ms} ms` +
      `（辨識 ${s.asr_ms} + 貼上 ${s.paste_ms}）`;
  }

  // 錄音中的即時音量
  $("level").style.width = Math.round((s.level || 0) * 100) + "%";

  const log = $("log");
  if (s.history && s.history.length !== lastCount) {
    lastCount = s.history.length;
    log.innerHTML = s.history.length
      ? s.history.map(h => `<div class="entry">
           <div class="txt">${esc(h.text)}</div>
           <div class="meta">${esc(h.time)} · ${h.ms} ms · ${h.chars} 字</div>
         </div>`).join("")
      : '<div class="empty">還沒有紀錄 —— 按住錄音鍵說一句話試試</div>';
  }

  // 有下載在跑時才定期重抓模型清單（平常不打擾伺服器）
  if (s.downloads && s.downloads.length && Date.now() - lastModelFetch > 1000) {
    lastModelFetch = Date.now();
    refreshModels();
  }

  // 兩類警告共用同一個提示區塊
  const warns = [s.vendor_warning, s.mic_warning].filter(Boolean);
  if (warns.length) {
    $("vendor").style.display = "block";
    $("vendor").innerHTML = warns.map(w => `<div>⚠️ ${esc(w)}</div>`).join("");
  } else {
    $("vendor").style.display = "none";
  }

  if (s.error) {
    $("state").textContent = "發生錯誤";
    $("statemeta").textContent = s.error;
  }
}

async function tick() {
  try {
    render(await api("/api/status"));
  } catch (e) {
    $("state").textContent = "連不上本機服務";
    $("statemeta").textContent = "請確認 VibeTalkie 還在執行";
    $("dot").className = "dot error";
  }
}

// ---------------------------------------------------------------- 模型

const MODEL_LABEL = {
  ready: "已下載", absent: "未下載", queued: "排隊中",
  downloading: "下載中", extracting: "解壓縮中",
  error: "失敗", cancelled: "已取消",
};

let modelsCache = [];
let lastModelFetch = 0;

function fmtMb(mb) {
  if (!mb) return "—";
  return mb >= 1000 ? (mb / 1000).toFixed(2) + " GB" : Math.round(mb) + " MB";
}

function renderModels(list) {
  modelsCache = list || [];
  const box = $("models");
  if (!modelsCache.length) {
    box.innerHTML = '<div class="empty">沒有可用的模型清單</div>';
    return;
  }
  box.innerHTML = modelsCache.map(m => {
    const st = m.state;
    const busy = st === "queued" || st === "downloading" || st === "extracting";
    const dl = m.download || {};

    let tags = "";
    if (m.active) tags += '<span class="tag on">使用中</span>';
    if (m.recommended && !m.active) tags += '<span class="tag rec">預設建議</span>';
    tags += `<span class="tag ${st === "error" ? "bad" : ""}">${MODEL_LABEL[st] || st}</span>`;

    let action = "";
    if (st === "ready") {
      action = m.active
        ? '<span class="size">目前使用中</span>'
        : `<button class="sm" data-select="${esc(m.name)}">切換使用</button>`;
    } else if (busy) {
      const extra = dl.speed_mbps ? `　${dl.speed_mbps} MB/s` : "";
      action = `<span class="size">${dl.downloaded_mb || 0} / ${dl.total_mb || "?"} MB${extra}</span>
                <button class="sm ghost" data-cancel="${esc(m.name)}">取消</button>`;
    } else {
      action = `<button class="sm" data-download="${esc(m.name)}">下載（${fmtMb(m.size_mb)}）</button>`;
    }

    let bar = "";
    if (busy) {
      const pct = st === "extracting" ? 100 : (dl.percent || 0);
      bar = `<div class="pbar"><i style="width:${pct}%"></i></div>`;
    }

    const errLine = (st === "error" && dl.error)
      ? `<div class="desc" style="color:var(--err)">⚠️ ${esc(dl.error)}</div>` : "";

    return `<div class="model">
      <div class="info">
        <div class="name">${esc(m.title || m.name)} ${tags}</div>
        <div class="desc">${esc(m.langs || "")}${m.langs ? "　·　" : ""}${fmtMb(m.size_mb)}</div>
        <div class="desc">${esc(m.note || "")}</div>
        ${errLine}
        ${bar}
      </div>
      <div class="side">${action}</div>
    </div>`;
  }).join("");

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
    if (!r.ok) {
      alert(r.message || "操作失敗");
    } else if (!quiet) {
      $("saved").textContent = r.message || "已送出";
      setTimeout(() => { $("saved").textContent = ""; }, 2500);
    }
  } catch (e) {
    alert("操作失敗：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    refreshModels();
  }
}

async function refreshModels() {
  try {
    const r = await api("/api/models");
    renderModels(r.models);
  } catch (e) {
    /* 靜默：狀態輪詢會再試 */
  }
}

async function loadConfig() {
  const c = await api("/api/config");
  $("trad").checked = !!c.traditional;
  $("mode").value = c.mode || "auto";

  // 裝置 index 會隨藍牙重連改變（實測踩過），所以 value 用 index、
  // 但同時帶上 name，存檔時以 name 為主要識別。
  // 見 app/vibetalkie.py 的 resolve_device()。
  const sel = $("device");
  const want = c.mic_name || "";
  sel.innerHTML = (c.devices || []).map(d => {
    const on = (want && d.name === want) || (!want && d.index === c.device_index);
    return `<option value="${d.index}" data-name="${esc(d.name)}"` +
           `${on ? " selected" : ""}>${esc(d.name)}</option>`;
  }).join("") || '<option>找不到音訊裝置</option>';
}

$("save").onclick = async () => {
  const sel = $("device");
  const opt = sel.options[sel.selectedIndex];
  await api("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      traditional: $("trad").checked,
      mode: $("mode").value,
      device_index: parseInt(sel.value, 10),
      mic_name: opt ? (opt.dataset.name || "") : "",
    }),
  });
  $("saved").textContent = "已儲存 ✓";
  setTimeout(() => { $("saved").textContent = ""; }, 2000);
};

loadConfig().catch(() => {});
refreshModels();
tick();
setInterval(tick, 500);
