"""Prompt SAM only inside seg == 0 and save the generator's outputs unchanged."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "var/datasets/h01-demo-v1/tiles/h01_b0000_demo_data/visual_512/slices_em/y0000_x0000"
REFERENCE = INPUT.parent.parent / "slices_seg_color/y0000_x0000"
SEGMENTATION = ROOT / "var/datasets/h01-demo-v1/tiles/h01_b0000_demo_data_512/h01_b0000_demo_data_y0000_x0000/seg.npy"
CONFIG = "configs/sam2.1/sam2.1_hiera_l.yaml"
GRID_SIZE = 64
PARAMS = dict(points_per_side=None, points_per_batch=128, pred_iou_thresh=0.8,
              stability_score_thresh=0.92, crop_n_layers=0,
              min_mask_region_area=0, use_m2m=True)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--device", default="cuda:1")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    references = sorted(REFERENCE.glob("*.png"))[:args.count]
    paths = [INPUT / path.name for path in references]
    if len(paths) != args.count:
        parser.error(f"Only {len(paths)} input images available")
    for key, folder in (("TMPDIR", "var/tmp"), ("TORCH_HOME", "var/cache/torch"),
                        ("CUDA_CACHE_PATH", "var/cache/cuda")):
        path = ROOT / folder
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)

    import numpy as np
    from PIL import Image
    import torch
    from sam2.build_sam import build_sam2
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
    from sam2.utils.amg import build_point_grid

    seg = np.load(SEGMENTATION, mmap_mode="r")
    prompt_grids = []
    grid = build_point_grid(GRID_SIZE)
    for path, reference in zip(paths, references):
        z = int(path.stem.removeprefix("z"))
        rgb = np.array(Image.open(path).convert("RGB"))
        ref = np.array(Image.open(reference).convert("RGB"))
        uncolored = seg[:, :, z] == 0
        if uncolored.shape != rgb.shape[:2] or ref.shape != rgb.shape:
            raise ValueError(f"Image/seg shape mismatch: {path.name}")
        if not np.array_equal(ref[uncolored], rgb[uncolored]):
            raise ValueError(f"Label 0 does not match reference gray EM pixels: {path.name}")
        height, width = uncolored.shape
        points_xy = (grid * np.array([width, height])).astype(int)
        selected = grid[uncolored[points_xy[:, 1], points_xy[:, 0]]]
        prompt_grids.append(selected)

    checkpoint = ROOT / "var/models/sam2.1_hiera_large.pt"
    torch.set_num_threads(4)
    model = build_sam2(CONFIG, str(checkpoint), device=args.device)
    generator = SAM2AutomaticMaskGenerator(model, point_grids=[grid], **PARAMS)
    out = ROOT / "var/exports" / (f"sam_uncolored_first{args.count}_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    (out / "raw_masks").mkdir(parents=True, exist_ok=False)
    metadata = dict(model="SAM 2.1 Hiera Large", config=CONFIG, params=PARAMS,
                    checkpoint_sha256=sha256(checkpoint), device=args.device,
                    input="raw EM", raw_mask_axes="NYX", slice_names=[p.name for p in paths],
                    prompt_region="Original seg[:, :, z] == 0, verified against gray reference pixels",
                    grid_points_per_side=GRID_SIZE, prompt_counts=[len(g) for g in prompt_grids],
                    output="Unmodified SAM2AutomaticMaskGenerator output; official quality/stability filtering and NMS apply",
                    custom_postprocessing=False, output_clipped_to_uncolored=False,
                    source_sha256={str(p.relative_to(ROOT)): sha256(p)
                                   for p in [*paths, *references, SEGMENTATION]})
    (out / "inference.json").write_text(json.dumps(metadata, indent=2))
    print(out, flush=True)
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16,
                                               enabled=args.device.startswith("cuda")):
        for index, path in enumerate(paths, 1):
            rgb = np.array(Image.open(path).convert("RGB"))
            generator.point_grids = [prompt_grids[index - 1]]
            predictions = generator.generate(rgb) if len(prompt_grids[index - 1]) else []
            masks = (np.stack([a["segmentation"] for a in predictions]) if predictions
                     else np.zeros((0, *rgb.shape[:2]), dtype=bool))
            np.savez_compressed(out / "raw_masks" / f"{path.stem}.npz", masks=masks)
            scores = [{k: v for k, v in a.items() if k != "segmentation"} for a in predictions]
            (out / "raw_masks" / f"{path.stem}.json").write_text(
                json.dumps(dict(name=path.name,
                                prompt_points_xy=(prompt_grids[index - 1] * np.array([rgb.shape[1], rgb.shape[0]])).tolist(),
                                masks=scores), indent=2))
            print(f"{index}/{len(paths)} {path.name}: {len(prompt_grids[index-1])} gray-region prompts, {len(predictions)} masks", flush=True)


if __name__ == "__main__":
    main()
