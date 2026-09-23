"""Local SAM 2.1 image inference. Loading is lazy; inference state is serialized."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import nullcontext

import numpy as np
from PIL import Image

from emqc.config import settings


class SAMUnavailable(RuntimeError):
    pass


def revision(block) -> tuple:
    """Detect edits, including undo and an edit replacing an earlier edit number."""
    return tuple((str(p.resolve()), p.stat().st_mtime_ns, p.stat().st_size)
                 for p in (block.path / "seg.npy", block.work / "seg_edit.npy", block.work / "edits.jsonl")
                 if p.exists())


class SAMService:
    def __init__(self):
        self.lock = threading.RLock()
        self.predictor = None
        self.image_key = None
        self.proposals = OrderedDict()
        self.error = None

    def status(self):
        installed = all(importlib.util.find_spec(name) is not None for name in ("torch", "sam2"))
        return {"model": "SAM 2.1", "config": settings.sam_config, "device": settings.sam_device,
                "installed": installed, "checkpoint_exists": settings.sam_checkpoint.is_file(),
                "loaded": self.predictor is not None, "error": self.error}

    def _load(self):
        if self.predictor is not None:
            return
        try:
            if not settings.sam_checkpoint.is_file():
                raise SAMUnavailable("SAM 权重未安装，请运行 scripts/install_sam.sh")
            import torch
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor

            if settings.sam_device.startswith("cuda") and not torch.cuda.is_available():
                raise SAMUnavailable("CUDA 不可用，请检查 GPU 权限和 PyTorch 安装")
            torch.set_num_threads(4)
            model = build_sam2(settings.sam_config, str(settings.sam_checkpoint), device=settings.sam_device)
            self.predictor = SAM2ImagePredictor(model)
            self.error = None
        except Exception as exc:
            self.error = str(exc)
            raise SAMUnavailable(f"SAM 加载失败: {exc}") from exc

    def predict(self, block, z, points, labels, box, only_background=True, snap_boundary=False, boundary_sensitivity=0.5):
        # Same lock order in predict and apply. Each proposal is tied to a source and revision.
        with block.lock, self.lock:
            self._load()
            import torch

            frame = block.em_slice(z)
            if frame.dtype != np.uint8:
                raise ValueError("SAM 当前要求 uint8 电镜切片")
            rgb = np.repeat(frame[..., None], 3, axis=2)
            key = (str(block.path.resolve()), z, hashlib.sha256(rgb.tobytes()).hexdigest())
            started = time.perf_counter()
            precision = torch.autocast("cuda", dtype=torch.bfloat16) if settings.sam_device.startswith("cuda") else nullcontext()
            try:
                with torch.inference_mode(), precision:
                    if key != self.image_key:
                        self.image_key = None
                        self.predictor.set_image(rgb)
                        self.image_key = key
                    masks, scores, _ = self.predictor.predict(
                        point_coords=np.asarray(points, dtype=np.float32) if points else None,
                        point_labels=np.asarray(labels, dtype=np.int32) if points else None,
                        box=np.asarray(box, dtype=np.float32) if box else None,
                        multimask_output=True,
                    )
            except RuntimeError as exc:
                self.image_key = None
                self.error = str(exc)
                raise SAMUnavailable(f"SAM 推理失败: {exc}") from exc
            best = int(np.argmax(scores))
            mask = np.asarray(masks[best], dtype=bool)
            snapped = False
            if snap_boundary:
                # 贴合膜边界: cut off the part of the mask that leaked through a membrane, extend the rest to the membrane
                from emqc.annotate.boundary import refine_mask
                try:
                    mask = refine_mask(mask, frame, points, labels, boundary_sensitivity)
                    snapped = True
                except Exception as exc:  # never lose the raw SAM result over a post-processing problem
                    self.error = f"boundary snap skipped: {exc}"
            if only_background and block.has_seg:
                mask = mask & (block.seg_slice(z) == 0)
            token = uuid.uuid4().hex
            now = time.monotonic()
            self.proposals = OrderedDict((k, v) for k, v in self.proposals.items() if now - v["created"] < 900)
            self.proposals[token] = {"path": str(block.path.resolve()), "work": str(block.work.resolve()), "z": z, "mask": mask,
                                     "revision": revision(block), "slice_rev": block.slice_rev(z), "created": now,
                                     "score": float(scores[best]), "points": points, "labels": labels, "box": box,
                                     "only_background": only_background, "candidate": best,
                                     "snap_boundary": snapped, "boundary_sensitivity": float(boundary_sensitivity)}
            while len(self.proposals) > 32:
                self.proposals.popitem(last=False)
            overlay = np.zeros((*mask.shape, 4), dtype=np.uint8)
            overlay[mask] = [0, 230, 200, 160]
            buf = io.BytesIO()
            Image.fromarray(overlay).save(buf, format="PNG")
            self.error = None
            return {"token": token, "z": z, "score": float(scores[best]), "n_px": int(mask.sum()),
                    "candidate": best, "scores": [float(s) for s in scores], "snap_boundary": snapped,
                    "seconds": round(time.perf_counter() - started, 3),
                    "mask_png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}

    def proposal(self, block, token):
        """The pending proposal for this token, checked to belong to this block. Read-only — it is NOT consumed,
        so the caller can ask questions about the mask (e.g. which label the neighbouring sections carry there)
        and still apply it afterwards."""
        with self.lock:
            p = self.proposals.get(token)
            if p is None or time.monotonic() - p["created"] >= 900:
                raise ValueError("预览已过期，请重新预测")
            if p["path"] != str(block.path.resolve()) or p["work"] != str(block.work.resolve()):
                raise ValueError("预览不属于当前数据块")
            return p

    def apply(self, block, token, new_id, by=None):
        with block.lock, self.lock:
            p = self.proposals.get(token)
            if p is None or time.monotonic() - p["created"] >= 900:
                raise ValueError("预览已过期，请重新预测")
            if p["path"] != str(block.path.resolve()):
                raise ValueError("预览不属于当前数据块")
            if p["work"] != str(block.work.resolve()):
                raise ValueError("标注工作目录已变化，请重新预测")
            # 预览只依赖这一片的图像和标签，所以只看这一片的版本：多人各改各的片时，别人的改动不会让你的预览作废。
            # 旧式预览（测试里手工造的）没有 slice_rev，退回整块文件指纹。
            stale = (p["slice_rev"] != block.slice_rev(p["z"])) if p.get("slice_rev") is not None else (p["revision"] != revision(block))
            if stale:
                raise ValueError("本片标注已变化，请重新预测后应用")
            rec = block.apply_mask(p["z"], p["mask"], new_id,
                                   {"model": "SAM 2.1", "config": settings.sam_config, "score": p["score"],
                                    "points": p["points"], "labels": p["labels"], "box": p["box"],
                                    "only_background": p["only_background"], "candidate": p["candidate"],
                                    "snap_boundary": p.get("snap_boundary", False)}, by=by)
            self.proposals.pop(token)
            return rec


service = SAMService()
