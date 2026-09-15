"""Cloud acquisition: URL/ROI parsing, chunk-cached reading, ROI precheck, registration and the crawl job.

No network: a fake CloudVolume stands in for cloud-volume and counts how many times each chunk is fetched,
which is the property that actually matters (reading slice by slice must not re-download the z-chunk).
"""
import numpy as np
import pytest

from emqc.registry import cloud


class FakeScale(dict):
    pass


class FakeCV:
    """Minimal CloudVolume stand-in: cv[x0:x1, y0:y1, z0:z1] -> (x, y, z, 1)."""

    calls: list = []

    def __init__(self, size=(4096, 4096, 256), chunk=(128, 128, 32), res=(8.0, 8.0, 33.0), dtype="uint8", encoding="jpeg", offset=(0, 0, 0)):
        self.scale = {"key": "8.0x8.0x33.0", "size": list(size), "chunk_sizes": [list(chunk)], "resolution": list(res), "encoding": encoding, "voxel_offset": list(offset)}
        self.info = {"scales": [self.scale]}
        self.dtype = np.dtype(dtype)
        self.num_channels = 1
        self.resolution = list(res)

    def __getitem__(self, sl):
        sx, sy, sz = sl
        FakeCV.calls.append((sx.start, sx.stop, sy.start, sy.stop, sz.start, sz.stop))
        nx, ny, nz = sx.stop - sx.start, sy.stop - sy.start, sz.stop - sz.start
        # value encodes the absolute coordinate so a wrong offset is visible
        x = np.arange(sx.start, sx.stop, dtype=np.int64)[:, None, None]
        y = np.arange(sy.start, sy.stop, dtype=np.int64)[None, :, None]
        z = np.arange(sz.start, sz.stop, dtype=np.int64)[None, None, :]
        a = ((x * 7 + y * 3 + z * 11) % 251).astype(self.dtype)
        return a.reshape(nx, ny, nz, 1)


@pytest.fixture
def fake_cv(monkeypatch):
    FakeCV.calls = []
    made = {}

    def _open(url, mip=0, fill_missing=True, parallel=1):
        cv = made.get((url, mip)) or FakeCV()
        made[(url, mip)] = cv
        return cv

    monkeypatch.setattr(cloud, "open_cv", _open)
    return FakeCV


def test_url_and_roi_parsing():
    assert cloud.normalize_url("gs://b/p") == "precomputed://gs://b/p"
    assert cloud.normalize_url("https://storage.googleapis.com/h01-release/data/x/") == "precomputed://gs://h01-release/data/x"
    assert cloud.normalize_url("precomputed://gs://b/p") == "precomputed://gs://b/p"
    with pytest.raises(ValueError):
        cloud.normalize_url("/local/dir")
    assert cloud.is_cloud("gs://b") and cloud.is_cloud("https://x/y") and not cloud.is_cloud("/tmp/x")
    assert cloud.parse_roi_arg("10-20,30-40,5-9") == {"x": [10, 20], "y": [30, 40], "z": [5, 9]}
    assert cloud.parse_roi_arg("z=5-9,x=10-20,y=30-40") == {"x": [10, 20], "y": [30, 40], "z": [5, 9]}
    assert cloud.align_outward(246000, 246700, 128) == (245888, 246784)
    with pytest.raises(ValueError):
        cloud.parse_roi_arg("10-20,30-40")


def test_reader_shape_alignment_and_chunk_cache(fake_cv, tmp_path):
    roi = {"x": [1000, 1500], "y": [2000, 2500], "z": [64, 100]}
    r = cloud.CloudVolumeReader("gs://b/em", roi, mip=1, cache_dir=tmp_path, align=True)
    # xy snapped outward to the 128 grid, z left alone (aligning z would pull in sections outside the ROI)
    assert r.roi == (896, 1536, 1920, 2560, 64, 100)
    assert r.shape == (36, 640, 640) and r.info.dtype == "uint8"
    d = r.describe()
    assert d["extent_um"] == [5.12, 5.12, 1.19] and d["lossy"] is True and d["chunk_xyz"] == [128, 128, 32]

    a = r.read_slice(0)
    assert a.shape == (640, 640)
    n_after_first = len(fake_cv.calls)
    assert n_after_first == 1
    for z in range(1, 32):  # same z-chunk -> no further fetches
        r.read_slice(z)
    assert len(fake_cv.calls) == n_after_first
    r.read_slice(32)  # crosses into the next z-chunk
    assert len(fake_cv.calls) == n_after_first + 1
    # the values carry absolute coordinates, so a wrong z offset would show up here
    exp = ((896 * 7 + 1920 * 3 + 64 * 11) % 251)
    assert int(a[0, 0]) == exp
    assert int(r.read_slice(5)[0, 0]) == ((896 * 7 + 1920 * 3 + 69 * 11) % 251)
    with pytest.raises(IndexError):
        r.read_slice(36)


def test_reader_disk_cache_survives_a_new_reader(fake_cv, tmp_path):
    roi = {"x": [0, 256], "y": [0, 256], "z": [0, 8]}
    cloud.CloudVolumeReader("gs://b/em", roi, mip=0, cache_dir=tmp_path).read_slice(0)
    n = len(fake_cv.calls)
    r2 = cloud.CloudVolumeReader("gs://b/em", roi, mip=0, cache_dir=tmp_path)
    r2.read_slice(0)
    assert len(fake_cv.calls) == n  # served from the on-disk chunk


