"""推理流演示：按平台的推理流把整卷一块一块取过来，跑一个「模型」，存下结果。

    .venv/bin/python scripts/infer_demo.py h01-demo-v1-y0000_x0512
    .venv/bin/python scripts/infer_demo.py h01-demo-v1 --api http://127.0.0.1:8793 --z-chunk 16 --delay 0.5
    .venv/bin/python scripts/infer_demo.py --stream-id 6     # 接上「数据交付」页上已经建好的会话 #6

「模型」是一个不需要 GPU 的经典膜检测：电镜里细胞膜是暗的细线，用 Sato 脊线滤波把它们找出来，
输出 0–255 的膜概率图。换成真模型时，只要把 model() 换掉，其余不用动。

运行时去测试网站的「数据交付」页，最下面「推理流会话」里能看到这个会话的进度、已取数据量和速率。
结果写在 var/infer_demo/<数据集>/：每个子块一个 .npy，外加一张对比图 preview.png 和 summary.json。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emqc.loader import StreamClient  # noqa: E402


def model(arr: np.ndarray) -> np.ndarray:
    """(n, H, W) uint8 EM -> (n, H, W) uint8 membrane probability. Replace with a real network."""
    from skimage.filters import sato

    out = np.empty(arr.shape, dtype=np.uint8)
    for k, sl in enumerate(arr):
        f = sl.astype(np.float32) / 255.0
        r = sato(f, sigmas=[1.5, 2.5], black_ridges=True)      # dark thin lines = membranes
        hi = np.percentile(r, 99.5) or 1.0
        out[k] = (np.clip(r / hi, 0, 1) * 255).astype(np.uint8)
    return out


def preview(pairs: list[tuple[str, np.ndarray, np.ndarray]], path: Path) -> None:
    """One row per sampled section: EM | membrane map | overlay."""
    from PIL import Image, ImageDraw

    S = 256
    rows = []
    for label, em, pr in pairs:
        e = Image.fromarray(em).convert("L").resize((S, S))
        p = Image.fromarray(pr).convert("L").resize((S, S))
        ov = Image.merge("RGB", (Image.fromarray(np.maximum(np.asarray(e), np.asarray(p))), e, e))  # membranes in red
        row = Image.new("RGB", (3 * S + 16, S + 22), "white")
        for j, im in enumerate((e.convert("RGB"), p.convert("RGB"), ov)):
            row.paste(im, (j * (S + 8), 22))
        ImageDraw.Draw(row).text((4, 4), f"{label}    EM  |  membrane map  |  overlay", fill="black")
        rows.append(row)
    sheet = Image.new("RGB", (rows[0].width, sum(r.height for r in rows)), "white")
    y = 0
    for r in rows:
        sheet.paste(r, (0, y))
        y += r.height
    sheet.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", nargs="?", help="数据集名（用 --stream-id 接已有会话时可省）")
    ap.add_argument("--stream-id", type=int, default=None, help="接上页面上已经建好的会话，而不是新建一个")
    ap.add_argument("--api", default="http://127.0.0.1:8793")
    ap.add_argument("--z-chunk", type=int, default=16)
    ap.add_argument("--client", default="demo-membrane")
    ap.add_argument("--skip-failed", action="store_true", help="跳过含未通过 QC 切片的子块")
    ap.add_argument("--delay", type=float, default=0.5, help="每块之间停一下，方便在网页上看进度")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    if a.stream_id is None and not a.dataset:
        ap.error("给一个数据集名，或者用 --stream-id 接上已有的会话")
    sc = StreamClient(a.api, a.dataset, z_chunk=a.z_chunk, client=a.client, skip_failed=a.skip_failed, stream_id=a.stream_id)
    a.dataset = sc.session["dataset_id"]
    out = a.out or ROOT / "var" / "infer_demo" / a.dataset
    out.mkdir(parents=True, exist_ok=True)
    print(f"会话 #{sc.stream_id}：{sc.session['n_items']} 个子块，约 {sc.session['est_bytes'] / 1e6:.1f} MB。去「数据交付」页看进度。", flush=True)
    t0, n, nbytes, pairs, frac = time.time(), 0, 0, [], []
    for item, arr in sc.items():                       # 和页面上「推理侧怎么接」是同一个写法
        pred = model(arr)                              # arr: (z1-z0, H, W)，一个 tile 的一段
        name = f"{item['block_id']}__z{item['z0']:05d}-{item['z1'] - 1:05d}.npy"
        np.save(out / name, pred)
        n += 1
        nbytes += arr.nbytes
        frac.append(float((pred > 128).mean()))
        if len(pairs) < 4 and (n == 1 or n % 2 == 0):
            mid = arr.shape[0] // 2
            pairs.append((f"{item['block_id']} z{item['z0'] + mid}", arr[mid], pred[mid]))
        bad = f"，含 {item['n_failed']} 张未通过 QC 的切片" if item.get("n_failed") else ""
        print(f"  [{n}/{sc.session['n_items']}] {item['block_id']} z{item['z0']}–{item['z1'] - 1}  形状 {arr.shape}  膜占比 {frac[-1]:.1%}{bad}", flush=True)
        time.sleep(a.delay)
    dt = time.time() - t0
    if pairs:
        preview(pairs, out / "preview.png")
    summary = {"dataset": a.dataset, "stream_id": sc.stream_id, "n_items": n, "bytes": nbytes, "seconds": round(dt, 1),
               "mean_membrane_fraction": round(float(np.mean(frac)), 4) if frac else None, "out_dir": str(out)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"完成：{n} 个子块、{nbytes / 1e6:.1f} MB、{dt:.0f} 秒。结果在 {out}（对比图 preview.png）")


if __name__ == "__main__":
    main()
