/* 问题切片浏览器：一张一张往下翻。
   页面里放一个 <script type="application/json" id="viewer-data">[...]</script>（items 见 dashboard._viewer_items），
   任何带 data-view-index / data-view-type 的元素点了就打开。← → 翻页，Esc 关闭，顶部按问题类型筛选。 */
(function () {
  let items = [], list = [], pos = 0, filter = "", el = null;

  function load() {
    const src = document.getElementById("viewer-data");
    if (!src) return false;
    try { items = JSON.parse(src.textContent || "[]"); } catch (e) { items = []; }
    return items.length > 0;
  }

  function build() {
    el = document.createElement("div");
    el.className = "vw";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.innerHTML = `<div class="vw-box">
        <div class="vw-top"><div class="vw-chips"></div><button class="vw-x" aria-label="关闭">✕</button></div>
        <div class="vw-stage"><button class="vw-nav vw-prev" aria-label="上一张">‹</button>
          <figure class="vw-fig"><img alt=""><figcaption></figcaption></figure>
          <button class="vw-nav vw-next" aria-label="下一张">›</button></div>
        <div class="vw-sim" hidden></div>
        <div class="vw-strip"></div>
        <div class="vw-hint muted">← → 翻页 · Esc 关闭</div></div>`;
    document.body.appendChild(el);
    el.addEventListener("click", e => { if (e.target === el) close(); });
    el.querySelector(".vw-x").onclick = close;
    el.querySelector(".vw-prev").onclick = () => go(pos - 1);
    el.querySelector(".vw-next").onclick = () => go(pos + 1);
  }

  function chips() {
    const counts = {};
    items.forEach(x => { counts[x.type] = counts[x.type] || { zh: x.zh, n: 0 }; counts[x.type].n++; });
    const all = `<button class="vw-chip ${filter ? "" : "on"}" data-t="">全部 <b>${items.length}</b></button>`;
    el.querySelector(".vw-chips").innerHTML = all + Object.entries(counts).sort((a, b) => b[1].n - a[1].n)
      .map(([t, c]) => `<button class="vw-chip ${filter === t ? "on" : ""}" data-t="${esc(t)}">${esc(c.zh)} <b>${c.n}</b></button>`).join("");
    el.querySelectorAll(".vw-chip").forEach(b => b.onclick = () => { setFilter(b.dataset.t); go(0); });
  }

  function setFilter(t) {
    filter = t || "";
    list = filter ? items.filter(x => x.type === filter) : items.slice();
    chips();
    const strip = el.querySelector(".vw-strip");
    strip.innerHTML = list.map((x, i) => `<button class="vw-th sv-${esc(x.severity)}" data-i="${i}" title="${esc(x.zh)} · z${x.z}"><img loading="lazy" src="${esc(x.url)}" alt=""></button>`).join("");
    strip.querySelectorAll(".vw-th").forEach(b => b.onclick = () => go(Number(b.dataset.i)));
  }

  function go(i) {
    if (!list.length) return;
    pos = (i + list.length) % list.length;
    const x = list[pos];
    const img = el.querySelector(".vw-fig img");
    img.src = x.url; img.alt = `${x.zh} z${x.z}`;
    img.className = "sv-" + x.severity;
    el.querySelector("figcaption").innerHTML =
      `<span class="vw-n mono">${pos + 1} / ${list.length}</span>
       <b>${esc(x.zh)}</b><span class="pill sv-pill-${esc(x.severity)}">${esc(x.severity)}</span>
       <span class="mono">${esc(x.dataset_id)} · z${x.z}${x.tile ? " · " + esc(x.tile) : ""}</span>
       <span class="muted mono">${esc(x.check)}${x.score == null ? "" : " · 分数 " + x.score}</span>
       <a href="${esc(x.href)}">打开这一段 →</a>
       <button class="vw-simbtn">🔍 找相似</button>`;
    el.querySelector(".vw-simbtn").onclick = () => similar(x);
    const sim = el.querySelector(".vw-sim"); sim.hidden = true; sim.innerHTML = "";
    el.querySelectorAll(".vw-th.on").forEach(b => b.classList.remove("on"));
    const th = el.querySelector(`.vw-th[data-i="${pos}"]`);
    if (th) { th.classList.add("on"); th.scrollIntoView({ block: "nearest", inline: "center" }); }
    [list[pos + 1], list[pos - 1]].forEach(n => { if (n) { const p = new Image(); p.src = n.url; } });  // preload neighbours
  }

  // 「找相似」：用 QC 算好的数值（统计量 + 各检查分数）在最近一次运行的所有切片里找最近邻
  async function similar(x) {
    const box = el.querySelector(".vw-sim");
    box.hidden = false;
    box.innerHTML = '<span class="muted">正在找相似的切片…</span>';
    let d;
    try {
      const r = await fetch(`/api/v1/qc/similar?run_id=${x.run_id}&block_id=${encodeURIComponent(x.block_id)}&z=${x.z}&k=12`);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      d = await r.json();
    } catch (e) { box.innerHTML = `<span class="muted">找不到：${esc(e.message)}</span>`; return; }
    if (list[pos] !== x) return;  // user moved on meanwhile
    const qz = (d.query && d.query.zh) || [];
    const head = d.n_same_problem == null ? "" :
      `最像的 ${d.hits.length} 张里，<b>${d.n_same_problem}</b> 张也有「${esc(qz.join("、"))}」中的问题`;
    box.innerHTML = `<div class="vw-sim-h"><b>和这张最像的切片</b><span class="muted">在 ${d.n_pool} 张切片里，按 QC 算出的数值比较（亮度、对比度、清晰度、黑块形状、各项检查分数），不用图像模型</span>${head ? `<span>${head}</span>` : ""}<button class="vw-sim-x" aria-label="收起">✕</button></div>
      <div class="vw-sim-row">${d.hits.map(h => `<a class="vw-sim-c ${h.passed ? "ok" : "bad"}" href="${esc(h.href)}" target="_blank" rel="noopener" title="距离 ${h.distance}（越小越像）">
        <img loading="lazy" src="${esc(h.url)}" alt=""><span class="t">${h.zh.length ? esc(h.zh.slice(0, 2).join("、")) : "正常"}</span>
        <span class="s mono">${esc(h.dataset_id)} · z${h.z}</span></a>`).join("") || '<span class="muted">没有可比较的切片</span>'}</div>`;
    box.querySelector(".vw-sim-x").onclick = () => { box.hidden = true; };
  }

  function key(e) {
    if (!el || el.hidden) return;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); go(pos + 1); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); go(pos - 1); }
    else if (e.key === "Escape") close();
  }

  function open(type, index) {
    if (!items.length && !load()) return;
    if (!el) build();
    el.hidden = false;
    document.body.classList.add("vw-open");
    setFilter(type || "");
    go(index || 0);
    el.querySelector(".vw-next").focus();
  }
  function close() { if (el) { el.hidden = true; document.body.classList.remove("vw-open"); } }

  document.addEventListener("keydown", key);
  document.addEventListener("click", e => {
    const t = e.target.closest("[data-view-type],[data-view-index]");
    if (!t || e.metaKey || e.ctrlKey || e.shiftKey) return;
    if (!items.length && !load()) return;
    e.preventDefault();
    const type = t.dataset.viewType || "";
    let idx = 0;
    if (t.dataset.viewUrl) {  // open on this exact picture
      const pool = type ? items.filter(x => x.type === type) : items;
      idx = Math.max(0, pool.findIndex(x => x.url === t.dataset.viewUrl));
    }
    open(type, idx);
  });
  window.openProblemViewer = open;
})();
