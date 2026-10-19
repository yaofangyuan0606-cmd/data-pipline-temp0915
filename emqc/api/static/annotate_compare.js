/* Baseline/current comparison. All images and counts come from one read-only snapshot. */
(() => {
  const $ = id => document.getElementById(`cmp-${id}`), API = "/api/v1/annotate/blocks";
  const fmt = n => Number(n).toLocaleString("zh-CN"), esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const state = {blocks: [], block: "", z: 0, snapshot: null, images: null, serial: 0, controller: null, page: 0, downloads: new Set(), zoom: 100,
                 marks: [], marksBlock: "", markMode: false, markSel: null};
  const ME = window.EMQC_USER || null;
  // One canvas of ours (标注后); the other two panes are embedded Neuroglancer iframes, see ngSync().
  const canvases = [$("after")], viewports = [...document.querySelectorAll(".cmp-viewport")];
  const wraps = canvases.map(c => c.parentElement);
  let layout = null;
  const image = url => new Promise((resolve, reject) => { const im = new Image(); im.onload = () => resolve(im); im.onerror = () => reject(new Error("图片读取失败")); im.src = url; });
  async function json(url, signal, body, method) {
    method = method || (body ? "POST" : "GET");
    const r = await fetch(url, method !== "GET" ? {signal, cache: "no-store", method, headers: {"content-type": "application/json"}, body: body ? JSON.stringify(body) : undefined} : {signal, cache: "no-store"});
    if (!r.ok) {
      // 审核等接口的 409 带着 {code, message, ...}：把原因原样显示出来，code 挂在错误上给调用方判断
      const e = await r.json().catch(() => ({})), d = e.detail;
      const err = new Error(typeof d === "string" ? d : d?.message || `请求失败 (${r.status})`);
      err.status = r.status;
      if (d && typeof d === "object") { err.code = d.code; err.info = d; }
      throw err;
    }
    return r.json();
  }
  function downloadButtons() {
    for (const format of ["csv", "json"]) $(format).disabled = !state.snapshot || state.downloads.has(format);
  }
  function decode(im, index = false) {
    const c = document.createElement("canvas"); c.width = im.width; c.height = im.height;
    const g = c.getContext("2d"); g.drawImage(im, 0, 0);
    const p = g.getImageData(0, 0, c.width, c.height).data, out = new Uint16Array(c.width * c.height);
    for (let i = 0; i < out.length; i++) out[i] = index ? (p[4*i] << 8) | p[4*i+1] : p[4*i];
    return out;
  }
  // Same stable id palette as the annotation workbench; ids stay strings above 2^53.
  function color(id) {
    if (id === "0") return [0, 0, 0];
    let h = 2166136261;
    for (let i = 0; i < id.length; i++) { h ^= id.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    const hue = h % 360, s = .62 + ((h >>> 9) % 30) / 100, l = .48 + ((h >>> 17) % 16) / 100, a = s * Math.min(l, 1-l);
    const f = n => { const k = (n + hue / 30) % 12; return Math.round(255 * (l - a * Math.max(-1, Math.min(k-3, 9-k, 1)))); };
    return [f(0), f(8), f(4)];
  }
  // native pixels per displayed pixel, rounded — the rim must be at least this wide to be visible
  function rimWidth(canvas) {
    const shown = canvas.getBoundingClientRect().width || canvas.clientWidth || canvas.width;
    // one extra native pixel on top of the scale so the rim lands at ~1.5 displayed px, not a faint 1 px
    return Math.max(2, Math.min(8, Math.round(canvas.width / Math.max(1, shown)) + 1));
  }
  function draw() {
    if (!state.images) return;
    const {em, indices, sources, changes} = state.images, data = state.snapshot;
    // Fixed alpha; the change marker is always on. This is the platform's current result — the "before" pictures
    // are the embedded public viewer (EM, and EM + c3) beside it.
    const alpha = 0.5, sourceMode = $("mode").value === "sources";
    $("after-caption").textContent = sourceMode ? "当前结果 · 按来源着色" : "当前编辑结果";
    const sourceColors = (data.report.source_legend || []).map(s => s.color.match(/\w\w/g).map(v => parseInt(v, 16)));
    const canSource = sourceMode && sources && sourceColors.length;                  // 轻量帧没有来源数据，按标签色画
    canvases.forEach(canvas => {
      const side = 1;                                                    // always the "after" picture
      canvas.width = em.width; canvas.height = em.height;
      const g = canvas.getContext("2d");
      const overlay = g.createImageData(em.width, em.height), pixels = overlay.data;
      const ids = data.after.ids, palette = ids.map(color), idx = indices[side];
      for (let i = 0; i < idx.length; i++) {
        const c = canSource ? sourceColors[sources[i]] : palette[idx[i]];
        if (ids[idx[i]] !== "0" || (canSource && changes[i])) {
          pixels.set(c, i*4); pixels[i*4+3] = Math.round(alpha*255);
        }
      }
      // Changed regions on the right are outlined with a black-outside / white-inside double edge. A filled tint
      // was tried first and failed twice over: the pink was indistinguishable from pink cells, and it hid the
      // colour the annotator had actually painted. A two-tone edge cannot be mistaken for any label colour and
      // leaves the interior visible.
      if (side) {
        // Rim width follows the display scale: a 1-px rim on a 1024 canvas shown at 317 px loses ~90% of its
        // pixels to downsampling (measured: 7,625 -> 702). Draw it `rim` native pixels wide so it survives.
        const W = em.width, H = em.height, rim = rimWidth(canvas);
        const changedAt = (x, y) => x >= 0 && y >= 0 && x < W && y < H && changes[y * W + x] > 0;
        const inner = new Uint8Array(W * H), outer = new Uint8Array(W * H);
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
          const i = y * W + x, me = changes[i] > 0;
          const l = changedAt(x - 1, y), r = changedAt(x + 1, y), u = changedAt(x, y - 1), d = changedAt(x, y + 1);
          if (me && !(l && r && u && d)) inner[i] = 1;
          else if (!me && (l || r || u || d)) outer[i] = 1;
        }
        // thicken both rims by (rim - 1) 4-neighbour dilations, each kept on its own side of the boundary
        const grow = (mask, keep) => {
          for (let k = 1; k < rim; k++) {
            const next = new Uint8Array(mask);
            for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
              const i = y * W + x;
              if (mask[i] || !keep(i)) continue;
              if ((x > 0 && mask[i-1]) || (x < W-1 && mask[i+1]) || (y > 0 && mask[i-W]) || (y < H-1 && mask[i+W])) next[i] = 1;
            }
            mask = next;
          }
          return mask;
        };
        const innerW = grow(inner, i => changes[i] > 0), outerB = grow(outer, i => changes[i] === 0);
        for (let i = 0; i < W * H; i++) {
          if (innerW[i]) { pixels.set([255, 255, 255], i*4); pixels[i*4+3] = 255; }
          else if (outerB[i]) { pixels.set([0, 0, 0], i*4); pixels[i*4+3] = 230; }
        }
        state.rimDrawn = rim;
      }
      const layer = document.createElement("canvas"); layer.width = em.width; layer.height = em.height;
      layer.getContext("2d").putImageData(overlay, 0, 0);
      g.drawImage(em, 0, 0); g.drawImage(layer, 0, 0);
    });
  }
  function rows() {
    if (!state.snapshot) return;
    const report = state.snapshot.report, source = $("source").value, search = $("search").value.trim();
    const filtered = report.labels.filter(r => r.id.includes(search) && (source === "all" || (source === "mixed" ? r.mixed : r.sources[source] > 0)));
    const pages = Math.max(1, Math.ceil(filtered.length/100)); state.page = Math.min(state.page, pages-1);
    $("rows").innerHTML = filtered.slice(state.page*100, (state.page+1)*100).map(r => `<tr><td>${esc(r.id)}${r.id === "0" ? "（背景）" : ""}${r.mixed ? '<span class="cmp-mixed">混合</span>' : ""}</td><td>${fmt(r.before_px)}</td><td>${fmt(r.current_px)}</td><td>${fmt(r.changed_px)}</td>${report.source_legend.map(s => `<td>${fmt(r.sources[s.key])}</td>`).join("")}</tr>`).join("") || '<tr><td colspan="10">没有符合条件的标签。</td></tr>';
    $("row-count").textContent = `${fmt(filtered.length)} 个标签（含背景）`;
    $("table-page").textContent = `${state.page+1} / ${pages}`;
    $("table-prev").disabled = state.page === 0; $("table-next").disabled = state.page+1 >= pages;
  }
  // ---------------------------------------------------------------- 改了哪些地方 / 标签增减
  const swatch = id => `<i class="cmp-dot" style="background:rgb(${color(id).join(",")})"></i>`;
  const idCell = id => id === "0" ? '<span class="muted">背景</span>' : `${swatch(id)}<span class="mono">${esc(id)}</span>`;
  function focusRegion(g) {
    viewports.forEach((viewport, side) => {
      const canvas = canvases[side], scale = canvas.clientWidth / canvas.width;
      viewport.scrollTo({left: wraps[side].offsetLeft + (g.cx + .5) * scale - viewport.clientWidth / 2,
                         top: wraps[side].offsetTop + (g.cy + .5) * scale - viewport.clientHeight / 2, behavior: "smooth"});
    });
    document.querySelectorAll(".cmp-ring").forEach(ring => {
      ring.hidden = false;
      ring.style.left = `${(g.x0 + g.x1) / 2 / canvases[0].width * 100}%`;
      ring.style.top = `${(g.y0 + g.y1) / 2 / canvases[0].height * 100}%`;
      ring.style.width = `${Math.max(g.x1 - g.x0, 8) / canvases[0].width * 100}%`;
      ring.style.height = `${Math.max(g.y1 - g.y0, 8) / canvases[0].height * 100}%`;
      ring.classList.remove("cmp-pulse"); void ring.offsetWidth; ring.classList.add("cmp-pulse");
    });
  }
  function renderChanges() {
    const changes = state.snapshot.changes || {regions: [], n_total: 0, hidden: 0, hidden_px: 0}, report = state.snapshot.report;
    const regions = changes.regions;
    const editors = (report.editors || []).filter(e => e.pixels).map(e => e.by ?? "未署名");
    $("regions-count").textContent = changes.n_total ? `${fmt(changes.n_total)} 处 · 共 ${fmt(report.changed_px)} 像素${editors.length ? ` · 改动人 ${editors.join("、")}` : ""}` : "";
    $("regions").innerHTML = regions.map((g, i) => `<tr class="cmp-click" data-region="${i}" tabindex="0"><td class="mono">${g.cx}, ${g.cy}</td><td>${fmt(g.px)}</td><td>${idCell(g.from_id)} → ${idCell(g.to_id)}</td><td>${g.by ? esc(g.by) : '<span class="muted">—</span>'}</td></tr>`).join("")
      || `<tr><td colspan="4" class="muted">${report.has_seg ? "这一片与原始分割完全一致。" : "此数据块没有分割标签。"}</td></tr>`;
    $("regions-more").textContent = changes.hidden ? `另有 ${fmt(changes.hidden)} 处更小的改动，合计 ${fmt(changes.hidden_px)} 像素，未逐条列出。` : "";
    // "新增了哪些标签" in practice means the whole balance sheet: what appeared, what grew, what was merged away.
    // Strictly-new ids are often none — merging and clearing reuse ids that were already in the baseline.
    const moved = report.labels.filter(l => l.current_px !== l.before_px)
                               .sort((a, b) => Math.abs(b.current_px - b.before_px) - Math.abs(a.current_px - a.before_px));
    const shown = moved.slice(0, 50);
    $("labels-count").textContent = moved.length ? `${fmt(moved.length)} 个标签有增减` : "";
    $("labels").innerHTML = shown.map(l => {
      const d = l.current_px - l.before_px;
      const tag = l.before_px === 0 ? '<span class="cmp-tag cmp-new">新出现</span>'
                : l.current_px === 0 ? '<span class="cmp-tag cmp-gone">已消失</span>' : "";
      return `<tr><td>${idCell(l.id)}${tag}</td><td>${fmt(l.before_px)}</td><td>${fmt(l.current_px)}</td><td class="${d > 0 ? "cmp-up" : "cmp-down"}">${d > 0 ? "+" : "−"}${fmt(Math.abs(d))}</td></tr>`;
    }).join("") || '<tr><td colspan="4" class="muted">没有标签的像素数发生变化。</td></tr>';
    $("labels-more").textContent = moved.length > shown.length ? `另有 ${fmt(moved.length - shown.length)} 个标签变化较小，未列出；完整清单见下方溯源报表。` : "";
  }
  $("regions").addEventListener("click", ev => {
    const row = ev.target.closest("tr[data-region]");
    if (row && state.snapshot) focusRegion(state.snapshot.changes.regions[+row.dataset.region]);
  });
  $("regions").addEventListener("keydown", ev => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const row = ev.target.closest("tr[data-region]");
    if (row && state.snapshot) { ev.preventDefault(); focusRegion(state.snapshot.changes.regions[+row.dataset.region]); }
  });

  function renderReport() {
    const r = state.snapshot.report, names = Object.fromEntries(r.source_legend.map(s => [s.key, s.name]));
    if ($("source").options.length === 2) r.source_legend.forEach(s => { const o = document.createElement("option"); o.value=s.key; o.textContent=s.name; $("source").append(o); });
    $("metrics").innerHTML = [["修改像素", r.changed_px], ["新增标签像素", r.added_px], ["改为其他标签", r.relabeled_px], ["清除为背景", r.removed_px]].map(([name, n], i) => `<div class="${i ? "" : "cmp-total"}"><small>${name}</small><strong>${fmt(n)}</strong></div>`).join("");
    $("legend").innerHTML = r.source_legend.map(s => `<span><i class="cmp-dot" style="background:${s.color}"></i>${esc(s.name)} <b>${fmt(r.sources[s.key].label_pixels)}</b> px</span>`).join("");
    $("policy").textContent = r.policy;
    $("warnings").hidden = !r.warnings.length; $("warnings").replaceChildren(...r.warnings.map(w => { const p = document.createElement("p"); p.textContent = w; return p; }));
    const kinds = {paint:"画笔 / 橡皮", fill:"填充 / 清除", merge:"合并", split:"切割 / 分离（历史）", sam:"SAM 应用", repair:"插值（历史）", refine:"边缘修缮", seed:"种子分割", bigfill:"大块填充", smartfill:"智能填充（历史）"};
    $("operations").innerHTML = r.operations.slice().reverse().map(e => `<tr><td>#${e.n} ${esc(kinds[e.kind] || e.kind)}</td><td>${esc(e.by ?? "未署名")}</td><td>${esc(names[e.source])}</td><td>${esc(e.ts || "—")}</td><td>${fmt(e.n_px_in_slice)}</td><td>${fmt(e.current_px)}</td><td>${esc(e.model || (e.source_sections ? `Z ${e.source_sections.join(", ")}` : "—"))}</td></tr>`).join("") || '<tr><td colspan="7">当前切片没有可读取的有效编辑记录。</td></tr>';
    rows();
  }
  // 操作流水（audit.jsonl）：包括被撤销的动作和撤销本身，只在展开时才去取
  let auditFor = "";
  async function renderAudit() {
    const box = $("audit");
    if (!box || !box.open || !state.block) return;
    const key = `${state.block}:${state.z}`;
    if (auditFor === key) return;
    auditFor = key;
    $("audit-rows").innerHTML = '<tr><td colspan="6" class="muted">加载中…</td></tr>';
    try {
      const r = await json(`${API}/${encodeURIComponent(state.block)}/audit?z=${state.z}&limit=300`);
      if (auditFor !== key) return;
      const kinds = {paint:"画笔 / 橡皮", fill:"填充 / 清除", merge:"合并", sam:"SAM 应用", repair:"插值（历史）", refine:"边缘修缮", seed:"种子分割", bigfill:"大块填充", clear:"批量删除"};
      $("audit-rows").innerHTML = r.entries.map(e => `<tr class="${e.action === "undo" ? "cmp-undo" : ""}"><td>${e.seq}</td><td>${esc(e.ts || "—")}</td><td>${esc(e.by ?? "未署名")}</td><td>${e.action === "undo" ? `撤销 #${e.n}${e.forced ? "（强制）" : ""}` : `#${e.n} ${esc(kinds[e.kind] || e.kind || "")}${e.z == null ? " · 整块" : ""}`}</td><td>${fmt(e.n_px ?? 0)}</td><td>${e.action === "undo" ? esc(e.of ?? "未署名") : '<span class="muted">—</span>'}</td></tr>`).join("")
        || '<tr><td colspan="6" class="muted">这一片还没有任何操作。</td></tr>';
    } catch (err) { if (auditFor === key) { auditFor = ""; $("audit-rows").innerHTML = `<tr><td colspan="6" class="muted">读取失败：${esc(err.message)}</td></tr>`; } }
  }
  $("audit")?.addEventListener("toggle", renderAudit);
  async function load(z = state.z, force = false) {
    const block = state.blocks.find(b => b.block_id === $("block").value); if (!block) return;
    if (play.timer && !play.internal) stopPlay(false);           // 手动翻页/换块/刷新：先停播
    if (force || state.block !== block.block_id) frames.clear();  // 刷新或换块后旧帧作废
    const nextZ = Math.max(0, Math.min(block.nz-1, Math.trunc(Number(z)) || 0));
    $("z").value = nextZ;
    // Navigation to the current (or already requested) slice is a no-op. Only Refresh retries it.
    if (!force && state.serial && state.block === block.block_id && state.z === nextZ) return;
    const changedBlock = state.block !== block.block_id;
    state.controller?.abort(); state.controller = new AbortController();
    const serial = ++state.serial;
    state.block = block.block_id; state.z = nextZ;
    state.images = state.snapshot = null; state.page = 0; auditFor = ""; RV.state = null; renderReview();
    $("page").setAttribute("aria-busy", "true");
    // Keep the existing layout while loading: hiding it collapses the document and resets image pan.
    $("page").classList.remove("cmp-load-failed");
    downloadButtons();
    $("z").value = state.z; $("z").max = block.nz-1; $("zmax").textContent = `/ ${block.nz-1}　共 ${block.nz} 片`;
    $("prev").disabled = state.z === 0; $("next").disabled = state.z === block.nz-1;
    $("status").textContent = `正在加载 ${state.block} · Z ${state.z}…`;
    $("pixel").textContent = "滚轮缩放 · ↑↓ / A Z 翻片 · 拖动平移（两侧同步） · 移动鼠标查看标签与来源";
    document.querySelectorAll(".cmp-cursor, .cmp-ring").forEach(c => { c.hidden = true; });
    const params = new URLSearchParams({block:state.block, z:state.z});
    history.replaceState(null, "", `/annotate/compare?${params}`); $("edit").href = `/annotate?${params}`;
    try {
      const data = await json(`${API}/${encodeURIComponent(state.block)}/compare/${state.z}`, state.controller.signal);
      const [em, before, after, sources, changes, editors] = await Promise.all([data.em_png, data.before.png, data.after.png, data.sources_png, data.changes_png, data.editors_png].map(u => u ? image(u) : null));
      if (serial !== state.serial) return;
      // editors: 每个像素最后是谁写的（0 = 无记录，k = data.editors[k-1]）
      state.snapshot = data; state.images = {em, indices:[decode(before, true), decode(after, true)], sources:decode(sources), changes:decode(changes), editors: editors ? decode(editors) : null};
      frames.set(frameKey(state.block, state.z), {snapshot: data, images: state.images});
      trimFrames();
      draw(); renderReport(); renderChanges(); renderAudit(); loadReview();
      dirButtons().forEach(b => { b.disabled = false; });
      for (const id of ["images", "report", "summary", "marks-panel"]) $(id).hidden = false;
      if (state.marksBlock !== state.block) loadMarks(true); else { renderMarks(); renderPins(); }
      ngSync();
      downloadButtons();
      const r = data.report;
      $("status").textContent = r.has_seg ? "" : `${state.block} 没有分割标签，只显示原始电镜图`;   // 常规信息不占一行，只留需要说明的情况
      if (changedBlock) state.zoom = 100;
      layoutImages(changedBlock);
    } catch (e) {
      if (serial === state.serial && e.name !== "AbortError") {
        $("status").textContent = `加载失败：${e.message}。可点击刷新重试。`;
        $("page").classList.add("cmp-load-failed");  // Do not present the previous slice as the failed one.
      }
    } finally {
      if (serial === state.serial) { state.controller = null; $("page").setAttribute("aria-busy", "false"); }
    }
  }
  async function download(format) {
    if (!state.snapshot || state.downloads.has(format)) return;
    const block = state.block, z = state.z, scope = $("scope").value, saved = state.snapshot.report;
    state.downloads.add(format); downloadButtons();
    const description = `${block} · ${scope === "block" ? "整个数据块" : `Z ${z}`} · ${format.toUpperCase()}`;
    $("export-status").textContent = `正在导出 ${description}…`;
    try {
      let blob;
      if (scope === "slice" && format === "json") blob = new Blob([JSON.stringify(saved, null, 2)], {type:"application/json"});
      else if (scope === "slice") {
        const headers = ["block_id", "z", "label_id", "before_px", "current_px", "changed_px", ...saved.source_legend.map(s => s.key), "mixed", "editors"];
        const csvCell = v => `"${String(v).replace(/"/g, '""')}"`;
        const guard = s => /^[=+\-@\t\r\n]/.test(s) ? "'"+s : s;        // 名字也是用户输入，同样防公式注入
        const safeBlock = guard(block);
        const editorsOf = r => guard((r.editors || []).map(e => `${e.by ?? "未署名"}:${e.px}`).join(";"));
        const lines = saved.labels.map(r => [safeBlock,z,r.id,r.before_px,r.current_px,r.changed_px,...saved.source_legend.map(s => r.sources[s.key]),r.mixed,editorsOf(r)].map(csvCell).join(","));
        blob = new Blob(["\ufeff"+headers.join(",")+"\r\n"+lines.join("\r\n")+"\r\n"], {type:"text/csv;charset=utf-8"});
      } else {
        const response = await fetch(`${API}/${encodeURIComponent(block)}/provenance?format=${format}`, {cache:"no-store"});
        if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.detail || `请求失败 (${response.status})`); }
        blob = await response.blob();
      }
      const url = URL.createObjectURL(blob), a = document.createElement("a"); a.href = url;
      a.download = `provenance-${block}-${scope === "block" ? "all" : `z${z}`}.${format}`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      $("export-status").textContent = `已导出 ${description}。`;
    } catch (e) { $("export-status").textContent = `${description} 导出失败：${e.message}`; }
    finally { state.downloads.delete(format); downloadButtons(); }
  }
  viewports.forEach((viewport, side) => {
    viewport.addEventListener("scroll", () => {
      const other = viewports[1-side];
      if (!other) return;
      if (Math.abs(other.scrollLeft-viewport.scrollLeft) > 1) other.scrollLeft = viewport.scrollLeft;
      if (Math.abs(other.scrollTop-viewport.scrollTop) > 1) other.scrollTop = viewport.scrollTop;
    });
    canvases[side].addEventListener("mousemove", event => {
      if (!state.images) return;
      const canvas = canvases[side], rect = canvas.getBoundingClientRect();
      const x = Math.floor((event.clientX-rect.left)*canvas.width/rect.width), y = Math.floor((event.clientY-rect.top)*canvas.height/rect.height);
      if (x<0 || y<0 || x>=canvas.width || y>=canvas.height) return;
      const i = y*canvas.width+x, {indices, sources} = state.images, data = state.snapshot;
      const src = sources && data.report.source_legend?.[sources[i]] ? `    来源：${data.report.source_legend[sources[i]].name}` : "";
      const k = state.images.editors ? state.images.editors[i] : 0;
      const who = k ? `    标注人：${data.editors?.[k - 1] ?? "未署名"}` : "";
      const orig = indices[0] && data.before ? `原始 ${data.before.ids[indices[0][i]]} → ` : "";
      $("pixel").textContent = `X ${x} · Y ${y} · Z ${state.z}    ${orig}当前 ${data.after.ids[indices[1][i]]}${src}${who}`;
      ngHover(x, y);
      document.querySelectorAll(".cmp-cursor").forEach(c => { c.hidden = false; c.style.left = `${(x+.5)/canvas.width*100}%`; c.style.top = `${(y+.5)/canvas.height*100}%`; });
    });
    viewport.addEventListener("mouseleave", () => { document.querySelectorAll(".cmp-cursor").forEach(c => { c.hidden = true; }); ngPending = null; ngPlace(null); });
    // 点一下（不是拖动）：让两个查看器居中到这个体素
    let press = null;
    canvases[side].addEventListener("mousedown", ev => { press = [ev.clientX, ev.clientY]; });
    canvases[side].addEventListener("mouseup", ev => {
      if (!press || Math.hypot(ev.clientX - press[0], ev.clientY - press[1]) > 3 || !state.images) { press = null; return; }
      press = null;
      const canvas = canvases[side], rect = canvas.getBoundingClientRect();
      const x = Math.floor((ev.clientX-rect.left)*canvas.width/rect.width), y = Math.floor((ev.clientY-rect.top)*canvas.height/rect.height);
      if (!(x >= 0 && y >= 0 && x < canvas.width && y < canvas.height)) return;
      if (state.markMode) { openMarkForm(x, y, ev.clientX, ev.clientY); return; }
      ngCentre(x, y);
    });
  });
  $("z").addEventListener("change", () => load($("z").value));     // 换块由批次 / 数据块选择器回调 load(0)
  $("z").addEventListener("wheel", ev => ev.preventDefault(), {passive: false});  // No native number-input wheel increments.
  $("prev").addEventListener("click", () => load(state.z-1)); $("next").addEventListener("click", () => load(state.z+1));
  $("refresh").addEventListener("click", () => state.blocks.length ? load(state.z, true) : init());
  $("mode").addEventListener("input", draw);
  // ---------------------------------------------------------------- 第三栏：嵌入公开的 H01 Neuroglancer
  // 我们的 EM/seg 与公开 c3 已逐像素验证一致，所以把它的图拉下来画成静态图会和左图一模一样；嵌活的查看器
  // 才有意义——能缩放、能开关图层、能开 3D。位置由服务端按 meta.geometry 算好（和「看这一点」同一套换算）。
  // 翻 z 时只改 URL 的 # 片段：Neuroglancer 监听 hashchange 就地更新，不会整页重载。
  // 两栏常驻：EM 原图、EM + c3 分割，各自一个 iframe，同一位置同一切片。
  let ngSerial = 0, ngLast = {block: "", z: -1, px: 0};
  // 每栏记住基础 URL、块角点和取景（中心 + 比例）。悬停指针由页面自己画在 iframe 上面：
  //   栏内像素 = (体素坐标 - 中心) / 比例 × f + 栏中心
  // 前提是栏里露出来的**正好是** xy 视口、栏中心就是 state.position。但 H01 这份 Neuroglancer 不理会
  // showUIControls 等开关：顶部两条栏（坐标栏 21 px + 图层栏 26 px = 47 px）总在，视口从 47 px 才开始；
  // 而且查看器窄于 750 px 时坐标栏会折行，栏更高——之前按「视口铺满 iframe」算，圆环就整体偏上了。
  // 所以 iframe 一律按不少于 NG_MIN_W 的宽度渲染（此时两条栏恒为 47 px），上移 47 px 把栏藏到盒子外，
  // 再用 transform 等比缩到盒子宽度（f = 盒宽 / iframe 宽）。这样盒子里露出的就是完整视口，换算精确。
  // iframe 不接鼠标；不再为每次移动改 URL 片段（那条路要让查看器重新应用整份状态，肉眼可见地卡），点击居中才改 URL。
  const NG_BARS = 47, NG_MIN_W = 800;
  const ngBase = {em: null, seg: null}, ngView = {em: null, seg: null}, ngGeom = {em: null, seg: null};
  let ngCorner = null, ngRaf = null, ngPending = null;
  function ngFit() {                             // 按盒子尺寸摆 iframe：宽 ≥ NG_MIN_W、上移一条栏、等比缩回盒子
    for (const key of ["em", "seg"]) {
      const frame = $(`ng-${key}-frame`), box = frame.parentElement;
      const bw = box.clientWidth, bh = box.clientHeight;
      if (!bw || !bh) continue;
      const iw = Math.max(bw, NG_MIN_W), f = bw / iw, ih = Math.round(bh / f + NG_BARS);
      frame.style.width = `${iw}px`; frame.style.height = `${ih}px`;
      frame.style.top = `${(-NG_BARS * f).toFixed(2)}px`;
      frame.style.transform = `scale(${f.toFixed(5)})`;
      ngGeom[key] = {bw, bh, iw, f};
    }
  }
  function ngParse(url) {
    try { const st = JSON.parse(decodeURIComponent(url.split("#!")[1])); return {pos: st.position, scale: st.crossSectionScale}; } catch (_) { return null; }
  }
  function ngWithPosition(url, pt) {
    try {
      const [head, frag] = url.split("#!"); const st = JSON.parse(decodeURIComponent(frag));
      st.position = pt;
      return head + "#!" + encodeURIComponent(JSON.stringify(st));
    } catch (_) { return url; }
  }
  function ngPlace(x, y) {                       // x, y: 标注后画布像素；null = 隐藏
    for (const key of ["em", "seg"]) {
      const mark = $(`ng-${key}-mark`), view = ngView[key], g = ngGeom[key];
      if (x == null || !view || !ngCorner || !g) { mark.hidden = true; continue; }
      const px = (ngCorner[0] + x + .5 - view.pos[0]) / view.scale * g.f + g.bw / 2;
      const py = (ngCorner[1] + y + .5 - view.pos[1]) / view.scale * g.f + g.bh / 2;
      const inside = px >= 0 && py >= 0 && px <= g.bw && py <= g.bh;
      mark.hidden = !inside;
      if (inside) mark.style.transform = `translate(${px.toFixed(1)}px, ${py.toFixed(1)}px)`;
    }
  }
  function ngHover(x, y) {                       // 最多每 16ms 一次（约 60 帧），跟手
    ngPending = [x, y];
    if (ngRaf) return;
    ngRaf = setTimeout(() => { ngRaf = null; const p = ngPending; ngPending = null; if (p) ngPlace(p[0], p[1]); }, 16);
  }
  function ngCentre(x, y) {                      // 点击：两个查看器居中到这个体素（一次 hashchange，可以接受）
    if (!ngCorner) return;
    const pt = [ngCorner[0] + x + .5, ngCorner[1] + y + .5, ngCorner[2] + state.z + .5];
    for (const key of ["em", "seg"]) {
      if (!ngBase[key]) continue;
      ngBase[key] = ngWithPosition(ngBase[key], pt); ngView[key] = ngParse(ngBase[key]);
      $(`ng-${key}-frame`).src = ngBase[key];
    }
    ngPlace(x, y);
  }
  // 取景宽度 = iframe 渲染宽度（≥ NG_MIN_W，见 ngFit）：块正好占满 iframe，缩回盒子后也正好占满盒子。
  // 首次同步时盒子可能还没排出尺寸，先按 NG_MIN_W 取景，等布局稳定后由下面的复查纠正。
  function ngWidth() { ngFit(); return ngGeom.em?.iw || NG_MIN_W; }
  async function ngSync() {
    layoutImages();
    if (!state.block) return;
    const px = ngWidth();
    if (ngLast.block === state.block && ngLast.z === state.z && Math.abs(px - ngLast.px) < ngLast.px * 0.15) return;
    ngLast = {block: state.block, z: state.z, px};
    const serial = ++ngSerial;
    // 首次同步常常发生在 iframe 还没有尺寸的瞬间（读到 0 → 退回 600 → 只显示块中心的一半）。等布局稳定后
    // 复查一次：真实宽度和这次用的差得多，就按真实宽度重新取景。ngLast 的 15% 守卫保证不会来回震荡。
    setTimeout(() => { const now = ngWidth(); if (Math.abs(now - px) >= px * 0.15) ngSync(); }, 600);
    const panes = [["em", "em", "公开 H01 · 电镜原图"], ["seg", "em+seg", "公开 H01 · c3 分割叠在电镜上"]];
    await Promise.all(panes.map(async ([key, layers, title]) => {
      const frame = $(`ng-${key}-frame`), note = $(`ng-${key}-note`), open = $(`ng-${key}-open`), cap = $(`ng-${key}-caption`);
      try {
        const r = await json(`${API}/${encodeURIComponent(state.block)}/neuroglancer/embed?z=${state.z}&px=${px}&layers=${encodeURIComponent(layers)}`);
        if (serial !== ngSerial) return;
        if (!r.url) { note.hidden = false; note.textContent = r.reason || "无法定位到公开数据集"; frame.removeAttribute("src"); open.removeAttribute("href"); return; }
        note.hidden = true; open.href = r.url;
        cap.textContent = `${title} · Z ${state.z}`;
        ngBase[key] = r.url; ngView[key] = ngParse(r.url); ngCorner = r.corner || ngCorner;
        if (frame.getAttribute("src") !== r.url) frame.src = r.url;     // 只换 # 片段：查看器就地更新，不重载
      } catch (e) { if (serial === ngSerial) { note.hidden = false; note.textContent = "加载失败：" + e.message; } }
    }));
  }
  // ---------------------------------------------------------------- 动态：当前片上下各 10 片来回播放
  // 逐帧走完整的 load() 太慢（每片要拉图 + 算溯源），所以先把 21 片都取好、解码好放进 frames，播放时只换
  // 画布内容；表格、URL、溯源等到停下来落在哪一片再更新。两个 Neuroglancer 栏改 URL 片段代价大（查看器
  // 重新应用整份状态），播放中每 250ms 跟一次，停下时精确对齐。
  const frames = new Map(), FRAME_CAP = 30, PLAY_RADIUS = 10;
  const frameKey = (block, z) => `${block}:${z}`;
  function trimFrames() { while (frames.size > FRAME_CAP) frames.delete(frames.keys().next().value); }
  async function fetchFrame(z, signal) {
    const key = frameKey(state.block, z);
    if (frames.has(key)) return frames.get(key);
    const data = await json(`${API}/${encodeURIComponent(state.block)}/compare/${z}?light=1`, signal);   // 只要图，不算溯源
    const [em, after, changes] = await Promise.all([data.em_png, data.after.png, data.changes_png].map(image));
    const frame = {snapshot: data, images: {em, indices: [null, decode(after, true)], sources: null, changes: decode(changes)}};
    frames.set(key, frame); trimFrames();
    return frame;
  }
  const play = {timer: null, tick: null, internal: false, mode: "fwd", lastMode: "fwd", dir: 1, lo: 0, hi: 0, controller: null, ngAt: 0};
  const dirButtons = () => [...document.querySelectorAll(".cmp-dir")];
  function markDir(mode) { dirButtons().forEach(b => b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false")); }
  function showFrame(z) {
    const frame = frames.get(frameKey(state.block, z)); if (!frame) return false;
    state.z = z; state.snapshot = frame.snapshot; state.images = frame.images;
    RV.state = null; renderReview();                             // 播放中不能审：轻量帧没有内容哈希
    draw(); renderPins();
    $("z").value = z;
    $("status").textContent = `${state.block} · Z ${z} · 动态播放 ${play.mode === "back" ? "◀ 向后" : play.mode === "fwd" ? "▶ 向前" : "⇄ 来回"} ${play.lo}–${play.hi}（再按空格停止）`;
    const now = performance.now();
    if (ngCorner && now - play.ngAt > 250) {                     // 查看器栏低频跟随
      play.ngAt = now;
      for (const key of ["em", "seg"]) {
        if (!ngBase[key]) continue;
        const v = ngView[key]; if (!v) continue;
        const pt = [v.pos[0], v.pos[1], ngCorner[2] + z + .5];
        $(`ng-${key}-frame`).src = ngWithPosition(ngBase[key], pt);
      }
    }
    return true;
  }
  async function startPlay(mode = play.lastMode) {
    if (!state.block || !state.images) return;
    play.mode = play.lastMode = mode;
    if (play.timer || play.controller) { markDir(mode); play.dir = mode === "back" ? -1 : 1; return; }   // 播放中：只换方向
    const block = state.blocks.find(b => b.block_id === state.block); if (!block) return;
    const centre = state.z;
    play.lo = Math.max(0, centre - PLAY_RADIUS); play.hi = Math.min(block.nz - 1, centre + PLAY_RADIUS);
    play.controller = new AbortController();
    const info = $("play-info");
    markDir(mode);
    // 预载：先中间再向两边，这样最先要播的帧最先到
    const order = [centre]; for (let d = 1; d <= PLAY_RADIUS; d++) { if (centre + d <= play.hi) order.push(centre + d); if (centre - d >= play.lo) order.push(centre - d); }
    let done = 0;
    try {
      info.textContent = "预热…";                                   // 一次读完这段 z 的两份体数据，比逐片读快一个量级
      // 预热只是加速：服务器忙（别人在预热）时跳过，逐片预载照样能播，只是慢一点
      await json(`${API}/${encodeURIComponent(state.block)}/compare/warm`, play.controller.signal, {z0: play.lo, z1: play.hi})
        .catch(e => { if (e.name === "AbortError") throw e; });
      // 4 路并发预载，顺序仍是先中间再两边
      const queue = order.slice();
      const worker = async () => { while (queue.length) { if (play.controller.signal.aborted) return; const z = queue.shift(); await fetchFrame(z, play.controller.signal); done++; info.textContent = `预载 ${done}/${order.length}`; } };
      await Promise.all([0, 1, 2, 3].map(worker));
      if (play.controller.signal.aborted) return;
    } catch (e) { if (e.name !== "AbortError") { info.textContent = "预载失败：" + e.message; stopPlay(false); } return; }
    info.textContent = `Z ${play.lo}–${play.hi}`;
    let z = centre; play.dir = play.mode === "back" ? -1 : 1; play.ngAt = 0;
    play.tick = () => {
      z += play.dir;
      if (play.mode === "pingpong") {                              // 来回：到头掉头
        if (z > play.hi) { z = play.hi - 1; play.dir = -1; }
        if (z < play.lo) { z = play.lo + 1; play.dir = 1; }
      } else {                                                     // 向前 / 向后：到头回到另一端，连续循环
        if (z > play.hi) z = play.lo;
        if (z < play.lo) z = play.hi;
      }
      z = Math.max(play.lo, Math.min(play.hi, z));
      showFrame(z);
    };
    play.timer = setInterval(play.tick, Math.round(1000 / (+$("fps").value || 8)));
  }
  function stopPlay(settle = true) {
    play.controller?.abort(); play.controller = null;
    if (play.timer) { clearInterval(play.timer); play.timer = null; }
    markDir(null); $("play-info").textContent = "";
    if (settle) { play.internal = true; load(state.z, true).finally(() => { play.internal = false; }); }   // 停在哪片就把那片完整加载：表格、URL、查看器对齐
  }
  dirButtons().forEach(b => b.addEventListener("click", () => {
    const active = play.timer || play.controller;
    if (active && play.mode === b.dataset.mode) stopPlay();       // 点亮着的那个：停
    else startPlay(b.dataset.mode);                                // 其它：开始，或播放中切方向
  }));
  $("fps").addEventListener("change", () => {                  // 播放中改速度：换个节拍继续，不重新预载
    if (play.timer) { clearInterval(play.timer); play.timer = setInterval(play.tick, Math.round(1000 / (+$("fps").value || 8))); }
  });

  // ---------------------------------------------------------------- 滚轮缩放 / 键盘翻 z / 拖动平移，和标注页一个习惯
  function layoutImages(reset = false) {
    if ($("images").hidden) return;
    const W = canvases[0].width, H = canvases[0].height;
    const width = Math.min(...viewports.map(v => v.offsetWidth));
    if (!width || !W || !H) return;
    // 100% means the whole image fits BOTH dimensions, independent of image aspect ratio.
    const height = Math.floor(Math.min(width * H / W, Math.max(240, innerHeight * .65)));
    const key = [W, H, width, height, state.zoom].join(":");
    if (!reset && layout?.key === key) return; // Slice reloads must keep pan exactly unchanged.
    const previous = wraps[0].getBoundingClientRect(), viewport = viewports[0].getBoundingClientRect();
    const center = !reset && layout && layout.W === W && layout.H === H
      ? [(viewport.left + viewports[0].clientWidth / 2 - previous.left) / previous.width,
         (viewport.top + viewports[0].clientHeight / 2 - previous.top) / previous.height]
      : [.5, .5];
    $("images").style.setProperty("--cmp-view-height", `${height}px`);
    viewports.forEach(v => { v.style.height = `${height}px`; });
    const scale = Math.min(width / W, height / H) * state.zoom / 100;
    wraps.forEach(w => { w.style.width = `${W * scale}px`; w.style.height = `${H * scale}px`; });
    viewports.forEach((v, i) => {
      v.scrollLeft = wraps[i].offsetLeft + center[0] * W * scale - v.clientWidth / 2;
      v.scrollTop = wraps[i].offsetTop + center[1] * H * scale - v.clientHeight / 2;
    });
    $("zoom").value = state.zoom; $("zoom-value").textContent = `${state.zoom}%`;
    layout = {key, W, H};
    ngFit();                                                    // 盒子高度变了，iframe 立刻跟上，不等 ResizeObserver
    if (state.images && state.rimDrawn && rimWidth(canvases[0]) !== state.rimDrawn) draw();   // rim must track the new scale
  }
  function setZoom(v) {
    state.zoom = Math.max(100, Math.min(400, Math.round(v / 25) * 25));
    layoutImages();
  }
  $("zoom").addEventListener("input", ev => setZoom(+ev.target.value));
  $("fit").addEventListener("click", () => { state.zoom = 100; layoutImages(true); });
  new ResizeObserver(() => layoutImages()).observe($("images"));
  let ngResize = null;
  new ResizeObserver(() => { ngFit(); clearTimeout(ngResize); ngResize = setTimeout(ngSync, 300); }).observe($("ng-em-frame").parentElement);
  window.addEventListener("resize", () => layoutImages());
  function onWheel(ev) {
    ev.preventDefault();
    if (!ev.deltaY || Math.abs(ev.deltaX) >= Math.abs(ev.deltaY)) return;
    setZoom(state.zoom + (ev.deltaY < 0 ? 25 : -25));
  }
  // 三栏滚轮统一缩放，翻片只使用 ↑↓ / A Z 或翻页控件。
  document.querySelectorAll(".cmp-ngbox").forEach(box => box.addEventListener("wheel", onWheel, {passive: false}));
  viewports.forEach(viewport => {
    viewport.addEventListener("wheel", onWheel, {passive: false});
    let drag = null;
    viewport.addEventListener("mousedown", ev => { if (ev.button !== 0) return; drag = {x: ev.clientX, y: ev.clientY, l: viewport.scrollLeft, t: viewport.scrollTop}; viewport.classList.add("cmp-dragging"); });
    // 直接写两侧的滚动位置，不依赖 scroll 事件来同步——后台标签页/未合成帧时 scroll 事件可能不发，两边会走散
    window.addEventListener("mousemove", ev => {
      if (!drag) return;
      const l = drag.l - (ev.clientX - drag.x), t = drag.t - (ev.clientY - drag.y);
      viewports.forEach(v => { v.scrollLeft = l; v.scrollTop = t; });
    });
    window.addEventListener("mouseup", () => { drag = null; viewport.classList.remove("cmp-dragging"); });
  });
  for (const id of ["source", "search"]) $(id).addEventListener("input", () => { state.page=0; rows(); });
  $("table-prev").addEventListener("click", () => { state.page--; rows(); }); $("table-next").addEventListener("click", () => { state.page++; rows(); });
  $("csv").addEventListener("click", () => download("csv")); $("json").addEventListener("click", () => download("json"));
  document.addEventListener("keydown", e => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(e.target.tagName)) return;
    if (["ArrowUp", "ArrowDown", "a", "z"].includes(e.key.length === 1 ? e.key.toLowerCase() : e.key)) {
      e.preventDefault();
      if (!e.repeat) load(state.z+(e.key === "ArrowUp" || e.key.toLowerCase() === "a" ? -1 : 1));
    }
    if (e.key === " " && !e.repeat) { e.preventDefault(); play.timer || play.controller ? stopPlay() : startPlay(play.lastMode); }
    if (e.key.toLowerCase() === "j" && !e.repeat && REVIEW_ON) { e.preventDefault(); goNextReview(); }
  });
  // ---------------------------------------------------------------- 标记与评论：钉在体素上的一句话 + 线程
  // 图上是编号图钉，下面是列表；同事打开带 mark= 的链接看到同一个点；AI agent 用同一套接口读写（评论 kind=agent 打「AI」标）。
  const MARKS = "/api/v1/annotate";
  const fmtTime = ts => {                                   // 接口给的是带时区的 UTC，按看的人的本地时间显示
    if (!ts) return "";
    const d = new Date(ts), p = n => String(n).padStart(2, "0");
    return isNaN(d) ? ts.replace("T", " ").slice(5, 16) : `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const canDelete = m => !ME || ME.role === "admin" || (!!ME.user && m.created_by_user === ME.user);
  const labelAt = (x, y) => { const im = state.images, d = state.snapshot; if (!im?.indices?.[1] || !d?.after) return null; return d.after.ids[im.indices[1][y * canvases[0].width + x]] ?? null; };
  async function loadMarks(silent) {
    const block = state.block; if (!block) return;
    try {
      const r = await json(`${MARKS}/blocks/${encodeURIComponent(block)}/marks`);
      if (block !== state.block) return;
      state.marks = r.marks; state.marksBlock = block; renderMarks(); renderPins();
    } catch (e) { if (!silent) $("marks-list").innerHTML = `<p class="muted">读取标记失败：${esc(e.message)}</p>`; }
  }
  const showResolved = () => $("marks-resolved").checked;
  function marksOnSlice(z) { return state.marks.filter(m => m.z === z && (showResolved() || m.status === "open")); }
  function renderPins() {
    const W = canvases[0].width, H = canvases[0].height, box = $("pins");
    if (!W || !H) { box.innerHTML = ""; return; }
    box.innerHTML = marksOnSlice(state.z).map(m => `<button type="button" class="cmp-pin ${m.status}${m.id === state.markSel ? " sel" : ""}" data-id="${m.id}" style="left:${(m.x + .5) / W * 100}%;top:${(m.y + .5) / H * 100}%" title="#${m.id} ${esc(m.created_by_name || "匿名")}：${esc(m.text)}"><span>${m.id}</span></button>`).join("");
  }
  function renderMarks() {
    const scope = $("marks-scope").value, list = state.marks.filter(m => (scope === "all" || m.z === state.z) && (showResolved() || m.status === "open"));
    const open = state.marks.filter(m => m.status === "open").length;
    $("marks-count").textContent = state.marks.length ? `${open} 个待处理 · 整块共 ${state.marks.length}` : "";
    $("marks-list").innerHTML = list.map(m => `<article class="cmp-mark ${m.status}${m.id === state.markSel ? " sel" : ""}" data-id="${m.id}">
      <header><b>#${m.id}</b><span class="mono">z ${m.z} · (${m.x}, ${m.y})${m.label_id && m.label_id !== "0" ? ` · 标签 ${esc(m.label_id)}` : ""}</span><span>${esc(m.created_by_name || "匿名")}</span><span class="muted">${fmtTime(m.created_at)}</span>${m.status === "resolved" ? `<span class="cmp-tag cmp-done">已解决 · ${esc(m.resolved_by_name || "")}</span>` : ""}</header>
      <p class="cmp-mark-text">${esc(m.text)}</p>
      ${m.comments.map(c => `<div class="cmp-comment ${c.kind}"><b>${esc(c.created_by_name || "匿名")}</b>${c.kind === "agent" ? '<span class="cmp-tag cmp-agent">AI</span>' : ""}<span class="muted">${fmtTime(c.created_at)}</span><div>${esc(c.text)}</div></div>`).join("")}
      <form class="cmp-reply" data-id="${m.id}"><input class="text" name="text" placeholder="回复…" maxlength="2000" autocomplete="off"><button type="submit">发送</button></form>
      <div class="cmp-mark-actions"><button type="button" data-act="go">定位</button><button type="button" data-act="link">复制链接</button><button type="button" data-act="${m.status === "open" ? "resolve" : "reopen"}">${m.status === "open" ? "标为已解决" : "重新打开"}</button>${canDelete(m) ? '<button type="button" data-act="del" class="danger">删除</button>' : ""}</div>
    </article>`).join("") || `<p class="muted">${scope === "all" ? "这个数据块还没有标记。" : "这一片还没有标记。"}点上面的「📍 标记」，再点图上的位置即可添加。</p>`;
  }
  function setMarkMode(on) {
    state.markMode = on;
    $("mark").setAttribute("aria-pressed", String(on));
    viewports.forEach(v => v.classList.toggle("cmp-marking", on));
    if (!on) hideMarkForm();
    $("pixel").textContent = on ? "标记模式：点标注后图上的位置，写一句话钉在那里；再点「标记」退出" : "滚轮缩放 · ↑↓ / A Z 翻片 · 拖动平移（两侧同步） · 移动鼠标查看标签与来源";
  }
  function openMarkForm(x, y, cx, cy) {
    const f = $("mark-form"); f.hidden = false; f.dataset.x = x; f.dataset.y = y;
    f.style.left = `${Math.max(8, Math.min(cx + 14, innerWidth - 300))}px`; f.style.top = `${Math.max(8, Math.min(cy + 14, innerHeight - 190))}px`;
    $("mark-form-where").textContent = `z ${state.z} · (${x}, ${y})${labelAt(x, y) && labelAt(x, y) !== "0" ? ` · 标签 ${labelAt(x, y)}` : ""}`;
    $("mark-form-err").textContent = ""; f.text.value = ""; f.text.focus();
    document.querySelectorAll(".cmp-pin.draft").forEach(e => e.remove());
    $("pins").insertAdjacentHTML("beforeend", `<span class="cmp-pin draft" style="left:${(x + .5) / canvases[0].width * 100}%;top:${(y + .5) / canvases[0].height * 100}%"><span>+</span></span>`);
  }
  function hideMarkForm() { $("mark-form").hidden = true; document.querySelectorAll(".cmp-pin.draft").forEach(e => e.remove()); }
  async function focusMark(m) {
    if (m.block_id !== state.block) { if (!state.picker?.show(m.block_id)) return; await load(m.z, true); }
    else if (m.z !== state.z) await load(m.z);
    state.markSel = m.id; renderMarks(); renderPins();
    focusRegion({cx: m.x, cy: m.y, x0: m.x - 6, x1: m.x + 7, y0: m.y - 6, y1: m.y + 7});
    if (ngCorner) ngCentre(m.x, m.y);
    document.querySelector(`.cmp-mark[data-id="${m.id}"]`)?.scrollIntoView({block: "nearest", behavior: "smooth"});
  }
  async function openMarkLink(id) {
    try { const m = await json(`${MARKS}/marks/${id}`); await focusMark(m); }
    catch (e) { $("status").textContent = `找不到标记 #${id}：${e.message}`; }
  }
  $("mark").addEventListener("click", () => setMarkMode(!state.markMode));
  $("mark-form-cancel").addEventListener("click", hideMarkForm);
  $("mark-form").addEventListener("keydown", ev => { if (ev.key === "Escape") hideMarkForm(); ev.stopPropagation(); });
  $("mark-form").addEventListener("submit", async ev => {
    ev.preventDefault();
    const f = ev.target, text = f.text.value.trim(); if (!text) return;
    const x = +f.dataset.x, y = +f.dataset.y;
    try {
      const m = await json(`${MARKS}/blocks/${encodeURIComponent(state.block)}/marks`, undefined, {z: state.z, x, y, text, label_id: labelAt(x, y)});
      hideMarkForm(); setMarkMode(false); state.markSel = m.id; await loadMarks();
    } catch (e) { $("mark-form-err").textContent = e.message; }
  });
  $("marks-scope").addEventListener("change", renderMarks);
  $("marks-resolved").addEventListener("change", () => { renderMarks(); renderPins(); });
  $("pins").addEventListener("click", ev => { const pin = ev.target.closest(".cmp-pin[data-id]"); if (!pin) return; const m = state.marks.find(x => x.id === +pin.dataset.id); if (m) focusMark(m); });
  $("marks-list").addEventListener("submit", async ev => {
    ev.preventDefault();
    const f = ev.target, text = f.text.value.trim(); if (!text) return;
    try { await json(`${MARKS}/marks/${f.dataset.id}/comments`, undefined, {text}); f.text.value = ""; await loadMarks(); }
    catch (e) { $("status").textContent = `回复失败：${e.message}`; }
  });
  $("marks-list").addEventListener("click", async ev => {
    const b = ev.target.closest("button[data-act]"); if (!b) return;
    const art = b.closest(".cmp-mark"), m = state.marks.find(x => x.id === +art.dataset.id); if (!m) return;
    try {
      if (b.dataset.act === "go") await focusMark(m);
      else if (b.dataset.act === "link") { const link = `${location.origin}${m.link}`; try { await navigator.clipboard.writeText(link); $("status").textContent = `已复制链接：${link}`; } catch (_) { prompt("复制这个链接", link); } }
      else if (b.dataset.act === "resolve" || b.dataset.act === "reopen") { await json(`${MARKS}/marks/${m.id}`, undefined, {status: b.dataset.act === "resolve" ? "resolved" : "open"}, "PATCH"); await loadMarks(); }
      else if (b.dataset.act === "del") { if (!confirm(`删除标记 #${m.id} 及其 ${m.comments.length} 条回复？不需要了通常标为已解决即可。`)) return; await json(`${MARKS}/marks/${m.id}`, undefined, undefined, "DELETE"); if (state.markSel === m.id) state.markSel = null; await loadMarks(); }
    } catch (e) { $("status").textContent = `操作失败：${e.message}`; }
  });
  setInterval(() => {                                            // 同事新加的标记、别人的审核结论 30 秒内出现；正在写的时候不刷
    if (document.visibilityState !== "visible" || !state.block) return;
    if ($("mark-form").hidden) loadMarks(true);
    if (REVIEW_ON && !$("rv-dialog").open && !play.timer && !play.controller && state.snapshot?.review_sha) loadReview();
  }, 30000);

  // ---------------------------------------------------------------- 审核：通过 / 退回修改 / 撤销通过（结论钉在画面上这一片的内容哈希上）
  // 规则在服务器：能不能点、为什么不能点，都由 /review/{z} 返回；这里只负责显示和把用户看到的哈希、审核版本原样带回去。
  const REVIEW_ON = !!ME && !!$("review");
  const RV = {state: null, busy: false, serial: 0, mode: null, target: null, draft: null, nav: 0};
  const ACT = (k) => $(`rv-${k}`);
  function playing() { return !!(play.timer || play.controller); }
  // 只在有需要提醒的事时才占一行（2026-10-15 用户要求省空间）：被退回时的意见、通过后又被改、没署名的改动、不能通过的原因。
  // 谁标记完成、参与人、未解决标记数这些常规信息不再显示；「通过」按钮灰掉时鼠标停上去仍能看到原因。
  function reviewLine(st) {
    if (st.history_error) return `<span class="cmp-rv-warn">${esc(st.why_not_decide)}</span>`;
    const parts = [];
    if (st.decided_by && st.decision === "request_changes" && (st.status === "returned" || st.status === "fixed")) {
      parts.push(`<b>${esc(st.decided_by)}</b> ${fmtTime(st.decided_at)} 退回修改${st.last_comment ? `：<q>${esc(st.last_comment)}</q>` : ""}`);
    }
    if (st.since_decision?.n && (st.status === "changed" || st.status === "fixed"))
      parts.push(`<span class="cmp-rv-warn">之后又改了 ${st.since_decision.n} 次（${esc(st.since_decision.by.join("、"))}，${fmt(st.since_decision.n_px)} 像素）</span>`);
    if (st.unattributed) parts.push(`<span class="cmp-rv-warn">${st.unattributed} 笔改动没有署名</span>`);
    const why = st.in_queue && st.why_not?.approve;
    if (why) parts.push(`<span class="cmp-rv-warn">${esc(why)}</span>`);
    return parts.join(" · ");
  }
  function renderReview() {
    if (!REVIEW_ON) return;
    const st = RV.state, ready = !!st && !!state.snapshot?.review_sha && !playing() && !RV.busy && !RV.nav;
    $("review").hidden = false;
    ACT("chip").textContent = st ? st.status_name : (playing() ? "播放中" : "…");
    ACT("chip").dataset.status = st ? st.status : "";
    const set = (id, action, label) => {
      const b = ACT(id), ok = ready && st.can[action];
      b.disabled = !ok;
      b.title = st ? (st.why_not[action] || label) : (playing() ? "播放中不能审核，先停下" : "正在加载…");
    };
    set("approve", "approve", "通过：整张切片都正确（包括没改动的部分），可以入库");
    set("approve-next", "approve", "通过这一片，然后跳到下一张待审");
    set("reject", "request_changes", "退回给标注员修改：写明原因，或先在图上钉标记");
    set("revoke", "revoke", "撤销通过：写明原因，这一片回到待审核");
    ACT("revoke").hidden = !st || !["approved", "changed"].includes(st.status);
    ACT("approve").textContent = st?.status === "changed" ? "重新通过" : "通过";
    ACT("next").disabled = !!RV.nav || (!!st && !st.can_decide);
    ACT("next").title = st && !st.can_decide ? (st.why_not_decide || "你不能审核") : "跳到下一张我能审的切片（快捷键 J）";
    const line = $("rv-line"), html = st ? reviewLine(st) : "";
    line.hidden = !html;
    line.innerHTML = html;
  }
  async function loadReview() {
    if (!REVIEW_ON || !state.block) return;
    const block = state.block, z = state.z, serial = ++RV.serial;
    try {
      const st = await json(`${API}/${encodeURIComponent(block)}/review/${z}`);
      if (serial !== RV.serial || block !== state.block || z !== state.z) return;   // 结果到的时候已经翻到别的片：丢掉
      RV.state = st;
    } catch (e) {
      if (serial === RV.serial) {                                 // 读不到就把按钮都灰掉，别留着能点却点不动的按钮
        RV.state = null; renderReview();
        $("rv-line").hidden = false; $("rv-line").textContent = `审核状态读取失败：${e.message}（点「刷新」重试）`;
      }
      return;
    }
    renderReview();
  }
  function settleFocus() { document.activeElement?.blur?.(); viewports[0]?.focus({preventScroll: true}); }   // 焦点回到图像：空格、A/Z 照常好用，不会误点到按钮
  // target：这个动作针对哪一片、看到的是哪一版（打开退回弹窗时就定下来，之后翻片也不会落到别的片上）
  function currentTarget() {
    const st = RV.state;
    if (!st || !state.snapshot?.review_sha || playing() || RV.nav) return null;
    return {block: state.block, z: state.z, sha: state.snapshot.review_sha, version: st.version, open_marks: st.open_marks || []};
  }
  function notReady(msg) { const e = new Error(msg); e.code = "not_ready"; return e; }
  async function reviewAct(action, extra = {}, target = currentTarget()) {
    if (!target) throw notReady("这一片还没加载好（或正在播放），请稍候再操作");
    if (RV.busy) throw notReady("上一个审核操作还没完成");
    const {block, z} = target;
    const body = {action, expect_version: target.version, ...extra};
    if (["approve", "request_changes", "revoke"].includes(action)) body.expect_sha = target.sha;
    RV.busy = true; renderReview();
    let retry = null;
    try {
      const next = await json(`${API}/${encodeURIComponent(block)}/review/${z}`, undefined, body);
      if (block === state.block && z === state.z) { RV.state = next; $("status").textContent = `${block} · Z ${z} · ${next.status_name}`; }
      return next;
    } catch (e) {
      if (e.code === "confirm") {                                 // 还有未解决标记 / 没署名的改动：一次问清，确认后带上全部事项再发
        if (confirm(e.message)) retry = {...extra, confirm: [...new Set([...(extra.confirm || []), ...(e.info?.what || [])])]};
        else return null;
      } else {
        $("status").textContent = `${e.message}`;
        if (e.code === "stale") { if (block === state.block && z === state.z) load(state.z, true); }   // 内容变了：重新加载这一片
        else loadReview();
        throw e;
      }
    } finally {
      RV.busy = false;
      if (!retry) { renderReview(); settleFocus(); }
    }
    return await reviewAct(action, retry, target);                // 等确认后的那一次真正完成，按钮才恢复
  }
  async function goNextReview() {
    if (!REVIEW_ON || RV.nav) return;
    const nav = RV.nav = (RV.nav || 0) + 1;                       // 找的过程中审核按钮全灰：不会对着正要被换掉的那一片下结论
    renderReview();
    $("status").textContent = "正在找下一张待审…";
    try {
      const r = await json(`/api/v1/annotate/review/next?${new URLSearchParams({block: state.block, z: state.z})}`);
      if (nav !== RV.nav) return;
      if (!r.next) { $("status").textContent = r.why ? `不能审核：${r.why}` : "没有待审的切片了。审核进度见左侧「审核进度」。"; return; }
      if (r.next.block_id !== state.block && !state.picker?.has(r.next.block_id)) {
        $("status").textContent = `下一张待审在新加入的数据块 ${r.next.block_id} 里，这个页面的数据块列表里还没有它：请刷新页面后再按「下一张待审」`;
        return;
      }
      if (r.next.block_id !== state.block) state.picker.show(r.next.block_id);   // 跨批时批次下拉也跟着切过去
      RV.nav = 0;
      await load(r.next.z, r.next.block_id !== state.block);
      // 先审完当前批次才会换批：换了就明说，免得以为还在原来那一批（只在确实切过去了的时候说）
      if (r.crossed_batch && state.block === r.next.block_id && state.z === r.next.z)
        $("status").textContent = `原来那一批没有你能审的切片了，已进入批次 ${r.next.batch_id} · ${$("status").textContent}`;
    } catch (e) { $("status").textContent = `找下一张待审失败：${e.message}`; }
    finally { if (nav === RV.nav) RV.nav = 0; renderReview(); settleFocus(); }
  }
  function openDialog(mode) {
    const target = currentTarget(); if (!target) return;
    RV.mode = mode; RV.target = target;
    const marks = mode === "request_changes" ? target.open_marks : [];
    $("rv-dialog-title").textContent = `${mode === "revoke" ? "撤销通过" : "退回修改"} · Z ${target.z}`;
    $("rv-dialog-hint").textContent = mode === "revoke" ? "撤销后这一片回到「待审核」。请写明原因，会记录在审核历史里。"
      : marks.length ? `这一片有 ${marks.length} 个未解决的标记，会作为退回意见一并记下；可以再补充几句。` : "写明要标注员改什么（也可以先关掉这个窗口，在图上钉标记说明问题）。";
    $("rv-dialog-marks").hidden = !marks.length;
    $("rv-dialog-marks").innerHTML = marks.map(m => `<li>#${m.id}（${m.x}, ${m.y}）${esc(m.text)}</li>`).join("");
    $("rv-comment-label").textContent = mode === "revoke" ? "原因（必填）" : marks.length ? "补充意见（可选）" : "意见（必填）";
    $("rv-submit").textContent = mode === "revoke" ? "撤销通过" : "退回修改";
    const d = RV.draft;                                           // 上次因为内容过期没提交成的意见：同一片、同一种操作就填回去
    $("rv-comment").value = d && d.block === target.block && d.z === target.z && d.mode === mode ? d.text : "";
    $("rv-dialog-error").hidden = true;
    $("rv-dialog").showModal(); $("rv-comment").focus();
  }
  if (REVIEW_ON) {
    const report = e => { if (e?.code === "not_ready") $("status").textContent = e.message; };
    ACT("approve").addEventListener("click", () => reviewAct("approve").catch(report));
    ACT("approve-next").addEventListener("click", async () => { const r = await reviewAct("approve").catch(e => { report(e); return null; }); if (r?.status === "approved") goNextReview(); });
    ACT("reject").addEventListener("click", () => openDialog("request_changes"));
    ACT("revoke").addEventListener("click", () => openDialog("revoke"));
    ACT("next").addEventListener("click", goNextReview);
    $("rv-cancel").addEventListener("click", () => $("rv-dialog").close());
    $("rv-dialog").addEventListener("close", settleFocus);
    $("rv-form").addEventListener("submit", async ev => {
      ev.preventDefault();
      const t = RV.target, comment = $("rv-comment").value.trim();
      const err = msg => { $("rv-dialog-error").textContent = msg; $("rv-dialog-error").hidden = false; };
      if (!t) return err("这一片还没加载好，请关掉弹窗稍候再试");
      if (t.block !== state.block || t.z !== state.z) return err(`页面已经切到别的切片了；这条意见是给 Z ${t.z} 的，请回到 Z ${t.z} 再操作`);
      const needText = RV.mode === "revoke" || !t.open_marks.length;
      if (needText && !comment) return err("请写明原因");
      $("rv-submit").disabled = true;
      try {
        const r = await reviewAct(RV.mode, comment ? {comment} : {}, t);
        if (r) { RV.draft = null; $("rv-dialog").close(); }
      } catch (e) {
        if (e.code === "stale") {                                 // 内容刚被改过：意见留着，关掉弹窗让人先看最新内容
          RV.draft = {block: t.block, z: t.z, mode: RV.mode, text: $("rv-comment").value};
          $("rv-dialog").close();
          $("status").textContent = `Z ${t.z} 刚被改过，已重新加载；你写的意见已保留，看过最新内容后再点「${RV.mode === "revoke" ? "撤销通过" : "退回修改"}」`;
        } else err(e.message);
      } finally { $("rv-submit").disabled = false; }
    });
    $("rv-dialog").addEventListener("keydown", ev => ev.stopPropagation());   // 弹窗里打字不触发翻页、播放
    renderReview();
  }

  async function init() {
    $("page").setAttribute("aria-busy", "true");
    for (const id of ["batch", "block", "z", "prev", "next", "refresh"]) $(id).disabled = true;
    $("status").textContent = "正在加载数据块…";
    try {
      const r = await json("/api/v1/annotate/comparison-blocks"); state.blocks = r.blocks.filter(b => !b.error);
      if (!state.blocks.length) { $("block").replaceChildren(); $("batch").replaceChildren(); $("status").textContent = "没有可读取的数据块。请检查数据配置后点击刷新重试。"; return; }
      if (!state.picker) state.picker = window.EMQCBlockPicker.create({ batchSel: $("batch"), blockSel: $("block"), blocks: state.blocks, onPick: () => load(0) });
      $("batch").disabled = $("block").disabled = $("z").disabled = false;
      const p = new URLSearchParams(location.search);
      if (state.blocks.some(b => b.block_id === p.get("block"))) state.picker.show(p.get("block"));
      await load(p.get("z") || 0, true);
      if (p.get("mark")) await openMarkLink(+p.get("mark"));       // 同事发来的链接：跳到那一片、定位到那个点
    } catch (e) { $("status").textContent = `加载失败：${e.message}。可点击刷新重试。`; }
    finally { $("page").setAttribute("aria-busy", "false"); $("refresh").disabled = false; }
  }
  init();
})();
