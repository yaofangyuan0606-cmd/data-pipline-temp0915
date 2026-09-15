/* EM Image QC · front-end behaviour. No framework: fetch + DOM. */

// ---------------------------------------------------------------- helpers
async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText);
  return data;
}
function flash(msg, isError) {
  const el = document.getElementById("flash");
  if (!el) return alert(msg);
  el.textContent = msg; el.style.display = "block";
  el.style.borderColor = isError ? "var(--high)" : "var(--accent-line)";
  el.style.background = isError ? "color-mix(in srgb, var(--high) 12%, var(--surface))" : "var(--accent-soft)";
}
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = v => v == null ? "–" : Math.round(v * 100) + "%";
const fmtTs = ts => (ts || "").replace("T", " ").slice(5, 19);
function filterRows(tableId, key, on) {
  const rows = document.querySelectorAll(`#${tableId} tbody tr`);
  if (key === "all") { rows.forEach(r => r.classList.remove("hide")); return; }
  if (tableId === "runs") { rows.forEach(r => r.classList.toggle("hide", !r.dataset[key])); return; }
  const active = [...document.querySelectorAll(".filters input[type=checkbox]:checked")].map(c => c.getAttribute("onchange").match(/'(\w+)',this/)[1]);
  rows.forEach(r => r.classList.toggle("hide", active.some(k => r.dataset[k] !== "1")));
}

// ---------------------------------------------------------------- live pill in the top bar
async function showInstance() {
  try {
    const d = await (await fetch("/api/v1/deployment")).json();
    const el = document.getElementById("instance");
    if (!el || d.name === "local") return;
    el.textContent = d.name + (d.allow_delete_files ? "" : " · 只读数据");
    el.title = d.allow_delete_files ? "" : "该实例禁止删除数据文件";
    el.hidden = false;
  } catch (e) { /* 本机实例没有这个端点也无所谓 */ }
}
document.addEventListener("DOMContentLoaded", showInstance);

async function liveTick() {
  const el = document.getElementById("live"); if (!el) return;
  try {
    const st = await (await fetch("/api/v1/pipeline/status")).json();
    const n = st.active.length;
    el.className = "live" + (n ? " busy" : "");
    el.textContent = n ? `运行中 ${n} · ${st.active[0].stage || ""}` : `空闲 · ${st.datasets.total} 数据集 · ${st.runs.total} 次运行`;
  } catch (e) { el.className = "live"; el.textContent = "服务不可达"; el.style.color = "var(--high)"; }
}
document.addEventListener("DOMContentLoaded", () => { liveTick(); setInterval(liveTick, 5000); });

