/* Baseline/current comparison. All images and counts come from one read-only snapshot. */
(() => {
  const $ = id => document.getElementById(`cmp-${id}`), API = "/api/v1/annotate/blocks";
  const fmt = n => Number(n).toLocaleString("zh-CN"), esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const state = {blocks: [], block: "", z: 0, snapshot: null, images: null, serial: 0, controller: null, page: 0, downloads: new Set(), zoom: 100, ng: false, ngBase: ""};
  const canvases = [$("before"), $("after")], viewports = [...document.querySelectorAll(".cmp-viewport")];
  const image = url => new Promise((resolve, reject) => { const im = new Image(); im.onload = () => resolve(im); im.onerror = () => reject(new Error("图片读取失败")); im.src = url; });
  async function json(url, signal) {
    const r = await fetch(url, {signal, cache: "no-store"});
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(typeof e.detail === "string" ? e.detail : `请求失败 (${r.status})`); }
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
  function draw() {
    if (!state.images) return;
    const {em, indices, sources, changes} = state.images, data = state.snapshot;
    // Fixed instead of a slider. The change marker is always on, but ONLY on the right: the left is "before",
    // it must show the baseline exactly as delivered, with nothing drawn on top of it.
    const alpha = 0.5, sourceMode = $("mode").value === "sources";
    // 左图默认是纯 EM 原图——「标注前」对标注员来说就是什么颜色都没有的那张；想对照交付的原始分割可以切过去
    const leftRaw = $("left-mode").value === "em";
    $("after-caption").textContent = sourceMode ? "当前结果 · 按来源着色" : "当前编辑结果";
    const sourceColors = data.report.source_legend.map(s => s.color.match(/\w\w/g).map(v => parseInt(v, 16)));
    canvases.forEach((canvas, side) => {
      canvas.width = em.width; canvas.height = em.height;
      const g = canvas.getContext("2d");
      if (!side && leftRaw) { g.drawImage(em, 0, 0); return; }           // 原图：一个像素的叠加都不画
      const overlay = g.createImageData(em.width, em.height), pixels = overlay.data;
      const ids = (side ? data.after : data.before).ids, palette = ids.map(color), idx = indices[side];
      for (let i = 0; i < idx.length; i++) {
        const c = side && sourceMode ? sourceColors[sources[i]] : palette[idx[i]];
        if (ids[idx[i]] !== "0" || (side && sourceMode && changes[i])) {
          pixels.set(c, i*4); pixels[i*4+3] = Math.round(alpha*255);
        }
      }
      // Changed regions on the right are outlined with a black-outside / white-inside double edge. A filled tint
      // was tried first and failed twice over: the pink was indistinguishable from pink cells, and it hid the
      // colour the annotator had actually painted. A two-tone edge cannot be mistaken for any label colour and
      // leaves the interior visible.
      if (side) {
        const W = em.width, H = em.height;
        const changedAt = (x, y) => x >= 0 && y >= 0 && x < W && y < H && changes[y * W + x] > 0;
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
          const i = y * W + x, me = changes[i] > 0;
          const nb = changedAt(x - 1, y) || changedAt(x + 1, y) || changedAt(x, y - 1) || changedAt(x, y + 1);
          const inner = me && !(changedAt(x - 1, y) && changedAt(x + 1, y) && changedAt(x, y - 1) && changedAt(x, y + 1));
          if (inner) { pixels.set([255, 255, 255], i*4); pixels[i*4+3] = 255; }        // white rim just inside
          else if (!me && nb) { pixels.set([0, 0, 0], i*4); pixels[i*4+3] = 230; }     // black rim just outside
        }
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
      viewport.scrollTo({left: (g.cx + .5) * scale - viewport.clientWidth / 2,
                         top: (g.cy + .5) * scale - viewport.clientHeight / 2, behavior: "smooth"});
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
    $("regions-count").textContent = changes.n_total ? `${fmt(changes.n_total)} 处 · 共 ${fmt(report.changed_px)} 像素` : "";
    $("regions").innerHTML = regions.map((g, i) => `<tr class="cmp-click" data-region="${i}" tabindex="0"><td class="mono">${g.cx}, ${g.cy}</td><td>${fmt(g.px)}</td><td>${idCell(g.from_id)} → ${idCell(g.to_id)}</td></tr>`).join("")
      || `<tr><td colspan="3" class="muted">${report.has_seg ? "这一片与原始分割完全一致。" : "此数据块没有分割标签。"}</td></tr>`;
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
    const kinds = {paint:"画笔 / 橡皮", fill:"填充 / 清除", merge:"合并", split:"切割 / 分离（历史）", sam:"SAM 应用", repair:"插值修补", smartfill:"智能填充（历史）"};
    $("operations").innerHTML = r.operations.slice().reverse().map(e => `<tr><td>#${e.n} ${esc(kinds[e.kind] || e.kind)}</td><td>${esc(names[e.source])}</td><td>${esc(e.ts || "—")}</td><td>${fmt(e.n_px_in_slice)}</td><td>${fmt(e.current_px)}</td><td>${esc(e.model || (e.source_sections ? `Z ${e.source_sections.join(", ")}` : "—"))}</td></tr>`).join("") || '<tr><td colspan="6">当前切片没有可读取的有效编辑记录。</td></tr>';
    rows();
  }
  async function load(z = state.z) {
    const block = state.blocks.find(b => b.block_id === $("block").value); if (!block) return;
    state.controller?.abort(); state.controller = new AbortController();
    const serial = ++state.serial;
    state.block = block.block_id; state.z = Math.max(0, Math.min(block.nz-1, Math.trunc(Number(z)) || 0));
    state.images = state.snapshot = null; state.page = 0;
    $("page").setAttribute("aria-busy", "true");
    for (const id of ["images", "report", "summary"]) $(id).hidden = true;
    downloadButtons();
    $("z").value = state.z; $("z").max = block.nz-1; $("zmax").textContent = `/ ${block.nz-1}　共 ${block.nz} 片`;
    $("prev").disabled = state.z === 0; $("next").disabled = state.z === block.nz-1;
    $("status").textContent = `正在加载 ${state.block} · Z ${state.z}…`;
    $("pixel").textContent = "移动鼠标查看两侧同一像素的标签与来源。";
    document.querySelectorAll(".cmp-cursor, .cmp-ring").forEach(c => { c.hidden = true; });
    const params = new URLSearchParams({block:state.block, z:state.z});
    history.replaceState(null, "", `/annotate/compare?${params}`); $("edit").href = `/annotate?${params}`;
    try {
      const data = await json(`${API}/${encodeURIComponent(state.block)}/compare/${state.z}`, state.controller.signal);
      const [em, before, after, sources, changes] = await Promise.all([data.em_png, data.before.png, data.after.png, data.sources_png, data.changes_png].map(image));
      if (serial !== state.serial) return;
      state.snapshot = data; state.images = {em, indices:[decode(before, true), decode(after, true)], sources:decode(sources), changes:decode(changes)};
      draw(); renderReport(); renderChanges();
      for (const id of ["images", "report", "summary"]) $(id).hidden = false;
      ngSync();
      downloadButtons();
      const r = data.report;
      $("status").textContent = `${state.block} · Z ${state.z} · ${em.width} × ${em.height} · ${!r.has_seg ? "此数据块没有分割标签，两侧均显示原始电镜图" : r.changed_px ? `${fmt(r.changed_px)} 像素与原始分割不同` : "当前结果与原始分割一致"}`;
    } catch (e) { if (serial === state.serial && e.name !== "AbortError") $("status").textContent = `加载失败：${e.message}。可点击刷新重试。`; }
    finally { if (serial === state.serial) $("page").setAttribute("aria-busy", "false"); }
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
        const headers = ["block_id", "z", "label_id", "before_px", "current_px", "changed_px", ...saved.source_legend.map(s => s.key), "mixed"];
        const csvCell = v => `"${String(v).replace(/"/g, '""')}"`;
        const safeBlock = /^[=+\-@\t\r\n]/.test(block) ? "'"+block : block;
        const lines = saved.labels.map(r => [safeBlock,z,r.id,r.before_px,r.current_px,r.changed_px,...saved.source_legend.map(s => r.sources[s.key]),r.mixed].map(csvCell).join(","));
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
      if (Math.abs(other.scrollLeft-viewport.scrollLeft) > 1) other.scrollLeft = viewport.scrollLeft;
      if (Math.abs(other.scrollTop-viewport.scrollTop) > 1) other.scrollTop = viewport.scrollTop;
    });
    canvases[side].addEventListener("mousemove", event => {
      if (!state.images) return;
      const canvas = canvases[side], rect = canvas.getBoundingClientRect();
      const x = Math.floor((event.clientX-rect.left)*canvas.width/rect.width), y = Math.floor((event.clientY-rect.top)*canvas.height/rect.height);
      if (x<0 || y<0 || x>=canvas.width || y>=canvas.height) return;
      const i = y*canvas.width+x, {indices, sources} = state.images, data = state.snapshot;
      $("pixel").textContent = `X ${x} · Y ${y} · Z ${state.z}    原始 ${data.before.ids[indices[0][i]]} → 当前 ${data.after.ids[indices[1][i]]}    来源：${data.report.source_legend[sources[i]].name}`;
      document.querySelectorAll(".cmp-cursor").forEach(c => { c.hidden = false; c.style.left = `${(x+.5)/canvas.width*100}%`; c.style.top = `${(y+.5)/canvas.height*100}%`; });
    });
    viewport.addEventListener("mouseleave", () => document.querySelectorAll(".cmp-cursor").forEach(c => { c.hidden = true; }));
  });
  $("block").addEventListener("change", () => load(0)); $("z").addEventListener("change", () => load($("z").value));
  $("prev").addEventListener("click", () => load(state.z-1)); $("next").addEventListener("click", () => load(state.z+1));
  $("refresh").addEventListener("click", () => state.blocks.length ? load() : init());
  $("mode").addEventListener("input", draw);
  $("left-mode").addEventListener("input", draw);
  // ---------------------------------------------------------------- 第三栏：嵌入公开的 H01 Neuroglancer
  // 我们的 EM/seg 与公开 c3 已逐像素验证一致，所以把它的图拉下来画成静态图会和左图一模一样；嵌活的查看器
  // 才有意义——能缩放、能开关图层、能开 3D。位置由服务端按 meta.geometry 算好（和「看这一点」同一套换算）。
  // 翻 z 时只改 URL 的 # 片段：Neuroglancer 监听 hashchange 就地更新，不会整页重载。
  let ngSerial = 0;
  async function ngSync() {
    const pane = $("ng-pane"), frame = $("ng-frame"), note = $("ng-note");
    $("images").classList.toggle("with-ng", state.ng);
    pane.hidden = !state.ng;
    if (!state.ng || !state.block) return;
    const serial = ++ngSerial, px = Math.max(300, Math.round($("ng-frame").clientWidth || 600));
    try {
      const r = await json(`${API}/${encodeURIComponent(state.block)}/neuroglancer/embed?z=${state.z}&px=${px}`);
      if (serial !== ngSerial) return;
      if (!r.url) { note.hidden = false; note.textContent = r.reason || "无法定位到公开数据集"; frame.removeAttribute("src"); $("ng-open").removeAttribute("href"); return; }
      note.hidden = true;
      $("ng-open").href = r.url;
      $("ng-caption").textContent = `公开 H01 · EM + c3 分割 · Z ${state.z} · 中心 ${r.center.join(", ")}`;
      if (frame.getAttribute("src") !== r.url) frame.src = r.url;
    } catch (e) { if (serial === ngSerial) { note.hidden = false; note.textContent = "加载失败：" + e.message; } }
  }
  $("ng").addEventListener("change", ev => { state.ng = ev.target.checked; try { localStorage.setItem("cmp-ng", state.ng ? "1" : "0"); } catch (_) {} ngSync(); });
  try { if (localStorage.getItem("cmp-ng") === "1") { state.ng = true; $("ng").checked = true; } } catch (_) {}
  // ---------------------------------------------------------------- 缩放 / 滚轮翻 z / 拖动平移，和标注页一个习惯
  function setZoom(v) {
    state.zoom = Math.max(100, Math.min(400, Math.round(v / 25) * 25));
    $("zoom").value = state.zoom; $("zoom-value").textContent = `${state.zoom}%`;
    document.querySelectorAll(".cmp-canvas-wrap").forEach(w => { w.style.width = `${state.zoom}%`; });
    try { localStorage.setItem("cmp-zoom", String(state.zoom)); } catch (_) {}
  }
  $("zoom").addEventListener("input", ev => setZoom(+ev.target.value));
  try { const saved = +localStorage.getItem("cmp-zoom"); if (saved) setZoom(saved); } catch (_) {}
  let wheelBusy = false;
  viewports.forEach(viewport => {
    viewport.addEventListener("wheel", ev => {
      ev.preventDefault();
      if (ev.ctrlKey || ev.metaKey) { setZoom(state.zoom + (ev.deltaY < 0 ? 25 : -25)); return; }
      if (wheelBusy) return;                                   // 一格滚轮只翻一片，别把一次惯性滚动变成十几片
      wheelBusy = true; setTimeout(() => { wheelBusy = false; }, 120);
      load(state.z + (ev.deltaY > 0 ? 1 : -1));
    }, {passive: false});
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
  document.addEventListener("keydown", e => { if (["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(e.target.tagName)) return; if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); load(state.z+(e.key === "ArrowLeft" ? -1 : 1)); } });
  async function init() {
    $("page").setAttribute("aria-busy", "true");
    for (const id of ["block", "z", "prev", "next", "refresh"]) $(id).disabled = true;
    $("status").textContent = "正在加载数据块…";
    try {
      const r = await json("/api/v1/annotate/comparison-blocks"); state.blocks = r.blocks.filter(b => !b.error);
      $("block").replaceChildren(...state.blocks.map(b => { const o = document.createElement("option"); o.value = b.block_id; o.textContent = b.block_id; return o; }));
      if (!state.blocks.length) { $("status").textContent = "没有可读取的数据块。请检查数据配置后点击刷新重试。"; return; }
      $("block").disabled = $("z").disabled = false;
      const p = new URLSearchParams(location.search);
      if (state.blocks.some(b => b.block_id === p.get("block"))) $("block").value = p.get("block");
      await load(p.get("z") || 0);
    } catch (e) { $("status").textContent = `加载失败：${e.message}。可点击刷新重试。`; }
    finally { $("page").setAttribute("aria-busy", "false"); $("refresh").disabled = false; }
  }
  init();
})();
