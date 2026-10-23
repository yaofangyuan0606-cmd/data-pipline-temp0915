/* 训练数据包页：功能 × 粒度矩阵 → 选格子 → 后台生成 → 看样本缩略图。接口见 /api/v1/packs。 */
(function () {
  let lastSig = "", ds = null, plan = null, sel = new Set(), planTimer = null, openPack = null, openCell = {};
  const PARTS = ["train", "val", "test", "unsplit"];
  const key = (t, l) => `${t}|${l}`;
  const fmtN = n => n == null ? "–" : Math.round(n).toLocaleString();

  function params() {
    const f = document.getElementById("tp-params"), v = n => f.elements[n].value.trim();
    const num = n => v(n) === "" ? null : Number(v(n));
    const size = v("patch_size").split(/[,\s×x]+/).map(Number).filter(x => x > 0);
    return { max_slices: num("max_slices"), qc_clean_ratio: num("qc_clean_ratio"), min_run: num("min_run"), shard_z: num("shard_z"),
             patch_size: size.length === 3 ? size : null, n_patches: num("n_patches"), seed: num("seed"), min_quality: num("min_quality") };
  }
  function saveSel() { try { localStorage.setItem("emqc.tp.sel." + ds, JSON.stringify([...sel])); } catch (e) {} }
  function loadSel() { try { return new Set(JSON.parse(localStorage.getItem("emqc.tp.sel." + ds) || "null") || []); } catch (e) { return new Set(); } }

  async function loadPlan() {
    if (!ds) return;
    const box = document.getElementById("tp-matrix");
    box.classList.add("loading");
    try { plan = await postJSON("/api/v1/packs/plan", { dataset_id: ds, ...params() }); }
    catch (e) { box.innerHTML = `<div class="muted">读取失败：${esc(e.message)}</div>`; return; }
    finally { box.classList.remove("loading"); }
    const readyKeys = new Set(plan.cells.filter(c => c.ready).map(c => key(c.task, c.level)));
    if (!sel.size && !loadSel().size) plan.cells.filter(c => c.ready && ["raw", "qc"].includes(c.task)).forEach(c => sel.add(key(c.task, c.level)));
    sel = new Set([...sel].filter(k => readyKeys.has(k)));
    facts(); matrix(); summary();
  }

  function facts() {
    const p = plan, bp = p.blocks_by_partition || {};
    const pills = ["train", "val", "test", "excluded", "none"].filter(k => bp[k]).map(k => `<span class="pill ${k === "excluded" ? "bad" : ""}"><i class="pt-dot pt-${k}"></i>${k} ${bp[k]}</span>`).join(" ");
    const labels = (p.labels || []).filter(a => !["skeleton", "agent_trace", "other"].includes(a.asset_type)).map(a => {
      const st = a.usable ? `<span class="pill ok">可用</span>` : a.validated ? `<span class="pill warn">验证未通过</span>` : a.can_validate ? `<button onclick="tpValidate(${a.asset_id}, this)">验证</button>` : `<span class="pill">点标注，不能用</span>`;
      return `<span class="tp-lab"><span class="mono">${esc(a.asset_type)}</span> <span class="muted mono">${esc(a.path)}</span> ${st}</span>`;
    }).join("") || `<span class="muted">没有登记标签 → 只能产出「原图」和「质检分类」</span>`;
    document.getElementById("tp-facts").innerHTML = `
      <div class="tp-fact"><div class="k">QC</div><div>${p.run_id ? `最近一次 <a href="/runs/${p.run_id}">#${p.run_id}</a> · ${p.n_blocks_qc}/${p.n_blocks} 个 block 有结果` : `<span class="s-bad">还没跑 QC</span> · <a href="/pipeline">去流水线</a>`}</div></div>
      <div class="tp-fact"><div class="k">划分（按 block）</div><div>${pills || '<span class="muted">–</span>'} <a class="muted" href="/patches?dataset=${encodeURIComponent(ds)}">调整 →</a></div></div>
      <div class="tp-fact grow"><div class="k">标签</div><div class="tp-labs">${labels}</div></div>`;
  }

  function partBar(bp) {
    const tot = PARTS.reduce((a, k) => a + (bp[k] || 0), 0);
    if (!tot) return "";
    return `<span class="tp-pbar" title="${PARTS.filter(k => bp[k]).map(k => `${k} ${bp[k]}`).join(" · ")}">${PARTS.filter(k => bp[k]).map(k => `<i class="pt-${k}" style="flex-grow:${bp[k]}"></i>`).join("")}</span>`;
  }

  function matrix() {
    const p = plan;
    const head = `<div class="tp-corner"><span class="muted">功能 ↓ · 粒度 →</span></div>` + p.levels.map(l => `<div class="tp-lh"><b>${l.zh}</b><span class="muted">${{ slice: "PNG + 标签 .npy", segment: "(n,H,W) .npy", patch: "(dz,dy,dx) .npy" }[l.level]}</span></div>`).join("");
    const rows = p.tasks.map(t => {
      const cells = p.levels.map(l => {
        const c = p.cells.find(x => x.task === t.task && x.level === l.level);
        if (!c.applicable) return `<div class="tp-cell na"><span class="muted">—</span></div>`;
        if (!c.ready) return `<div class="tp-cell off" title="${esc(c.reason || "")}"><span class="tp-why">${esc(c.reason || "不可用")}</span></div>`;
        const k = key(t.task, l.level), on = sel.has(k);
        const extra = c.by_class ? `坏 ${c.by_class.fail} · 好 ${c.by_class.clean}` : c.n_sections ? `${fmtN(c.n_sections)} 张切片` : "";
        return `<button type="button" class="tp-cell ${on ? "on" : ""}" data-k="${k}" aria-pressed="${on}">
          <span class="tp-chk">${on ? "✓" : ""}</span>
          <span class="tp-nrow"><span class="tp-n">${c.approx ? "≤ " : ""}${fmtN(c.n)}</span><span class="tp-unit">${{ slice: "张", segment: "段", patch: "个" }[l.level]}</span></span>
          ${partBar(c.by_partition)}
          <span class="tp-meta">${extra ? extra + " · " : ""}约 ${fmtBytes(c.est_bytes)}</span></button>`;
      }).join("");
      return `<div class="tp-task"><b>${t.zh}</b><span class="muted">${esc(t.use)}</span>${t.label ? `<span class="tp-lbl">标签：${esc(t.label)}</span>` : `<span class="tp-lbl">不需要标签</span>`}</div>${cells}`;
    }).join("");
    const box = document.getElementById("tp-matrix");
    box.innerHTML = head + rows;
    box.querySelectorAll("button.tp-cell").forEach(b => b.onclick = () => { const k = b.dataset.k; sel.has(k) ? sel.delete(k) : sel.add(k); saveSel(); matrix(); summary(); });
  }

  function summary() {
    const cs = plan ? plan.cells.filter(c => sel.has(key(c.task, c.level))) : [];
    const n = cs.reduce((a, c) => a + c.n, 0), b = cs.reduce((a, c) => a + c.est_bytes, 0);
    document.getElementById("tp-sel-n").textContent = cs.length ? `已选 ${cs.length} 格` : "还没选";
    document.getElementById("tp-sel-detail").textContent = cs.length ? `· 约 ${fmtN(n)} 个样本 · 约 ${fmtBytes(b)} · 写到服务器的导出目录` : "· 在上面的矩阵里点格子";
    document.getElementById("tp-build").disabled = !cs.length;
    const p = params();
    document.getElementById("tp-params-sum").textContent = `· 2D 每格 ≤ ${p.max_slices} 张 · 3D 段 ≥ ${p.min_run} 张 · patch ${(p.patch_size || []).join("×")} × ${p.n_patches}${p.min_quality != null ? " · 综合分 ≥ " + p.min_quality : ""}`;
  }

  window.tpPick = function (mode) {
    if (!plan) return;
    sel = new Set(mode === "none" ? [] : plan.cells.filter(c => c.ready && (mode === "ready" || ["raw", "qc"].includes(c.task))).map(c => key(c.task, c.level)));
    saveSel(); matrix(); summary();
  };
  window.tpParamsChanged = function () { summary(); clearTimeout(planTimer); planTimer = setTimeout(loadPlan, 450); };
  window.tpValidate = async function (assetId, btn) {
    btn.disabled = true; btn.textContent = "验证中…";
    try { await postJSON(`/api/v1/datasets/${encodeURIComponent(ds)}/assets/${assetId}/validate`, {}); flash("标签验证完成"); }
    catch (e) { flash("验证失败：" + e.message, true); }
    loadPlan();
  };
  window.tpBuild = async function (btn) {
    const cells = [...sel].map(k => k.split("|"));
    btn.disabled = true;
    try {
      const j = await postJSON("/api/v1/packs", { dataset_id: ds, cells, ...params() });
      flash(`数据包 #${j.job_id} 已开始生成`);
      openPack = j.job_id;
      refreshPacks();
    } catch (e) { flash("没能开始：" + e.message, true); }
    btn.disabled = false;
  };
  window.tpSelect = function (v) { ds = v; sel = loadSel(); openCell = {}; history.replaceState(null, "", `/training?dataset=${encodeURIComponent(v)}`); loadPlan(); refreshPacks(); };

  // ---------------------------------------------------------------- pack list
  async function refreshPacks() {
    if (!ds) return;
    let jobs;
    try { jobs = await (await fetch(`/api/v1/packs?dataset_id=${encodeURIComponent(ds)}`)).json(); } catch (e) { return; }
    const sig = JSON.stringify(jobs) + "|" + openPack + "|" + JSON.stringify(openCell);
    if (sig === lastSig) return;          // 没变化就不重画，缩略图不闪
    lastSig = sig;
    document.getElementById("tp-count").textContent = jobs.length;
    const box = document.getElementById("tp-packs");
    if (!jobs.length) { box.innerHTML = `<div class="empty">这个数据集还没有数据包</div>`; return; }
    if (openPack == null) openPack = jobs[0].job_id;
    box.innerHTML = jobs.map(j => packCard(j)).join("");
    box.querySelectorAll("[data-open]").forEach(b => b.onclick = () => { openPack = openPack === +b.dataset.open ? -1 : +b.dataset.open; lastSig = ""; refreshPacks(); });
    box.querySelectorAll("[data-cell]").forEach(b => b.onclick = () => { openCell[b.dataset.job] = b.dataset.cell; lastSig = ""; refreshPacks(); });
    box.querySelectorAll("[data-copy]").forEach(b => b.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(b.dataset.copy); flash("路径已复制"); });
    box.querySelectorAll("[data-cancel]").forEach(b => b.onclick = async () => { try { await postJSON(`/api/v1/exports/${b.dataset.cancel}/cancel`, {}); } catch (e) { flash(e.message, true); } refreshPacks(); });
    const j = jobs.find(x => x.job_id === openPack);
    if (j && (j.cells || []).length) samples(j);
  }

  function packCard(j) {
    const running = ["queued", "running"].includes(j.status), open = j.job_id === openPack;
    const prog = j.n_blocks ? Math.round(100 * j.n_blocks_done / j.n_blocks) : 0;
    const cells = (j.cells || []).map(c => `<span class="tp-chip ${c.status}" title="${esc(c.reason || "")}">${esc(c.task_zh || c.task)} · ${esc(c.level_zh || c.level)} <b>${c.n}</b></span>`).join("");
    const pending = (j.params.cells || []).length - (j.cells || []).length;
    return `<div class="card tp-pack ${open ? "open" : ""}">
      <div class="tp-pack-h">
        <button class="tp-pack-t" data-open="${j.job_id}"><span class="mono">#${j.job_id}</span> <span class="pill ${j.status === "done" ? "ok" : j.status === "error" ? "bad" : ""}">${esc(j.status)}</span>
          <span class="muted">${(j.finished_at || j.started_at || j.created_at || "").replace("T", " ").slice(0, 16)}</span></button>
        <span class="muted">${fmtN(j.n_sections)} 个样本 · ${fmtN(j.n_shards)} 个文件 · ${fmtBytes(j.n_bytes)}</span>
        ${running ? `<span class="tp-prog"><i style="width:${prog}%"></i></span><span class="muted mono">${j.n_blocks_done}/${j.n_blocks} 格</span><button data-cancel="${j.job_id}">取消</button>` : ""}
      </div>
      <div class="tp-chips">${cells}${pending > 0 && running ? `<span class="tp-chip wait">还有 ${pending} 格…</span>` : ""}</div>
      ${j.error ? `<pre class="code s-bad" style="max-height:120px;overflow:auto">${esc(j.error.split("\n")[0])}</pre>` : ""}
      ${j.out_dir ? `<div class="tp-path"><span class="muted">服务器路径</span> <code>${esc(j.out_dir)}</code> <button data-copy="${esc(j.out_dir)}">复制</button>
        <a href="/api/v1/packs/${j.job_id}/manifest" target="_blank">manifest</a></div>` : ""}
      ${open && (j.cells || []).length ? `<div class="tp-samples" id="tp-samples-${j.job_id}"><div class="muted">加载样本…</div></div>` : ""}
    </div>`;
  }

  const sampleCache = {};
  async function samples(j) {
    const box = document.getElementById(`tp-samples-${j.job_id}`); if (!box) return;
    const done = j.cells.filter(c => c.n > 0);
    if (!done.length) { box.innerHTML = `<div class="muted">没有样本</div>`; return; }
    const cur = openCell[j.job_id] && done.some(c => key(c.task, c.level) === openCell[j.job_id]) ? openCell[j.job_id] : key(done[0].task, done[0].level);
    const [t, l] = cur.split("|");
    const ck = `${j.job_id}|${cur}|${j.n_sections}`;
    let d = sampleCache[ck];
    if (!d) {
      try { d = sampleCache[ck] = await (await fetch(`/api/v1/packs/${j.job_id}/samples?task=${t}&level=${l}&n=12`)).json(); } catch (e) { return; }
    }
    const tabs = done.map(c => `<button class="${key(c.task, c.level) === cur ? "on" : ""}" data-cell="${key(c.task, c.level)}" data-job="${j.job_id}">${esc(c.task_zh)} · ${esc(c.level_zh)}</button>`).join("");
    const cell = done.find(c => key(c.task, c.level) === cur);
    const grid = d.samples.map(x => `<figure class="tp-thumb" title="${esc(x.file)}${x.label && x.label !== "None" ? "\n" + esc(x.label) : ""}"><img loading="lazy" src="${x.thumb}" alt="">
      <figcaption><i class="pt-dot pt-${esc(x.partition)}"></i>${esc(x.partition)} · z${esc(x.z0)}${+x.z1 - +x.z0 > 1 ? "–" + (+x.z1 - 1) : ""}${x.label && x.label !== "None" ? ` · <b>${esc(x.label)}</b>` : ""}</figcaption></figure>`).join("");
    const html = `<div class="tp-tabs">${tabs}</div>
      <div class="muted tp-cellinfo">${fmtN(d.n_total)} 个样本 · 索引 <code>${esc(cell.index)}</code>${cell.label ? ` · 标签：${esc(cell.label)}${cell.task === "qc" ? "（写在每张图下面）" : "（缩略图上叠成彩色）"}` : ""}${cell.unreadable_sections ? ` · 跳过读不了的切片 z ${cell.unreadable_sections.join(", ")}` : ""}${cell.skipped_empty_label ? ` · 跳过 ${cell.skipped_empty_label} 张标签为空的切片` : ""}</div>
      <div class="tp-grid">${grid}</div>`;
    if (box.dataset.k !== ck) { box.innerHTML = html; box.dataset.k = ck; }
    box.querySelectorAll("[data-cell]").forEach(b => b.onclick = () => { openCell[b.dataset.job] = b.dataset.cell; samples(j); });
  }

  window.tpInit = function () {
    const s = document.getElementById("tp-ds");
    ds = s && s.value;
    if (!ds) { document.getElementById("tp-matrix").innerHTML = `<div class="muted">还没有数据集，先去流水线扫描</div>`; return; }
    sel = loadSel();
    loadPlan(); refreshPacks();
    setInterval(() => { if (!document.hidden) refreshPacks(); }, 2500);
  };
})();
