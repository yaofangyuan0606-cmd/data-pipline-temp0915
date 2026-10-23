"""用标定集给「找相似」打分：每张有问题的样本找最像的 10 张，看其中有几张是同一类问题。

    .venv/bin/python scripts/calib_similarity_eval.py <calib/crops 目录> [--k 10] [--cap 150] [--out var/calib_eval]

标定集的结构：crops/<检查项>/{fail,clean,near_miss,hint}/*.png，旁边可选 manifest.csv（用来知道同一张切片
在别的检查项里是不是也算 fail）。

做法
  1. 每张图片统一成 512×512 灰度，按固定随机种子打乱后每 64 张一组，在内存里跑一遍平台的 QC
     （和正式运行同一套代码），得到每张图的统计量和各检查分数。不写数据库、不动原图。
  2. 两种特征分别评测：
       「只用统计量」   亮度、对比度、清晰度、熵、黑块形状……不含任何判定分数 —— 公平的数字
       「统计量 + 分数」 再加上切片级检查分数 —— 浏览器里「找相似」实际用的
  3. 对每张 fail 样本找 k 个最近邻（排除同一张切片在别的文件夹里的副本、同一数据同一 z 的其他 tile），
     精度 = k 个里也是这一类 fail 的比例。随机基线 = 这一类在样本池里的占比。

注意
  - 标定集的 fail / clean 是平台当时的判定，不是人工标注；这里测的是"数值特征能不能把同类问题聚到一起"，
    不是检测本身准不准。「统计量 + 分数」因为含分数，天然偏高，看「只用统计量」更公平。
  - 序列检查（切片跳变、错位、形变、顺序错误）要和前后切片比较才看得出，这批图片没有前后切片，
    它们的结果只能看统计量那一列，而且偏低是正常的。
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emqc.qc.base import SERIAL_CHECKS, SLICE_CHECKS, BlockInfo, DatasetInfo, QCConfig  # noqa: E402
from emqc.qc.runner import QCRunner  # noqa: E402
from emqc.qc.similar import build_space  # noqa: E402

CATEGORIES = ("fail", "clean", "near_miss", "hint")
SIZE = 512
ZH = {"blank_slice": "空白切片", "missing_slice": "缺失切片", "corrupt_slice": "损坏切片", "severe_blur": "严重模糊", "saturation": "过曝 / 饱和",
      "crack": "裂缝 / 褶皱", "brightness_jump": "亮度突变", "contrast_drift": "对比度漂移", "charging_artifact": "荷电伪影",
      "contamination": "表面污染", "slice_jump": "切片跳变", "local_misalignment": "局部错位", "global_misalignment": "整体错位",
      "z_order_error": "顺序错误", "local_deformation": "局部形变", "section_deformation": "切片形变"}


class ListReader:
    """Minimal VolumeReader over in-memory 2-D arrays (what the ingest stage needs)."""

    def __init__(self, arrs):
        self.arrs = arrs

    def read_slice(self, z):
        if not 0 <= z < len(self.arrs):
            raise IndexError(z)
        return self.arrs[z]

    def close(self):
        pass


def load(path: Path) -> np.ndarray:
    im = Image.open(path).convert("L")
    if im.size != (SIZE, SIZE):
        s = min(im.size)
        l, t = (im.width - s) // 2, (im.height - s) // 2
        im = im.crop((l, t, l + s, t + s)).resize((SIZE, SIZE), Image.BILINEAR)
    return np.asarray(im, dtype=np.uint8)


def collect(crops: Path, cap: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for check_dir in sorted(p for p in crops.iterdir() if p.is_dir()):
        for cat in CATEGORIES:
            files = sorted((check_dir / cat).glob("*.png"))
            if cat in ("near_miss", "hint") and len(files) > cap:
                files = sorted(rng.sample(files, cap))
            out += [{"check": check_dir.name, "category": cat, "path": f, "file": f.name} for f in files]
    return out


def source_index(crops: Path) -> dict[str, tuple]:
    """crop file name -> (dataset, block, z) from manifest.csv next to crops/ (optional)."""
    m = crops.parent / "manifest.csv"
    out = {}
    if m.is_file():
        with m.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                out[Path(r["crop"]).name] = (r["dataset"], r["block"], r["z"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("crops", type=Path)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--cap", type=int, default=150, help="near_miss / hint 每个文件夹最多取多少张（全部取会很慢）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=ROOT / "var" / "calib_eval")
    a = ap.parse_args()

    samples = collect(a.crops, a.cap, a.seed)
    src = source_index(a.crops)
    for s in samples:
        s["src"] = src.get(s["file"]) or (str(s["path"]),)  # without a manifest every file is its own slice
    # every check under which this source slice is a fail (a slice can be in several folders)
    fail_of = defaultdict(set)
    for s in samples:
        if s["category"] == "fail":
            fail_of[s["src"]].add(s["check"])
    for s in samples:
        s["labels"] = fail_of.get(s["src"], set())
    print(f"{len(samples)} 张样本（fail {sum(s['category'] == 'fail' for s in samples)}），开始计算特征…", flush=True)

    # 1. features: the platform's own QC, in memory, 64 shuffled images per group
    order = list(range(len(samples)))
    random.Random(a.seed).shuffle(order)
    runner = QCRunner(QCConfig(), preview_root=None)
    t0 = time.time()
    for g in range(0, len(order), 64):
        idx = order[g:g + 64]
        arrs = []
        for i in idx:
            try:
                arrs.append(load(samples[i]["path"]))
            except Exception:
                arrs.append(np.zeros((SIZE, SIZE), np.uint8))
                samples[i]["unreadable"] = True
        ds = DatasetInfo("calib", (len(arrs), SIZE, SIZE), "uint8")
        (ctx, _res), = runner.process_group(ListReader(arrs), ds, [BlockInfo("calib", 0, len(arrs), 0, SIZE, 0, SIZE)])
        for i, rec in zip(idx, ctx.slices):
            samples[i]["stats"], samples[i]["scores"] = dict(rec.stats or {}), dict(rec.scores or {})
            samples[i]["ok"] = rec.ok
        done = min(g + 64, len(order))
        if (g // 64) % 10 == 0 or done == len(order):
            el = time.time() - t0
            print(f"  {done}/{len(order)}  {el:.0f}s，预计还要 {el / done * (len(order) - done):.0f}s", flush=True)

    pool = [s for s in samples if s.get("ok") and not s.get("unreadable")]
    slice_names = sorted(c.name for c in SLICE_CHECKS)
    serial = {c.name for c in SERIAL_CHECKS}
    spaces = {
        "只用统计量": build_space([(s["stats"], {}) for s in pool], keys=list(range(len(pool))), score_names=[]),
        "统计量 + 分数": build_space([(s["stats"], s["scores"]) for s in pool], keys=list(range(len(pool))), score_names=slice_names),
    }

    # 2. precision@k per check
    def same_section(i, j):
        si, sj = pool[i]["src"], pool[j]["src"]
        return si == sj or (len(si) == 3 and len(sj) == 3 and si[0] == sj[0] and si[2] == sj[2])

    checks = sorted({s["check"] for s in pool if s["category"] == "fail"})
    rows = []
    for c in checks:
        q = [i for i, s in enumerate(pool) if s["category"] == "fail" and s["check"] == c]
        base = sum(1 for s in pool if c in s["labels"]) / max(len(pool) - 1, 1)
        row = {"check": c, "zh": ZH.get(c, c), "serial": c in serial, "n": len(q), "random": base}
        for name, sp in spaces.items():
            hits = []
            for i in q:
                # a class with fewer than k other members can never fill k slots: look at that many neighbours instead
                avail = sum(1 for j, s in enumerate(pool) if j != i and c in s["labels"] and not same_section(i, j))
                k_eff = min(a.k, avail)
                if k_eff == 0:
                    continue
                nn = sp.nearest(i, k=k_eff, exclude=lambda j, i=i: same_section(i, j))
                hits.append(sum(1 for j, _ in nn if c in pool[j]["labels"]) / max(len(nn), 1))
            row[name] = float(np.mean(hits)) if hits else None
            row["k_eff"] = min(a.k, max(len(q) - 1, 0))
        rows.append(row)

    # 3. report
    a.out.mkdir(parents=True, exist_ok=True)
    names = list(spaces)
    lines = [f"# 找相似：标定集评测（k = {a.k}）", "",
             f"样本池 {len(pool)} 张（near_miss / hint 每个文件夹最多取 {a.cap} 张），{sum(r['n'] for r in rows)} 张 fail 样本作查询。", "",
             "精度 = 最像的 k 张里也是这一类问题的比例（这一类不到 k 张时，只看同样数量的最近邻）；随机 = 随便抽一张是这一类的概率；倍数 = 只用统计量 ÷ 随机。", "",
             "| 问题类型 | 查询数 | 只用统计量 | 统计量 + 分数 | 随机 | 倍数 |", "|---|---:|---:|---:|---:|---:|"]
    pct = lambda v: "–" if v is None else f"{100 * v:.0f}%"
    for r in sorted(rows, key=lambda r: (r["serial"], -(r[names[0]] or 0))):
        lift = (r[names[0]] / r["random"]) if r["random"] and r[names[0]] is not None else None
        tag = "（序列检查，仅供参考）" if r["serial"] else ""
        lines.append(f"| {r['zh']}{tag} | {r['n']} | {pct(r[names[0]])} | {pct(r[names[1]])} | {pct(r['random'])} | {'–' if lift is None else f'{lift:.1f}×'} |")
    sl = [r for r in rows if not r["serial"]]
    for name in names:
        v = [r[name] for r in sl if r[name] is not None]
        lines.append("")
        lines.append(f"切片级检查平均（{name}）：**{pct(float(np.mean(v)) if v else None)}**")
    lines += ["", "标定集的 fail / clean 是平台当时的判定，不是人工标注；「统计量 + 分数」含判定分数，天然偏高，看「只用统计量」更公平。",
              "序列检查需要前后切片，这批图片没有，结果仅供参考。"]
    report = "\n".join(lines)
    (a.out / "report.md").write_text(report, encoding="utf-8")
    (a.out / "results.json").write_text(json.dumps({"k": a.k, "n_pool": len(pool), "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print(report)
    print(f"\n报告已写到 {a.out / 'report.md'}")


if __name__ == "__main__":
    main()
