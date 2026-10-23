/* LangGraph 工作流的实时视图。
   图的拓扑来自 /api/v1/qc/workflow（后端从编译好的 LangGraph 读出来的节点与边），
   运行轨迹来自 /api/v1/qc/runs/{id}/workflow（每个节点进入 / 离开一条记录，按 id 增量拉）。
   拉到的新记录排进队列逐条播放：目标节点点亮、走过的边上有一个点跑过去、节点下面的次数和耗时跟着涨。
   积压多时自动加快，所以页面永远跟得上真实进度；跑完的运行可以「回放」。 */

let _wfTopo = null;
function wfTopology() { return _wfTopo || (_wfTopo = fetch("/api/v1/qc/workflow").then(r => r.json())); }

const WF = { colW: 122, termW: 64, rowH: 84, nodeW: 104, nodeH: 46, top: 74, left: 14 };
const WF_STAGE_NODES = new Set(["ingest", "slice_qc", "serial_qc", "aggregate", "persist"]);

class WorkflowView {
  constructor(el, runId, opts = {}) {
    this.el = el; this.runId = runId; this.live = !!opts.live; this.compact = !!opts.compact;
    this.lastId = 0; this.queue = []; this.history = []; this.stats = {}; this.cur = "__start__"; this.pos = null;
    this.timer = null; this.playing = false; this.replaying = false; this.stopped = false;
    this.el.classList.add("wf");
    this.el.innerHTML = '<span class="muted">加载工作流…</span>';
    this.init();
  }

  async init() {
    const topo = await wfTopology();
    if (!topo.available) { this.el.innerHTML = '<div class="muted">服务器没有安装 langgraph，运行使用的是 loop 引擎。</div>'; return; }
    this.topo = topo;
    this.render();
    const r = await this.fetch(0);
    if (!r) return;
    if (r.engine !== "langgraph") {
      this.caption(`这次运行用的是 <b>${esc(r.engine)}</b> 引擎，没有工作流轨迹（设置 <span class="mono">EMQC_WORKFLOW_ENGINE=langgraph</span> 后新运行会有）。`);
      this.el.classList.add("wf-off"); return;
    }
    // baseline: whatever already happened is shown as-is (no animation); only new steps are animated
    this.history = r.trace.slice();
    this.lastId = r.last_id;
    this.stats = JSON.parse(JSON.stringify(r.nodes || {}));
    const lastEnter = [...r.trace].reverse().find(d => d.phase === "enter");
    if (lastEnter) { this.cur = lastEnter.node; this.pos = lastEnter; }
    if (!this.live && ["finalize", "cancel"].includes(this.cur)) this.cur = "__end__";
    this.status = r.status; this.nSteps = r.n_steps;
    this.paintAll();
    this.caption();
    if (this.live && ["queued", "running"].includes(r.status)) this.poll();
  }

  async fetch(after) {
    try {
      const x = await fetch(`/api/v1/qc/runs/${this.runId}/workflow?after_id=${after}`);
      return x.ok ? await x.json() : null;
    } catch (e) { return null; }
  }

  poll() {
    if (this.stopped) return;
    clearTimeout(this.pollT);
    this.pollT = setTimeout(async () => {
      const r = await this.fetch(this.lastId);
      if (r) {
        if (r.trace.length) { this.history.push(...r.trace); this.lastId = r.last_id; if (!this.replaying) { this.queue.push(...r.trace); this.play(); } }
        this.status = r.status;
        if (!["queued", "running"].includes(r.status)) { this.live = false; if (!this.replaying) this.queue.push({ _final: true }); this.play(); return; }
      }
      this.poll();
    }, 700);
  }

  stop() { this.stopped = true; clearTimeout(this.pollT); clearTimeout(this.timer); }

