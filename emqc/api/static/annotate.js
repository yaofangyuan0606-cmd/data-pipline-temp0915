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
    block: null, info: null, W: 0, H: 0, z: Math.max(0, parseInt(new URLSearchParams(location.search).get("z"), 10) || 0),
    tool: "pick", brush: 4, cur: "0", createdIds: [], bulk: null,
    view: "overlay", rightSegOnly: false, curtain: false, curtainX: 0, blink: false, fade: false, fadeMax: 0.45, fadeRaf: 0,
    opacity: 0.45, outline: false, showEm: true, showSeg: true, hover: true,
    zoom: 1, tx: 0, ty: 0,
    cache: new Map(), loading: new Map(), cacheVersion: 0, hoverXY: null, hoverPane: 0, regionEntry: null, regions: [],
    playing: null, drag: null, stroke: null, mergeFirst: null, mergeBusy: false, spacePan: false, curtainDrag: false,
    samPoints: [], samLabels: [], samBox: null, samStart: null, samPreview: null, samMask: null, samRequest: null, samSequence: 0,
    samNeighbour: null, repair: null, repairMask: null,
    historySequence: 0, ngMode: null, ngRequest: null, ngSequence: 0, ngWindow: null, ngTimer: null,
    who: "", hbTimer: null, revs: new Map(), undoTarget: null,
  };
  // 登录用户（服务端按会话记标注人；这里只用于显示和"是不是我"的判断）。没开登录的实例是 null → 页面里填名字。
  const ME = window.EMQC_USER || null;
  function mkPane(id) {
    const stage = $(id), cv = stage.querySelector(".vast-canvas"), [em, seg, hi] = cv.querySelectorAll("canvas");
    return { stage, cv, em, seg, hi, gEm: em.getContext("2d"), gSeg: seg.getContext("2d"), gHi: hi.getContext("2d") };
  }
  const P = [mkPane("an-stage"), mkPane("an-stage2")];
  // Put shared notifications in the sidebar, outside both image panes.
  $("an-notices").prepend($("flash"));
  $("flash").setAttribute("role", "status");
  $("an-notice-close").addEventListener("click", () => { $("flash").style.display = "none"; });
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
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------------ data
  async function getJSON(u) { const r = await fetch(u); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); }
  async function postJSON(u, body, signal) {
    const r = await fetch(u, { signal, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    if (!r.ok) {
      // 409 的 detail 是个对象：{code, message, latest, rev}——把 code 挂在错误上，调用方按它决定怎么办
      const d = (await r.json().catch(() => ({}))).detail;
      const err = new Error(typeof d === "string" ? d : d?.message || r.statusText);
      err.status = r.status;
      if (d && typeof d === "object") { err.code = d.code; err.info = d; }
      if (r.status === 401) { location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`; }   // 会话过期：回登录页
      throw err;
    }
    return r.json();
  }
  const loadImg = url => new Promise((ok, bad) => { const im = new Image(); im.onload = () => ok(im); im.onerror = () => bad(new Error("image " + url)); im.src = url; });

  function decodeIdx(img) {
    const oc = document.createElement("canvas"); oc.width = img.width; oc.height = img.height;
    const g = oc.getContext("2d", { willReadFrequently: true }); g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, oc.width, oc.height).data, n = oc.width * oc.height, idx = new Uint16Array(n);
    const u32 = new Uint32Array(d.buffer);                  // R = low byte, G = next: read both in one load
    for (let i = 0; i < n; i++) { const p = u32[i]; idx[i] = ((p & 255) << 8) | ((p >>> 8) & 255); }
    return idx;
  }

  function fetchZ(z) {
    if (S.cache.has(z)) return Promise.resolve(S.cache.get(z));
    if (S.loading.has(z)) return S.loading.get(z);
    const b = encodeURIComponent(S.block), ver = S.info?.n_edits || 0, version = S.cacheVersion;
    // 先取标签表（带这一片的版本号 rev），再取索引图：图至少和 rev 一样新。反过来（图旧、rev 新）会让页面拿着
    // 新版本号去改旧画面，服务端就查不出别人刚做的改动了。图的 URL 带上 rev，撤销再重做（改动数不变）也不会命中旧缓存。
    const p = (async () => {
      const emP = loadImg(`${API}/blocks/${b}/em/${z}.png?v=${encodeURIComponent(S.info.em_version || "1")}`);
      const tab = S.info.has_seg ? await getJSON(`${API}/blocks/${b}/labels/${z}.json?v=${ver}`) : null;
      const lab = S.info.has_seg ? await loadImg(`${API}/blocks/${b}/labels/${z}.png?v=${ver}&r=${tab?.rev ?? 0}`) : null;
      return [await emP, lab, tab];
    })().then(([em, lab, tab]) => {
      if (tab && version === S.cacheVersion) { S.createdIds = tab.created_ids || []; if (tab.rev != null) S.revs.set(z, tab.rev); }
      // rev：这一片的版本号；改标签时随请求带回去，服务端据此知道我看到的是不是最新的
      const e = { em, idx: lab ? decodeIdx(lab) : null, ids: tab ? tab.ids : ["0"], counts: tab ? tab.counts : [], rev: tab && tab.rev != null ? tab.rev : null, segImgs: new Map() };
      if (version !== S.cacheVersion || S.loading.get(z) !== p) return e;
      S.cache.set(z, e); S.loading.delete(z);
      while (S.cache.size > 24) { const k = S.cache.keys().next().value; if (k !== S.z) S.cache.delete(k); else break; }
      return e;
    }).catch(err => { if (S.loading.get(z) === p) S.loading.delete(z); throw err; });
    S.loading.set(z, p);
    return p;
  }
  function prefetch() { for (const d of [1, -1, 2, -2, 3, -3]) { const z = S.z + d; if (z >= 0 && z < S.info.shape_zyx[0]) fetchZ(z).catch(() => {}); } }
  function invalidate(z) { S.cache.delete(z); S.loading.delete(z); if (z === S.z) clearRegions(); }
  function dropAll() { S.cacheVersion++; S.cache.clear(); S.loading.clear(); clearRegions(); }

  // ------------------------------------------------------------------ 标注人：每一笔改动记在谁名下
  // 平台没有账号，"谁"就是标注员在右上角填的名字：存在本机浏览器里，每次写入随请求带给服务端，写进每条记录。
  const WHO_KEY = "emqc.annotator";
  const when = ts => typeof ts === "string" && ts.length >= 16 ? ts.slice(11, 16) : "刚才";
  const editLabel = e => e.kind === "smartfill" ? "智能填充（历史）" : e.kind === "repair" ? "修补·插值" : e.kind === "clear" ? (e.scope === "batch" ? `批量删除·${e.n_ids} 个` : "清除") : e.kind === "split" ? (e.mode === "line" ? "切割（历史）" : "分离（历史）") : e.kind === "sam" ? "SAM 分割" : e.kind === "merge" ? (e.scope === "component" ? "合并·两块" : e.scope === "block" ? "合并·整块" : "合并·本片") : e.kind === "fill" ? (e.whole_slice ? "整片" : "填充") : "涂抹";
  function cleanWho(v) { return String(v || "").replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, 64); }
  function setWho(v, save = true) {
    S.who = cleanWho(v);
    if ($("an-who")) $("an-who").value = S.who;
    if (save) { try { localStorage.setItem(WHO_KEY, S.who); } catch (_) {} }
    heartbeat();
  }
  if (ME) S.who = ME.name;
  else { try { S.who = cleanWho(localStorage.getItem(WHO_KEY)); } catch (_) { S.who = ""; } }
  if ($("an-who")) {
    $("an-who").value = S.who;
    $("an-who").addEventListener("change", ev => { setWho(ev.target.value); if (S.who) flash(`之后的改动记在「${S.who}」名下`); });
  }
  const isMine = e => !!e && (ME?.user ? e.by_user === ME.user : (!!S.who && e.by === S.who));
  // 没填名字就想改标签：先问（只在没开登录的实例上；登录了名字来自会话）。看图不需要名字，改动需要。
  function ensureWho() {
    if (ME || S.who) return true;
    const dlg = $("an-who-dialog");
    if (!dlg.open) { $("an-who-input").value = ""; dlg.showModal(); $("an-who-input").focus(); }
    return false;
  }
  $("an-who-ok").addEventListener("click", () => {
    const v = cleanWho($("an-who-input").value);
    if (!v) { $("an-who-input").focus(); return; }
    setWho(v); $("an-who-dialog").close(); flash(`之后的改动记在「${v}」名下，可以开始改了`);
  });
  $("an-who-later").addEventListener("click", () => $("an-who-dialog").close());
  $("an-who-input").addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); $("an-who-ok").click(); } });
  for (const id of ["an-who-dialog", "an-undo-confirm", "an-undo-all-confirm"]) $(id).addEventListener("keydown", ev => ev.stopPropagation());   // 对话框里的按键不落到画布快捷键上
  // 写入请求的公共字段：谁在改，以及我看到的是这一片的哪个版本（服务端据此判断有没有别人在我之后动过它）
  function editBody(z, body) { return { ...body, annotator: ME ? undefined : (S.who || undefined), expect_rev: z != null && S.revs.has(z) ? S.revs.get(z) : undefined }; }
  // 服务端说"别人在你之后改过这一片"（409 stale）：重载这一片，把话转给标注员。返回 true 表示已处理完
  async function stale(err, z) {
    if (err.code !== "stale") return false;
    flash(err.message, true); invalidate(z);
    if (z === S.z) await goZ(z, true, true);              // 已经翻到别的片就只作废缓存，不把人拽回去
    return true;
  }
  // SAM 应用 / 修补应用被拒（409：这一片在预览之后变了）：同样刷新这一片，让人看到最新画面再决定
  function refreshIfConflict(err, z) { if (err.status === 409) { invalidate(z); if (z === S.z) goZ(z, true, true); } }
  // 撤销撞上别人的改动（409 not_yours）：说清楚是谁几点改的，确认了才带 force 再撤
  function undoAsk(latest) {
    S.undoTarget = latest?.n ?? null;                    // 钉住确认的那一笔：中间要是又多了一笔，服务端会拒绝而不是撤错
    $("an-undo-msg").innerHTML = `本片最近一次改动是 <b>${esc(latest?.by ?? "未署名")}</b> 在 ${esc(when(latest?.ts))} 做的（${esc(editLabel(latest || {}))}，${Number(latest?.n_px || 0).toLocaleString("zh-CN")} 像素）。`
      + `<br>撤销别人的改动会记入操作流水（撤销人、被撤销人）。确定要撤销吗？`;
    $("an-undo-confirm").showModal();
  }
  // 全部撤销：把本片所有改动一次撤掉，回到标注前。先确认——撤了没有"重做"。
  async function undoAllAsk() {
    if (S.mergeBusy || !ensureWho() || !S.block) return;
    let r;
    try { r = await getJSON(`${API}/blocks/${encodeURIComponent(S.block)}/edits?z=${S.z}&limit=1`); } catch (err) { flash("读取本片历史失败：" + err.message, true); return; }
    if (!r.n) { flash("本片没有可撤销的改动"); return; }
    const px = (r.editors || []).reduce((s, e) => s + (e.n_px || 0), 0);
    const others = (r.editors || []).filter(e => !(ME?.user ? e.by === S.who : e.by === S.who) && e.by);
    $("an-undo-all-msg").innerHTML = `将撤销 <b>z ${S.z}</b> 这一片上的全部 <b>${r.n}</b> 笔改动（共 ${px.toLocaleString("zh-CN")} 像素），回到标注前的状态。`
      + (others.length ? `<br>其中包含别人的改动：${others.map(e => `${esc(e.by)} ×${e.n}`).join("、")}。` : "")
      + `<br>每一笔都记入操作流水；撤了不能再恢复。`;
    $("an-undo-all-confirm").showModal();
  }
  async function undoAll() {
    $("an-undo-all-confirm").close();
    if (S.mergeBusy || !ensureWho()) return;
    const z = S.z;
    clearSAM(); mergeBusy(true); mergeArm(null);
    $("an-merge-hint").textContent = "正在撤销本片全部改动，请稍候…";
    try {
      const force = !ME || ME.role !== "annotator";          // 审核员 / 管理员可以连别人的一起撤；标注员只能撤自己的
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/undo-all?z=${z}`, editBody(z, { force }));
      S.info.n_edits = r.n_edits;
      if (r.rev != null) S.revs.set(z, r.rev);
      invalidate(z); await goZ(z, true, true);
      flash(`已撤销本片全部 ${r.undone.n} 笔改动（${Number(r.undone.n_px || 0).toLocaleString("zh-CN")} 像素），回到标注前`);
    } catch (err) {
      if (await stale(err, z)) return;
      if (err.code === "not_yours") { flash(`本片有 ${err.info?.latest?.by ?? "别人"} 的改动；只有审核员或管理员能撤销别人的改动`, true); return; }
      flash("全部撤销失败：" + err.message, true);
    } finally { mergeBusy(false); mergeArm(null); }
  }
  $("an-undo-all").addEventListener("click", undoAllAsk);
  $("an-undo-all-no").addEventListener("click", () => $("an-undo-all-confirm").close());
  $("an-undo-all-yes").addEventListener("click", undoAll);
  $("an-undo-no").addEventListener("click", () => $("an-undo-confirm").close());
  $("an-undo-yes").addEventListener("click", () => { $("an-undo-confirm").close(); undo(true, S.undoTarget); });

  // ------------------------------------------------------------------ 多人：同块还有谁在线、别人改了我正在看的这一片
  // 每 15 秒向服务端报一次"我在这块的第 z 片"，换回同块的其他人；同时比对这一片的版本号：
  // 变了而且最后一笔不是我 → 重载这一片并提示。翻片后 1.5 秒也报一次，让"也在本片"的提示跟得上。
  async function heartbeat() {
    if (!S.block || !S.info || document.visibilityState !== "visible") return;
    const z = S.z, block = S.block;
    let r;
    try { r = await postJSON(`${API}/blocks/${encodeURIComponent(block)}/presence`, { z, annotator: S.who || undefined }); } catch (_) { return; }
    if (block !== S.block || z !== S.z) return;
    const el = $("an-presence"), same = r.others.filter(o => o.same_slice);
    el.hidden = !r.others.length;
    el.classList.toggle("same", same.length > 0);
    el.innerHTML = r.others.length ? `同块在线：${r.others.map(o => `<b>${esc(o.by)}</b>${o.same_slice ? "（也在本片！）" : `（z ${o.z}）`}`).join(" · ")}` : "";
    const mineRev = S.revs.get(z);
    if (S.cache.has(z) && mineRev != null && r.rev != null && r.rev > mineRev && !S.stroke && !S.mergeBusy && !S.drag) {
      const l = r.latest, mine = isMine(l);
      invalidate(z); await goZ(z, true, true);
      if (!mine && l) flash(`${l.by || "未署名的操作"} 在 ${when(l.ts)} ${l.action === "undo" ? "撤销了本片的一次改动" : `改了本片 ${Number(l.n_px || 0).toLocaleString("zh-CN")} 像素`}，已刷新`);
    }
  }
  setInterval(heartbeat, 15000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") heartbeat(); });

  // ------------------------------------------------------------------ rendering
  function segCanvas(e, outline) {
    const key = `${outline}|${e.ids.length}`;
    const hit = e.segImgs.get(key); if (hit) return hit;
    const W = S.W, H = S.H, img = P[0].gSeg.createImageData(W, H), d = img.data, idx = e.idx;
    const pal = new Uint8Array(e.ids.length * 3);
    for (let k = 1; k < e.ids.length; k++) { const c = colorOf(e.ids[k]); pal[k * 3] = c[0]; pal[k * 3 + 1] = c[1]; pal[k * 3 + 2] = c[2]; }
    if (!outline) {
      // one 32-bit store per pixel instead of four 8-bit ones; the palette is pre-packed in the canvas byte order
      const v = new Uint32Array(d.buffer), pal32 = new Uint32Array(e.ids.length);
      for (let k = 1; k < e.ids.length; k++) pal32[k] = (255 << 24) | (pal[k * 3 + 2] << 16) | (pal[k * 3 + 1] << 8) | pal[k * 3];
      for (let i = 0; i < idx.length; i++) { const k = idx[i]; if (k) v[i] = pal32[k]; }
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
    const pin = S.mergeFirst && S.mergeFirst.z === S.z ? regionAt(e, ...S.mergeFirst.xy) : null;
    const hover = S.hover && S.hoverXY ? regionAt(e, ...S.hoverXY) : null;
    if (pin) drawRegion(pin, [255, 214, 10], 90);
    if (hover && (!pin || !pin.mask[S.hoverXY[1] * S.W + S.hoverXY[0]])) drawRegion(hover, [255, 255, 255], 60);
    drawSAM();
    if (S.repairMask && !S.blink) for (const p of panes()) p.gHi.drawImage(S.repairMask, 0, 0);
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
  // A label may occur in several disconnected places. Cache only the two regions
  // currently pointed at/selected, and flood from the actual cursor pixel.
  function clearRegions() { S.regionEntry = null; S.regions = []; }
  function regionAt(e, x, y) {
    if (!e.idx || !inside(x, y)) return null;
    const W = S.W, H = S.H, idx = e.idx, seed = y * W + x, label = idx[seed];
    if (!label) return null;
    if (S.regionEntry !== e) { clearRegions(); S.regionEntry = e; }
    const hit = S.regions.find(r => r.mask[seed]); if (hit) return hit;
    const mask = new Uint8Array(idx.length), pixels = [], stack = [seed];
    let minX = x, maxX = x, minY = y, maxY = y;
    while (stack.length) {
      const pos = stack.pop(); if (mask[pos] || idx[pos] !== label) continue;
      const py = Math.floor(pos / W); let left = pos % W, right = left;
      while (left > 0 && idx[py * W + left - 1] === label && !mask[py * W + left - 1]) left--;
      while (right + 1 < W && idx[py * W + right + 1] === label && !mask[py * W + right + 1]) right++;
      minX = Math.min(minX, left); maxX = Math.max(maxX, right); minY = Math.min(minY, py); maxY = Math.max(maxY, py);
      let above = false, below = false;
      for (let px = left; px <= right; px++) {
        const i = py * W + px; mask[i] = 1; pixels.push(i);
        const up = py > 0 && idx[i - W] === label && !mask[i - W];
        const down = py + 1 < H && idx[i + W] === label && !mask[i + W];
        if (up && !above) stack.push(i - W);
        if (down && !below) stack.push(i + W);
        above = up; below = down;
      }
    }
    const region = { label, mask, pixels, minX, maxX, minY, maxY, idx };
    S.regions.unshift(region); S.regions.length = Math.min(2, S.regions.length);
    return region;
  }
  function drawRegion(r, color, alpha) {
    const W = S.W, H = S.H, width = r.maxX - r.minX + 1;
    const img = P[0].gHi.createImageData(width, r.maxY - r.minY + 1), d = img.data;
    for (const i of r.pixels) {
      const x = i % W, y = Math.floor(i / W), k = r.label;
      const edge = x === 0 || y === 0 || x === W - 1 || y === H - 1 || r.idx[i - 1] !== k || r.idx[i + 1] !== k || r.idx[i - W] !== k || r.idx[i + W] !== k;
      const j = ((y - r.minY) * width + x - r.minX) * 4;
      d[j] = color[0]; d[j + 1] = color[1]; d[j + 2] = color[2]; d[j + 3] = edge ? 235 : alpha;
    }
    // A transparent bounding box must not erase another selected region.
    const canvas = document.createElement("canvas"); canvas.width = img.width; canvas.height = img.height;
    canvas.getContext("2d").putImageData(img, 0, 0);
    for (const p of panes()) p.gHi.drawImage(canvas, r.minX, r.minY);
  }
  function applyView() { const t = `translate(${S.tx}px,${S.ty}px) scale(${S.zoom})`; for (const p of P) p.cv.style.transform = t; status(); }
  function fit() {
    const width = Math.min(...panes().map(p => p.stage.clientWidth));
    const height = Math.min(...panes().map(p => p.stage.clientHeight));
    if (!S.W || !S.H || !width || !height) return;
    S.zoom = Math.min(width / S.W, height / S.H) * 0.98;
    S.tx = (width - S.W * S.zoom) / 2; S.ty = (height - S.H * S.zoom) / 2; applyView();
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
    s += ` · ${Math.round(S.zoom * 100)}%`;
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
  async function goZ(z, keepHover, force = false) {
    if (S.mergeBusy && !force) return;
    const nz = S.info.shape_zyx[0]; z = Math.max(0, Math.min(nz - 1, z | 0));
    if (z !== S.z) { if (S.ngMode) setTool("pick"); mergeArm(null); clearSAM(); clearRepair(); clearNeighbourList("an-neighbour-list"); clearTimeout(S.hbTimer); S.hbTimer = setTimeout(heartbeat, 1500); }
    const block = S.block;
    S.z = z; $("an-z").value = z; $("an-zr").value = z;
    $("an-compare").href = `/annotate/compare?block=${encodeURIComponent(S.block)}&z=${z}`;
    if (!keepHover) S.hoverXY = null;
    const history = editList();
    let entry;
    try { entry = await fetchZ(z); } catch (e) { $("an-status").textContent = "加载失败: " + e.message; return; }
    if (S.z !== z || S.block !== block || S.cache.get(z) !== entry) return;
    render(); status(); segList(); prefetch(); await history;
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
    if (S.mergeBusy) return;
    clear3D();
    if (t.startsWith("sam") && S.tool === t) t = "pick";
    if (!t.startsWith("sam")) clearSAM();
    clearRepair();                       // 修补是整片操作，切到任何画笔工具都说明注意力已经离开它
    S.tool = t; mergeArm(null);
    if ((t === "merge" || t === "ng" || t.startsWith("sam")) && S.playing) { clearInterval(S.playing); S.playing = null; $("an-play").textContent = "▶ 连播"; }
    document.querySelectorAll(".tool").forEach(b => { const active = b.dataset.tool === t; b.classList.toggle("active", active); b.setAttribute("aria-pressed", String(active)); });
    for (const p of P) { p.stage.classList.toggle("pan", t === "pan"); p.stage.dataset.tool = t; }   // 光标跟着工具走（见 style.css）
    renderHi();
  }
  function setCur(id) { S.cur = String(id); $("an-cur-id").textContent = S.cur === "0" ? "未选择" : S.cur; $("an-cur-sw").style.background = S.cur === "0" ? "transparent" : css(colorOf(S.cur)); status(); segList(); samButtons(); }
  function pick(x, y) { const id = idAt(x, y); if (id == null || id === "0") return; setCur(id); if (S.tool === "merge") mergeArm({ id, xy: [x, y], z: S.z }); }

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
    if (S.cur === "0" && S.tool !== "clear") { flash("请先选择或新建标签"); return; }
    if (!ensureWho()) return;
    const old = idAt(x, y); if (old === S.cur) return;
    const k = ensureIdx(e, S.cur), z = S.z;
    if (whole) { const t = e.idx[y * S.W + x]; for (let i = 0; i < e.idx.length; i++) if (e.idx[i] === t) e.idx[i] = k; }
    else floodLocal(e, x, y, k);
    e.segImgs.clear(); clearRegions(); render();               // optimistic: show it now, reconcile after the server answers
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/fill`, editBody(z, { z, x, y, new_id: S.cur, whole_slice: !!whole }));
      afterEdit(r, z);
    } catch (err) { if (await stale(err, z)) return; flash("填充失败: " + err.message, true); invalidate(z); goZ(z, true); }
  }

  // Each pair is independent: A then B -> B takes A's colour; C then D -> D takes C's.
  function mergeArm(first) {
    S.mergeFirst = first;
    const h = $("an-merge-hint"); h.hidden = S.tool !== "merge";
    h.textContent = S.mergeBusy ? "正在合并，请稍候…" : first
      ? "已选第一块，请点第二块 · Esc 取消"
      : "依次点两块，保留第一块颜色";
    renderHi();
  }
  function mergeBusy(on) {
    S.mergeBusy = on;
    document.querySelectorAll(".vast-tools button, .vast-tools input, .vast-tools select").forEach(el => el.disabled = on);
    samButtons();
    $("an-rp-apply").disabled = on || !S.repair?.n_px;
  }
  async function mergeInto(x, y) {
    if (S.mergeBusy || !ensureWho()) return;
    const clicked = idAt(x, y); if (clicked == null) return;
    if (clicked === "0") { flash("请选择色块，背景不参与合并"); return; }
    if (!S.mergeFirst) { mergeArm({ id: clicked, xy: [x, y], z: S.z }); setCur(clicked); return; }
    const first = S.mergeFirst, z = S.z;
    mergeBusy(true); mergeArm(null);                         // consume the pair before sending; repeated clicks cannot reuse it
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/merge-pair`, editBody(z, { z, first: first.xy, second: [x, y] }));
      await afterEdit(r, z);
      setCur(r.edit ? r.edit.new_id : first.id);
      flash(r.edit ? "已合并，可选择下一对" : "标签相同，可选择下一对");
    } catch (err) {
      if (await stale(err, z)) return;
      flash("合并失败：" + err.message, true);
      invalidate(z); await goZ(z, true, true);
    } finally { mergeBusy(false); mergeArm(null); }
  }

  function strokeStart(x, y) { if (!ensureWho()) return; if (S.tool === "brush" && S.cur === "0") { flash("请先选择或新建标签"); return; } S.stroke = { pts: [[x, y]], z: S.z, id: S.tool === "erase" ? "0" : S.cur }; strokeDot(x, y); }
  function strokeDot(x, y) {
    const id = S.stroke.id;
    let ink;
    if (id !== "0") {
      const e = S.cache.get(S.stroke.z); if (!e?.idx) return;
      // Preview the same pixel discs as the server, clipped to background even in outline view.
      // Canvas transparency alone cannot identify background: labelled interiors may be hidden.
      ink = new Path2D();
      for (let row = Math.max(0, y - S.brush); row <= Math.min(S.H - 1, y + S.brush); row++) {
        const dx = Math.floor(Math.sqrt(S.brush * S.brush - (row - y) ** 2));
        const end = Math.min(S.W - 1, x + dx);
        let start = -1;
        for (let col = Math.max(0, x - dx); col <= end + 1; col++) {
          const empty = col <= end && e.ids[e.idx[row * S.W + col]] === "0";
          if (empty && start < 0) start = col;
          if (!empty && start >= 0) { ink.rect(start, row, col - start, 1); start = -1; }
        }
      }
    }
    for (const p of segPanes()) {
      const g = p.gSeg; g.save(); g.globalCompositeOperation = id === "0" ? "destination-out" : "source-over";
      g.fillStyle = id === "0" ? "#000" : `rgba(${colorOf(id).join(",")},${S.view === "side" ? 1 : S.opacity})`;
      if (ink) g.fill(ink);
      else { g.beginPath(); g.arc(x + 0.5, y + 0.5, S.brush + 0.5, 0, Math.PI * 2); g.fill(); }
      g.restore();
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
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/paint`, editBody(st.z, { z: st.z, points: st.pts, radius: S.brush, new_id: st.id }));
      afterEdit(r, st.z);
    } catch (err) { if (await stale(err, st.z)) return; flash("涂抹失败: " + err.message, true); invalidate(st.z); goZ(st.z, true); }
  }
  async function afterEdit(r, z) {
    clearSAM();
    S.info.n_edits = r.n_edits;
    if (r.rev != null) S.revs.set(z, r.rev);
    invalidate(z); if (z === S.z) await goZ(z, true, true);
  }
  async function undo(force = false, n = null) {
    if (S.mergeBusy || !ensureWho()) return;
    const z = S.z;
    clearSAM();
    mergeBusy(true);
    mergeArm(null);
    $("an-merge-hint").textContent = "正在撤销，请稍候…";
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/undo?z=${z}`, editBody(z, { force: !!force, n: n ?? undefined }));
      if (!r.undone) { flash("本片没有可撤销的改动"); await editList(); return; }
      S.info.n_edits = r.n_edits;
      if (r.rev != null) S.revs.set(z, r.rev);
      invalidate(z);
      await goZ(z, true, true);
      if (force && r.undone.by && !isMine(r.undone)) flash(`已撤销 ${r.undone.by} 的改动（已记入操作流水）`);
    } catch (err) {
      if (await stale(err, z)) return;
      if (err.code === "not_yours") {
        if (err.info?.can_override === false) { flash(`本片最近一次改动是 ${err.info.latest?.by ?? "别人"} 做的；只有审核员或管理员能撤销别人的改动`, true); return; }
        undoAsk(err.info?.latest); return;
      }
      flash("撤销失败: " + err.message, true);
    }
    finally { mergeBusy(false); mergeArm(null); }
  }
  async function reserveLabel() {
    const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/new-id?z=${S.z}`);
    dropAll(); S.createdIds = r.created_ids; await goZ(S.z, true, true);
    return r.id;
  }
  async function newId() {
    if (S.mergeBusy || !S.info?.has_seg) return;
    mergeBusy(true);
    try { const id = await reserveLabel(); setCur(id); flash(`已新建标签 ${id}`); }
    catch (err) { flash("新建失败: " + err.message, true); }
    finally { mergeBusy(false); }
  }

  // ------------------------------------------------------------------ 修补损坏切片
  // The image inside a black cut is gone for good and is never fabricated; what gets filled back are the labels,
  // interpolated from the cells' shapes on the nearest good sections either side. Preview first, then apply.
  async function repairScan() {
    if (!S.block) return;
    const el = $("an-rp-info");
    el.textContent = "正在扫描整块…";
    try {
      const r = await getJSON(`${API}/blocks/${encodeURIComponent(S.block)}/repair/scan`);
      if (!r.n) { el.textContent = "整块没有检测到损坏切片。"; return; }
      const list = r.sections.slice(0, 12).map(s => `<a href="#" data-z="${s.z}">z${s.z}</a> ${(s.fraction * 100).toFixed(0)}%${s.whole ? "(整片)" : ""}`).join("、");
      el.innerHTML = `${r.n} 片有损坏：${list}${r.n > 12 ? " …" : ""}`;
      el.querySelectorAll("a[data-z]").forEach(a => a.addEventListener("click", ev => { ev.preventDefault(); goZ(+a.dataset.z); }));
    } catch (err) { el.textContent = "扫描失败：" + err.message; }
  }
  async function repairPreview() {
    if (!S.block || S.mergeBusy) return;
    const el = $("an-rp-info");
    el.textContent = "正在按上下切片插值…";
    $("an-rp-apply").disabled = true; S.repair = null;
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/repair/preview`, { z: S.z });
      S.repairMask = await loadImg(r.mask_png); S.repair = r;
      $("an-rp-apply").disabled = !r.n_px;
      el.textContent = `可补 ${r.n_px} 像素 · 不确定 ${r.uncertain_px} 像素 · 保留 ${r.unfilled_px} 像素。`
        + `参考切片 ${r.source_sections.join("、")}。` + (r.note || "");
    } catch (err) { S.repair = null; S.repairMask = null; el.textContent = "预览失败：" + err.message; }
    finally { renderHi(); }
  }
  async function repairApply() {
    if (!S.repair || S.mergeBusy || !ensureWho()) return;
    const z = S.z, token = S.repair.token;
    mergeBusy(true);
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/repair/apply`, editBody(z, { token }));
      await afterEdit(r, z);
      flash(r.edit ? `已补 ${r.edit.n_px} 像素的标签（插值，可 Ctrl+Z 撤销）` : "没有需要改动的像素");
    } catch (err) { flash("修补失败：" + err.message, true); refreshIfConflict(err, z); }
    finally { S.repair = null; S.repairMask = null; $("an-rp-apply").disabled = true; mergeBusy(false); renderHi(); }
  }
  $("an-rp-scan").addEventListener("click", repairScan);
  $("an-rp-prev").addEventListener("click", repairPreview);
  $("an-rp-apply").addEventListener("click", repairApply);

  // ------------------------------------------------------------------ 3D viewing, cancellable while links are loading
  function clear3D() {
    S.ngSequence++; S.ngRequest?.abort(); S.ngRequest = null;
    if (S.ngWindow && !S.ngWindow.closed) S.ngWindow.close();
    S.ngWindow = null; S.ngMode = null;
    clearInterval(S.ngTimer); S.ngTimer = null;
    for (const id of ["an-ng", "an-ng-block"]) {
      $(id).classList.remove("active"); $(id).setAttribute("aria-pressed", "false");
    }
    $("an-ng-info").textContent = "";
  }
  function toggle3D(mode, point = null) {
    if (S.mergeBusy || !S.block) return;
    if (S.ngMode === mode) { setTool("pick"); return; }
    setTool("ng"); S.ngMode = mode;
    const button = $(mode === "point" ? "an-ng" : "an-ng-block");
    button.classList.add("active"); button.setAttribute("aria-pressed", "true");
    $("an-ng-info").textContent = mode === "point" ? "点击图像选择位置" : "";
    if (mode === "block" || point) open3D(point);
  }
  async function open3D(point) {
    if (!S.ngMode || (S.ngMode === "point" && !point)) return;
    const mode = S.ngMode, sequence = ++S.ngSequence;
    S.ngRequest?.abort();
    const request = new AbortController(); S.ngRequest = request;
    // Open synchronously in the click/keyboard gesture, before awaiting the link.
    // Keep the handle to close exactly this viewer when its mode is cancelled.
    const viewer = S.ngWindow && !S.ngWindow.closed ? S.ngWindow : window.open("about:blank", "_blank");
    if (!viewer) { setTool("pick"); $("an-ng-info").textContent = "请允许弹出 3D 窗口后重试"; return; }
    viewer.opener = null; S.ngWindow = viewer;
    clearInterval(S.ngTimer);
    S.ngTimer = setInterval(() => { if (viewer.closed && S.ngWindow === viewer) setTool("pick"); }, 500);
    $("an-ng-info").textContent = "正在打开 3D…";
    try {
      const path = mode === "block" ? `/neuroglancer/block?z=${S.z}` : `/neuroglancer?z=${S.z}&x=${point[0]}&y=${point[1]}`;
      const response = await fetch(`${API}/blocks/${encodeURIComponent(S.block)}${path}`, {signal: request.signal});
      const r = await response.json();
      if (sequence !== S.ngSequence) return;
      if (!response.ok || !r.url) throw new Error(r.reason || r.detail || "无法打开该数据块");
      viewer.location = r.url;
      $("an-ng-info").textContent = "3D 已打开 · 再次点击关闭";
    } catch (err) {
      if (sequence !== S.ngSequence || err.name === "AbortError") return;
      setTool("pick"); $("an-ng-info").textContent = "打开失败：" + err.message;
    } finally { if (sequence === S.ngSequence) S.ngRequest = null; }
  }

  // The repair proposal is pinned to one section and one revision of the labels; leaving that context must take the
  // green overlay and the 应用 button with it, or a stale proposal stays on screen looking applicable.
  const RP_HINT = `插值补标签。绿：新增 · 斜纹：不确定 · 红：保留原样。`;
  function clearRepair() {
    if (!S.repair && !S.repairMask) return;
    S.repair = null; S.repairMask = null;
    const a = $("an-rp-apply"); if (a) a.disabled = true;
    const el = $("an-rp-info"); if (el) el.innerHTML = RP_HINT;
  }
  async function clearAt(x, y, whole) {                     // 清除: set to background, then re-colour with any tool
    const keep = S.cur;
    S.cur = "0";
    try { await fill(x, y, whole); } finally { setCur(keep); }
  }
  $("an-ng").addEventListener("click", () => toggle3D("point"));
  $("an-ng-block").addEventListener("click", () => toggle3D("block"));
  // SAM previews never change labels until the user applies them.
  function samButtons() {
    const disabled = S.mergeBusy || !S.samPreview?.n_px || !S.info?.has_seg;
    $("an-sam-apply").disabled = disabled || S.cur === "0";
    $("an-newid").disabled = S.mergeBusy || !S.info?.has_seg;
    $("an-sam-new").disabled = disabled;
    $("an-sam-neighbour").disabled = disabled || !S.samNeighbour?.found;
  }
  function discardSAMPreview() { S.samPreview = null; S.samMask = null; S.samNeighbour = null; neighbourInfo(""); clearNeighbourList("an-sam-neighbour-list"); samButtons(); }
  function neighbourInfo(text) { const el = $("an-sam-neighbour-info"); if (el) el.textContent = text; }
  function clearSAM() {
    S.samSequence++; S.samRequest?.abort(); S.samRequest = null;
    S.samPoints = []; S.samLabels = []; S.samBox = null; S.samStart = null;
    discardSAMPreview();
    $("an-sam-result").textContent = "";
    renderHi();
  }
  function drawSAM() {
    if (S.blink) return;
    for (const p of panes()) {
      const g = p.gHi;
      if (S.samMask) g.drawImage(S.samMask, 0, 0);
      g.save(); g.lineWidth = 2 / S.zoom;
      if (S.samBox) { const [x0,y0,x1,y1] = S.samBox; g.strokeStyle = "#00e6c8"; g.strokeRect(x0,y0,x1-x0,y1-y0); }
      S.samPoints.forEach(([x,y], i) => {
        g.beginPath(); g.arc(x+.5, y+.5, 4 / S.zoom, 0, Math.PI*2);
        g.fillStyle = S.samLabels[i] ? "#00e6c8" : "#ff6262"; g.fill();
        g.strokeStyle = "#111"; g.stroke();
      });
      g.restore();
    }
  }
  async function predictSAM() {
    if (S.mergeBusy || !S.block) return;
    if (!S.samPoints.length && !S.samBox) { flash("请先点选或框选目标"); return; }
    S.samRequest?.abort();
    const request = new AbortController(), sequence = ++S.samSequence; S.samRequest = request;
    discardSAMPreview();
    $("an-sam-result").textContent = "正在生成预览…";
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/sam/predict`, {
        z: S.z, points: S.samPoints, labels: S.samLabels, box: S.samBox,
        only_background: $("an-sam-background").checked,
        snap_boundary: $("an-sam-snap").checked,
        boundary_sensitivity: +$("an-sam-sens").value / 100,
      }, request.signal);
      if (sequence !== S.samSequence) return;
      const mask = await loadImg(r.mask_png);
      if (sequence !== S.samSequence) return;
      S.samMask = mask; S.samPreview = r;
      S.samNeighbour = null; samNeighbourLookup(r.token);      // 顺便问一句邻片这块地方是谁
      $("an-sam-result").textContent = `预览 ${r.n_px} 像素` + (r.n_px ? " · 确认后应用" : " · 请调整提示或填充范围");
      $("an-sam-status").textContent = "模型就绪";
    } catch (err) { if (sequence === S.samSequence && err.name !== "AbortError") $("an-sam-result").textContent = "分割失败：" + err.message; }
    finally { if (sequence === S.samSequence) { S.samRequest = null; samButtons(); renderHi(); } }
  }
  // ---------------------------------------------------------------- 跨片取色
  // 分割漏标时，本片那块是空的，没有颜色可吸；颜色在隔壁片上。这里只把 id 取回来，画在哪、写哪一片仍然由人决定。
  // 自动挑的是最近的一片，未必是想要的那个细胞——半径内每一片的答案都摆出来，点一行就换。
  function neighbourList(elId, res, onPick) {
    const el = $(elId);
    if (!el) return;
    const list = res?.candidates || [];
    const draw = chosen => {
      el.hidden = list.length < 2;                       // 只有一个候选，没什么可选的
      // 一种颜色一行（服务端已按 id 去重，只留最近那片）；出现的片数越多越可信，所以一并显示。
      el.innerHTML = el.hidden ? "" : list.map((c, i) =>
        `<div class="row${c.z_src === chosen?.z_src ? " cur" : ""}" data-i="${i}" `
        + `title="改用 z${c.z_src} 的这个颜色（它出现在 z${(c.slices || [c.z_src]).join("、z")}）">`
        + `<span class="sw" style="background:${css(colorOf(c.id))}"></span>`
        + `<span class="id">z${c.z_src} · ${c.id}</span>`
        + `<span class="n">${Math.round(c.share * 100)}% · ${c.n_slices || 1}片</span></div>`).join("");
    };
    el.onclick = ev => {
      const row = ev.target.closest(".row[data-i]");
      if (!row) return;
      const c = list[+row.dataset.i];
      draw(c); onPick(c);
    };
    draw(res);
  }
  function clearNeighbourList(elId) { const el = $(elId); if (el) { el.hidden = true; el.innerHTML = ""; } }

  async function samNeighbourLookup(token) {
    neighbourInfo("正在看邻片这块区域是谁…");
    try {
      const n = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/neighbour-label`, { z: S.z, token });
      if (S.samPreview?.token !== token) return;          // 期间又预览了一次，这份结果已经过期
      S.samNeighbour = n;
      neighbourInfo(n.found
        ? `邻片 z${n.z_src} 在这块区域是 ${n.id}（占 ${Math.round(n.share * 100)}%${n.others?.length ? `，另有 ${n.others.length} 个 id` : ""}）`
        : `前后 ${n.radius} 片在这块区域都没有标签`);
      neighbourList("an-sam-neighbour-list", n, c => {
        S.samNeighbour = { ...n, ...c, picked: "manual" };
        neighbourInfo(`改用 z${c.z_src} 的 ${c.id}（占 ${Math.round(c.share * 100)}%）`);
        samButtons();
      });
    } catch (err) { S.samNeighbour = null; neighbourInfo("邻片取色失败：" + err.message); }
    finally { samButtons(); }
  }
  async function neighbourPick() {
    const p = S.hoverXY;
    if (!S.block || !p) { flash("把鼠标放到要取色的位置上再按 L", true); return; }
    try {
      const n = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/neighbour-label`, { z: S.z, x: p[0], y: p[1] });
      if (!n.found) { clearNeighbourList("an-neighbour-list"); flash(`前后 ${n.radius} 片在这一点都没有标签`, true); return; }
      setCur(n.id);
      neighbourList("an-neighbour-list", n, c => { setCur(c.id); flash(`已改用 z${c.z_src} 的颜色 ${c.id}`); });
      flash(`已取 z${n.z_src} 的颜色 ${n.id}（相隔 ${n.distance} 片）`
            + (n.candidates.length > 1 ? `，上下 10 片内共 ${n.candidates.length} 种颜色可选，见左栏` : ""));
    } catch (err) { flash("邻片取色失败：" + err.message, true); }
  }

  async function applySAM(mode) {
    if (S.mergeBusy || !S.samPreview?.n_px) return;
    if (mode === "cur" && S.cur === "0") { flash("请先选择标签"); return; }
    if (mode === "neighbour" && !S.samNeighbour?.found) { flash("邻片在这块区域也没有标签", true); return; }
    if (!ensureWho()) return;
    const z = S.z, token = S.samPreview.token, from = S.samNeighbour;
    mergeBusy(true);
    try {
      const id = mode === "new" ? await reserveLabel() : mode === "neighbour" ? from.id : S.cur;
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/sam/apply`, editBody(z, {token, new_id: id}));
      await afterEdit(r, z); setCur(id);
      $("an-sam-result").textContent = mode === "neighbour"
        ? `已用 z${from.z_src} 的颜色 ${id} 填了 ${r.edit?.n_px || 0} 像素 · Ctrl/⌘+Z 撤销`
        : `已填 ${r.edit?.n_px || 0} 像素 · Ctrl/⌘+Z 撤销`;
    } catch (err) { discardSAMPreview(); $("an-sam-result").textContent = "应用失败：" + err.message; refreshIfConflict(err, z); }
    finally { mergeBusy(false); renderHi(); }
  }
  $("an-sam-clear").addEventListener("click", clearSAM);
  $("an-sam-new").addEventListener("click", () => applySAM("new"));
  $("an-sam-apply").addEventListener("click", () => applySAM("cur"));
  $("an-sam-neighbour").addEventListener("click", () => applySAM("neighbour"));
  $("an-neighbour-pick").addEventListener("click", neighbourPick);
  $("an-sam-sens").addEventListener("input", ev => { $("an-sam-sens-v").textContent = ev.target.value + "%"; });
  for (const id of ["an-sam-background", "an-sam-snap", "an-sam-sens"]) $(id).addEventListener("change", () => {
    discardSAMPreview(); renderHi(); if (S.samPoints.length || S.samBox) predictSAM();
  });

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
      if (S.mergeBusy) return;
      if (S.ngMode) { if (S.ngMode === "point") open3D([x, y]); return; }
      if (ev.altKey || S.tool === "pick") { pick(x, y); return; }
      if (S.tool === "sam") {
        if (!ev.shiftKey && !ev.ctrlKey && !ev.metaKey) clearSAM();
        if (ev.shiftKey && !S.samLabels.includes(1) && !S.samBox) { flash("请先点击或框选目标，再 Shift+点击排除"); return; }
        if (S.samPoints.length >= 64) { flash("最多 64 个提示点，请清除后重试"); return; }
        discardSAMPreview(); S.samPoints.push([x,y]); S.samLabels.push(ev.shiftKey ? 0 : 1);
        renderHi(); predictSAM(); return;
      }
      if (S.tool === "sam-box") {
        if (!ev.ctrlKey && !ev.metaKey) clearSAM();
        discardSAMPreview(); S.samStart = [x,y]; S.samBox = [x,y,x,y]; return;
      }
      if (S.tool === "fill") { fill(x, y, ev.shiftKey); return; }
      if (S.tool === "merge") { mergeInto(x, y); return; }
      if (S.tool === "clear") { clearAt(x, y, ev.shiftKey); return; }
      if (S.tool === "brush" || S.tool === "erase") strokeStart(x, y);
    });
    let pending = false;
    p.stage.addEventListener("mousemove", ev => {
      if (S.drag) { S.tx = S.drag.tx + ev.clientX - S.drag.x; S.ty = S.drag.ty + ev.clientY - S.drag.y; applyView(); return; }
      const previous = S.hoverXY, [x, y] = toImg(ev, p); S.hoverXY = inside(x, y) ? [x, y] : null; S.hoverPane = i;
      if (S.samStart) {
        const [sx,sy] = S.samStart, bx = Math.max(0,Math.min(S.W-1,x)), by = Math.max(0,Math.min(S.H-1,y));
        S.samBox = [Math.min(sx,bx),Math.min(sy,by),Math.max(sx,bx)+1,Math.max(sy,by)+1];
        renderHi(); return;
      }
      if (S.curtainDrag) { S.curtainX = Math.max(0, Math.min(S.W, x)); render(); return; }
      p.stage.style.cursor = nearCurtain(x) ? "col-resize" : "";
      if (S.stroke) { if (S.hoverXY) strokeMove(x, y); return; }
      const changed = !previous || !S.hoverXY || previous[0] !== x || previous[1] !== y; status();
      if (!pending && (changed || S.view === "side" || S.tool === "brush" || S.tool === "erase")) { pending = true; requestAnimationFrame(() => { pending = false; renderHi(); }); }
    });
    p.stage.addEventListener("mouseleave", () => { S.hoverXY = null; renderHi(); status(); });
  }
  P.forEach(bindStage);
  window.addEventListener("mouseup", () => {
    if (!S.samStart) return;
    S.samStart = null;
    if (S.samBox[2] <= S.samBox[0] || S.samBox[3] <= S.samBox[1]) { S.samBox = null; renderHi(); return; }
    predictSAM();
  });
  window.addEventListener("mouseup", () => { S.curtainDrag = false; if (S.drag) { S.drag = null; for (const q of P) q.stage.classList.remove("panning"); } if (S.stroke) strokeEnd(); });

  // ------------------------------------------------------------------ keyboard
  document.addEventListener("keydown", ev => {
    if (ev.target instanceof Element && ev.target.matches("input,select,textarea")) return;
    if (S.mergeBusy) return;
    const k = ev.key;
    if ((ev.ctrlKey || ev.metaKey) && k.toLowerCase() === "z") { ev.preventDefault(); undo(); return; }
    if (k === "ArrowUp" || k === "w") { ev.preventDefault(); goZ(S.z - 1, true); }
    else if (k === "ArrowDown" || k === "s") { ev.preventDefault(); goZ(S.z + 1, true); }
    else if (k === "PageUp") { ev.preventDefault(); goZ(S.z - 10, true); }
    else if (k === "PageDown") { ev.preventDefault(); goZ(S.z + 10, true); }
    else if (k === "Home") goZ(0); else if (k === "End") goZ(S.info.shape_zyx[0] - 1);
    else if (k === "Escape") { if (S.ngMode || S.tool.startsWith("sam")) setTool("pick"); mergeArm(null); clearSAM(); clearRepair(); renderHi(); }
    else if (k === "m") setTool("merge");
    else if (k.toLowerCase() === "u" && !ev.repeat) toggle3D("point", S.hoverXY);
    else if (k === "p") setTool("pick"); else if (k === "f") setTool("fill"); else if (k === "b") setTool("brush"); else if (k === "e") setTool("erase"); else if (k === "h") setTool("pan");
    else if (k === "[") setBrush(S.brush - 1); else if (k === "]") setBrush(S.brush + 1);
    else if (k === "o") { $("an-outline").checked = S.outline = !S.outline; render(); }
    else if (k === "v") setView(S.view === "side" ? "overlay" : "side");
    else if (k === "g") setFade(!S.fade);
    else if (k === ",") setOpacity(S.opacity - 0.05); else if (k === ".") setOpacity(S.opacity + 0.05);
    else if (k === "c") { $("an-curtain").checked = S.curtain = !S.curtain; if (S.curtain && !S.curtainX) S.curtainX = S.W >> 1; render(); }
    else if (k === "Tab") { ev.preventDefault(); if (!S.blink) { S.blink = true; render(); } }
    else if (k === "n") newId();
    else if (k === "l") neighbourPick();
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
    const ids = [...new Set([...e.ids.filter(id => id !== "0"), ...S.createdIds])];
    const counts = new Map(e.ids.map((id, k) => [id, e.counts[k] || 0])), created = new Set(S.createdIds);
    let rows = ids.map((id, k) => [id, counts.get(id) || 0, k]).filter(r => r[0] !== "0" && (!q || r[0].includes(q)));
    rows.sort((a, b) => (b[0] === S.cur) - (a[0] === S.cur) || b[1] - a[1]);
    $("an-nseg").textContent = `${ids.length} 个`;
    const group = (key, title, labels) => `<section class="vast-label-group" data-label-group="${key}" aria-labelledby="an-labels-${key}">`
      + `<div class="vast-list-heading" id="an-labels-${key}"><span>${title}</span><span class="muted mono">${labels.length}</span></div>`
      + labels.slice(0, 400).map(([id, n, k]) => `<div class="row ${id === S.cur ? "cur" : ""}${S.bulk?.has(id) ? " bulk-on" : ""}" data-id="${id}" data-k="${k}">${S.bulk ? `<input type="checkbox" class="bulk-box" ${S.bulk.has(id) ? "checked" : ""} tabindex="-1">` : ""}<span class="sw" style="background:${css(colorOf(id))}"></span><span class="id" title="${id}">${id}</span><span class="n">${n || "未使用"}</span></div>`).join("")
      + (!labels.length ? `<div class="vast-list-note">${q ? "无匹配标签" : "暂无"}</div>` : "")
      + (labels.length > 400 ? `<div class="vast-list-note">还有 ${labels.length - 400} 个，请搜索</div>` : "") + `</section>`;
    box.innerHTML = group("created", "新建标签", rows.filter(([id]) => created.has(id)))
      + group("existing", "已有标签", rows.filter(([id]) => !created.has(id)));
  }
  $("an-segs").addEventListener("click", ev => {
    const r = ev.target.closest(".row[data-id]"); if (!r) return;
    if (S.bulk) { const id = r.dataset.id; S.bulk.has(id) ? S.bulk.delete(id) : S.bulk.add(id); segList(); bulkBar(); return; }
    setCur(r.dataset.id);
  });
  // ---------------------------------------------------------------- 批量删除（只动本片）
  // 高频误触点，所以：先进入选择模式勾标签，再点「删除所选」，再在对话框里确认——三步，最后一步才写数据；
  // 写成一笔，Ctrl/⌘+Z 一次撤回。
  function bulkBar() {
    const bar = $("an-bulk-bar"); if (!bar) return;
    bar.hidden = !S.bulk;
    if (S.bulk) { const n = S.bulk.size; $("an-bulk-n").textContent = n ? `已选 ${n} 个` : "点标签行勾选"; $("an-bulk-go").disabled = !n; }
    $("an-bulk").setAttribute("aria-pressed", S.bulk ? "true" : "false");
  }
  function bulkToggle() { S.bulk = S.bulk ? null : new Set(); segList(); bulkBar(); }
  function bulkPixels() {
    const e = S.cache.get(S.z); if (!e || !S.bulk) return 0;
    let n = 0; e.ids.forEach((id, k) => { if (S.bulk.has(id)) n += e.counts[k] || 0; }); return n;
  }
  function bulkAsk() {
    if (!S.bulk?.size || !ensureWho()) return;
    const ids = [...S.bulk], px = bulkPixels();
    $("an-bulk-msg").innerHTML = `将把 <b>z ${S.z}</b> 这一片上选中的 <b>${ids.length}</b> 个标签、共 <b>${px.toLocaleString("zh-CN")}</b> 个像素清为背景。`
      + `<br>只影响本片，其他切片不动；完成后可 Ctrl/⌘+Z 一次撤销。`
      + `<div class="an-bulk-ids mono">${ids.slice(0, 12).join("、")}${ids.length > 12 ? ` …等 ${ids.length} 个` : ""}</div>`;
    $("an-bulk-confirm").showModal();
  }
  async function bulkRun() {
    const dlg = $("an-bulk-confirm"); dlg.close();
    if (!S.bulk?.size || S.mergeBusy) return;
    const z = S.z, ids = [...S.bulk];
    mergeBusy(true);
    try {
      const r = await postJSON(`${API}/blocks/${encodeURIComponent(S.block)}/clear-labels`, editBody(z, { z, ids }));
      await afterEdit(r, z);
      flash(r.edit ? `已删除 ${ids.length} 个标签、${r.edit.n_px.toLocaleString("zh-CN")} 像素（本片），Ctrl/⌘+Z 可撤销` : "所选标签在本片上没有像素");
      S.bulk = null; segList(); bulkBar();
    } catch (err) { if (await stale(err, z)) return; flash("批量删除失败：" + err.message, true); }
    finally { mergeBusy(false); renderHi(); }
  }
  $("an-bulk").addEventListener("click", bulkToggle);
  $("an-bulk-cancel").addEventListener("click", () => { S.bulk = null; segList(); bulkBar(); });
  $("an-bulk-go").addEventListener("click", bulkAsk);
  $("an-bulk-no").addEventListener("click", () => $("an-bulk-confirm").close());
  $("an-bulk-yes").addEventListener("click", bulkRun);
  $("an-search").addEventListener("input", segList);

  async function editList() {
    const block = S.block, z = S.z, sequence = ++S.historySequence;
    $("an-nedit").textContent = "…";
    $("an-edits").textContent = "加载中…";
    try {
      const r = await getJSON(`${API}/blocks/${encodeURIComponent(block)}/edits?z=${z}&limit=30`);
      if (block !== S.block || z !== S.z || sequence !== S.historySequence) return;
      $("an-nedit").textContent = `${r.n} 次改动`;
      const label = editLabel;
      // A repair writes a different id per pixel, so its new_id is the text "N 个 id" — there is no one colour for it.
      const swatch = e => /^\d+$/.test(String(e.new_id)) && e.new_id !== "0" ? css(colorOf(e.new_id)) : "transparent";
      $("an-edits").innerHTML = r.edits.map(e => `<div class="row"><span class="sw" style="background:${swatch(e)}"></span><span class="id">#${e.n} ${label(e)} ${e.z == null ? `${e.n_slices} 片` : "z" + e.z} → ${esc(e.new_id)}${e.by ? ` <span class="by">· ${esc(e.by)}</span>` : ""}</span><span class="n">${e.n_px}px</span></div>`).join("") || `<div class="row"><span class="n">本片还没有改动</span></div>`;
      $("an-editors").textContent = r.editors?.length ? "改动人：" + r.editors.map(x => `${x.by || "未署名"} ×${x.n}`).join(" · ") : "";
    } catch (_) {
      if (block !== S.block || z !== S.z || sequence !== S.historySequence) return;
      $("an-nedit").textContent = "—"; $("an-edits").textContent = "历史加载失败，请重试"; $("an-editors").textContent = "";
    }
  }

  // ------------------------------------------------------------------ controls
  function setBrush(v) { S.brush = Math.max(0, Math.min(60, v | 0)); $("an-brush").value = S.brush; $("an-brush-v").textContent = S.brush; renderHi(); }
  document.querySelectorAll(".tool").forEach(b => b.addEventListener("click", () => setTool(b.dataset.tool)));
  $("an-brush").addEventListener("input", ev => setBrush(+ev.target.value));
  $("an-newid").addEventListener("click", newId);
  $("an-prev").addEventListener("click", () => goZ(S.z - 1));
  $("an-next").addEventListener("click", () => goZ(S.z + 1));
  $("an-z").addEventListener("change", ev => goZ(+ev.target.value));
  $("an-zr").addEventListener("input", ev => goZ(+ev.target.value, true));
  $("an-undo").addEventListener("click", () => undo());
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
  // Panels also resize when toolbar text wraps or the view mode changes, without a window resize.
  const stageResize = new ResizeObserver(() => { if (S.info) fit(); });
  P.forEach(p => stageResize.observe(p.stage));

  // ------------------------------------------------------------------ blocks
  async function selectBlock(id) {
    if (S.mergeBusy) return;
    if (S.ngMode) setTool("pick");
    clearSAM(); clearRepair();
    if (S.playing) { clearInterval(S.playing); S.playing = null; $("an-play").textContent = "▶ 连播"; }
    S.block = id; S.createdIds = []; dropAll(); S.revs.clear(); S.hoverXY = null; mergeArm(null);
    const pr = $("an-presence"); pr.hidden = true; pr.innerHTML = ""; pr.classList.remove("same");
    clearTimeout(S.hbTimer); S.hbTimer = setTimeout(heartbeat, 1500);
    S.info = await getJSON(`${API}/blocks/${encodeURIComponent(id)}`);
    const [nz, H, W] = S.info.shape_zyx; S.W = W; S.H = H; S.curtainX = W >> 1;
    for (const p of P) { for (const c of [p.em, p.seg, p.hi]) { c.width = W; c.height = H; } p.cv.style.width = W + "px"; p.cv.style.height = H + "px"; }
    $("an-z").max = nz - 1; $("an-zr").max = nz - 1;
    // 框里是切片序号 z，从 0 起算，所以这里是"最大序号"而不是总数。只写 "/ 99" 会被读成"共 99 页"，
    // 实际有 100 片，所以把总数一并写出来。
    $("an-nz").textContent = `/ ${nz - 1}　共 ${nz} 片`;
    const g = S.info.voxel_size_nm ? ` · ${S.info.voxel_size_nm.join("×")} nm` : "";
    $("an-meta").textContent = `${W}×${H}×${nz}${g}${S.info.has_seg ? "" : " · 无分割"}`;
    history.replaceState(null, "", `/annotate?block=${encodeURIComponent(id)}`);
    setCur("0"); fit(); await goZ(Math.min(S.z, nz - 1));
  }
  async function init() {
    setView(S.view);
    getJSON(`${API}/sam/status`).then(r => {
      $("an-sam-status").textContent = r.installed && r.checkpoint_exists
        ? (r.loaded ? "模型就绪" : "首次使用时加载") : "模型未安装";
    }).catch(() => { $("an-sam-status").textContent = "模型状态暂不可用"; });
    const r = await getJSON(`${API}/blocks`);
    const sel = $("an-block");
    if (!r.blocks.length) { sel.innerHTML = `<option>没有数据块</option>`; $("an-status").textContent = r.root ? `在 ${r.root} 下没有找到 em.npy` : "未配置 EMQC_ANNOTATE_ROOT"; return; }
    sel.innerHTML = r.blocks.map(b => `<option value="${b.block_id}" ${b.error ? "disabled" : ""}>${b.block_id}${b.error ? " · 无法读取" : b.has_seg ? "" : " · 无分割"}${b.n_edits ? ` · ${b.n_edits} 改动` : ""}</option>`).join("");
    const pre = window.AN_PRESELECT && r.blocks.some(b => b.block_id === window.AN_PRESELECT) ? window.AN_PRESELECT : r.blocks.find(b => !b.error)?.block_id;
    sel.value = pre; sel.addEventListener("change", () => selectBlock(sel.value));
    await selectBlock(pre);
    heartbeat();
    if (!ME && !S.who) ensureWho();     // 没开登录的实例：一进来就问名字；"先看看"可以跳过，改标签时会再问
  }
  init().catch(err => { $("an-status").textContent = "初始化失败: " + err.message; });
})();
