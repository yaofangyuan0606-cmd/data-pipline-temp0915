/* 运行详情页的「数据分布图」：每张切片一个点，位置 = 统计量 + 检查分数做 PCA 后的前两维。
   离得近 = 数值上长得像；坏切片聚成的团 = 同一种问题。悬停看缩略图，点击打开那张切片。 */
(function () {
  const CAT = ["st-c1", "st-c2", "st-c3", "st-c4", "st-c5"];   // 和全站同一套校验过的类别色，按数量从多到少分配
  let tip;

  function drivers(ax) {
    return ax.drivers.map(d => `${esc(d.feature)}${d.sign > 0 ? "↑" : "↓"}`).join("、");
  }

  window.sliceMapInit = async function (runId) {
    const box = document.getElementById("slicemap"); if (!box) return;
    let d;
    try { d = await (await fetch(`/api/v1/qc/runs/${runId}/map`)).json(); } catch (e) { box.innerHTML = `<span class="muted">读取失败</span>`; return; }
    if (!d.points || d.points.length < 3) { box.innerHTML = `<span class="muted">切片太少，画不出分布图</span>`; return; }
    const pts = d.points;
    const cnt = {};
    pts.forEach(p => { if (!p.passed) { const t = p.type || "other"; cnt[t] = (cnt[t] || 0) + 1; } });
    let order = Object.keys(cnt).filter(t => t !== "other").sort((a, b) => cnt[b] - cnt[a]);
    // 前 5 类各一个颜色，其余（和没写类型的）合成「其他问题」一组：图例里不会出现两个一样的灰
    const top = new Set(order.slice(0, CAT.length));
    const rest = Object.keys(cnt).filter(t => !top.has(t));
    order = order.slice(0, CAT.length);
    if (rest.length) { cnt.other = rest.reduce((a, t) => a + cnt[t], 0); order.push("other"); }
    const color = {}; order.forEach((t, i) => color[t] = t === "other" ? "sm-other" : CAT[i]);
    const catOf = p => p.passed ? "clean" : (top.has(p.type) ? p.type : "other");
    const nClean = pts.filter(p => p.passed).length;
    const W = 760, H = 430, P = 18;
    // 坐标做一次 asinh 压缩：离群的坏切片（空白、过曝）离主团很远，线性坐标会把几百张正常切片挤成一个点
    const squash = vals => { const srt = vals.slice().sort((a, b) => a - b), med = srt[srt.length >> 1];
      const mad = vals.map(v => Math.abs(v - med)).sort((a, b) => a - b)[vals.length >> 1] || 1; return v => Math.asinh((v - med) / mad); };
    const tx = squash(pts.map(p => p.x)), ty = squash(pts.map(p => p.y));
    const xs = pts.map(p => tx(p.x)), ys = pts.map(p => ty(p.y));
    const [x0, x1] = [Math.min(...xs), Math.max(...xs)], [y0, y1] = [Math.min(...ys), Math.max(...ys)];
    const sx = v => P + (tx(v) - x0) / ((x1 - x0) || 1) * (W - 2 * P), sy = v => H - P - (ty(v) - y0) / ((y1 - y0) || 1) * (H - 2 * P);
    pts.forEach(p => { p.px = sx(p.x); p.py = sy(p.y); });
    const dots = pts.slice().sort((a, b) => (a.passed === b.passed ? 0 : a.passed ? -1 : 1)).map(p => {
      const c = catOf(p);
      return p.passed ? `<circle cx="${p.px.toFixed(1)}" cy="${p.py.toFixed(1)}" r="3" class="sm-clean" data-c="clean"/>`
        : `<circle cx="${p.px.toFixed(1)}" cy="${p.py.toFixed(1)}" r="5" class="sm-bad ${color[c]}" data-c="${esc(c)}"/>`;
    }).join("");
    const zh = t => t === "other" ? "其他问题" : (d.type_zh[t] || t);
    const legend = `<button class="sm-leg on" data-c="clean"><i class="sm-clean-k"></i>通过 <b>${nClean}</b></button>` +
      order.map(t => `<button class="sm-leg on" data-c="${esc(t)}"><i class="${color[t]}"></i>${esc(zh(t))} <b>${cnt[t]}</b></button>`).join("");
    const ax = d.axes;
    box.innerHTML = `<div class="sm-legend">${legend}<span class="muted sm-hint">点图例只看这一类 · 再点一次恢复</span></div>
      <div class="sm-plot"><svg viewBox="0 0 ${W} ${H}" class="sm-svg" role="img" aria-label="切片分布图">
        <rect x="0" y="0" width="${W}" height="${H}" class="sm-bg"/>${dots}<circle r="9" class="sm-ring" cx="-50" cy="-50"/></svg></div>
      <div class="sm-axes muted"><span><b>横轴 →</b> ${drivers(ax[0])}（区分了 ${Math.round(ax[0].explained * 100)}% 的差异）</span>
        <span><b>纵轴 ↑</b> ${drivers(ax[1])}（${Math.round(ax[1].explained * 100)}%）</span>
        <span>共 ${d.n} 张切片 · 离得近 = 统计特征和检查分数都相似 · 坐标做了压缩，离群点也画得下 · 点击打开那张切片</span></div>`;
    const svg = box.querySelector("svg"), ring = box.querySelector(".sm-ring");
    if (!tip) { tip = document.createElement("div"); tip.className = "sm-tip"; tip.hidden = true; document.body.appendChild(tip); }
    let focus = null, cur = null;
    const visible = p => !focus || catOf(p) === focus;
    function nearest(ev) {
      const r = svg.getBoundingClientRect(), k = W / r.width;
      const mx = (ev.clientX - r.left) * k, my = (ev.clientY - r.top) * k;
      let best = null, bd = 14 * 14 * k * k;
      for (const p of pts) { if (!visible(p)) continue; const dd = (p.px - mx) ** 2 + (p.py - my) ** 2; if (dd < bd || (dd === bd && !p.passed)) { bd = dd; best = p; } }
      return best;
    }
    svg.addEventListener("mousemove", ev => {
      const p = nearest(ev); cur = p;
      if (!p) { tip.hidden = true; ring.setAttribute("cx", -50); svg.style.cursor = ""; return; }
      ring.setAttribute("cx", p.px); ring.setAttribute("cy", p.py); svg.style.cursor = "pointer";
      tip.innerHTML = `<img src="${p.url}" alt=""><div><b>z ${p.z}</b> · <span class="mono">${esc(p.block_id)}</span><br>${p.passed ? '<span class="pill ok">通过</span>' : `<span class="pill bad">不通过</span> ${p.zh.map(esc).join("、")}`}<br>质量分 ${p.quality == null ? "–" : p.quality}${p.severity && p.severity !== "none" ? ` · ${esc(p.severity)}` : ""}</div>`;
      tip.hidden = false;
      const x = Math.min(ev.clientX + 16, innerWidth - tip.offsetWidth - 8), y = Math.min(ev.clientY + 16, innerHeight - tip.offsetHeight - 8);
      tip.style.left = x + "px"; tip.style.top = y + "px";
    });
    svg.addEventListener("mouseleave", () => { tip.hidden = true; ring.setAttribute("cx", -50); cur = null; });
    svg.addEventListener("click", () => { if (cur) location.href = cur.href; });
    box.querySelectorAll(".sm-leg").forEach(b => b.onclick = () => {
      focus = focus === b.dataset.c ? null : b.dataset.c;
      box.querySelectorAll(".sm-leg").forEach(x => x.classList.toggle("on", !focus || x.dataset.c === focus));
      svg.querySelectorAll("circle[data-c]").forEach(c => c.classList.toggle("dim", !!focus && c.dataset.c !== focus));
    });
  };
})();