  // ---------------------------------------------------------------- layout
  xOf(rank) {  // ranks that hold only START / END are narrow
    const term = new Set(this.topo.nodes.filter(n => n.terminal).map(n => n.rank));
    const nonTerm = new Set(this.topo.nodes.filter(n => !n.terminal).map(n => n.rank));
    let x = WF.left;
    for (let r = 0; r < rank; r++) x += (term.has(r) && !nonTerm.has(r)) ? WF.termW : WF.colW;
    return x;
  }
  box(id) {
    const n = this.topo.nodes.find(m => m.id === id);
    const term = n.terminal, w = term ? 30 : WF.nodeW, h = term ? 30 : WF.nodeH;
    const cx = this.xOf(n.rank) + (term ? 18 : WF.nodeW / 2), cy = WF.top + n.row * WF.rowH + WF.nodeH / 2;
    return { n, cx, cy, w, h, x: cx - w / 2, y: cy - h / 2 };
  }
  edgePath(e) {
    const a = this.box(e.source), b = this.box(e.target);
    if (e.back) {  // loop: arc over the top
      const span = Math.abs(a.n.rank - b.n.rank) + 1, lift = 16 + span * 7;
      const y0 = a.y, y1 = b.y, top = Math.min(y0, y1) - lift;
      return { d: `M${a.cx},${y0} C${a.cx},${top} ${b.cx},${top} ${b.cx},${y1}`, lx: (a.cx + b.cx) / 2, ly: top + lift * 0.22 };
    }
    const x0 = a.x + a.w, y0 = a.cy, x1 = b.x, y1 = b.cy;
    if (Math.abs(y0 - y1) < 1) return { d: `M${x0},${y0} L${x1},${y1}`, lx: (x0 + x1) / 2, ly: a.y - 8 };  // short hop: label sits above the gap
    // leave sideways from the source, enter the target from the left
    const sx = a.cx, sy = y1 > y0 ? a.y + a.h : a.y;
    const dx = Math.max(30, (x1 - sx) * 0.5);
    return { d: `M${sx},${sy} C${sx},${y1} ${x1 - dx},${y1} ${x1},${y1}`, lx: sx + 34, ly: y1 - 6 };
  }

  render() {
    const t = this.topo;
    const maxRank = Math.max(...t.nodes.map(n => n.rank));
    const W = this.xOf(maxRank) + 50, H = WF.top + t.n_rows * WF.rowH - 18;
    const edges = t.edges.map(e => {
      const p = this.edgePath(e);
      const lbl = e.label ? `<text class="wf-elabel" x="${p.lx}" y="${p.ly}" text-anchor="middle">${esc(e.label)}</text>` : "";
      return `<g class="wf-edge ${e.conditional ? "cond" : ""} ${e.back ? "back" : ""}" data-k="${esc(e.source)}>${esc(e.target)}"><path d="${p.d}" marker-end="url(#wf-arrow-${this.runId})"/>${lbl}</g>`;
    }).join("");
    const nodes = t.nodes.map(n => {
      const b = this.box(n.id);
      if (n.terminal) return `<g class="wf-node term" data-id="${n.id}"><circle cx="${b.cx}" cy="${b.cy}" r="13"/><text x="${b.cx}" y="${b.cy + 4}" text-anchor="middle">${n.id === "__start__" ? "▶" : "■"}</text><text class="wf-sub" x="${b.cx}" y="${b.cy + 30}" text-anchor="middle">${esc(n.label)}</text></g>`;
      return `<g class="wf-node" data-id="${n.id}" tabindex="0"><title>${esc(n.id)}</title><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="7"/>
        <text class="wf-title" x="${b.cx}" y="${b.y + 19}" text-anchor="middle">${esc(n.label)}</text>
        <text class="wf-sub" x="${b.cx}" y="${b.y + 35}" text-anchor="middle" data-sub></text></g>`;
    }).join("");
    this.el.innerHTML = `<div class="wf-bar"><span class="pill wf-engine">LangGraph</span><span class="wf-cap mono"></span>
        <span class="grow"></span><button class="wf-replay" hidden>▶ 回放</button></div>
      <div class="wf-canvas"><svg viewBox="0 0 ${W} ${H}" style="min-width:${Math.round(W * 0.82)}px" role="img" aria-label="QC 工作流状态图">
        <defs><marker id="wf-arrow-${this.runId}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="wf-arrowhead"/></marker></defs>
        <g class="wf-edges">${edges}</g><g class="wf-dots"></g><g class="wf-nodes">${nodes}</g></svg></div>
      ${this.compact ? "" : `<details class="wf-mermaid"><summary>图定义（LangGraph → mermaid）</summary><pre class="json">${esc(t.mermaid || "")}</pre></details>`}`;
    this.svg = this.el.querySelector("svg");
    this.el.querySelector(".wf-replay").onclick = () => this.replay();
    this.el.querySelectorAll(".wf-node:not(.term)").forEach(g => g.addEventListener("click", () => this.nodeClick(g.dataset.id)));
  }

