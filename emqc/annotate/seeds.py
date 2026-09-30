"""CPU seed watershed proposals in the existing displayed (y, x) frame.

Image-filter baseline, not a trained ilastik classifier. Nothing is written to
labels until explicit application of a current, user-owned preview.
"""
from __future__ import annotations

import base64
import io
import threading
import time
import uuid
from collections import OrderedDict

import numpy as np
from PIL import Image
from scipy import ndimage
from skimage.segmentation import watershed

from emqc.annotate.store import Actor

MAX_AREA = 1024 * 1024
CONTEXT_MARGIN = 32
TTL = 900


def _unit(values):
    return np.clip(values / max(float(np.percentile(values, 99)), 1e-6), 0, 1)


def membrane_elevation(image, scale=1.2):
    """Dark Hessian ridges plus local darkness; organelles can also respond."""
    f = np.asarray(image, dtype=np.float32)
    smooth = ndimage.gaussian_filter(f, scale)
    mean = ndimage.gaussian_filter(f, max(5.0, scale * 6))
    dark = _unit(np.maximum(mean - smooth, 0))
    xx = ndimage.gaussian_filter(f, scale, order=(0, 2))
    yy = ndimage.gaussian_filter(f, scale, order=(2, 0))
    xy = ndimage.gaussian_filter(f, scale, order=(1, 1))
    ridge = _unit(np.maximum((xx + yy + np.sqrt((xx - yy) ** 2 + 4 * xy ** 2)) / 2, 0))
    return np.round((0.65 * dark + 0.35 * ridge) * 4095).astype(np.uint16)


def segment(image, existing, box, strokes, scale=1.2):
    """A bounded ROI watershed, with continuous strokes and protected labels."""
    h, w = image.shape
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise ValueError("框选范围超出切片")
    rh, rw = y1 - y0, x1 - x0
    if min(rh, rw) < 8 or rh * rw > MAX_AREA:
        raise ValueError("框选边长至少 8 像素，面积最多 1024×1024；请缩小范围")
    if not np.isfinite(scale) or not 0.6 <= scale <= 3:
        raise ValueError("边界尺度应在 0.6–3 像素之间")
    if not strokes or len(strokes) > 64 or sum(len(s['points']) for s in strokes) > 8192:
        raise ValueError("请添加目标种子，最多 64 笔、8192 个采样点")
    seeds = [np.zeros((rh, rw), bool), np.zeros((rh, rw), bool)]
    for stroke in strokes:
        label, points = stroke['label'], stroke['points']
        if label not in (0, 1) or not points or len(points) > 512:
            raise ValueError("种子笔迹无效")
        if any(not (x0 + 2 <= x < x1 - 2 and y0 + 2 <= y < y1 - 2) for x, y in points):
            raise ValueError("请把种子画在框内，并离框边至少 2 像素")
        for i, (x, y) in enumerate(points):
            px, py = points[max(0, i - 1)]
            n = max(abs(x - px), abs(y - py)) + 1
            xs = np.rint(np.linspace(px, x, n)).astype(int) - x0
            ys = np.rint(np.linspace(py, y, n)).astype(int) - y0
            seeds[label][ys, xs] = True
    bg, fg = [ndimage.binary_dilation(s, iterations=1) for s in seeds]
    if not fg.any():
        raise ValueError("请先在目标内部画绿色种子")
    if (fg & bg).any():
        raise ValueError("目标与排除种子重叠，请撤回上一笔或重新画种子")
    occupied = existing[y0:y1, x0:x1] != 0
    if (fg & occupied).any():
        raise ValueError("目标种子碰到已有标签，请撤回并画在未标注区域")
    # The ROI is an output constraint, not biological background. Solve with
    # image context outside it, so the artificial negative rim cannot carve a
    # gap into a cell touching the box (or the physical image boundary).
    cx0, cy0 = max(0, x0 - CONTEXT_MARGIN), max(0, y0 - CONTEXT_MARGIN)
    cx1, cy1 = min(w, x1 + CONTEXT_MARGIN), min(h, y1 + CONTEXT_MARGIN)
    crop = image[cy0:cy1, cx0:cx1]
    roi = (slice(y0 - cy0, y1 - cy0), slice(x0 - cx0, x1 - cx0))
    occupied_context = existing[cy0:cy1, cx0:cx1] != 0
    foreground = np.zeros(crop.shape, bool)
    foreground[roi] = fg
    excluded = np.zeros(crop.shape, bool)
    excluded[roi] = bg
    rim = np.zeros(crop.shape, bool)
    rim[[0, -1], :] = True
    rim[:, [0, -1]] = True
    rim[roi] = False  # Never force background on an output/image edge itself.
    markers = np.zeros(crop.shape, dtype=np.int32)
    markers[rim | excluded | occupied_context] = 2
    markers[foreground] = 1
    if not np.any(markers == 2):
        raise ValueError("范围覆盖整张图且没有背景参照，请在目标外添加红色排除种子")
    assigned = watershed(membrane_elevation(crop, scale), markers, connectivity=1)
    candidate = (assigned == 1) & ~occupied_context & ~excluded
    # Connectivity is evaluated in context before clipping: a cell can leave
    # and re-enter the selected box, without creating a new biological object.
    components, _ = ndimage.label(candidate)
    selected = np.unique(components[foreground])
    candidate &= np.isin(components, selected[selected != 0])
    mask = candidate[roi].copy()
    edge = bool(mask[0, :].any() or mask[-1, :].any() or mask[:, 0].any() or mask[:, -1].any())
    warnings = []
    if mask.sum() < 64:
        warnings.append("候选较小：请检查种子是否落在膜或细胞器上，可撤回后改点目标内部")
    if edge:
        clipped = ((y0 > 0 and mask[0, :].any()) or (y1 < h and mask[-1, :].any())
                   or (x0 > 0 and mask[:, 0].any()) or (x1 < w and mask[:, -1].any()))
        warnings.append("候选到达框边，仅保存框内部分；需要完整目标时请扩大范围，并核对是否越过细胞膜"
                        if clipped else "候选已贴到图像最外侧，边缘像素已保留；请核对细胞膜")
    if np.percentile(crop, 95) - np.percentile(crop, 5) < 10:
        warnings.append("该范围对比度较低，请结合原图核对边界")
    return mask, warnings


