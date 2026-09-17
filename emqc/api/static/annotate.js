/* Slice annotation viewer (VAST-style). No framework: fetch + canvas.
 *
 * Data per section z comes as two images and one table: the EM PNG, a *label index* PNG (R = hi byte, G = lo byte
 * of a uint16 index) and idx -> id table. Colouring, eyedropping and hover-highlight all run on the index map in the
 * browser; only fill / paint / merge / undo go to the server, which is the only place labels are actually changed.
 * ids are strings throughout (H01 ids exceed 2^53).
 *
 * Two view modes: "overlay" (one pane, segmentation drawn over the EM at the chosen opacity) and "side" (two panes,
 * left EM / right segmentation, sharing zoom, pan, z and a linked cursor). Every pane is a stack of three canvases:
 * EM, segmentation, highlight.
 */
(() => {
  const API = "/api/v1/annotate";
  const $ = id => document.getElementById(id);
  const S = {
    block: null, info: null, W: 0, H: 0, z: 0,
    tool: "pick", brush: 4, cur: "0",
    view: "overlay", rightSegOnly: false, curtain: false, curtainX: 0, blink: false, fade: false, fadeMax: 0.45, fadeRaf: 0,
    opacity: 0.45, outline: false, showEm: true, showSeg: true, hover: true,
    zoom: 1, tx: 0, ty: 0,
    cache: new Map(), loading: new Map(), hoverIdx: -1, hoverXY: null, hoverPane: 0,
    playing: null, drag: null, stroke: null, mergeFrom: null, pinIdx: -1, spacePan: false, curtainDrag: false,
  };
  function mkPane(id) {
    const stage = $(id), cv = stage.querySelector(".vast-canvas"), [em, seg, hi] = cv.querySelectorAll("canvas");
    return { stage, cv, em, seg, hi, gEm: em.getContext("2d"), gSeg: seg.getContext("2d"), gHi: hi.getContext("2d") };
  }
  const P = [mkPane("an-stage"), mkPane("an-stage2")];
  const panes = () => (S.view === "side" ? P : [P[0]]);
  const segPanes = () => (S.view === "side" ? [P[1]] : (S.showSeg ? [P[0]] : []));   // where the segmentation is drawn
  const nearCurtain = x => S.view === "overlay" && S.curtain && Math.abs(x - S.curtainX) <= 6 / S.zoom;

  // ------------------------------------------------------------------ colours: stable per id, everywhere
  const colorCache = new Map();
  function colorOf(id) {
    if (id === "0") return [0, 0, 0];
    let c = colorCache.get(id);
    if (c) return c;
    let h = 2166136261;                                   // FNV-1a over the id string
    for (let i = 0; i < id.length; i++) { h ^= id.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    const hue = h % 360, sat = 0.62 + ((h >>> 9) % 30) / 100, lig = 0.48 + ((h >>> 17) % 16) / 100;
    c = hsl(hue, sat, lig); colorCache.set(id, c); return c;
  }
  function hsl(h, s, l) {
    const k = n => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
    const f = n => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return [Math.round(255 * f(0)), Math.round(255 * f(8)), Math.round(255 * f(4))];
  }
  const css = c => `rgb(${c[0]},${c[1]},${c[2]})`;

  // ------------------------------------------------------------------ data
  async function getJSON(u) { const r = await fetch(u); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); }
  async function postJSON(u, body) { const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); }
  const loadImg = url => new Promise((ok, bad) => { const im = new Image(); im.onload = () => ok(im); im.onerror = () => bad(new Error("image " + url)); im.src = url; });

  function decodeIdx(img) {
    const oc = document.createElement("canvas"); oc.width = img.width; oc.height = img.height;
    const g = oc.getContext("2d", { willReadFrequently: true }); g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, oc.width, oc.height).data, n = oc.width * oc.height, idx = new Uint16Array(n);
    for (let i = 0, j = 0; i < n; i++, j += 4) idx[i] = (d[j] << 8) | d[j + 1];
    return idx;
  }

  function fetchZ(z) {
    if (S.cache.has(z)) return Promise.resolve(S.cache.get(z));
    if (S.loading.has(z)) return S.loading.get(z);
    const b = encodeURIComponent(S.block), ver = S.info?.n_edits || 0;
    const p = Promise.all([
      loadImg(`${API}/blocks/${b}/em/${z}.png`),
      S.info.has_seg ? loadImg(`${API}/blocks/${b}/labels/${z}.png?v=${ver}`) : null,
      S.info.has_seg ? getJSON(`${API}/blocks/${b}/labels/${z}.json?v=${ver}`) : null,
    ]).then(([em, lab, tab]) => {
      const e = { em, idx: lab ? decodeIdx(lab) : null, ids: tab ? tab.ids : ["0"], counts: tab ? tab.counts : [], segImgs: new Map() };
      S.cache.set(z, e); S.loading.delete(z);
      while (S.cache.size > 24) { const k = S.cache.keys().next().value; if (k !== S.z) S.cache.delete(k); else break; }
      return e;
    }).catch(err => { S.loading.delete(z); throw err; });
    S.loading.set(z, p);
    return p;
  }
  function prefetch() { for (const d of [1, -1, 2, -2, 3, -3]) { const z = S.z + d; if (z >= 0 && z < S.info.shape_zyx[0]) fetchZ(z).catch(() => {}); } }
  function invalidate(z) { S.cache.delete(z); S.loading.delete(z); }
  function dropAll() { S.cache.clear(); S.loading.clear(); }

  // ------------------------------------------------------------------ rendering
  function segCanvas(e, outline) {
    const key = `${outline}|${e.ids.length}`;
    const hit = e.segImgs.get(key); if (hit) return hit;
    const W = S.W, H = S.H, img = P[0].gSeg.createImageData(W, H), d = img.data, idx = e.idx;
    const pal = new Uint8Array(e.ids.length * 3);
    for (let k = 1; k < e.ids.length; k++) { const c = colorOf(e.ids[k]); pal[k * 3] = c[0]; pal[k * 3 + 1] = c[1]; pal[k * 3 + 2] = c[2]; }
    if (!outline) {
      for (let i = 0, j = 0; i < idx.length; i++, j += 4) { const k = idx[i]; if (k) { d[j] = pal[k * 3]; d[j + 1] = pal[k * 3 + 1]; d[j + 2] = pal[k * 3 + 2]; d[j + 3] = 255; } }
    } else {
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const i = y * W + x, k = idx[i];
        if (!k) continue;
        if ((x + 1 < W && idx[i + 1] !== k) || (y + 1 < H && idx[i + W] !== k) || (x > 0 && idx[i - 1] !== k) || (y > 0 && idx[i - W] !== k)) {
          const j = i * 4; d[j] = pal[k * 3]; d[j + 1] = pal[k * 3 + 1]; d[j + 2] = pal[k * 3 + 2]; d[j + 3] = 255;
        }
      }
    }
    const oc = document.createElement("canvas"); oc.width = W; oc.height = H; oc.getContext("2d").putImageData(img, 0, 0);
    e.segImgs.clear(); e.segImgs.set(key, oc); return oc;
  }
  function drawSeg(g, e, alpha) {
    g.save(); g.globalAlpha = S.outline ? Math.min(1, alpha + 0.35) : alpha; g.drawImage(segCanvas(e, S.outline), 0, 0); g.restore();
  }

  function render() {
    const e = S.cache.get(S.z); if (!e) return;
    for (const p of P) { p.gEm.clearRect(0, 0, S.W, S.H); p.gSeg.clearRect(0, 0, S.W, S.H); }
    const p0 = P[0];
    if (S.showEm) p0.gEm.drawImage(e.em, 0, 0);
    if (S.view === "overlay") {
      if (e.idx && S.showSeg && !S.blink) {
        drawSeg(p0.gSeg, e, S.opacity);
        if (S.curtain) p0.gSeg.clearRect(0, 0, S.curtainX, S.H);            // left of the divider: EM only
      }
    } else {
      const p1 = P[1];                                                       // right pane: EM + segmentation, to compare with the bare EM on the left
      if (!S.rightSegOnly) p1.gEm.drawImage(e.em, 0, 0);
      if (e.idx && S.showSeg && !S.blink) drawSeg(p1.gSeg, e, S.rightSegOnly ? 1 : S.opacity);
    }
    renderHi();
  }
  function renderHi() {
    for (const p of P) p.gHi.clearRect(0, 0, S.W, S.H);
    const e = S.cache.get(S.z); if (!e) return;
    const marks = [];
    if (S.pinIdx > 0 && e.idx) marks.push([S.pinIdx, [255, 214, 10], 90]);
    if (S.hover && e.idx && S.hoverIdx > 0 && S.hoverIdx !== S.pinIdx) marks.push([S.hoverIdx, [255, 255, 255], 60]);
    if (marks.length) {
      const img = P[0].gHi.createImageData(S.W, S.H), d = img.data, idx = e.idx, W = S.W, H = S.H;
      for (const [k, c, fillA] of marks) for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const i = y * W + x; if (idx[i] !== k) continue;
        const edge = (x + 1 < W && idx[i + 1] !== k) || (y + 1 < H && idx[i + W] !== k) || (x > 0 && idx[i - 1] !== k) || (y > 0 && idx[i - W] !== k);
        const j = i * 4; d[j] = c[0]; d[j + 1] = c[1]; d[j + 2] = c[2]; d[j + 3] = edge ? 235 : fillA;
      }
      for (const p of panes()) p.gHi.putImageData(img, 0, 0);
    }
    if (S.view === "overlay" && S.curtain) {
      const g = P[0].gHi, cx = S.curtainX + 0.5, lw = 1 / S.zoom;
      g.lineWidth = 3 * lw; g.strokeStyle = "rgba(0,0,0,.55)"; g.beginPath(); g.moveTo(cx, 0); g.lineTo(cx, S.H); g.stroke();
      g.lineWidth = lw; g.strokeStyle = "#ffd60a"; g.beginPath(); g.moveTo(cx, 0); g.lineTo(cx, S.H); g.stroke();
      g.fillStyle = "#ffd60a"; g.beginPath(); g.arc(cx, S.H / 2, 9 * lw, 0, Math.PI * 2); g.fill();
      g.fillStyle = "#000"; g.font = `${Math.max(8, 11 / S.zoom)}px sans-serif`; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText("⇔", cx, S.H / 2);
    }
    if (!S.hoverXY) return;
    const [x, y] = S.hoverXY, lw = 1 / S.zoom;
    if (S.tool === "brush" || S.tool === "erase") for (const p of panes()) {
      const g = p.gHi; g.beginPath(); g.arc(x + 0.5, y + 0.5, S.brush + 0.5, 0, Math.PI * 2);
      g.lineWidth = lw; g.strokeStyle = S.tool === "erase" ? "#ff6b6b" : css(colorOf(S.cur)); g.stroke();
      g.strokeStyle = "rgba(0,0,0,.6)"; g.lineWidth = lw / 2; g.stroke();
    }
    if (S.view === "side") panes().forEach((p, i) => {            // linked cursor: cross-hair in the pane you are *not* in
      if (i === S.hoverPane) return;
      const g = p.gHi, r = 14 / S.zoom; g.lineWidth = lw; g.strokeStyle = "rgba(255,255,255,.9)";
      g.beginPath(); g.moveTo(x + 0.5 - r, y + 0.5); g.lineTo(x + 0.5 + r, y + 0.5); g.moveTo(x + 0.5, y + 0.5 - r); g.lineTo(x + 0.5, y + 0.5 + r); g.stroke();
      g.strokeStyle = "rgba(0,0,0,.7)"; g.lineWidth = lw / 2; g.stroke();
    });
  }
  function applyView() { const t = `translate(${S.tx}px,${S.ty}px) scale(${S.zoom})`; for (const p of P) p.cv.style.transform = t; status(); }
  function fit() {
    const r = P[0].stage.getBoundingClientRect(); S.zoom = Math.max(0.05, Math.min(r.width / S.W, r.height / S.H) * 0.98);
    S.tx = (r.width - S.W * S.zoom) / 2; S.ty = (r.height - S.H * S.zoom) / 2; applyView();
  }
  function zoomAt(f, cx, cy) {
    const nz = Math.min(64, Math.max(0.05, S.zoom * f));
    S.tx = cx - (cx - S.tx) * nz / S.zoom; S.ty = cy - (cy - S.ty) * nz / S.zoom; S.zoom = nz; applyView();
  }
  function toImg(ev, p) { const r = p.cv.getBoundingClientRect(); return [Math.floor((ev.clientX - r.left) / S.zoom), Math.floor((ev.clientY - r.top) / S.zoom)]; }
  const inside = (x, y) => x >= 0 && y >= 0 && x < S.W && y < S.H;
  function idAt(x, y) { const e = S.cache.get(S.z); if (!e || !e.idx || !inside(x, y)) return null; return e.ids[e.idx[y * S.W + x]]; }

  function status() {
    const nz = S.info ? S.info.shape_zyx[0] : 0;
    let s = `z ${S.z} / ${nz - 1}`;
    if (S.hoverXY) { const [x, y] = S.hoverXY; const id = idAt(x, y); s += ` · x ${x} y ${y}` + (id != null ? ` · id ${id}` : ""); }
    s += ` · 当前 ${S.cur} · ${Math.round(S.zoom * 100)}%`;
    $("an-status").textContent = s;
  }

  // ------------------------------------------------------------------ opacity: slider, keys, auto fade
  function setOpacity(v, fromSlider) {
    S.opacity = Math.max(0, Math.min(1, v)); if (!fromSlider) $("an-op").value = Math.round(S.opacity * 100);
    $("an-op-v").textContent = Math.round(S.opacity * 100) + "%"; render();
  }
  function fadeTick(ts) {
    if (!S.fade) return;
    const T = 2600, ph = (ts % T) / T;                       // 0 -> max -> 0, one breath every 2.6 s
    setOpacity(S.fadeMax * (0.5 - 0.5 * Math.cos(2 * Math.PI * ph)));
    S.fadeRaf = requestAnimationFrame(fadeTick);
  }
  function setFade(on) {
    S.fade = on; $("an-fade").checked = on;
    if (on) { S.fadeMax = S.opacity > 0.05 ? S.opacity : 0.6; S.fadeRaf = requestAnimationFrame(fadeTick); }
    else { cancelAnimationFrame(S.fadeRaf); setOpacity(S.fadeMax); }
  }

  // ------------------------------------------------------------------ view mode
  function setView(v) {
    S.view = v;
    P[1].stage.classList.toggle("off", v !== "side");
    $("an-stages").classList.toggle("side", v === "side");
    $("an-curtain-row").classList.toggle("off", v !== "overlay"); $("an-rightseg-row").classList.toggle("off", v !== "side");
    document.querySelectorAll("input[name=an-view]").forEach(r => r.checked = r.value === v);
    if (S.info) { fit(); render(); }
  }

  // ------------------------------------------------------------------ z navigation
  async function goZ(z, keepHover) {
    const nz = S.info.shape_zyx[0]; z = Math.max(0, Math.min(nz - 1, z | 0));
    S.z = z; $("an-z").value = z; $("an-zr").value = z;
    if (!keepHover) S.hoverIdx = -1;
    try { await fetchZ(z); } catch (e) { $("an-status").textContent = "加载失败: " + e.message; return; }
    if (S.z !== z) return;                                // user moved on while we were loading
    S.pinIdx = S.mergeFrom ? S.cache.get(z).ids.indexOf(S.mergeFrom) : -1;
    render(); status(); segList(); prefetch();
  }
  let wheelAcc = 0;
  function onWheel(ev, p) {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) { const r = p.stage.getBoundingClientRect(); zoomAt(Math.exp(-ev.deltaY * 0.002), ev.clientX - r.left, ev.clientY - r.top); return; }
    wheelAcc += ev.deltaY; const step = ev.deltaMode === 1 ? 1 : 40;   // one section per notch, not per pixel
    if (Math.abs(wheelAcc) >= step) { const n = Math.trunc(wheelAcc / step); wheelAcc -= n * step; goZ(S.z + n, true); }
  }

  // ------------------------------------------------------------------ tools
  function setTool(t) {
    S.tool = t; mergeArm(null);
    document.querySelectorAll(".tool").forEach(b => b.classList.toggle("active", b.dataset.tool === t));
    for (const p of P) p.stage.classList.toggle("pan", t === "pan");
    renderHi();
  }
  function setCur(id) { S.cur = String(id); $("an-cur-id").textContent = S.cur; $("an-cur-sw").style.background = S.cur === "0" ? "transparent" : css(colorOf(S.cur)); status(); segList(); }
  function pick(x, y) { const id = idAt(x, y); if (id == null) return; setCur(id); if (S.tool === "merge") mergeArm(id !== "0" ? id : null); }   // Alt+click in merge mode: (re)select the first cell

  function floodLocal(e, x, y, newIdx) {           // scanline flood fill on the index map, 4-connectivity
    const W = S.W, H = S.H, idx = e.idx, target = idx[y * W + x]; if (target === newIdx) return 0;
    const stack = [[x, y]]; let n = 0;
    while (stack.length) {
      let [px, py] = stack.pop(); let i = py * W + px; if (idx[i] !== target) continue;
      let x0 = px; while (x0 > 0 && idx[i - 1] === target) { x0--; i--; }
      let x1 = px; i = py * W + px; while (x1 + 1 < W && idx[i + 1] === target) { x1++; i++; }
      for (let xx = x0, k = py * W + x0; xx <= x1; xx++, k++) {
        idx[k] = newIdx; n++;
        if (py > 0 && idx[k - W] === target) stack.push([xx, py - 1]);
        if (py + 1 < H && idx[k + W] === target) stack.push([xx, py + 1]);
      }
    }
    return n;
  }
  function ensureIdx(e, id) { let k = e.ids.indexOf(id); if (k < 0) { e.ids.push(id); e.counts.push(0); k = e.ids.length - 1; } return k; }

  async function fill(x, y, whole) {
    const e = S.cache.get(S.z); if (!e || !e.idx) return;
    const old = idAt(x, y); if (old === S.cur) return;
    const k = ensureIdx(e, S.cur), z = S.z;
    if (whole) { const t = e.idx[y * S.W + x]; for (let i = 0; i < e.idx.length; i++) if (e.idx[i] === t) e.idx[i] = k; }
    else floodLocal(e, x, y, k);
    e.segImgs.clear(); render();                                // optimistic: show it now, reconcile after the server answers
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/fill`, { z, x, y, new_id: S.cur, whole_slice: !!whole });
      afterEdit(r, z);
    } catch (err) { flash("填充失败: " + err.message, true); invalidate(z); goZ(z, true); }
  }

  // Merge is a two-click gesture. Which click is the keeper is a setting (an-dir):
  //   first_into_second (default): click the cell to change, then the target -> the first takes the second's id/colour
  //   second_into_first:           click the keeper, then the cell to absorb   -> the second takes the first's id/colour
  const mergeDir = () => document.querySelector("input[name=an-dir]:checked").value;
  function mergeArm(id) {
    S.mergeFrom = id; const e = S.cache.get(S.z); S.pinIdx = id && e ? e.ids.indexOf(id) : -1;
    const h = $("an-merge-hint"); h.hidden = !id;
    if (id) h.textContent = mergeDir() === "first_into_second"
      ? `已选 ${id}（黄框）。再点目标细胞，${id} 会变成目标的 id 和颜色；Esc 取消`
      : `保留方 ${id}（黄框）。再点要并入的细胞，它会变成 ${id} 的 id 和颜色；Esc 取消`;
    renderHi();
  }
  const mergeScope = () => document.querySelector("input[name=an-scope]:checked").value;
  async function mergeInto(x, y) {
    const clicked = idAt(x, y); if (clicked == null) return;
    if (!S.mergeFrom) { if (clicked === "0") { flash("先点一个细胞"); return; } mergeArm(clicked); setCur(clicked); return; }
    if (clicked === S.mergeFrom) { flash("点的是同一个细胞"); return; }
    if (clicked === "0") { flash("不能并到背景；要删细胞请用橡皮"); return; }
    const firstIntoSecond = mergeDir() === "first_into_second";
    const from = firstIntoSecond ? S.mergeFrom : clicked, to = firstIntoSecond ? clicked : S.mergeFrom;
    const scope = mergeScope(), z = S.z, e = S.cache.get(z);
    if (e && e.idx) {                                            // optimistic: recolour this slice now
      const kf = e.ids.indexOf(from), kt = ensureIdx(e, to);
      if (kf >= 0) { for (let i = 0; i < e.idx.length; i++) if (e.idx[i] === kf) e.idx[i] = kt; e.counts[kt] = (e.counts[kt] || 0) + (e.counts[kf] || 0); e.counts[kf] = 0; }
      e.segImgs.clear(); render();
    }
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/merge`, { from_id: from, to_id: to, scope, z });
      if (!r.edit) { flash("没有可合并的体素"); return; }
      if (scope === "block") dropAll();
      flash(`已把 ${from} 并入 ${to}：${from} 现在是 ${to} 的 id 和颜色（${r.edit.n_px} 个体素，${r.edit.n_slices} 片）。再点一个细胞开始下一次`);
      mergeArm(null); setCur(to);                                // pairwise: the result becomes the current label, next click starts afresh
      afterEdit(r, z);
    } catch (err) { flash("合并失败: " + err.message, true); dropAll(); goZ(z, true); }
  }

  function strokeStart(x, y) { S.stroke = { pts: [[x, y]], z: S.z, id: S.tool === "erase" ? "0" : S.cur }; strokeDot(x, y); }
  function strokeDot(x, y) {
    const id = S.stroke.id;
    for (const p of segPanes()) {
      const g = p.gSeg; g.save(); g.globalCompositeOperation = id === "0" ? "destination-out" : "source-over";
      g.fillStyle = id === "0" ? "#000" : `rgba(${colorOf(id).join(",")},${S.view === "side" ? 1 : S.opacity})`;
      g.beginPath(); g.arc(x + 0.5, y + 0.5, S.brush + 0.5, 0, Math.PI * 2); g.fill(); g.restore();
    }
  }
  function strokeMove(x, y) {
    const p = S.stroke.pts, [lx, ly] = p[p.length - 1]; if (lx === x && ly === y) return;
    const n = Math.max(Math.abs(x - lx), Math.abs(y - ly));
    for (let t = 1; t <= n; t++) strokeDot(Math.round(lx + (x - lx) * t / n), Math.round(ly + (y - ly) * t / n));
    p.push([x, y]);
  }
  async function strokeEnd() {
    const st = S.stroke; S.stroke = null; if (!st) return;
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/paint`, { z: st.z, points: st.pts, radius: S.brush, new_id: st.id });
      afterEdit(r, st.z);
    } catch (err) { flash("涂抹失败: " + err.message, true); invalidate(st.z); goZ(st.z, true); }
  }
  function afterEdit(r, z) {
    S.info.n_edits = r.n_edits; $("an-nedit").textContent = `${r.n_edits} 次改动`;
    invalidate(z); if (z === S.z) goZ(z, true); editList();
  }
  async function undo() {
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/undo`);
      if (!r.undone) { flash("没有可撤销的改动"); return; }
      S.info.n_edits = r.n_edits; $("an-nedit").textContent = `${r.n_edits} 次改动`;
      if (r.undone.z == null || r.undone.kind === "merge") dropAll();       // 3-D edit: every slice may differ
      else { invalidate(r.undone.z); if (r.undone.z !== S.z) invalidate(S.z); }
      goZ(S.z, true); editList();
    } catch (err) { flash("撤销失败: " + err.message, true); }
  }
  async function newId() {
    try { const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/new-id`); setCur(r.id); if (S.tool === "pick") setTool("fill"); flash(`新标签 ${r.id}，已选为当前标签`); }
    catch (err) { flash("新建失败: " + err.message, true); }
  }

  // ------------------------------------------------------------------ mouse: the same handlers on every pane
  function bindStage(p, i) {
    p.stage.addEventListener("contextmenu", ev => ev.preventDefault());
    p.stage.addEventListener("wheel", ev => onWheel(ev, p), { passive: false });
    p.stage.addEventListener("mousedown", ev => {
      const [x, y] = toImg(ev, p);                        // before focus(): focusing may scroll the page and move the canvas
      p.stage.focus({ preventScroll: true });
      if (ev.button === 0 && nearCurtain(x)) { S.curtainDrag = true; return; }
      if (ev.button === 2 || ev.button === 1 || S.tool === "pan" || S.spacePan) { S.drag = { x: ev.clientX, y: ev.clientY, tx: S.tx, ty: S.ty }; for (const q of P) q.stage.classList.add("panning"); return; }
      if (ev.button !== 0 || !inside(x, y)) return;
      if (ev.altKey || S.tool === "pick") { pick(x, y); return; }
      if (S.tool === "fill") { fill(x, y, ev.shiftKey); return; }
      if (S.tool === "merge") { mergeInto(x, y); return; }
      if (S.tool === "brush" || S.tool === "erase") strokeStart(x, y);
    });
    let pending = false;
    p.stage.addEventListener("mousemove", ev => {
      if (S.drag) { S.tx = S.drag.tx + ev.clientX - S.drag.x; S.ty = S.drag.ty + ev.clientY - S.drag.y; applyView(); return; }
      const [x, y] = toImg(ev, p); S.hoverXY = inside(x, y) ? [x, y] : null; S.hoverPane = i;
      if (S.curtainDrag) { S.curtainX = Math.max(0, Math.min(S.W, x)); render(); return; }
      p.stage.style.cursor = nearCurtain(x) ? "col-resize" : "";
      if (S.stroke) { if (S.hoverXY) strokeMove(x, y); return; }
      const e = S.cache.get(S.z), k = e && e.idx && S.hoverXY ? e.idx[y * S.W + x] : -1;
      const changed = k !== S.hoverIdx; S.hoverIdx = k; status();
      if (!pending && (changed || S.view === "side" || S.tool === "brush" || S.tool === "erase")) { pending = true; requestAnimationFrame(() => { pending = false; renderHi(); }); }
    });
    p.stage.addEventListener("mouseleave", () => { S.hoverXY = null; S.hoverIdx = -1; renderHi(); status(); });
  }
  P.forEach(bindStage);
  window.addEventListener("mouseup", () => { S.curtainDrag = false; if (S.drag) { S.drag = null; for (const q of P) q.stage.classList.remove("panning"); } if (S.stroke) strokeEnd(); });

  // ------------------------------------------------------------------ keyboard
  document.addEventListener("keydown", ev => {
    if (ev.target instanceof Element && ev.target.matches("input,select,textarea")) return;
    const k = ev.key;
    if ((ev.ctrlKey || ev.metaKey) && k.toLowerCase() === "z") { ev.preventDefault(); undo(); return; }
    if (k === "ArrowUp" || k === "w") { ev.preventDefault(); goZ(S.z - 1, true); }
    else if (k === "ArrowDown" || k === "s") { ev.preventDefault(); goZ(S.z + 1, true); }
    else if (k === "PageUp") { ev.preventDefault(); goZ(S.z - 10, true); }
    else if (k === "PageDown") { ev.preventDefault(); goZ(S.z + 10, true); }
    else if (k === "Home") goZ(0); else if (k === "End") goZ(S.info.shape_zyx[0] - 1);
    else if (k === "Escape") { if (S.mergeFrom) mergeArm(null); }
    else if (k === "m") setTool("merge");
    else if (k === "p") setTool("pick"); else if (k === "f") setTool("fill"); else if (k === "b") setTool("brush"); else if (k === "e") setTool("erase"); else if (k === "h") setTool("pan");
    else if (k === "[") setBrush(S.brush - 1); else if (k === "]") setBrush(S.brush + 1);
    else if (k === "o") { $("an-outline").checked = S.outline = !S.outline; render(); }
    else if (k === "v") setView(S.view === "side" ? "overlay" : "side");
    else if (k === "g") setFade(!S.fade);
    else if (k === ",") setOpacity(S.opacity - 0.05); else if (k === ".") setOpacity(S.opacity + 0.05);
    else if (k === "c") { $("an-curtain").checked = S.curtain = !S.curtain; if (S.curtain && !S.curtainX) S.curtainX = S.W >> 1; render(); }
    else if (k === "Tab") { ev.preventDefault(); if (!S.blink) { S.blink = true; render(); } }
    else if (k === "n") newId();
    else if (k === "0") fit(); else if (k === "1") { S.zoom = 1; applyView(); }
    else if (k === "+" || k === "=") { const r = P[0].stage.getBoundingClientRect(); zoomAt(1.25, r.width / 2, r.height / 2); }
    else if (k === "-") { const r = P[0].stage.getBoundingClientRect(); zoomAt(0.8, r.width / 2, r.height / 2); }
    else if (k === " ") { ev.preventDefault(); S.spacePan = true; for (const q of P) q.stage.classList.add("pan"); }
  });
  document.addEventListener("keyup", ev => {
    if (ev.key === " ") { S.spacePan = false; if (S.tool !== "pan") for (const q of P) q.stage.classList.remove("pan"); }
    if (ev.key === "Tab" && S.blink) { S.blink = false; render(); }
  });

  // ------------------------------------------------------------------ side panels
  function segList() {
    const e = S.cache.get(S.z), box = $("an-segs"); if (!e || !e.idx) { box.innerHTML = ""; $("an-nseg").textContent = ""; return; }
    const q = $("an-search").value.trim();
    let rows = e.ids.map((id, k) => [id, e.counts[k] || 0, k]).filter(r => r[0] !== "0" && (!q || r[0].includes(q)));
    rows.sort((a, b) => b[1] - a[1]);
    $("an-nseg").textContent = `${e.ids.length - 1} 个`;
    box.innerHTML = rows.slice(0, 400).map(([id, n, k]) => `<div class="row ${id === S.cur ? "cur" : ""}" data-id="${id}" data-k="${k}"><span class="sw" style="background:${css(colorOf(id))}"></span><span class="id" title="${id}">${id}</span><span class="n">${n}</span></div>`).join("")
      + (rows.length > 400 ? `<div class="row"><span class="n">… 还有 ${rows.length - 400} 个，用搜索框过滤</span></div>` : "");
  }
  $("an-segs").addEventListener("click", ev => { const r = ev.target.closest(".row[data-id]"); if (r) setCur(r.dataset.id); });
  $("an-segs").addEventListener("mouseover", ev => { const r = ev.target.closest(".row[data-id]"); if (r) { S.hoverIdx = +r.dataset.k; renderHi(); } });
  $("an-segs").addEventListener("mouseleave", () => { S.hoverIdx = -1; renderHi(); });
  $("an-search").addEventListener("input", segList);

  async function editList() {
    try {
      const r = await getJSON(`${API}/blocks/${encodeURIComponent(S.block)}/edits?limit=30`);
      const label = e => e.kind === "merge" ? (e.scope === "block" ? "合并·整块" : "合并·本片") : e.kind === "fill" ? (e.whole_slice ? "整片" : "填充") : "涂抹";
      $("an-edits").innerHTML = r.edits.map(e => `<div class="row"><span class="sw" style="background:${e.new_id === "0" ? "transparent" : css(colorOf(e.new_id))}"></span><span class="id">#${e.n} ${label(e)} ${e.z == null ? `${e.n_slices} 片` : "z" + e.z} → ${e.new_id}</span><span class="n">${e.n_px}px</span></div>`).join("") || `<div class="row"><span class="n">还没有改动</span></div>`;
    } catch (_) { /* panel is informational */ }
  }

  // ------------------------------------------------------------------ controls
  function setBrush(v) { S.brush = Math.max(0, Math.min(60, v | 0)); $("an-brush").value = S.brush; $("an-brush-v").textContent = S.brush; renderHi(); }
  document.querySelectorAll(".tool").forEach(b => b.addEventListener("click", () => setTool(b.dataset.tool)));
  $("an-brush").addEventListener("input", ev => setBrush(+ev.target.value));
  $("an-newid").addEventListener("click", newId);
  $("an-bg").addEventListener("click", () => setCur("0"));
  $("an-prev").addEventListener("click", () => goZ(S.z - 1));
  $("an-next").addEventListener("click", () => goZ(S.z + 1));
  $("an-z").addEventListener("change", ev => goZ(+ev.target.value));
  $("an-zr").addEventListener("input", ev => goZ(+ev.target.value, true));
  $("an-undo").addEventListener("click", undo);
  $("an-play").addEventListener("click", () => {
    if (S.playing) { clearInterval(S.playing); S.playing = null; $("an-play").textContent = "▶ 连播"; return; }
    const fps = Math.max(1, Math.min(60, +$("an-fps").value || 8));
    $("an-play").textContent = "■ 停止";
    S.playing = setInterval(() => { const nz = S.info.shape_zyx[0]; goZ((S.z + 1) % nz, true); }, 1000 / fps);
  });
  $("an-show-em").addEventListener("change", ev => { S.showEm = ev.target.checked; render(); });
  $("an-show-seg").addEventListener("change", ev => { S.showSeg = ev.target.checked; render(); });
  $("an-op").addEventListener("input", ev => { if (S.fade) setFade(false); setOpacity(ev.target.value / 100, true); });
  $("an-fade").addEventListener("change", ev => setFade(ev.target.checked));
  $("an-outline").addEventListener("change", ev => { S.outline = ev.target.checked; render(); });
  $("an-hover").addEventListener("change", ev => { S.hover = ev.target.checked; renderHi(); });
  $("an-rightseg").addEventListener("change", ev => { S.rightSegOnly = ev.target.checked; render(); });
  $("an-curtain").addEventListener("change", ev => { S.curtain = ev.target.checked; if (S.curtain && !S.curtainX) S.curtainX = S.W >> 1; render(); });
  document.querySelectorAll("input[name=an-view]").forEach(r => r.addEventListener("change", () => setView(r.value)));
  document.querySelectorAll("input[name=an-dir]").forEach(r => r.addEventListener("change", () => { if (S.mergeFrom) mergeArm(S.mergeFrom); }));
  window.addEventListener("resize", () => { if (S.info) fit(); });

  // ------------------------------------------------------------------ blocks
  async function selectBlock(id) {
    if (S.playing) { clearInterval(S.playing); S.playing = null; $("an-play").textContent = "▶ 连播"; }
    S.block = id; dropAll(); S.hoverIdx = -1; mergeArm(null);
    S.info = await getJSON(`${API}/blocks/${encodeURIComponent(id)}`);
    const [nz, H, W] = S.info.shape_zyx; S.W = W; S.H = H; S.curtainX = W >> 1;
    for (const p of P) { for (const c of [p.em, p.seg, p.hi]) { c.width = W; c.height = H; } p.cv.style.width = W + "px"; p.cv.style.height = H + "px"; }
    $("an-z").max = nz - 1; $("an-zr").max = nz - 1; $("an-nz").textContent = `/ ${nz - 1}`;
    $("an-nedit").textContent = `${S.info.n_edits} 次改动`;
    const g = S.info.voxel_size_nm ? ` · ${S.info.voxel_size_nm.join("×")} nm` : "";
    $("an-meta").textContent = `${W}×${H}×${nz}${g}${S.info.has_seg ? "" : " · 无分割"}${S.info.has_working_copy ? " · 有编辑副本" : ""}`;
    history.replaceState(null, "", `/annotate?block=${encodeURIComponent(id)}`);
    setCur("0"); fit(); await goZ(Math.min(S.z, nz - 1)); editList();
  }
  async function init() {
    setView(S.view);
    const r = await getJSON(`${API}/blocks`);
    const sel = $("an-block");
    if (!r.blocks.length) { sel.innerHTML = `<option>没有数据块</option>`; $("an-status").textContent = r.root ? `在 ${r.root} 下没有找到 em.npy` : "未配置 EMQC_ANNOTATE_ROOT"; return; }
    sel.innerHTML = r.blocks.map(b => `<option value="${b.block_id}" ${b.error ? "disabled" : ""}>${b.block_id}${b.error ? " · 无法读取" : b.has_seg ? "" : " · 无分割"}${b.n_edits ? ` · ${b.n_edits} 改动` : ""}</option>`).join("");
    const pre = window.AN_PRESELECT && r.blocks.some(b => b.block_id === window.AN_PRESELECT) ? window.AN_PRESELECT : r.blocks.find(b => !b.error)?.block_id;
    sel.value = pre; sel.addEventListener("change", () => selectBlock(sel.value));
    await selectBlock(pre);
  }
  init().catch(err => { $("an-status").textContent = "初始化失败: " + err.message; });
})();