  nodeClick(id) {
    const panel = document.getElementById("logpanel");
    if (!panel || !panel._log || !WF_STAGE_NODES.has(id)) return;
    const blk = this.pos && this.pos.node === id && id !== "ingest" ? this.pos.block : null;
    panel._log.setNode(id, blk);
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------------------------------------------------------------- painting
  node(id) { return this.svg && this.svg.querySelector(`.wf-node[data-id="${id}"]`); }
  edge(a, b) { return this.svg && this.svg.querySelector(`.wf-edge[data-k="${a}>${b}"]`); }
  fmtS(s) { return s == null ? "" : s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(1)} s`; }

  paintNode(id) {
    const g = this.node(id); if (!g) return;
    const st = this.stats[id];
    g.classList.toggle("visited", !!(st && st.visits));
    g.classList.toggle("err", !!(st && st.errors));
    g.classList.toggle("active", this.cur === id && (this.live || this.playing));
    const sub = g.querySelector("[data-sub]");
    if (sub) sub.textContent = st && st.visits ? `×${st.visits}${st.total_s ? " · " + this.fmtS(st.total_s) : ""}` : "—";
  }
  paintAll() {
    this.topo.nodes.forEach(n => this.paintNode(n.id));
    const used = new Set();
    let prev = "__start__";
    for (const d of this.history) if (d.phase === "enter") { used.add(`${prev}>${d.node}`); prev = d.node; }
    if (!this.live && ["finalize", "cancel"].includes(prev)) used.add(`${prev}>__end__`);
    this.svg.querySelectorAll(".wf-edge").forEach(e => e.classList.toggle("used", used.has(e.dataset.k)));
    const s = this.node("__start__"); if (s) s.classList.toggle("visited", this.history.length > 0);
    const end = this.node("__end__"); if (end) end.classList.toggle("visited", this.cur === "__end__");
    this.el.querySelector(".wf-replay").hidden = this.live || !this.history.length;
  }

  caption(html) {
    const cap = this.el.querySelector(".wf-cap"); if (!cap) return;
    if (html) { cap.innerHTML = html; return; }
    const p = this.pos || {};
    const parts = [];
    if (this.nSteps) parts.push(`第 ${this.nSteps} 步`);
    const inRange = p.group == null || p.n_groups == null || p.group < p.n_groups;  // the last next_group has run past the end
    if (p.z_start != null && inRange && this.cur !== "__end__") parts.push(`z ${p.z_start}–${p.z_end - 1}` + (p.n_groups ? `（${(p.group ?? 0) + 1}/${p.n_groups} 段）` : ""));
    if (p.n_tiles > 1 && p.tile != null && p.tile < p.n_tiles) parts.push(`tile ${p.tile + 1}/${p.n_tiles}`);
    const lbl = (this.topo.nodes.find(n => n.id === this.cur) || {}).label || this.cur;
    const state = this.cur === "__end__" ? (this.status === "cancelled" ? "已取消" : this.status === "error" ? "出错" : "已完成") : (this.live || this.playing ? `正在 <b>${esc(lbl)}</b>` : `停在 ${esc(lbl)}`);
    parts.push(state);
    if (this.live && p.block && WF_STAGE_NODES.has(this.cur) && this.cur !== "ingest") parts.push(`<span class="muted">${esc(p.block)}</span>`);
    cap.innerHTML = parts.join(" · ");
  }

  // a dot runs along the edge just taken
  flashEdge(a, b, ms) {
    const g = this.edge(a, b); if (!g) return;
    g.classList.add("used");
    g.classList.remove("wf-flash"); void g.getBoundingClientRect(); g.classList.add("wf-flash");
    clearTimeout(g._wfT); g._wfT = setTimeout(() => g.classList.remove("wf-flash"), Math.max(500, ms * 1.8));
    const path = g.querySelector("path"), len = path.getTotalLength();
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("r", "4.5"); dot.setAttribute("class", "wf-dot");
    this.svg.querySelector(".wf-dots").appendChild(dot);
    const t0 = performance.now();
    const step = now => {
      const k = Math.min(1, (now - t0) / ms), e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      const pt = path.getPointAtLength(e * len);
      dot.setAttribute("cx", pt.x); dot.setAttribute("cy", pt.y);
      if (k < 1) requestAnimationFrame(step); else dot.remove();
    };
    requestAnimationFrame(step);
  }

  // ---------------------------------------------------------------- playback
  apply(d, ms) {
    if (d._final) {
      if (["finalize", "cancel"].includes(this.cur)) { this.flashEdge(this.cur, "__end__", ms); this.cur = "__end__"; }
      this.playing = false; this.paintAll(); this.caption(); return;
    }
    const st = this.stats[d.node] || (this.stats[d.node] = { visits: 0, total_s: 0, last_s: null, phase: null, errors: 0 });
    st.phase = d.phase;
    if (d.phase === "enter") {
      const prev = this.cur;
      this.flashEdge(prev, d.node, ms);
      st.visits += 1; this.nSteps = d.step || (this.nSteps || 0) + 1;
      this.cur = d.node; this.pos = d;
      this.paintNode(prev);
    } else {
      if (d.duration_s != null) { st.total_s = (st.total_s || 0) + d.duration_s; st.last_s = d.duration_s; }
      if (d.phase === "error") st.errors = (st.errors || 0) + 1;
    }
    this.paintNode(d.node);
    this.caption();
  }

  play() {
    if (this.timer || !this.queue.length) return;
    this.playing = true;
    const tick = () => {
      if (!this.queue.length) { this.timer = null; if (!this.live) { this.playing = false; this.paintAll(); this.caption(); } return; }
      const enters = this.queue.filter(d => d.phase === "enter").length || 1;
      const budget = this.replaying ? this.replayBudget : 1400;               // ms to drain the backlog
      const gap = Math.max(this.replaying ? 25 : 45, Math.min(380, budget / enters));
      // exits are applied together with the following enter so each animation step is one transition
      let d = this.queue.shift();
      while (d && d.phase !== "enter" && !d._final) { this.apply(d, gap); d = this.queue.shift(); }
      if (d) this.apply(d, gap * 0.9);
      this.timer = setTimeout(tick, gap);
    };
    tick();
  }

  replay() {
    if (!this.history.length) return;
    clearTimeout(this.timer); this.timer = null;
    this.replaying = true; this.stats = {}; this.cur = "__start__"; this.pos = null; this.nSteps = 0;
    this.svg.querySelectorAll(".wf-edge").forEach(e => e.classList.remove("used"));
    this.topo.nodes.forEach(n => this.paintNode(n.id));
    const n = this.history.filter(d => d.phase === "enter").length;
    this.replayBudget = Math.min(24000, Math.max(4000, n * 160));
    this.queue = [...this.history, { _final: true }];
    const done = this.queue[this.queue.length - 1];
    const orig = this.apply.bind(this);
    this.apply = (d, ms) => { orig(d, ms); if (d === done) { this.replaying = false; this.apply = orig; } };
    this.play();
  }
}