// ---------------------------------------------------------------- dataset actions
async function scanDatasets(btn) {
  btn.disabled = true; const old = btn.textContent; btn.textContent = "扫描中…";
  try {
    const res = await postJSON("/api/v1/datasets/scan");
    const errs = Object.keys(res.errors || {}).length;
    flash(`扫描 ${res.scanned} 个目录：新增 ${res.registered.length}，更新 ${res.updated.length}` + (errs ? `，错误 ${errs}：${JSON.stringify(res.errors)}` : ""), errs > 0);
    setTimeout(() => location.reload(), 900);
  } catch (e) { flash("扫描失败: " + e.message, true); btn.disabled = false; btn.textContent = old; }
}
async function runQC(datasetId, btn, blockIds) {
  if (btn) { btn.disabled = true; btn.textContent = "已排队…"; }
  try {
    const run = await postJSON("/api/v1/qc/runs", { dataset_id: datasetId, block_ids: blockIds || null });
    flash(`QC 运行 #${run.run_id} 已启动（${datasetId}）`);
    pollRun(run.run_id, btn);
  } catch (e) { flash("启动失败: " + e.message, true); if (btn) { btn.disabled = false; btn.textContent = "运行 QC"; } }
}
async function pollRun(runId, btn, onDone) {
  const run = await (await fetch(`/api/v1/qc/runs/${runId}`)).json();
  const p = run.progress == null ? 0 : Math.round(run.progress * 100);
  if (btn) btn.textContent = `${run.status} ${p}%`;
  const bar = document.getElementById("run-progress"); if (bar) bar.value = p;
  const st = document.getElementById("run-status"); if (st) st.textContent = `${run.status} · ${run.stage || ""} · ${run.n_blocks_done}/${run.n_blocks} blocks`;
  if (["done", "error", "cancelled"].includes(run.status)) {
    flash(run.status === "done" ? `运行 #${runId} 完成：留存率 ${pct(run.retention_rate)}，quality ${run.quality_score?.toFixed(3)}` : `运行 #${runId} ${run.status}：${run.error || ""}`, run.status !== "done");
    if (onDone) onDone(run); else setTimeout(() => location.reload(), 900);
    return;
  }
  setTimeout(() => pollRun(runId, btn, onDone), 1500);
}
async function deleteDataset(datasetId, btn) {
  if (!confirm(`删除数据集 ${datasetId} 的注册信息、全部 QC 结果和预览图？`)) return;
  const files = confirm("同时删除磁盘上的数据文件？\n仅当目录在 data_root 之下才允许；选“取消”只删注册与结果。");
  btn.disabled = true;
  try {
    const r = await fetch(`/api/v1/datasets/${datasetId}?remove_files=${files}`, { method: "DELETE" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    flash(`已删除 ${datasetId}：${data.runs} 次运行` + (data.files_removed ? "，数据文件已删除" : ""));
    setTimeout(() => location.href = "/", 900);
  } catch (e) { flash("删除失败: " + e.message, true); btn.disabled = false; }
}
async function cancelRun(runId, btn) {
  btn.disabled = true; btn.textContent = "取消中…";
  try { await postJSON(`/api/v1/qc/runs/${runId}/cancel`); flash(`已请求取消 #${runId}，当前 block 组结束后停止`); }
  catch (e) { flash("取消失败: " + e.message, true); btn.disabled = false; btn.textContent = "取消"; }
}
async function rerun(datasetId, btn) {
  btn.disabled = true;
  try { const runs = await postJSON("/api/v1/qc/runs/batch", { dataset_ids: [datasetId] }); flash(`已启动 #${runs[0].run_id}`); if (typeof refreshActive === "function" && document.getElementById("active")) refreshActive(); else setTimeout(() => location.href = `/runs/${runs[0].run_id}`, 600); }
  catch (e) { flash("启动失败: " + e.message, true); }
  btn.disabled = false;
}

// ---------------------------------------------------------------- execution graph (nodes = stages per z range / tile)
const STAGES = ["ingest", "slice_qc", "serial_qc", "aggregate", "persist"];
const STAGE_ZH = { ingest: "读取 · 统计", slice_qc: "切片检查", serial_qc: "序列检查", aggregate: "汇总 · 分级", persist: "落库" };

function nodeHtml(n, extra) {
  const dur = n.duration_s == null ? "" : `<span class="dur">${n.duration_s.toFixed(1)} s</span>`;
  const label = n.stage === "ingest" ? `${n.n_tiles} tile · 解码一次` : (extra || "");
  return `<div class="node n-${n.status} ${n.warn ? 'n-warn' : ''}" data-stage="${n.stage}" data-block="${n.block_id || ''}" onclick="nodeClick(this)" title="${esc(n.key)} · ${n.status}${n.n_events ? ' · ' + n.n_events + ' 条日志' : ''}">
    <div class="node-h"><b>${STAGE_ZH[n.stage] || n.stage}</b>${dur}</div><div class="node-s">${esc(label)}${n.n_events ? ` <span class="ev">${n.n_events}</span>` : ""}</div></div>`;
}
function renderGraph(g) {
  if (!g.groups.length) return '<div class="empty">没有 block</div>';
  let html = `<div class="dag-head"><span></span>${STAGES.map(s => `<span>${s}</span>`).join("")}</div>`;
  for (const grp of g.groups) {
    const rows = grp.tiles.length;
    html += `<div class="dag" style="grid-template-rows: repeat(${rows}, auto)">`;
    html += `<div class="dag-z" style="grid-column:1; grid-row: 1 / span ${rows}"><b>z ${grp.z_start}–${grp.z_end - 1}</b><span class="muted">${rows} tile</span></div>`;
    html += `<div class="dag-ingest" style="grid-column:2; grid-row: 1 / span ${rows}">${nodeHtml(grp.ingest)}</div>`;
    grp.tiles.forEach((t, i) => {
      const lbl = t.block_id.includes("_") ? t.block_id.slice(t.block_id.indexOf("_") + 1) : "整张";
      STAGES.slice(1).forEach((st, j) => {
        const n = t.nodes[st];
        const extra = st === "persist" && t.done ? `${lbl} · ${t.grade || ""} ${t.retention_rate == null ? "" : pct(t.retention_rate)}` : lbl;
        html += `<div style="grid-column:${j + 3}; grid-row:${i + 1}">${nodeHtml(n, extra)}</div>`;
      });
    });
    html += `</div>`;
  }
  return html;
}
function nodeClick(el) {
  const panel = document.getElementById("logpanel"); if (!panel || !panel._log) return;
  const stage = el.dataset.stage, block = el.dataset.block;
  document.querySelectorAll(".node.sel").forEach(n => n.classList.remove("sel")); el.classList.add("sel");
  panel._log.setNode(stage, block);
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- log panel (one per page; filters: run, node, level, search)
function LogPanel(container, opts) {
  const L = { runs: {}, runId: opts.runId || null, stage: null, block: null, level: "", q: "", paused: false, lastId: {}, seen: 0 };
  container.innerHTML = `<div class="log-tools">
      <select id="log-run" class="text" style="width:auto" title="运行"></select>
      <select id="log-level" class="text" style="width:auto"><option value="">全部级别</option><option value="warn">warn 及以上</option><option value="error">仅 error</option></select>
      <span id="log-node" class="pill" style="display:none"></span>
      <input id="log-q" class="text" style="width:200px" placeholder="搜索日志…">
      <label class="muted" style="font-size:12px"><input type="checkbox" id="log-pause"> 暂停滚动</label>
      <label class="muted" style="font-size:12px"><input type="checkbox" id="log-data"> 显示数据</label>
      <button onclick="document.getElementById('log-body').innerHTML=''">清空</button></div>
    <pre id="log-body" class="log">等待事件…</pre>`;
  const body = container.querySelector("#log-body"), runSel = container.querySelector("#log-run"), nodeEl = container.querySelector("#log-node");
  runSel.onchange = () => { L.runId = Number(runSel.value) || null; L.reset(); };
  container.querySelector("#log-level").onchange = e => { L.level = e.target.value; L.reset(); };
  container.querySelector("#log-q").oninput = e => { L.q = e.target.value.trim(); clearTimeout(L._t); L._t = setTimeout(() => L.reset(), 300); };
  container.querySelector("#log-pause").onchange = e => L.paused = e.target.checked;
  container.querySelector("#log-data").onchange = () => body.classList.toggle("show-data");
  L.setRuns = (list) => {  // [{run_id, dataset_id, status}]
    const cur = runSel.value;
    runSel.innerHTML = list.map(r => `<option value="${r.run_id}">#${r.run_id} ${esc(r.dataset_id)} · ${r.status}</option>`).join("");
    if (list.length && !list.some(r => String(r.run_id) === cur)) { runSel.value = String(list[0].run_id); L.runId = list[0].run_id; L.reset(); } else runSel.value = cur;
  };
  L.setNode = (stage, block) => {
    L.stage = stage; L.block = block || null;
    nodeEl.style.display = ""; nodeEl.innerHTML = `节点 ${esc(stage)}${block ? " · " + esc(block) : ""} <a href="#" onclick="event.preventDefault(); document.getElementById('logpanel')._log.setNode(null,null)">✕</a>`;
    if (!stage) nodeEl.style.display = "none";
    L.reset();
  };
  L.reset = () => { body.innerHTML = "等待事件…"; L.lastId[L.runId] = 0; L.seen = 0; L.seenIds = new Set(); L.tick(); };
  L.tick = async () => {
    if (!L.runId || L.busy) return;  // one fetch at a time: overlapping polls would append the same events twice
    L.busy = true;
    try { await L._fetch(); } finally { L.busy = false; }
  };
  L.seenIds = new Set();
  L._fetch = async () => {
    const p = new URLSearchParams({ after_id: L.lastId[L.runId] || 0, limit: 400 });
    if (L.stage) p.set("stage", L.stage); if (L.block) p.set("block_id", L.block); if (L.level) p.set("level", L.level); if (L.q) p.set("q", L.q);
    const r = await fetch(`/api/v1/qc/runs/${L.runId}/events?${p}`); if (!r.ok) return;
    const events = await r.json(); if (!events.length) return;
    if (body.textContent === "等待事件…") body.textContent = "";
    for (const e of events) {
      if (L.seenIds.has(e.id)) { L.lastId[L.runId] = Math.max(L.lastId[L.runId] || 0, e.id); continue; }
      L.seenIds.add(e.id);
      const line = document.createElement("div"); line.className = "ln " + e.level;
      const tag = e.stage ? `<span class="tag-s st-${esc(e.stage)}">${esc(e.stage)}</span>` : "";
      const blk = e.block_id ? `<span class="tag-b">${esc(e.block_id.includes("_") ? e.block_id.slice(e.block_id.indexOf("_") + 1) : e.block_id)}</span>` : "";
      const data = e.data && Object.keys(e.data).length ? `<span class="data">${esc(JSON.stringify(e.data))}</span>` : "";
      line.innerHTML = `<span class="ts">${fmtTs(e.ts)}</span> <span class="lv">${e.level.toUpperCase().padEnd(5)}</span> ${tag}${blk} ${esc(e.message)}${data}`;
      body.appendChild(line); L.lastId[L.runId] = e.id; L.seen++;
    }
    const cnt = document.getElementById("log-count"); if (cnt) cnt.textContent = `${L.seen} 条`;
    if (!L.paused) body.scrollTop = body.scrollHeight;
  };
  container._log = L;
  return L;
}

// ---------------------------------------------------------------- resource sparklines
function spark(values, max, cls) {
  if (!values.length) return "";
  const w = 160, h = 34, n = values.length;
  const pts = values.map((v, i) => `${(i / Math.max(n - 1, 1) * w).toFixed(1)},${(h - Math.min(v, max) / max * (h - 2) - 1).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline class="${cls}" points="${pts}"/></svg>`;
}
async function sysTick() {
  const box = document.getElementById("sysmon"); if (!box) return;
  try {
    const m = await (await fetch("/api/v1/system/metrics?last=90")).json();
    const s = m.samples, last = m.latest || {};
    if (!m.available) { box.innerHTML = '<span class="muted">未安装 psutil，无法采样</span>'; return; }
    const cards = [
      { k: `CPU · ${m.cpu_count} 核`, v: (last.cpu_pct ?? 0).toFixed(0) + "%", sub: `本进程 ${(last.proc_cpu_pct ?? 0).toFixed(0)}% · load ${last.load1 ?? "–"}`, series: s.map(x => x.cpu_pct || 0), max: 100 },
      { k: "内存", v: (last.mem_pct ?? 0).toFixed(0) + "%", sub: `${last.mem_used_gb ?? "–"} / ${last.mem_total_gb ?? "–"} GB · 本进程 ${(last.proc_rss_mb ?? 0).toFixed(0)} MB`, series: s.map(x => x.mem_pct || 0), max: 100 },
    ];
    if (m.gpu_available && last.gpus && last.gpus.length) {
      last.gpus.forEach((g, i) => cards.push({ k: `GPU ${g.index} · ${g.name}`, v: g.util_pct.toFixed(0) + "%", sub: `${(g.mem_used_mb / 1024).toFixed(1)} / ${(g.mem_total_mb / 1024).toFixed(1)} GB${g.temp_c != null ? " · " + g.temp_c + "°C" : ""}`, series: s.map(x => (x.gpus && x.gpus[i]) ? x.gpus[i].util_pct : 0), max: 100 }));
    } else cards.push({ k: "GPU", v: "无", sub: "本机没有 nvidia-smi；有 GPU 的机器上自动显示利用率与显存", series: [], max: 100 });
    box.innerHTML = cards.map(c => `<div class="tile" style="padding:8px 10px"><div class="k">${c.k}</div><div style="display:flex;align-items:center;gap:8px"><span class="v" style="font-size:18px">${c.v}</span>${spark(c.series, c.max, "sp")}</div><div class="sub">${c.sub}</div></div>`).join("");
    const meta = document.getElementById("sys-meta"); if (meta) meta.textContent = `每 ${m.interval_s} s 采样`;
  } catch (e) { box.innerHTML = '<span class="muted">资源采样不可用</span>'; }
}

// ---------------------------------------------------------------- pipeline console
let pipelineState = { runs: {}, graphs: {}, finished: new Set() };
function toggleChecks(ev, on) { ev.preventDefault(); document.querySelectorAll('input[name=check]').forEach(c => c.checked = on); }

async function launchRuns(ev) {
  ev.preventDefault();
  const f = ev.target, msg = document.getElementById("launch-msg");
  const datasets = [...f.querySelectorAll('input[name=dataset]:checked')].map(c => c.value);
  if (!datasets.length) { msg.textContent = "先选至少一个数据集"; return false; }
  const checks = [...f.querySelectorAll('input[name=check]:checked')].map(c => c.value);
  const blockIds = f.block_ids.value.split(",").map(x => x.trim()).filter(Boolean);
  const config = {};
  try {
    const th = JSON.parse(f.thresholds.value || "{}"), pa = JSON.parse(f.params.value || "{}");
    if (Object.keys(th).length) config.thresholds = th; if (Object.keys(pa).length) config.params = pa;
  } catch (e) { msg.textContent = "JSON 无法解析: " + e.message; return false; }
  if (checks.length) config.checks_enabled = checks;
  if (f.pass_max_severity.value) config.pass_max_severity = f.pass_max_severity.value;
  msg.textContent = "启动中…";
  try {
    const runs = await postJSON("/api/v1/qc/runs/batch", { dataset_ids: datasets, block_ids: blockIds.length ? blockIds : null, config: Object.keys(config).length ? config : null });
    msg.textContent = `已启动 ${runs.length} 条运行：` + runs.map(r => "#" + r.run_id).join(" ");
    refreshActive();
  } catch (e) { msg.textContent = "启动失败: " + e.message; }
  return false;
}

function runCard(r, g) {
  const p = r.progress == null ? 0 : Math.round(r.progress * 100);
  const nDone = (r.blocks || []).filter(b => b.done).length;
  const ret = r.retention_so_far == null ? "留存率 –" : `留存率 <b>${pct(r.retention_so_far)}</b>（已完成 ${nDone} block）`;
  const grades = Object.entries(r.grades_so_far || {}).map(([g, n]) => `<span class="grade grade-${g}">${g}</span><span class="muted mono">×${n}</span>`).join(" ");
  return `<div class="run-card" id="run-${r.run_id}">
    <div class="head"><b><a href="/runs/${r.run_id}" class="mono">#${r.run_id}</a> <a href="/datasets/${esc(r.dataset_id)}">${esc(r.dataset_id)}</a></b>
      <span class="pill on">${r.status}${r.cancel_requested ? " · cancelling" : ""}</span>
      <progress max="100" value="${p}"></progress><span class="mono">${nDone}/${r.n_blocks} blocks · ${r.n_findings} findings</span>
      <span class="mono">${ret} ${grades}</span><span class="muted mono" style="font-size:11px">${esc(r.stage || "")}</span>
      <button class="danger" onclick="cancelRun(${r.run_id}, this)" ${r.cancel_requested ? "disabled" : ""}>取消</button></div>
    <div class="graph-wrap">${g ? renderGraph(g) : '<span class="muted">加载节点图…</span>'}</div></div>`;
}

async function refreshActive() {
  const runs = await (await fetch("/api/v1/qc/runs/active")).json();
  const box = document.getElementById("active"); if (!box) return;
  document.getElementById("active-count").textContent = runs.length;
  const graphs = await Promise.all(runs.map(r => fetch(`/api/v1/qc/runs/${r.run_id}/graph`).then(x => x.json()).catch(() => null)));
  const prev = Object.keys(pipelineState.runs).map(Number);
  box.innerHTML = runs.length ? runs.map((r, i) => runCard(r, graphs[i])).join("") : '<div class="empty" id="active-empty">没有正在运行的任务</div>';
  const now = new Set(runs.map(x => x.run_id));
  const finished = prev.filter(id => !now.has(id) && !pipelineState.finished.has(id));
  pipelineState.runs = Object.fromEntries(runs.map(x => [x.run_id, x]));
  const panel = document.getElementById("logpanel");
  if (panel && panel._log) {
    const list = [...runs.map(r => ({ run_id: r.run_id, dataset_id: r.dataset_id, status: r.status })), ...[...pipelineState.finished, ...finished].map(id => pipelineState.doneMeta?.[id] || { run_id: id, dataset_id: "", status: "done" })];
    if (list.length) panel._log.setRuns(list);
    panel._log.tick();
  }
  for (const id of finished) { pipelineState.finished.add(id); await showFinished(id); }
}

async function showFinished(runId) {
  const r = await fetch(`/api/v1/qc/runs/${runId}`); if (!r.ok) return;
  const run = await r.json();
  pipelineState.doneMeta = pipelineState.doneMeta || {}; pipelineState.doneMeta[runId] = { run_id: runId, dataset_id: run.dataset_id, status: run.status };
  let box = document.getElementById("finished");
  if (!box) {
    const h = document.createElement("h2"); h.innerHTML = '本次完成 <span class="cnt">留存率与等级来自刚结束的运行</span>';
    box = document.createElement("div"); box.id = "finished"; box.className = "active-list";
    const anchor = document.getElementById("active");
    anchor.parentNode.insertBefore(h, anchor.nextSibling); anchor.parentNode.insertBefore(box, h.nextSibling);
  }
  const grades = Object.entries(run.grade_counts || {}).map(([g, n]) => `<span class="grade grade-${g}">${g}</span><span class="muted mono">×${n}</span>`).join(" ");
  const rows = (run.blocks || []).map(b => `<tr><td class="mono"><a href="/datasets/${esc(run.dataset_id)}/blocks/${esc(b.block_id)}?run_id=${run.run_id}">${esc(b.block_id)}</a></td><td><span class="grade grade-${b.grade || 'none'}">${b.grade || "–"}</span></td><td class="num mono">${pct(b.retention_rate)}</td><td class="num mono">${b.n_passed}/${b.n_slices}</td><td class="num mono">${b.longest_clean_run ?? "–"}</td><td>${esc(b.dominant_failure || "–")}</td><td class="num mono">${b.duration_s == null ? "–" : b.duration_s.toFixed(1)}</td></tr>`).join("");
  const card = document.createElement("div"); card.className = "run-card";
  card.innerHTML = `<div class="head"><b><a href="/runs/${run.run_id}" class="mono">#${run.run_id}</a> <a href="/datasets/${esc(run.dataset_id)}">${esc(run.dataset_id)}</a></b><span class="pill ${run.status === 'done' ? 'ok' : ''}">${run.status}</span>
      <span class="mono">留存率 <b style="font-size:16px">${pct(run.retention_rate)}</b> · quality ${run.quality_score == null ? "–" : run.quality_score.toFixed(3)} · ${grades}</span>
      <span class="muted mono" style="font-size:11px">${run.metrics && run.metrics.n_slices_passed != null ? run.metrics.n_slices_passed + "/" + run.metrics.n_slices_input + " tile-sections 通过" : ""}${run.metrics && run.metrics.duration_s ? " · " + run.metrics.duration_s.toFixed(1) + " s" : ""}</span></div>
    <table class="dense" style="margin-top:8px"><thead><tr><th>block</th><th>等级</th><th class="num">留存率</th><th class="num">通过</th><th class="num">clean run</th><th>主要问题</th><th class="num">耗时 s</th></tr></thead><tbody>${rows}</tbody></table>`;
  box.prepend(card);
}

function pipelineInit(activeIds) {
  const panel = document.getElementById("logpanel");
  if (panel) LogPanel(panel, { runId: activeIds && activeIds.length ? activeIds[0] : null });
  refreshActive(); setInterval(refreshActive, 1500);
  sysTick(); setInterval(sysTick, 3000);
}

// ---------------------------------------------------------------- run page
async function runPageInit(runId, live) {
  const panel = document.getElementById("logpanel");
  const L = panel ? LogPanel(panel, { runId }) : null;
  if (L) { L.setRuns([{ run_id: runId, dataset_id: "", status: live ? "running" : "done" }]); L.reset(); }
  const draw = async () => {
    const g = await (await fetch(`/api/v1/qc/runs/${runId}/graph`)).json();
    document.getElementById("graph").innerHTML = renderGraph(g);
  };
  await draw();
  if (live) { const t = setInterval(async () => { await draw(); if (L) L.tick(); const r = await (await fetch(`/api/v1/qc/runs/${runId}`)).json(); if (!["queued", "running"].includes(r.status)) { clearInterval(t); location.reload(); } }, 1500); }
}


// ---------------------------------------------------------------- delivery page
let deliveryState = { lastEvent: {}, seen: {} };

function fmtBytes(n) { if (n == null) return "–"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + " " + u[i]; }

async function launchExport(ev) {
  ev.preventDefault();
  const f = ev.target, msg = document.getElementById("export-msg");
  const body = { dataset_id: f.dataset_id.value, min_run: +f.min_run.value || 1, shard_z: +f.shard_z.value || 0, resume: f.resume.checked };
  if (f.min_quality.value) body.min_quality = +f.min_quality.value;
  const assets = f.with_assets.value.split(",").map(x => x.trim()).filter(Boolean); if (assets.length) body.with_assets = assets;
  const blocks = f.block_ids.value.split(",").map(x => x.trim()).filter(Boolean); if (blocks.length) body.block_ids = blocks;
  msg.textContent = "启动中…";
  try { const j = await postJSON("/api/v1/exports", body); msg.textContent = `任务 #${j.job_id} 已开始`; deliveryState.lastEvent[j.job_id] = 0; refreshExports(); }
  catch (e) { msg.textContent = "启动失败: " + e.message; }
  return false;
}

async function cancelExport(id, btn) { btn.disabled = true; try { await postJSON(`/api/v1/exports/${id}/cancel`); } catch (e) { flash("取消失败: " + e.message, true); btn.disabled = false; } }

function renderExport(j) {
  const pct = j.progress == null ? 0 : Math.round(j.progress * 100);
  const running = j.status === "queued" || j.status === "running";
  return `<div class="run-card" id="export-${j.job_id}">
    <div class="head"><b>导出 #${j.job_id} · <a href="/datasets/${j.dataset_id}">${j.dataset_id}</a></b> <span class="pill ${j.status === 'done' ? 'ok' : ''}">${j.status}${j.cancel_requested ? ' · cancelling' : ''}</span>
      <progress max="100" value="${pct}"></progress>
      <span class="mono">${j.n_blocks_done}/${j.n_blocks} blocks · ${j.n_shards} 分片 · ${j.n_sections} 张 · ${fmtBytes(j.n_bytes)}${j.n_shards_reused ? ' · ' + j.n_shards_reused + ' 复用' : ''}${j.n_asset_files ? ' · 资产 ' + j.n_asset_files : ''}</span>
      <span class="muted mono">QC run #${j.run_id} · min_run ${j.params.min_run} · shard_z ${j.params.shard_z}</span>
      ${running ? `<button onclick="cancelExport(${j.job_id}, this)" ${j.cancel_requested ? 'disabled' : ''}>取消</button>` : `<a class="btn" href="/api/v1/exports/${j.job_id}/manifest" target="_blank">清单</a>`}</div>
    ${j.out_dir ? `<div class="muted mono" style="font-size:11px;margin-top:6px">${j.out_dir}</div>` : ''}
    ${j.error ? `<pre class="json">${j.error.split("\n")[0]}</pre>` : ''}
    <pre class="log" id="export-log-${j.job_id}" style="max-height:140px;margin-top:8px"></pre></div>`;
}

async function refreshExports() {
  const r = await fetch("/api/v1/exports?limit=20"); const jobs = await r.json();
  const box = document.getElementById("exports"); if (!box) return;
  document.getElementById("export-count").textContent = jobs.length;
  const logs = {}; box.querySelectorAll("pre.log").forEach(p => logs[p.id] = p.innerHTML);
  box.innerHTML = jobs.length ? jobs.map(renderExport).join("") : '<div class="empty">还没有导出任务</div>';
  for (const [id, html] of Object.entries(logs)) { const el = document.getElementById(id); if (el) el.innerHTML = html; }
  await Promise.all(jobs.map(async j => {
    const after = deliveryState.lastEvent[j.job_id] || 0;
    const ev = await (await fetch(`/api/v1/exports/${j.job_id}/events?after_id=${after}`)).json();
    const el = document.getElementById(`export-log-${j.job_id}`); if (!el || !ev.length) return;
    for (const e of ev) { const line = document.createElement("div"); line.className = e.level; line.innerHTML = `<span class="ts">${(e.ts || '').replace('T', ' ')}</span> ${e.message}`; el.appendChild(line); deliveryState.lastEvent[j.job_id] = e.id; }
    el.scrollTop = el.scrollHeight;
  }));
}

async function launchStream(ev) {
  ev.preventDefault();
  const f = ev.target, msg = document.getElementById("stream-msg");
  const body = { dataset_id: f.dataset_id.value, z_chunk: +f.z_chunk.value || 16, order: f.order.value, skip_failed: f.skip_failed.checked, client: f.client.value || null };
  msg.textContent = "创建中…";
  try { const sess = await postJSON("/api/v1/streams", body); msg.innerHTML = `会话 #${sess.stream_id}：${sess.n_items} 个子块，约 ${fmtBytes(sess.est_bytes)}。客户端用 <code>stream_id=${sess.stream_id}</code> 或直接用 StreamClient 新建。`; refreshStreams(); }
  catch (e) { msg.textContent = "创建失败: " + e.message; }
  return false;
}

async function closeStream(id, btn) { btn.disabled = true; try { await postJSON(`/api/v1/streams/${id}/close`, { status: "aborted" }); refreshStreams(); } catch (e) { flash("关闭失败: " + e.message, true); btn.disabled = false; } }

async function refreshStreams() {
  const r = await fetch("/api/v1/streams?limit=30"); const rows = await r.json();
  const body = document.getElementById("streams-body"); if (!body) return;
  document.getElementById("stream-count").textContent = rows.length;
  body.innerHTML = rows.length ? rows.map(x => {
    const pct = x.progress == null ? 0 : Math.round(x.progress * 100);
    const secs = x.updated_at && x.created_at ? (new Date(x.updated_at) - new Date(x.created_at)) / 1000 : 0;
    const rate = secs > 0 && x.cursor ? (x.cursor / secs * 60).toFixed(1) + " 块/分" : "–";
    return `<tr><td class="mono">#${x.stream_id}</td><td><a href="/datasets/${x.dataset_id}">${x.dataset_id}</a></td><td>${x.client || '–'}</td><td><span class="pill ${x.status === 'done' ? 'ok' : ''}">${x.status}</span></td>
      <td><div style="display:flex;gap:8px;align-items:center"><progress max="100" value="${pct}" style="width:120px"></progress><span class="mono">${x.cursor}/${x.n_items}</span></div></td>
      <td class="num mono">${x.n_items}</td><td class="num mono">${x.n_acked}${x.n_failed_items ? ' <span class="s-bad">(' + x.n_failed_items + ' 失败)</span>' : ''}</td><td class="num mono">${fmtBytes(x.n_bytes)} / ${fmtBytes(x.est_bytes)}</td><td class="num mono">${rate}</td>
      <td class="muted mono" style="font-size:11px">${(x.created_at || '').replace('T', ' ')}</td><td>${x.status === 'open' ? `<button onclick="closeStream(${x.stream_id}, this)">终止</button>` : ''}</td></tr>`;
  }).join("") : '<tr><td colspan="11" class="muted">还没有推理流会话</td></tr>';
}

function deliveryInit() { refreshExports(); refreshStreams(); setInterval(refreshExports, 2000); setInterval(refreshStreams, 2000); }


// ---------------------------------------------------------------- patch factory page
let pfDs = null;
function pfSelect(ds) { pfDs = ds; history.replaceState(null, "", `/patches?dataset=${encodeURIComponent(ds)}`); pfRefreshAll(); }
function statusPill(st) { const cls = st === "ok" || st === "done" ? "ok" : (st === "warn" || st === "empty" ? "warn" : (st ? "bad" : "")); return `<span class="pill ${cls}">${esc(st || "未验证")}</span>`; }

async function pfLabels() {
  const rows = await (await fetch(`/api/v1/data/${pfDs}/labels`)).json();
  const body = document.querySelector("#pf-labels tbody");
  body.innerHTML = rows.length ? rows.map(a => {
    const v = a.validation; const canVal = (a.format || "").startsWith("image_stack") || ["npy", "precomputed"].includes(a.format);
    const shape = v && v.label_shape ? `${(v.native_shape || v.label_shape).join("×")}${v.downsampled_by > 1 ? ` → 等距抽样到 EM 网格 (1/${v.downsampled_by})` : ""}${v.scale_to_em ? (v.scale_to_em[0] > 1 ? " · 仅 1/" + v.scale_to_em[0] + " 分辩率" : "") : " · 与 EM 对不上"}` : "–";
    const issues = v ? (Object.entries(v.issues || {}).map(([k, n]) => `<span class="pill">${esc(k)} ${n}</span>`).join("") || '<span class="muted">无</span>') : "";
    const notes = v && v.notes && v.notes.length ? `<details style="margin-top:3px"><summary class="muted" style="font-size:11px;cursor:pointer">说明 (${v.notes.length})</summary><div class="muted" style="font-size:11px;line-height:1.4;max-width:420px">${v.notes.map(esc).join("<br>")}</div></details>` : "";
    return `<tr><td class="mono">${esc(a.asset_type)}</td><td class="mono">${esc(a.path)}</td><td class="mono">${shape}</td><td>${statusPill(v ? v.status : null)}${v && v.usable_for_patches ? ' <span class="pill ok">可出 patch</span>' : ""}</td><td>${issues}${notes}</td>
      <td style="white-space:nowrap">${canVal ? `<button onclick="pfValidate(${a.id}, this)">验证</button>` : '<span class="muted">非体数据</span>'}</td></tr>`;
  }).join("") : '<tr><td colspan="6" class="muted">没有标签资产</td></tr>';
}
async function pfValidate(assetId, btn) { btn.disabled = true; btn.textContent = "验证中…"; try { await postJSON(`/api/v1/datasets/${pfDs}/assets/${assetId}/validate`, {}); } catch (e) { flash("验证失败: " + e.message, true); } await pfLabels(); await pfReady(); }

async function pfPartitionView() {
  const p = await (await fetch(`/api/v1/datasets/${pfDs}/partition`)).json();
  const c = p.counts || {}; const cfg = p.config || {};
  const bar = ["train", "val", "test", "excluded", "none"].map(k => `<span class="pill ${k === 'train' ? 'ok' : (k === 'excluded' ? 'bad' : '')}">${k} ${c[k] || 0}</span>`).join(" ");
  const rows = (p.blocks || []).map(b => `<tr><td class="mono">${esc(b.block_id)}</td><td>${esc(b.split)}</td><td><b>${esc(b.partition)}</b></td><td>${esc(b.latest_grade || "–")}</td><td class="num mono">${b.difficulty == null ? "–" : b.difficulty.toFixed(2)}</td><td class="mono" style="font-size:11px">${esc(b.label_version || "–")}</td></tr>`).join("");
  document.getElementById("pf-partition").innerHTML = `<div style="margin-bottom:8px">${bar}${cfg.ratios ? ` <span class="muted mono" style="font-size:11px">ratios ${cfg.ratios.join(":")} · seed ${cfg.seed}${cfg.adjacent_cross_partition_pairs ? " · 跨划分相邻块对 " + cfg.adjacent_cross_partition_pairs : ""}</span>` : ""}</div>
    ${cfg.note ? `<div class="muted" style="font-size:12px;margin-bottom:6px">${esc(cfg.note)}</div>` : ""}
    <div class="scroll" style="max-height:220px"><table class="dense"><thead><tr><th>block</th><th>用途</th><th>划分</th><th>等级</th><th class="num">难度</th><th>标签版本</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
async function pfPartition(force) {
  const msg = document.getElementById("pf-part-msg");
  if (force && !confirm("强制重排会改变 val/test 集合，之前基于旧划分的实验将不可比。继续？")) return;
  const ratios = document.getElementById("pf-ratios").value.trim() || null;
  try { const r = await postJSON(`/api/v1/datasets/${pfDs}/partition`, { ratios, force }); msg.textContent = `已更新：${JSON.stringify(r.result.counts)}`; } catch (e) { msg.textContent = "失败: " + e.message; }
  await pfPartitionView();
}

const PF_ZH = { segmentation: "分割", membrane: "膜 / 边界", synapse: "突触", mitochondria: "线粒体", proofreading: "校对候选", hard_negative: "困难负样本", failure: "失败样本" };
async function pfReady() {
  const r = await (await fetch(`/api/v1/data/${pfDs}/patch-readiness`)).json();
  document.getElementById("pf-ready").innerHTML = Object.entries(r.types).map(([t, v]) => `<div class="ready ${v.ready ? 'ok' : ''}"><b>${t}</b>${PF_ZH[t] || ""} · ${v.ready ? "可生成" : "未就绪"}${v.ready ? "" : `<span class="why" title="${esc(v.reason)}">${esc(v.reason)}</span>`}</div>`).join("");
}
async function pfCreate(ev) {
  ev.preventDefault();
  const f = ev.target, msg = document.getElementById("pf-msg");
  const body = { dataset_id: pfDs, patch_type: f.patch_type.value, size: f.size.value.split(",").map(Number), n: +f.n.value || 64, seed: +f.seed.value || 0, only_passed: f.only_passed.checked };
  const parts = f.partitions.value.split(/[ ,]+/).filter(Boolean); if (parts.length) body.partitions = parts;
  if (f.min_quality.value) body.min_quality = +f.min_quality.value;
  if (f.params.value.trim()) { try { body.params = JSON.parse(f.params.value); } catch (e) { msg.textContent = "阈值覆盖不是合法 JSON"; return false; } }
  msg.textContent = "生成中…";
  try { const p = await postJSON("/api/v1/patchsets", body); msg.textContent = p.status === "done" ? `集合 #${p.set_id}：${p.n_patches} 个 patch，检查${p.checks.passed ? "通过" : "未通过"}` : `集合 #${p.set_id} ${p.status}：${p.reason || ""}`; }
  catch (e) { msg.textContent = "失败: " + e.message; }
  await pfSets();
  return false;
}
async function pfDelete(id, btn) { if (!confirm(`删除 patch 集合 #${id}？`)) return; btn.disabled = true; await fetch(`/api/v1/patchsets/${id}`, { method: "DELETE" }); await pfSets(); }
async function pfSets() {
  const rows = await (await fetch(`/api/v1/patchsets?dataset_id=${pfDs}&limit=50`)).json();
  document.getElementById("pf-count").textContent = rows.length;
  document.getElementById("pf-sets-body").innerHTML = rows.length ? rows.map(p => {
    const bp = Object.entries(p.counts.by_partition || {}).map(([k, n]) => `${k} ${n}`).join(" · ") || "–";
    const ck = p.checks && p.checks.n_patches ? (p.checks.passed ? '<span class="pill ok">通过</span>' : `<span class="pill bad">未通过</span> <span class="mono" style="font-size:11px">dup ${p.checks.n_duplicate_exact} · leak ${p.checks.n_overlap_cross_partition} · out ${p.checks.n_outside_block}</span>`) : "–";
    return `<tr><td class="mono">#${p.set_id}</td><td><b>${esc(p.patch_type)}</b></td><td>${statusPill(p.status)}${p.reason ? `<div class="muted" style="font-size:11px;max-width:320px">${esc(p.reason)}</div>` : ""}</td><td class="num mono">${p.n_patches}</td><td class="mono" style="font-size:11px">${esc(bp)}</td><td class="mono">${(p.params.size || []).join("×")}</td><td class="mono" style="font-size:11px">${esc(p.label_version || "–")}</td><td>${ck}</td><td class="mono">${p.qc_run_id ? "#" + p.qc_run_id : "–"}</td><td class="muted mono" style="font-size:11px">${fmtTs(p.created_at)}</td>
      <td style="white-space:nowrap"><a class="btn" href="/api/v1/patchsets/${p.set_id}/manifest" target="_blank">清单</a> <button onclick="pfDelete(${p.set_id}, this)">删除</button></td></tr>`;
  }).join("") : '<tr><td colspan="11" class="muted">还没有 patch 集合</td></tr>';
}
async function pfRefreshAll() { await Promise.all([pfLabels(), pfPartitionView(), pfReady(), pfSets()]); }
function pfInit() { pfDs = document.getElementById("pf-ds").value; pfRefreshAll(); }


// ---------------------------------------------------------------- acquisition page
function cwRoi(v) { const p = v.split(",").map(x => x.trim()).filter(Boolean); const o = {}; ["x","y","z"].forEach((a,i)=>{ const [lo,hi]=(p[i]||"").split("-"); o[a]=[+lo,+hi]; }); return o; }
let cwLast = { roi: null, url: null, mip: 1, precheck: null };

async function cwVolume(ev) {
  ev.preventDefault(); const f = ev.target, msg = document.getElementById("cw-vol-msg");
  msg.textContent = "读取中…";
  try {
    const d = await (await fetch(`/api/v1/crawl/volume?url=${encodeURIComponent(f.url.value)}&mip=${+f.mip.value||0}`)).json();
    if (d.detail) throw new Error(d.detail);
    cwLast.url = f.url.value; cwLast.mip = +f.mip.value || 0;
    const um = d.size_xyz.map((v,i)=>(v*d.resolution_nm[i]/1000).toFixed(0));
    document.getElementById("cw-vol").innerHTML = `<table class="dense" style="margin-top:10px"><tbody>
      <tr><td>分辨率</td><td class="mono">${d.resolution_nm.join(" × ")} nm　<span class="muted">mip ${d.mip} / 共 ${d.n_mips} 级</span></td></tr>
      <tr><td>体素尺寸</td><td class="mono">${d.size_xyz.join(" × ")}　<span class="muted">≈ ${um.join(" × ")} µm</span></td></tr>
      <tr><td>chunk</td><td class="mono">${d.chunk_xyz.join(" × ")}　<span class="muted">传输的最小单位</span></td></tr>
      <tr><td>编码</td><td class="mono">${esc(d.encoding)} ${d.lossy ? '<span class="pill bad">有损</span>' : '<span class="pill ok">无损</span>'}　dtype ${esc(d.dtype)}</td></tr></tbody></table>`;
    msg.textContent = "";
  } catch (e) { msg.textContent = "失败: " + e.message; }
  return false;
}

async function cwPrecheck(ev) {
  ev.preventDefault(); const f = ev.target, msg = document.getElementById("cw-pre-msg");
  const roi = cwRoi(f.roi.value); cwLast.roi = roi;
  msg.textContent = "预判中…";
  try {
    const d = await postJSON("/api/v1/crawl/precheck", { roi, mask_url: f.mask_url.value, min_wanted: +f.min_wanted.value, max_defect: +f.max_defect.value });
    cwLast.precheck = d;
    const comp = Object.entries(d.composition).map(([k,v]) => `<span class="pill ${k==='fissure'?'bad':(k==='neuropil'?'ok':'')}">${esc(k)} ${(v*100).toFixed(1)}%</span>`).join(" ");
    document.getElementById("cw-pre").innerHTML = `<div style="margin-top:10px">${comp}</div>
      <div style="margin-top:8px"><span class="pill ${d.verdict==='ok'?'ok':'bad'}">${d.verdict==='ok'?'值得爬':'建议换一块'}</span>
      <span class="mono muted" style="font-size:11px">neuropil ${(d.wanted_frac*100).toFixed(1)}% · fissure ${(d.defect_frac*100).toFixed(2)}% · 逐轴倍数 ${d.divisors_xyz.join("/")} · ${d.seconds}s</span></div>
      ${d.reasons.length ? `<div class="muted" style="font-size:12px;margin-top:4px">${d.reasons.map(esc).join("<br>")}</div>` : ""}`;
    msg.textContent = "";
  } catch (e) { msg.textContent = "失败: " + e.message; }
  return false;
}

function cwBody(f) {
  const roi = cwLast.roi || cwRoi(document.querySelector('input[name=roi]').value);
  const b = { dataset_id: f.dataset_id.value.trim(), url: cwLast.url || document.querySelector('input[name=url]').value, roi, mip: cwLast.mip };
  if (!b.dataset_id) throw new Error("请填 dataset_id");
  return b;
}

async function cwRegister(ev) {
  ev.preventDefault(); const f = ev.target, msg = document.getElementById("cw-reg-msg");
  msg.textContent = "注册中…";
  try {
    const b = cwBody(f);
    b.species = f.species.value || null; b.brain_region = f.brain_region.value || null; b.require_precheck_ok = f.require_ok.checked;
    if (f.seg_url.value.trim()) b.assets = [{ type: "gt_segmentation", url: f.seg_url.value.trim(), mip: +f.seg_mip.value || 0, format: "precomputed_cloud", label_encoding: "gray" }];
    const d = await postJSON("/api/v1/crawl/register", b);
    msg.innerHTML = `已注册 <a href="/datasets/${d.dataset_id}">${esc(d.dataset_id)}</a>：${d.shape.z}×${d.shape.y}×${d.shape.x}，可直接跑 QC`;
  } catch (e) { msg.textContent = "失败: " + e.message; }
  return false;
}

async function cwFetch() {
  const f = document.querySelector('#content form:last-of-type') || document.forms[2];
  const msg = document.getElementById("cw-reg-msg");
  msg.textContent = "启动下载…";
  try { const j = await postJSON("/api/v1/crawl/jobs", cwBody(f)); msg.textContent = `采集任务 #${j.job_id} 已开始`; cwJobs(); }
  catch (e) { msg.textContent = "失败: " + e.message; }
}

async function cwJobs() {
  const rows = await (await fetch("/api/v1/crawl/jobs?limit=20")).json();
  document.getElementById("cw-count").textContent = rows.length;
  const box = document.getElementById("cw-jobs");
  box.innerHTML = rows.length ? rows.map(j => {
    const pct = j.progress == null ? 0 : Math.round(j.progress * 100);
    const running = j.status === "queued" || j.status === "running";
    const w = j.wire || {};
    return `<div class="run-card"><div class="head"><b>采集 #${j.job_id} · ${esc(j.dataset_id)}</b> <span class="pill ${j.status==='done'?'ok':''}">${esc(j.status)}</span>
      <progress max="100" value="${pct}"></progress>
      <span class="mono">${j.n_done}/${j.n_sections} 张 · ${fmtBytes(j.n_bytes)}${w.chunks_fetched ? ` · ${w.chunks_fetched} chunk / ${(w.seconds||0).toFixed(1)}s` : ""}</span>
      <span class="muted mono">mip ${j.mip} · ${j.roi ? `x${j.roi.x.join("-")} y${j.roi.y.join("-")} z${j.roi.z.join("-")}` : ""}</span>
      ${running ? `<button onclick="cwCancel(${j.job_id}, this)">取消</button>` : ""}</div>
      ${j.out_dir ? `<div class="muted mono" style="font-size:11px;margin-top:6px">${esc(j.out_dir)}</div>` : ""}
      ${j.error ? `<pre class="json">${esc(j.error.split("\n")[0])}</pre>` : ""}</div>`;
  }).join("") : '<div class="empty">还没有采集任务</div>';
}
async function cwCancel(id, btn) { btn.disabled = true; try { await postJSON(`/api/v1/crawl/jobs/${id}/cancel`); } catch (e) { flash("取消失败: " + e.message, true); btn.disabled = false; } }
function cwInit() { cwJobs(); setInterval(cwJobs, 2500); }
