/* Baseline/current comparison. All images and counts come from one read-only snapshot. */
(() => {
  const $ = id => document.getElementById(`cmp-${id}`), API = "/api/v1/annotate/blocks";
  const fmt = n => Number(n).toLocaleString("zh-CN"), esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const state = {blocks: [], block: "", z: 0, snapshot: null, images: null, serial: 0, controller: null, page: 0, downloads: new Set(), zoom: 100};
  // One canvas of ours (标注后); the other two panes are embedded Neuroglancer iframes, see ngSync().
  const canvases = [$("after")], viewports = [...document.querySelectorAll(".cmp-viewport")];
  const wraps = canvases.map(c => c.parentElement);
  let layout = null;
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
    const sourceColors = data.report.source_legend.map(s => s.color.match(/\w\w/g).map(v => parseInt(v, 16)));
    canvases.forEach(canvas => {
      const side = 1;                                                    // always the "after" picture
      canvas.width = em.width; canvas.height = em.height;
      const g = canvas.getContext("2d");
      const overlay = g.createImageData(em.width, em.height), pixels = overlay.data;
      const ids = data.after.ids, palette = ids.map(color), idx = indices[side];
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
  async function load(z = state.z, force = false) {
    const block = state.blocks.find(b => b.block_id === $("block").value); if (!block) return;
    const nextZ = Math.max(0, Math.min(block.nz-1, Math.trunc(Number(z)) || 0));
    $("z").value = nextZ;
    // Navigation to the current (or already requested) slice is a no-op. Only Refresh retries it.
    if (!force && state.serial && state.block === block.block_id && state.z === nextZ) return;
    const changedBlock = state.block !== block.block_id;
    state.controller?.abort(); state.controller = new AbortController();
    const serial = ++state.serial;
    state.block = block.block_id; state.z = nextZ;
    state.images = state.snapshot = null; state.page = 0;
    $("page").setAttribute("aria-busy", "true");
    // Keep the existing layout while loading: hiding it collapses the document and resets image pan.
    $("page").classList.remove("cmp-load-failed");
    downloadButtons();
    $("z").value = state.z; $("z").max = block.nz-1; $("zmax").textContent = `/ ${block.nz-1}　共 ${block.nz} 片`;
    $("prev").disabled = state.z === 0; $("next").disabled = state.z === block.nz-1;
    $("status").textContent = `正在加载 ${state.block} · Z ${state.z}…`;
    $("pixel").textContent = "滚轮翻片（一次手势一片） · Ctrl/⌘+滚轮缩放 · 拖动平移（两侧同步） · 移动鼠标查看标签与来源";
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
      $("status").textContent = `${state.block} · Z ${state.z} · ${em.width} × ${em.height} · ${!r.has_seg ? "此数据块没有分割标签，只显示原始电镜图" : r.changed_px ? `${fmt(r.changed_px)} 像素与原始分割不同` : "当前结果与原始分割一致"}`;
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
      $("pixel").textContent = `X ${x} · Y ${y} · Z ${state.z}    原始 ${data.before.ids[indices[0][i]]} → 当前 ${data.after.ids[indices[1][i]]}    来源：${data.report.source_legend[sources[i]].name}`;
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
      if (x >= 0 && y >= 0 && x < canvas.width && y < canvas.height) ngCentre(x, y);
    });
  });
  $("block").addEventListener("change", () => load(0)); $("z").addEventListener("change", () => load($("z").value));
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
  //   iframe 像素 = (体素坐标 - 中心) / 比例 + iframe 中心
  // 嵌入状态隐藏了查看器的顶部 UI 且 iframe 不接鼠标，所以这个换算是精确的；不再为每次移动改 URL 片段
  //（那条路要让查看器重新应用整份状态，肉眼可见地卡）。点击居中才改 URL。
  const ngBase = {em: null, seg: null}, ngView = {em: null, seg: null};
  let ngCorner = null, ngRaf = null, ngPending = null;
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
      const mark = $(`ng-${key}-mark`), view = ngView[key], frame = $(`ng-${key}-frame`);
      if (x == null || !view || !ngCorner) { mark.hidden = true; continue; }
      const w = frame.clientWidth, h = frame.clientHeight;
      const px = (ngCorner[0] + x + .5 - view.pos[0]) / view.scale + w / 2;
      const py = (ngCorner[1] + y + .5 - view.pos[1]) / view.scale + h / 2;
      const inside = px >= 0 && py >= 0 && px <= w && py <= h;
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
  // 取景尺寸用 iframe 的实际宽度（首次同步时布局可能还没排出来，向上找有宽度的容器兜底）
  function ngWidth(frame) {
    const w = frame.clientWidth || frame.parentElement?.clientWidth || Math.floor($("images").clientWidth / 3) || 600;
    return Math.max(300, Math.round(w));
  }
  async function ngSync() {
    layoutImages();
    if (!state.block) return;
    const px = ngWidth($("ng-em-frame"));
    if (ngLast.block === state.block && ngLast.z === state.z && Math.abs(px - ngLast.px) < ngLast.px * 0.15) return;
    ngLast = {block: state.block, z: state.z, px};
    const serial = ++ngSerial;
    // 首次同步常常发生在 iframe 还没有尺寸的瞬间（读到 0 → 退回 600 → 只显示块中心的一半）。等布局稳定后
    // 复查一次：真实宽度和这次用的差得多，就按真实宽度重新取景。ngLast 的 15% 守卫保证不会来回震荡。
    setTimeout(() => { const now = ngWidth($("ng-em-frame")); if (Math.abs(now - px) >= px * 0.15) ngSync(); }, 600);
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
  // ---------------------------------------------------------------- 缩放 / 滚轮翻 z / 拖动平移，和标注页一个习惯
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
  new ResizeObserver(() => { clearTimeout(ngResize); ngResize = setTimeout(ngSync, 300); }).observe($("ng-em-frame"));
  window.addEventListener("resize", () => layoutImages());
  let wheelGesture = null;
  function onWheel(ev) {
      // Horizontal swipes and zero-delta events are not requests to turn a slice.
      if (!ev.deltaY || Math.abs(ev.deltaX) >= Math.abs(ev.deltaY)) return;
      ev.preventDefault();
      const first = wheelGesture === null;
      clearTimeout(wheelGesture);
      // Rearm after the gesture has stopped, not on a repeating timer during its inertia tail.
      // Zoom shares this guard so releasing Ctrl during a gesture cannot turn a slice.
      wheelGesture = setTimeout(() => { wheelGesture = null; }, 250);
      if (ev.ctrlKey || ev.metaKey) { setZoom(state.zoom + (ev.deltaY < 0 ? 25 : -25)); return; }
      if (first) load(state.z + (ev.deltaY > 0 ? 1 : -1));
  }
  // 查看器栏的 iframe 不接鼠标，滚轮落在外层盒子上：同样翻 z，三栏一起动；拖动只属于我们自己的画布
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
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      if (!e.repeat) load(state.z+(e.key === "ArrowLeft" ? -1 : 1));
    }
  });
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
      await load(p.get("z") || 0, true);
    } catch (e) { $("status").textContent = `加载失败：${e.message}。可点击刷新重试。`; }
    finally { $("page").setAttribute("aria-busy", "false"); $("refresh").disabled = false; }
  }
  init();
})();
