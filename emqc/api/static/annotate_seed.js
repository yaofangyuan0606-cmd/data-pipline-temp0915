/* Seed-assisted membrane segmentation. Preview state is separate from label edits. */
window.createAnnotationSeeds = function (h) {
  const $ = id => document.getElementById(id), S = h.state;
  const T = {box: null, strokes: [], drawing: null, start: null, preview: null, mask: null, sequence: 0, request: null};
  const message = (text, tone = 'hint') => {
    $('an-seed-result').textContent = text;
    $('an-seed-result').dataset.tone = tone;
  };
  const active = () => S.tool.startsWith('seed-');
  function buttons() {
    const disabled = S.mergeBusy || !T.preview?.n_px || !S.info?.has_seg;
    $('an-seed-new').disabled = disabled;
    $('an-seed-new').title = disabled ? '先标记目标并检查预览' : '为预览区域新建标签并填色';
    $('an-seed-apply').disabled = disabled || S.cur === '0';
    $('an-seed-apply').title = S.cur === '0' ? '先从标签列表选择颜色，或按住 Alt 在图上拾色' : `填入当前标签 ${S.cur}`;
    $('an-seed-full').disabled = S.mergeBusy || !S.info?.has_seg;
    document.querySelector('button[data-tool="seed-box"]').disabled = S.mergeBusy || !S.info?.has_seg;
    for (const tool of ['seed-fg', 'seed-bg']) document.querySelector(`button[data-tool="${tool}"]`).disabled = S.mergeBusy || !T.box;
    $('an-seed-back').disabled = S.mergeBusy || !T.strokes.length;
    $('an-seed-clear').disabled = S.mergeBusy || !T.box;
    $('an-seed-predict').disabled = S.mergeBusy || !!T.request || !T.box || !T.strokes.some(s => s.label === 1);
    $('an-seed-scale').disabled = S.mergeBusy;
    $('an-seed-result').setAttribute('aria-busy', String(!!T.request));
    $('an-seed-range').textContent = !T.box ? '未选择' :
      T.box[0] === 0 && T.box[1] === 0 && T.box[2] === S.W && T.box[3] === S.H ? '全片' :
      `${T.box[2] - T.box[0]} × ${T.box[3] - T.box[1]}`;
    const step = !T.box ? 1 : T.preview?.n_px ? 3 : 2;
    for (let i = 1; i <= 3; i++) {
      const el = $(`an-seed-step-${i}`);
      if (i === step) el.setAttribute('aria-current', 'step');
      else el.removeAttribute('aria-current');
    }
  }
  function discard() {
    T.sequence++; T.request?.abort(); T.request = null;
    T.preview = null; T.mask = null; buttons();
  }
  function clear() {
    T.box = null; T.strokes = []; T.drawing = null; T.start = null;
    discard(); message('框选一个目标，或选择全片范围。');
  }
  function changeTool(tool) {
    if (!tool.startsWith('seed-')) clear();
  }
  function inBox(x, y) {
    return T.box && x >= T.box[0] + 2 && y >= T.box[1] + 2 && x < T.box[2] - 2 && y < T.box[3] - 2;
  }
  function start(x, y, ev) {
    if (!active()) return false;
    if (S.tool === 'seed-box') {
      clear(); T.start = [x, y]; T.box = [x, y, x + 1, y + 1];
    } else {
      if (!inBox(x, y)) { message('请先框定范围；种子应离框边至少 2 像素。'); return true; }
      if (T.strokes.length >= 64 || T.strokes.reduce((n, s) => n + s.points.length, 0) >= 7680) {
        message('种子较多，请撤回几笔或重新框选目标。'); return true;
      }
      discard();
      T.drawing = {label: ev.shiftKey || S.tool === 'seed-bg' ? 0 : 1, points: [[x, y]]};
      T.strokes.push(T.drawing);
      message('绿色保留目标，红色排除邻居；松开鼠标生成预览。');
    }
    h.render(); return true;
  }
  function move(x, y) {
    if (T.start) {
      const [sx, sy] = T.start;
      x = Math.max(0, Math.min(S.W - 1, x)); y = Math.max(0, Math.min(S.H - 1, y));
      T.box = [Math.min(sx, x), Math.min(sy, y), Math.max(sx, x) + 1, Math.max(sy, y) + 1];
      h.render(); return true;
    }
    if (!T.drawing) return false;
    if (inBox(x, y)) {
      const pts = T.drawing.points, [px, py] = pts[pts.length - 1];
      if (pts.length < 512 && Math.hypot(x - px, y - py) >= 2) pts.push([x, y]);
    }
    h.render(); return true;
  }
  function finish() {
    if (T.start) {
      T.start = null;
      // Snap near-edge drags to the true pixel bounds; a user need not land
      // the pointer exactly on the outermost row or column of the image.
      const snap = Math.max(1, Math.ceil(8 / S.zoom));
      if (T.box[0] <= snap) T.box[0] = 0;
      if (T.box[1] <= snap) T.box[1] = 0;
      if (S.W - T.box[2] <= snap) T.box[2] = S.W;
      if (S.H - T.box[3] <= snap) T.box[3] = S.H;
      const [x0, y0, x1, y1] = T.box;
      if (Math.min(x1 - x0, y1 - y0) < 8 || (x1 - x0) * (y1 - y0) > 1048576) {
        T.box = null; message('范围太小或太大：边长至少 8 像素，面积最多 1024×1024。', 'error'); buttons(); h.render(); return;
      }
      h.setTool('seed-fg'); message('在目标内部点一下或画短线，松开后自动预览。');
    } else if (T.drawing) { T.drawing = null; predict(); }
    buttons();
  }
  function draw() {
    if (S.blink) return;
    for (const p of h.panes()) {
      const g = p.gHi;
      if (T.mask && T.preview) g.drawImage(T.mask, T.preview.box[0], T.preview.box[1]);
      g.save(); g.lineWidth = 1.5 / S.zoom;
      if (T.box) {
        const [x0, y0, x1, y1] = T.box;
        g.strokeStyle = '#ffe36a'; g.setLineDash([5 / S.zoom, 3 / S.zoom]); g.strokeRect(x0, y0, x1 - x0, y1 - y0); g.setLineDash([]);
      }
      for (const s of T.strokes) {
        const color = s.label ? '#39f582' : '#ff5468';
        g.beginPath(); s.points.forEach(([x, y], i) => i ? g.lineTo(x + .5, y + .5) : g.moveTo(x + .5, y + .5));
        g.lineCap = 'round'; g.lineJoin = 'round'; g.lineWidth = Math.max(3, 4 / S.zoom); g.strokeStyle = '#111'; g.stroke();
        g.lineWidth = Math.max(2, 2 / S.zoom); g.strokeStyle = color; g.stroke();
        const [x, y] = s.points[0];
        g.beginPath(); g.arc(x + .5, y + .5, Math.max(1.5, 3 / S.zoom), 0, 2 * Math.PI);
        g.fillStyle = color; g.fill();
      }
      g.restore();
    }
  }
  async function predict() {
    if (S.mergeBusy || !S.block || !T.box) return;
    discard();
    if (!T.strokes.some(s => s.label === 1)) { message('请先画绿色目标种子。'); h.render(); return; }
    if (!h.ensureWho()) return;
    const sequence = T.sequence, request = new AbortController(); T.request = request;
    message('正在生成预览…'); buttons(); h.render();
    try {
      const r = await h.post(`${h.api}/blocks/${encodeURIComponent(S.block)}/seed/predict`, h.editBody(S.z, {
        z: S.z, box: T.box, strokes: T.strokes, scale: +$('an-seed-scale').value,
      }), request.signal);
      if (sequence !== T.sequence) return;
      const mask = await h.loadImg(r.mask_png);
      if (sequence !== T.sequence) return;
      T.preview = r; T.mask = mask;
      message(`预览 ${r.n_px.toLocaleString('zh-CN')} 像素 · 尚未填色` + (r.warnings.length ? '\n' + r.warnings.join('\n') : '\n检查边界，满意后在下方填色。'), r.warnings.length ? 'warning' : 'ready');
    } catch (err) { if (sequence === T.sequence && err.name !== 'AbortError') message('分割失败：' + err.message, 'error'); }
    finally { if (sequence === T.sequence) { T.request = null; buttons(); h.render(); } }
  }
  async function apply(create) {
    if (S.mergeBusy || !T.preview?.n_px || (!create && S.cur === '0') || !h.ensureWho()) return;
    const z = S.z, block = S.block, token = T.preview.token;
    h.busy(true);
    try {
      const id = create ? await h.reserveLabel() : S.cur;
      const r = await h.post(`${h.api}/blocks/${encodeURIComponent(block)}/seed/apply`, h.editBody(z, {token, new_id: id}));
      await h.afterEdit(r, z); h.setCur(id);
      message(`已填色 ${r.edit?.n_px || 0} 像素，可撤销。框选下一个目标继续。`, 'ready');
    } catch (err) { discard(); h.refreshIfConflict(err, z); message('应用失败：' + err.message, 'error'); }
    finally { h.busy(false); h.render(); }
  }
  $('an-seed-full').addEventListener('click', () => {
    if (S.mergeBusy || !S.info) return;
    if (S.W * S.H > 1048576) { message('这张切片太大，请框选不超过 1024×1024 的区域。'); return; }
    clear(); T.box = [0, 0, S.W, S.H];
    if (S.tool !== 'seed-fg') h.setTool('seed-fg');
    message('已选完整切片，包括最外侧像素；请画绿色目标和红色排除种子。');
    buttons(); h.render();
  });
  $('an-seed-clear').addEventListener('click', () => {
    if (S.mergeBusy) return;
    clear();
    if (S.tool !== 'seed-box') h.setTool('seed-box');
    h.render();
  });
  $('an-seed-back').addEventListener('click', () => { if (!S.mergeBusy) { T.strokes.pop(); T.drawing = null; predict(); } });
  $('an-seed-predict').addEventListener('click', predict);
  $('an-seed-new').addEventListener('click', () => apply(true));
  $('an-seed-apply').addEventListener('click', () => apply(false));
  $('an-seed-scale').addEventListener('input', ev => {
    $('an-seed-scale-v').textContent = Number(ev.target.value).toFixed(1) + ' px';
    discard(); message('尺度已改变；松开滑条更新预览。'); h.render();
  });
  $('an-seed-scale').addEventListener('change', predict);
  window.addEventListener('blur', () => { if (T.drawing || T.start) { clear(); h.render(); } });
  return {clear, changeTool, buttons, draw, start, move, finish};
};
