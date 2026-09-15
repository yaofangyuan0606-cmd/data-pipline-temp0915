"""Command line entry point:  python -m emqc <command>"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from emqc.config import settings


def cmd_init_db(_):
    from emqc.db import init_db

    init_db()
    print(f"tables ensured on {settings.db_url.split('@')[-1]}")


def cmd_scan(args):
    from emqc.db import init_db, session_scope
    from emqc.registry.scanner import scan

    init_db()
    with session_scope() as s:
        res = scan(s, Path(args.data_root) if args.data_root else None)
    print(json.dumps(res.as_dict(), indent=1, ensure_ascii=False))


def cmd_delete(args):
    from emqc.db import session_scope
    from emqc.registry.scanner import delete_dataset

    with session_scope() as s:
        for d in args.dataset_id:
            print(d, json.dumps(delete_dataset(s, d, remove_files=args.files), ensure_ascii=False))


def cmd_export(args):
    from emqc.qc.export import export_passed

    m = export_passed(args.dataset_id, args.out, run_id=args.run, fmt=args.fmt, min_run=args.min_run, shard_z=args.shard_z, block_ids=args.blocks or None, min_quality=args.min_quality, with_assets=[a for a in (args.with_assets or "").split(",") if a], resume=not args.no_resume)
    print(f"exported {m['n_sections']} sections in {m['n_shards']} shards ({m['bytes']/1e6:.1f} MB, {m['n_shards_reused']} reused) from run {m['run_id']} -> {args.out}/{args.dataset_id}")
    for c in m["shards"]:
        print(f"  {c['block_id']}  z{c['z_start']}-{c['z_end']-1}  n={c['n']:3d}  grade {c['grade']}  {c['file']}")
    for k, v in m["assets"].items():
        print(f"  assets {k}: {v['copied']} files -> {v['dir']}")


def cmd_validate_labels(args):
    from sqlalchemy import select

    from emqc.db import session_scope
    from emqc.db.models import Dataset, DatasetAsset
    from emqc.qc.labels import validate_label_asset

    with session_scope() as s:
        ds = s.get(Dataset, args.dataset_id)
        if ds is None:
            raise SystemExit(f"unknown dataset {args.dataset_id}")
        from emqc.qc.runner import open_dataset_volume

        q = select(DatasetAsset).where(DatasetAsset.dataset_id == ds.dataset_id, DatasetAsset.asset_type != "em_image")
        if args.asset_type:
            q = q.where(DatasetAsset.asset_type == args.asset_type)
        assets = list(s.scalars(q))  # materialise: validate_label_asset commits inside the loop
        em = open_dataset_volume(ds)
        for a in assets:
            if not (a.format or "").startswith(("image_stack", "precomputed")) and a.format not in ("npy",):
                print(f"  skip {a.asset_type:24s} {a.path}  ({a.format or 'no format'}: not a volume)")
                continue
            v = validate_label_asset(s, ds, a, em=em, step=args.step, max_sections=args.max_sections)
            print(f"  {v['status']:5s} {a.asset_type:24s} {a.path:20s} shape {v.get('label_shape')} scale {v.get('scale_to_em')} issues {v.get('issues')} usable_for_patches={v.get('usable_for_patches')}")
            for n in v.get("notes") or []:
                print(f"        {n}")


def cmd_partition(args):
    from sqlalchemy import select

    from emqc.db import session_scope
    from emqc.db.models import Block, Dataset
    from emqc.patches.partition import assign_partitions

    with session_scope() as s:
        ds = s.get(Dataset, args.dataset_id)
        if ds is None:
            raise SystemExit(f"unknown dataset {args.dataset_id}")
        blocks = list(s.scalars(select(Block).where(Block.dataset_id == ds.dataset_id)))
        res = assign_partitions(blocks, ratios=args.ratios or settings.partition_ratios, seed=args.seed if args.seed is not None else settings.partition_seed, force=args.force)
        ds.metadata_json = {**(ds.metadata_json or {}), "partition": {**res.as_dict(), "forced": args.force}}
        print(json.dumps(res.as_dict(), indent=1, ensure_ascii=False))
        for b in sorted(blocks, key=lambda b: (b.z_start, b.y_start, b.x_start)):
            print(f"  {b.block_id:28s} split={b.split:12s} partition={b.partition:8s} grade={b.latest_grade or '-'} difficulty={'-' if b.difficulty is None else round(b.difficulty, 3)}")


def cmd_patches(args):
    from emqc.db import session_scope
    from emqc.db.models import Dataset
    from emqc.patches.generate import PatchSpec, generate_patch_set, readiness

    with session_scope() as s:
        ds = s.get(Dataset, args.dataset_id)
        if ds is None:
            raise SystemExit(f"unknown dataset {args.dataset_id}")
        if args.type == "readiness":
            print(json.dumps(readiness(s, ds), indent=1, ensure_ascii=False))
            return
        size = tuple(int(v) for v in args.size.split(","))
        spec = PatchSpec(patch_type=args.type, size=size, n=args.n, seed=args.seed, partitions=args.partitions or None, only_passed=not args.include_failed, min_quality=args.min_quality, params=json.loads(args.params or "{}"))
        p = generate_patch_set(s, ds, spec)
        print(f"patch set #{p.id} {p.patch_type}: {p.status} {p.n_patches} patches  counts={p.counts_json.get('by_partition')} checks_passed={p.checks_json.get('passed')}")
        if p.reason:
            print("  reason:", p.reason)


def cmd_cloud(args):
    """采集：看卷 / 预判 ROI / 注册 / 下载。"""
    from emqc.crawl.index import H01_PROPS_URL, load_properties, parse, select as select_rows
    from emqc.crawl.precheck import H01_MASK_URL, tissue_profile
    from emqc.crawl.register import CrawlRunner, create_crawl_job, register_cloud_roi
    from emqc.db import session_scope
    from emqc.registry.cloud import parse_roi_arg, volume_info

    if args.action == "volume":
        print(json.dumps(volume_info(args.url, args.mip), indent=1, ensure_ascii=False))
        return
    if args.action == "index":
        rows, tags = parse(load_properties(args.props or H01_PROPS_URL))
        if args.list_tags:
            print(f"{len(rows)} 个 segment，{len(tags)} 个标签:")
            print("  " + ", ".join(tags))
            return
        for r in select_rows(rows, tag=args.tag, min_voxels=args.min_voxels, top=args.top):
            print(f"  id {r['id']:<22} " + "  ".join(f"{k}={v:g}" for k, v in r.items() if k not in ("id", "tags") and isinstance(v, (int, float))) + f"  tags={','.join(r.get('tags') or [])}")
        return
    roi = parse_roi_arg(args.roi)
    if args.action == "precheck":
        p = tissue_profile(roi, mask_url=args.mask_url or H01_MASK_URL, min_wanted=args.min_neuropil, max_defect=args.max_fissure)
        print(f"  组织构成 {p['composition']}")
        print(f"  neuropil {p['wanted_frac']:.1%}  fissure {p['defect_frac']:.2%}  →  {p['verdict']}  {'; '.join(p['reasons']) or '值得爬'}  ({p['seconds']}s)")
        return
    pre = None
    if not args.no_precheck:
        try:
            pre = tissue_profile(roi, mask_url=args.mask_url or H01_MASK_URL, min_wanted=args.min_neuropil, max_defect=args.max_fissure)
            print(f"  预判: neuropil {pre['wanted_frac']:.1%} fissure {pre['defect_frac']:.2%} → {pre['verdict']}")
            if pre["verdict"] == "reject" and not args.force:
                raise SystemExit("  ROI 预判不合格，加 --force 可强制继续：" + "; ".join(pre["reasons"]))
        except SystemExit:
            raise
        except Exception as e:
            print(f"  预判跳过（{type(e).__name__}: {e}）")
    if args.action == "register":
        assets = [{"type": "gt_segmentation", "url": args.seg_url, "mip": args.seg_mip, "format": "precomputed_cloud", "label_encoding": "gray"}] if args.seg_url else None
        with session_scope() as s:
            ds = register_cloud_roi(s, args.dataset_id, args.url, roi, mip=args.mip, name=args.name, species=args.species,
                                    brain_region=args.brain_region, assets=assets, precheck=pre)
            m = ds.metadata_json
            print(f"  已注册 {ds.dataset_id}: {ds.size_z}×{ds.size_y}×{ds.size_x}  体素 {ds.voxel_size_x_nm}/{ds.voxel_size_y_nm}/{ds.voxel_size_z_nm} nm  {ds.size_class}")
            print(f"  实读 ROI {m['roi']}（已对齐 chunk 网格）  编码 {m['encoding']}{' 有损' if m['lossy'] else ''}")
            print(f"  不下载，QC 与 patch 按需读取。跑 QC: python -m emqc run {ds.dataset_id}")
        return
    if args.action == "fetch":
        job_id = create_crawl_job(args.dataset_id, args.url, roi, args.mip, args.out, align=not args.no_align, resume=not args.no_resume, precheck=pre)
        r = CrawlRunner(job_id).run()
        print(f"  任务 #{job_id} {r['status']}：{r['n_sections']} 张，{r['bytes']/1e6:.1f} MB → {r['out_dir']}")
        print(f"  线上取回 {r['wire']['chunks_fetched']} 个 chunk，{r['wire']['seconds']:.1f}s。扫描后即成为普通本地数据集：python -m emqc scan")
        return


def cmd_datasets(_):
    from emqc.db import session_scope
    from emqc.registry.scanner import list_datasets

    with session_scope() as s:
        for d in list_datasets(s):
            print(f"{d.dataset_id:28s} {d.size_class:5s} {d.size_z:5d}x{d.size_y}x{d.size_x} {d.dtype:6s} blocks={len(d.blocks):3d} status={d.status:10s} q={d.latest_quality_score} ret={d.latest_retention_rate}")


def cmd_run(args):
    from emqc.qc.runner import run_sync

    run_id = run_sync(args.dataset_id, args.blocks or None)
    print(f"run {run_id} done")


def cmd_checks(_):
    from emqc.qc import catalog

    for c in catalog():
        flag = "impl" if c["implemented"] else "STUB"
        print(f"[{flag}] {c['level']:6s} {c['stage']:9s} {c['name']:22s} -> {c['failure_type']:18s} ({c['maturity']}) {c['description']}")


def cmd_serve(args):
    import uvicorn

    uvicorn.run("emqc.api.app:app", host=args.host or settings.api_host, port=args.port or settings.api_port, reload=args.reload, log_level="info")


def cmd_make_sample(args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.make_sample_dataset import main as make_main

    argv = []
    if args.dest:
        argv += ["--dest", args.dest]
    return make_main(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="emqc", description="EM Image QC platform v0.1")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create tables").set_defaults(fn=cmd_init_db)
    sp = sub.add_parser("scan", help="discover datasets under the data root and register them")
    sp.add_argument("--data-root")
    sp.set_defaults(fn=cmd_scan)
    sub.add_parser("datasets", help="list registered datasets").set_defaults(fn=cmd_datasets)
    dp = sub.add_parser("delete", help="remove dataset(s) with all QC results and previews")
    dp.add_argument("dataset_id", nargs="+")
    dp.add_argument("--files", action="store_true", help="also delete the data directory (must be under data_root)")
    dp.set_defaults(fn=cmd_delete)
    rp = sub.add_parser("run", help="run the QC pipeline for a dataset (synchronously)")
    rp.add_argument("dataset_id")
    rp.add_argument("--blocks", nargs="*", help="block ids, default all")
    rp.set_defaults(fn=cmd_run)
    sub.add_parser("checks", help="list QC checks").set_defaults(fn=cmd_checks)
    cl = sub.add_parser("cloud", help="采集：云端 precomputed 卷的查看 / ROI 预判 / 注册 / 下载")
    cl.add_argument("action", choices=["volume", "index", "precheck", "register", "fetch"])
    cl.add_argument("--url", default="gs://h01-release/data/20210601/4nm_raw", help="EM 卷 URL")
    cl.add_argument("--roi", help="x0-x1,y0-y1,z0-z1（所选 mip 的体素单位）")
    cl.add_argument("--mip", type=int, default=1)
    cl.add_argument("--dataset-id")
    cl.add_argument("--name")
    cl.add_argument("--species")
    cl.add_argument("--brain-region")
    cl.add_argument("--seg-url", help="同 ROI 的分割卷，注册为 gt_segmentation")
    cl.add_argument("--seg-mip", type=int, default=0)
    cl.add_argument("--mask-url", help="组织类型图（默认 H01 masking）")
    cl.add_argument("--min-neuropil", type=float, default=0.5)
    cl.add_argument("--max-fissure", type=float, default=0.02)
    cl.add_argument("--no-precheck", action="store_true")
    cl.add_argument("--force", action="store_true", help="预判不合格也继续")
    cl.add_argument("--out", help="fetch 的落盘目录")
    cl.add_argument("--no-align", action="store_true")
    cl.add_argument("--no-resume", action="store_true")
    cl.add_argument("--props", help="segment_properties URL")
    cl.add_argument("--tag")
    cl.add_argument("--min-voxels", type=float, default=0.0)
    cl.add_argument("--top", type=int, default=20)
    cl.add_argument("--list-tags", action="store_true")
    cl.set_defaults(fn=cmd_cloud)
    vl = sub.add_parser("validate-labels", help="validate label assets (GT / masks / predictions) against the EM volume")
    vl.add_argument("dataset_id")
    vl.add_argument("--asset-type")
    vl.add_argument("--step", type=int, default=1, help="check every k-th section (large datasets)")
    vl.add_argument("--max-sections", type=int)
    vl.set_defaults(fn=cmd_validate_labels)
    pt = sub.add_parser("partition", help="assign / show the train-val-test holdout partition of a dataset's blocks")
    pt.add_argument("dataset_id")
    pt.add_argument("--ratios", help="train,val,test e.g. 0.8,0.1,0.1")
    pt.add_argument("--seed", type=int)
    pt.add_argument("--force", action="store_true", help="reshuffle every eligible block (changes the test set!)")
    pt.set_defaults(fn=cmd_partition)
    pa = sub.add_parser("patches", help="generate a patch set (or 'readiness' to see which types this dataset supports)")
    pa.add_argument("dataset_id")
    pa.add_argument("--type", required=True, help="segmentation | membrane | synapse | mitochondria | proofreading | hard_negative | failure | readiness")
    pa.add_argument("--size", default="16,256,256", help="dz,dy,dx")
    pa.add_argument("--n", type=int, default=64)
    pa.add_argument("--seed", type=int, default=0)
    pa.add_argument("--partitions", nargs="*", help="train val test (default: all three; failure: every block)")
    pa.add_argument("--include-failed", action="store_true", help="do not restrict windows to QC-passing sections")
    pa.add_argument("--min-quality", type=float)
    pa.add_argument("--params", help="JSON overrides of acceptance thresholds")
    pa.set_defaults(fn=cmd_patches)
    ep = sub.add_parser("export", help="export the QC-passing sections of a dataset as npy stacks (or png) + manifest")
    ep.add_argument("dataset_id")
    ep.add_argument("--out", default=str(settings.export_dir))
    ep.add_argument("--fmt", choices=["npy", "png"], default="npy")
    ep.add_argument("--min-run", type=int, default=1, help="only export contiguous passing runs of at least this many sections")
    ep.add_argument("--shard-z", type=int, default=16, help="max sections per shard file (0 = one file per contiguous run)")
    ep.add_argument("--no-resume", action="store_true", help="rewrite shards even if they already exist")
    ep.add_argument("--min-quality", type=float)
    ep.add_argument("--run", type=int, help="QC run id (default: latest)")
    ep.add_argument("--blocks", nargs="*")
    ep.add_argument("--with-assets", help="comma-separated asset types to copy for the same z, e.g. gt_segmentation")
    ep.set_defaults(fn=cmd_export)
    sv = sub.add_parser("serve", help="start the API + dashboard")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(fn=cmd_serve)
    ms = sub.add_parser("make-sample", help="generate demo datasets into the data root")
    ms.add_argument("--dest")
    ms.set_defaults(fn=cmd_make_sample)
    args = p.parse_args(argv)
    return args.fn(args) or 0