def test_roi_outside_the_volume_is_rejected(fake_cv, tmp_path):
    with pytest.raises(ValueError, match="outside the volume"):
        cloud.CloudVolumeReader("gs://b/em", {"x": [0, 256], "y": [0, 256], "z": [0, 999]}, cache_dir=tmp_path)


def test_precheck_uses_per_axis_divisors(monkeypatch):
    """masking is 64/64/66 nm against an 8/8/33 nm ROI -> divide x,y by 8 but z only by 2.
    One divisor for all three axes reads a different part of the volume and does not raise."""
    from emqc.crawl import precheck

    class MaskCV(FakeCV):
        def __init__(self):
            super().__init__(size=(512, 512, 128), chunk=(64, 64, 64), res=(64.0, 64.0, 66.0), dtype="uint64", encoding="compressed_segmentation")

        def __getitem__(self, sl):
            a = super().__getitem__(sl)
            out = np.full(a.shape, 1, dtype=np.uint64)  # neuropil
            out[..., : max(1, a.shape[2] // 10), :] = 7  # a slab of fissure
            return out

    monkeypatch.setattr(precheck, "open_cv", lambda url, mip=0: MaskCV())
    p = precheck.tissue_profile({"x": [0, 800], "y": [0, 800], "z": [0, 20]})
    assert p["divisors_xyz"] == [8, 8, 2]
    assert p["mask_shape_xyz"] == [100, 100, 10]
    assert p["composition"]["neuropil"] > 0.5 and "fissure" in p["composition"]
    assert p["verdict"] == "reject" and any("fissure" in r for r in p["reasons"])
    ok = precheck.tissue_profile({"x": [0, 800], "y": [0, 800], "z": [0, 20]}, max_defect=0.5)
    assert ok["verdict"] == "ok" and not ok["reasons"]


def test_register_roi_and_run_qc_against_it(fake_cv, tmp_path, monkeypatch):
    from sqlalchemy import select

    from emqc.config import settings
    from emqc.crawl.register import register_cloud_roi
    from emqc.db import init_db, session_scope
    from emqc.db.models import Block, Dataset
    from emqc.qc.runner import open_dataset_volume, run_sync

    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    init_db()
    roi = {"x": [1000, 1500], "y": [2000, 2500], "z": [0, 24]}
    with session_scope() as s:
        ds = register_cloud_roi(s, "cloud_roi_test", "gs://b/em", roi, mip=1, species="human",
                                assets=[{"type": "gt_segmentation", "url": "gs://b/seg", "mip": 0, "format": "precomputed_cloud"}])
        assert ds.em_format == "precomputed_cloud" and ds.root_path == "precomputed://gs://b/em"
        assert (ds.size_z, ds.size_y, ds.size_x) == (24, 640, 640)
        m = ds.metadata_json
        assert m["roi"] == {"x": [896, 1536], "y": [1920, 2560], "z": [0, 24]}  # stored ROI is the one actually read
        assert m["roi_requested"]["x"] == [1000, 1500] and m["mip"] == 1 and m["lossy"] is True
        assert ds.voxel_size_x_nm == 8.0 and ds.voxel_size_z_nm == 33.0
        assert [a.asset_type for a in ds.assets] == ["gt_segmentation"]
        blocks = list(s.scalars(select(Block).where(Block.dataset_id == "cloud_roi_test")))
        assert blocks and all(b.source_volume == "precomputed://gs://b/em#mip1" for b in blocks)

    with session_scope() as s:  # the reader the pipeline gets back matches the declared shape
        r = open_dataset_volume(s.get(Dataset, "cloud_roi_test"))
        assert r.shape == (24, 640, 640) and r.read_slice(0).shape == (640, 640)

    run_id = run_sync("cloud_roi_test")
    with session_scope() as s:
        ds = s.get(Dataset, "cloud_roi_test")
        assert ds.latest_run_id == run_id and ds.latest_grade in ("A", "B", "C", "D")
        assert ds.latest_retention_rate is not None


def test_crawl_job_materialises_and_resumes(fake_cv, tmp_path, monkeypatch):
    from emqc.config import settings
    from emqc.crawl.register import CrawlRunner, create_crawl_job
    from emqc.db import init_db, session_scope
    from emqc.db.models import CrawlEvent, CrawlJob

    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    init_db()
    roi = {"x": [0, 256], "y": [0, 256], "z": [0, 6]}
    out = tmp_path / "out"
    jid = create_crawl_job("crawl_test", "gs://b/em", roi, mip=0, out_dir=str(out))
    res = CrawlRunner(jid).run()
    assert res["status"] == "done" and res["n_sections"] == 6
    pngs = sorted((out / "em").glob("z*.png"))
    assert len(pngs) == 6 and (out / "dataset.json").is_file()
    with session_scope() as s:
        j = s.get(CrawlJob, jid)
        assert j.n_done == 6 and j.n_bytes > 0 and j.wire_json["chunks_fetched"] >= 1
        assert any("crawl done" in e.message for e in s.scalars(__import__("sqlalchemy").select(CrawlEvent).where(CrawlEvent.job_id == jid)))
    n_calls = len(fake_cv.calls)
    jid2 = create_crawl_job("crawl_test", "gs://b/em", roi, mip=0, out_dir=str(out))
    res2 = CrawlRunner(jid2).run()
    assert res2["status"] == "done" and len(list((out / "em").glob("z*.png"))) == 6
    assert len(fake_cv.calls) == n_calls  # everything reused, nothing re-downloaded
