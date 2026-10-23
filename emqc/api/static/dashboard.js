/* 流水线 dashboard：/api/v1/pipeline/dashboard 一次取回各维度的数据，这里画成图。
   不用图表库，纯 SVG / HTML。所有颜色来自 style.css 的变量（深色模式自动换）。
   每个有图的卡片都能悬停看数值，并能展开「数据表」。 */
(function () {
  const STAGES = ["ingest", "slice_qc", "serial_qc", "aggregate", "persist"];
  const STAGE_ZH = { ingest: "读取 · 统计", slice_qc: "切片检查", serial_qc: "序列检查", aggregate: "汇总 · 分级", persist: "落库" };
  const SEV = ["critical", "high", "medium", "low"];
  const SEV_ZH = { critical: "critical 致命", high: "high 严重", medium: "medium 中等", low: "low 轻微" };
  const CHECK_ZH = { blank_slice: "空白切片", missing_slice: "缺失切片", corrupt_slice: "损坏切片", severe_blur: "严重模糊", saturation: "过曝 / 饱和", crack: "裂缝 / 褶皱",
    brightness_jump: "亮度突变", contrast_drift: "对比度漂移", charging_artifact: "荷电伪影", contamination: "表面污染", slice_jump: "切片跳变",
    local_misalignment: "局部错位", global_misalignment: "整体错位", z_order_error: "顺序错误", local_deformation: "局部形变", section_deformation: "切片形变" };
  const pctf = v => v == null ? "–" : Math.round(v * 100) + "%";
  const num = v => v == null ? "–" : (v >= 1000 ? Math.round(v).toLocaleString() : (Math.round(v * 10) / 10).toString());
  const short = id => id.length > 26 ? id.slice(0, 24) + "…" : id;
  let state = { days: 0, dataset: "" }, timer = null;

  // ---------------------------------------------------------------- tooltip (one for the page)
  let tip;
  function tipInit(root) {
    if (!tip) { tip = document.createElement("div"); tip.className = "dash-tip"; tip.hidden = true; document.body.appendChild(tip); }
    root.addEventListener("mouseover", e => { const t = e.target.closest("[data-tip]"); if (!t) return; tip.innerHTML = t.dataset.tip; tip.hidden = false; });
    root.addEventListener("mousemove", e => { if (tip.hidden) return; const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8); tip.style.left = x + "px"; tip.style.top = (e.clientY + 14) + "px"; });
    root.addEventListener("mouseout", e => { if (e.target.closest("[data-tip]")) tip.hidden = true; });
  }
  // lines are already HTML (values passed through esc); only escape for the attribute itself
  const tipAttr = lines => `data-tip="${lines.join("<br>").replace(/&/g, "&amp;").replace(/"/g, "&quot;")}"`;

  function card(title, sub, body, table) {
    return `<section class="card dash-card"><div class="dash-h"><b>${title}</b>${sub ? `<span class="muted">${sub}</span>` : ""}</div>${body}
      ${table ? `<details class="dash-table"><summary class="muted">数据表</summary>${table}</details>` : ""}</section>`;
  }
  const empty = msg => `<div class="muted dash-empty">${msg}</div>`;

  // ---------------------------------------------------------------- KPI row
  function kpis(d) {
    const k = d.kpi, dl = d.delivery;
    const tiles = [
      ["数据集", k.n_datasets, `${k.n_runs} 次运行${k.n_runs_error ? ` · <span class="s-bad">${k.n_runs_error} 次出错</span>` : ""}`],
      ["已检查切片", num(k.n_sections), "各数据集最近一次运行"],
      ["留存率", pctf(k.retention), `${num(k.n_sections - k.n_bad)} 张可用`, true],
      ["剔除的切片", num(k.n_bad), `${num(k.n_problems)} 条 high 以上的问题`],
      ["处理速度", k.sections_per_s == null ? "–" : num(k.sections_per_s), "切片 / 秒（各次运行中位数）"],
      ["已交付", num(dl.export_sections), `张进训练分片 · ${num(dl.stream_chunks)} 块推理`],
    ];
    return `<div class="tiles dash-kpis">${tiles.map(([t, v, s, acc]) => `<div class="tile ${acc ? "accent" : ""}"><div class="k">${t}</div><div class="v">${v}</div><div class="sub">${s}</div></div>`).join("")}</div>`;
  }

  // ---------------------------------------------------------------- 1. retention over runs (line)
  function trend(runs) {
    if (!runs.length) return card("留存率趋势", "", empty("这个范围内还没有完成的运行"));
    const W = 560, H = 190, L = 36, R = 12, T = 12, B = 30, pw = W - L - R, ph = H - T - B, n = runs.length;
    const x = i => L + (n === 1 ? pw / 2 : i * pw / (n - 1)), y = v => T + (1 - (v || 0)) * ph;
    const grid = [0, 0.5, 1].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="dg"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" class="dx">${v * 100}%</text>`).join("");
    const pts = runs.map((r, i) => `${x(i).toFixed(1)},${y(r.retention).toFixed(1)}`).join(" ");
    const dots = runs.map((r, i) => {
      const g = Object.entries(r.grades || {}).map(([k, v]) => `${k}×${v}`).join(" ");
      const t = tipAttr([`<b>#${r.run_id} · ${r.dataset_id}</b>`, `留存率 ${pctf(r.retention)} · quality ${r.quality == null ? "–" : r.quality.toFixed(3)}`, `等级 ${g || "–"}`, r.finished_at ? r.finished_at.replace("T", " ") : ""]);
      return `<g ${t}><circle cx="${x(i)}" cy="${y(r.retention)}" r="11" class="hit"/><circle cx="${x(i)}" cy="${y(r.retention)}" r="4.5" class="dd"/></g>`;
    }).join("");
    const lab = i => `<text x="${x(i)}" y="${H - 8}" text-anchor="${n === 1 ? "middle" : i === 0 ? "start" : "end"}" class="dx">#${runs[i].run_id} · ${(runs[i].finished_at || "").slice(5, 10)}</text>`;
    const svg = `<svg viewBox="0 0 ${W} ${H}" class="dash-svg" role="img" aria-label="每次运行的留存率">${grid}${n > 1 ? `<polyline points="${pts}" class="dl"/>` : ""}${dots}${lab(0)}${n > 1 ? lab(n - 1) : ""}</svg>`;
    const table = `<table class="dense"><tr><th>run</th><th>dataset</th><th>完成</th><th class="num">留存率</th><th class="num">quality</th></tr>${runs.slice().reverse().map(r => `<tr><td class="mono">#${r.run_id}</td><td>${esc(r.dataset_id)}</td><td class="mono">${(r.finished_at || "").replace("T", " ")}</td><td class="num mono">${pctf(r.retention)}</td><td class="num mono">${r.quality == null ? "–" : r.quality.toFixed(3)}</td></tr>`).join("")}</table>`;
    return card("留存率趋势", `每次运行 · 共 ${n} 次`, svg, table);
  }

  // ---------------------------------------------------------------- 2. per-dataset quality (bars + grade stacks)
  function datasets(ds) {
    if (!ds.length) return card("各数据集质量", "", empty("还没有跑过 QC 的数据集"));
    const rows = ds.slice().sort((a, b) => (a.retention || 0) - (b.retention || 0)).map(d => {
      const tot = Object.values(d.block_grades).reduce((a, b) => a + b, 0) || 1;
      const segs = ["A", "B", "C", "D"].filter(g => d.block_grades[g]).map(g => `<i class="g-${g}" style="flex-grow:${d.block_grades[g]}" ${tipAttr([`<b>${esc(d.dataset_id)}</b>`, `${g} 级 block × ${d.block_grades[g]}（${Math.round(100 * d.block_grades[g] / tot)}%）`])}></i>`).join("");
      return `<a class="dq-name" href="/datasets/${encodeURIComponent(d.dataset_id)}" title="${esc(d.dataset_id)}">${esc(short(d.dataset_id))}</a>
        <span class="grade grade-${d.grade || "none"}">${d.grade || "–"}</span>
        <span class="dq-bar" ${tipAttr([`<b>${esc(d.dataset_id)}</b>`, `留存率 ${pctf(d.retention)}`, `${num(d.n_sections)} 张中剔除 ${num(d.n_bad)} 张`, `运行 #${d.run_id}`])}><i style="width:${100 * (d.retention || 0)}%"></i></span><span class="mono dq-v">${pctf(d.retention)}</span>
        <span class="dq-stack">${segs}</span>`;
    }).join("");
    const legend = `<div class="dash-legend">${["A", "B", "C", "D"].map(g => `<span><i class="g-${g}"></i>${g}</span>`).join("")}<span class="muted">右侧色条 = block 等级构成</span></div>`;
    const table = `<table class="dense"><tr><th>dataset</th><th>等级</th><th class="num">留存率</th><th class="num">切片</th><th class="num">剔除</th><th>block 等级</th></tr>${ds.map(d => `<tr><td>${esc(d.dataset_id)}</td><td>${d.grade || "–"}</td><td class="num mono">${pctf(d.retention)}</td><td class="num mono">${num(d.n_sections)}</td><td class="num mono">${num(d.n_bad)}</td><td class="mono">${Object.entries(d.block_grades).map(([g, n]) => g + "×" + n).join(" ")}</td></tr>`).join("")}</table>`;
    return card("各数据集质量", "最近一次运行 · 留存率从低到高", `<div class="dq-grid">${rows}</div>${legend}`, table);
  }

  // ---------------------------------------------------------------- 3. problem types (bars)
  function failures(fs) {
    if (!fs.length) return card("问题类型", "", empty("没有 high 以上的问题"));
    const top = fs.slice(0, 10), mx = top[0].n, total = fs.reduce((a, f) => a + f.n, 0);
    const rows = top.map(f => `<span class="hb-l">${esc(f.zh)}</span><span class="hb-bar" ${tipAttr([`<b>${esc(f.zh)}</b>`, `${f.n} 条（占 ${Math.round(100 * f.n / total)}%）`, esc(f.type)])}><i style="width:${100 * f.n / mx}%"></i></span><span class="mono hb-v">${f.n}</span>`).join("");
    const table = `<table class="dense"><tr><th>问题类型</th><th class="num">条数</th></tr>${fs.map(f => `<tr><td>${esc(f.zh)} <span class="muted mono">${esc(f.type)}</span></td><td class="num mono">${f.n}</td></tr>`).join("")}</table>`;
    return card("问题类型", `high 以上 · 共 ${total} 条${fs.length > 10 ? " · 显示前 10 类" : ""}`, `<div class="hb-grid">${rows}</div>`, table);
  }

  // ---------------------------------------------------------------- 4. checks x severity (stacked bars)
  function checks(cs) {
    if (!cs.length) return card("各检查项触发", "", empty("没有发现问题"));
    const top = cs.slice(0, 12), mx = top[0].n;
    const rows = top.map(c => {
      const segs = SEV.filter(s => c.by_severity[s]).map(s => `<i class="sv-${s}" style="width:${100 * c.by_severity[s] / mx}%" ${tipAttr([`<b>${esc(CHECK_ZH[c.check] || c.check)}</b>`, `${SEV_ZH[s]}：${c.by_severity[s]} 条`, `合计 ${c.n} 条`])}></i>`).join("");
      return `<span class="hb-l">${esc(CHECK_ZH[c.check] || c.check)}</span><span class="hb-bar hb-stack">${segs}</span><span class="mono hb-v">${c.n}</span>`;
    }).join("");
    const legend = `<div class="dash-legend">${SEV.map(s => `<span><i class="sv-${s}"></i>${SEV_ZH[s]}</span>`).join("")}</div>`;
    const table = `<table class="dense"><tr><th>检查项</th>${SEV.map(s => `<th class="num">${s}</th>`).join("")}<th class="num">合计</th></tr>${cs.map(c => `<tr><td>${esc(CHECK_ZH[c.check] || c.check)}</td>${SEV.map(s => `<td class="num mono">${c.by_severity[s] || 0}</td>`).join("")}<td class="num mono">${c.n}</td></tr>`).join("")}</table>`;
    return card("各检查项触发", "按严重度分段 · high 及以上不通过", `${legend}<div class="hb-grid">${rows}</div>`, table);
  }

  // ---------------------------------------------------------------- 5. time per stage per run (stacked columns)
  function stages(runs) {
    const rs = runs.filter(r => Object.keys(r.stages || {}).length).slice(-20);
    if (!rs.length) return card("各阶段耗时", "", empty("还没有耗时数据"));
    const W = 560, H = 200, L = 40, R = 8, T = 10, B = 26, pw = W - L - R, ph = H - T - B;
    const tot = r => STAGES.reduce((a, s) => a + (r.stages[s] || 0), 0);
    const mx = Math.max(...rs.map(tot)) || 1, bw = Math.min(34, pw / rs.length - 6);
    const y = v => T + ph - v / mx * ph;
    const grid = [0, 0.5, 1].map(f => `<line x1="${L}" x2="${W - R}" y1="${y(mx * f)}" y2="${y(mx * f)}" class="dg"/><text x="${L - 6}" y="${y(mx * f) + 4}" text-anchor="end" class="dx">${num(mx * f)}s</text>`).join("");
    const cols = rs.map((r, i) => {
      const cx = L + (i + 0.5) * pw / rs.length - bw / 2;
      let acc = 0;
      const t = tot(r);
      const segs = STAGES.map((s, k) => {
        const v = r.stages[s] || 0; if (!v) return "";
        const y0 = y(acc), y1 = y(acc + v); acc += v;
        const h = Math.max(0, y0 - y1 - (acc < t ? 2 : 0));  // 2px surface gap between stacked fills
        return `<rect x="${cx}" y="${y1}" width="${bw}" height="${h}" class="st-c${k + 1}" ${k === STAGES.length - 1 || acc >= t ? 'rx="3"' : ""}/>`;
      }).join("");
      const lines = [`<b>#${r.run_id} · ${esc(r.dataset_id)}</b>`, ...STAGES.filter(s => r.stages[s]).map(s => `${STAGE_ZH[s]}：${num(r.stages[s])} s（${Math.round(100 * r.stages[s] / t)}%）`), `合计 ${num(t)} s`];
      return `<g ${tipAttr(lines)}><rect x="${cx - 3}" y="${T}" width="${bw + 6}" height="${ph}" class="hit"/>${segs}</g><text x="${cx + bw / 2}" y="${H - 8}" text-anchor="middle" class="dx">#${r.run_id}</text>`;
    }).join("");
    const legend = `<div class="dash-legend">${STAGES.map((s, k) => `<span><i class="st-c${k + 1}"></i>${STAGE_ZH[s]}</span>`).join("")}</div>`;
    const table = `<table class="dense"><tr><th>run</th>${STAGES.map(s => `<th class="num">${STAGE_ZH[s]}</th>`).join("")}<th class="num">合计 s</th></tr>${rs.slice().reverse().map(r => `<tr><td class="mono">#${r.run_id}</td>${STAGES.map(s => `<td class="num mono">${num(r.stages[s] || 0)}</td>`).join("")}<td class="num mono">${num(tot(r))}</td></tr>`).join("")}</table>`;
    return card("各阶段耗时", `每次运行各阶段的累计 CPU 时间${runs.length > 20 ? " · 最近 20 次" : ""}`, `${legend}<svg viewBox="0 0 ${W} ${H}" class="dash-svg" role="img" aria-label="各阶段耗时">${grid}${cols}</svg>`, table);
  }

  // ---------------------------------------------------------------- 6. throughput per run (columns)
  function throughput(runs) {
    const rs = runs.filter(r => r.sections_per_s).slice(-20);
    if (!rs.length) return card("处理速度", "", empty("还没有速度数据"));
    const W = 560, H = 170, L = 40, R = 8, T = 10, B = 26, pw = W - L - R, ph = H - T - B;
    const mx = Math.max(...rs.map(r => r.sections_per_s)), bw = Math.min(34, pw / rs.length - 6), y = v => T + ph - v / mx * ph;
    const grid = [0, 0.5, 1].map(f => `<line x1="${L}" x2="${W - R}" y1="${y(mx * f)}" y2="${y(mx * f)}" class="dg"/><text x="${L - 6}" y="${y(mx * f) + 4}" text-anchor="end" class="dx">${num(mx * f)}</text>`).join("");
    const cols = rs.map((r, i) => {
      const cx = L + (i + 0.5) * pw / rs.length - bw / 2;
      return `<g ${tipAttr([`<b>#${r.run_id} · ${esc(r.dataset_id)}</b>`, `${num(r.sections_per_s)} 切片 / 秒`, `${num(r.n_sections)} 张用了 ${num(r.wall_s)} 秒`, `读入 ${fmtBytes(r.bytes_read)}`])}><rect x="${cx - 3}" y="${T}" width="${bw + 6}" height="${ph}" class="hit"/><rect x="${cx}" y="${y(r.sections_per_s)}" width="${bw}" height="${T + ph - y(r.sections_per_s)}" rx="3" class="dbar"/></g><text x="${cx + bw / 2}" y="${H - 8}" text-anchor="middle" class="dx">#${r.run_id}</text>`;
    }).join("");
    const table = `<table class="dense"><tr><th>run</th><th class="num">切片</th><th class="num">耗时 s</th><th class="num">切片/秒</th></tr>${rs.slice().reverse().map(r => `<tr><td class="mono">#${r.run_id}</td><td class="num mono">${num(r.n_sections)}</td><td class="num mono">${num(r.wall_s)}</td><td class="num mono">${num(r.sections_per_s)}</td></tr>`).join("")}</table>`;
    return card("处理速度", "切片 / 秒 · 每次运行", `<svg viewBox="0 0 ${W} ${H}" class="dash-svg" role="img" aria-label="每次运行的处理速度">${grid}${cols}</svg>`, table);
  }

  // ---------------------------------------------------------------- 7. delivery
  function delivery(dl) {
    const t = (k, v, s) => `<div class="dv"><div class="k">${k}</div><div class="v">${v}</div><div class="sub muted">${s}</div></div>`;
    return card("交付", `<a href="/delivery">去数据交付页 →</a>`, `<div class="dv-grid">
      ${t("训练分片", num(dl.export_sections) + " 张", `${dl.n_exports} 次导出 · ${fmtBytes(dl.export_bytes)}`)}
      ${t("推理流", num(dl.stream_chunks) + " 块", `${dl.n_streams} 个会话${dl.n_streams_open ? ` · ${dl.n_streams_open} 个进行中` : ""} · ${fmtBytes(dl.stream_bytes)}`)}</div>`);
  }

  // ---------------------------------------------------------------- page
  async function load() {
    const box = document.getElementById("dash-body"); if (!box) return;
    const p = new URLSearchParams({ days: state.days });
    if (state.dataset) p.set("dataset_id", state.dataset);
    let d;
    try { d = await (await fetch(`/api/v1/pipeline/dashboard?${p}`)).json(); } catch (e) { return; }
    const sel = document.getElementById("dash-ds");
    if (sel && sel.options.length !== d.filters.datasets.length + 1) {
      sel.innerHTML = `<option value="">全部数据集</option>` + d.filters.datasets.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
      sel.value = state.dataset;
    }
    const open = new Set([...box.querySelectorAll("details.dash-table[open]")].map(x => x.closest(".dash-card").querySelector(".dash-h b").textContent));
    box.innerHTML = kpis(d) + `<div class="dash-grid">${trend(d.runs)}${datasets(d.datasets)}${failures(d.failures)}${checks(d.checks)}${stages(d.runs)}${throughput(d.runs)}${delivery(d.delivery)}</div>`;
    box.querySelectorAll(".dash-card").forEach(c => { if (open.has(c.querySelector(".dash-h b").textContent)) { const t = c.querySelector("details.dash-table"); if (t) t.open = true; } });
  }

  window.dashboardInit = function () {
    const root = document.getElementById("dashboard"); if (!root) return;
    try { const saved = JSON.parse(localStorage.getItem("emqc.dash") || "{}"); state = { ...state, ...saved }; } catch (e) {}
    const days = document.getElementById("dash-days"), ds = document.getElementById("dash-ds");
    days.value = String(state.days);
    const save = () => { try { localStorage.setItem("emqc.dash", JSON.stringify(state)); } catch (e) {} };
    days.onchange = () => { state.days = +days.value; save(); load(); };
    ds.onchange = () => { state.dataset = ds.value; save(); load(); };
    tipInit(root);
    load();
    clearInterval(timer); timer = setInterval(load, 10000);
  };
  window.dashboardRefresh = load;
})();