def _owner(by):
    actor = Actor.coerce(by)
    return (actor.id, actor.user, actor.name) if actor else None


class SeedService:
    def __init__(self):
        self.lock = threading.RLock()
        self.proposals = OrderedDict()

    def predict(self, block, z, box, strokes, scale=1.2, by=None):
        started = time.perf_counter()
        with block.lock:
            image = block.em_slice(z)
            existing = block.seg_slice(z) if block.has_seg else np.zeros(image.shape, np.uint64)
            mask, warnings = segment(image, existing, box, strokes, scale)
            proposal = dict(path=str(block.path.resolve()), work=str(block.work.resolve()), z=z,
                            box=list(box), strokes=strokes, scale=scale, mask=mask,
                            slice_rev=block.slice_rev(z), owner=_owner(by), created=time.monotonic())
        token = uuid.uuid4().hex
        with self.lock:
            self.proposals = OrderedDict((k, p) for k, p in self.proposals.items()
                                         if time.monotonic() - p['created'] < TTL)
            self.proposals[token] = proposal
            while len(self.proposals) > 32:
                self.proposals.popitem(last=False)
        overlay = np.zeros((*mask.shape, 4), np.uint8)
        overlay[mask] = [70, 230, 120, 145]
        buf = io.BytesIO()
        Image.fromarray(overlay).save(buf, format='PNG')
        return dict(token=token, z=z, box=list(box), n_px=int(mask.sum()), warnings=warnings,
                    slice_rev=proposal['slice_rev'], seconds=round(time.perf_counter() - started, 3),
                    mask_png='data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode())

    def apply(self, block, token, new_id, by=None):
        with block.lock, self.lock:
            p = self.proposals.get(token)
            if p is None or time.monotonic() - p['created'] >= TTL:
                raise ValueError("预览已过期，请重新生成")
            if p['path'] != str(block.path.resolve()) or p['work'] != str(block.work.resolve()):
                raise ValueError("预览不属于当前数据块或工作目录")
            if p['owner'] != _owner(by):
                raise ValueError("请使用自己的种子分割预览")
            if p['slice_rev'] != block.slice_rev(p['z']):
                raise ValueError("本片标注已变化，请重新生成预览")
            if new_id <= 0:
                raise ValueError("请选择非零标签；种子分割仅用于补标")
            if not block.has_seg:
                raise ValueError("当前数据块没有标签基线，无法应用")
            plane = block.seg_slice(p['z'])
            mask = np.zeros(plane.shape, dtype=bool)
            x0, y0, x1, y1 = p['box']
            # Enforce protection again under the write lock, regardless of UI.
            mask[y0:y1, x0:x1] = p['mask'] & (plane[y0:y1, x0:x1] == 0)
            metadata = dict(algorithm='membrane-seeded-watershed-v2-context', context_margin_px=CONTEXT_MARGIN, box=p['box'],
                            strokes=p['strokes'], scale=p['scale'], only_background=True)
            rec = block.apply_mask(p['z'], mask, new_id, metadata, kind='seed', by=by)
            self.proposals.pop(token)
            return rec


service = SeedService()
