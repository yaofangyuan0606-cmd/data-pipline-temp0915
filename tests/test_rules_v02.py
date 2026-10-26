"""rules-v0.2 回归测试：每条修复一个用例，都在合成的 EM 样序列上跑完整的 ingest + 切片级 + 序列级检查。

覆盖：整张填充值记为缺失；读取失败重试并记为 read_error；细裂缝按长度判、贴边细条不算裂缝；荷电与污染扣掉
无数据区；大面积污染不再静默放行；切片级已判死的切片不再被序列检查重复判；逐格检查不隔片比较；永久性跳变 /
整体平移只在发生处报一次；局部形变的容差参数生效。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import emqc.qc.serial_checks  # noqa: F401,E402  (registers the checks)
import emqc.qc.slice_checks  # noqa: F401,E402
from emqc.qc.base import SERIAL_CHECKS, SLICE_CHECKS, BlockContext, BlockInfo, DatasetInfo, QCConfig, Severity, build_checks  # noqa: E402
from emqc.qc.stages import stage_checks, stage_ingest_group  # noqa: E402
from emqc.registry.readers import SliceIOError, VolumeInfo, VolumeReader  # noqa: E402
from scripts.make_sample_dataset import synth_em_stack  # noqa: E402

N, SIZE = 40, 512


class ArrayReader(VolumeReader):
    """In-memory volume; `fail[z] = n` makes the first n reads of z raise a read (I/O) error."""

    def __init__(self, vol: np.ndarray, fail: dict[int, int] | None = None):
        self._v = vol
        self.fs = None
        self.fail = dict(fail or {})
        self.info = VolumeInfo(shape=tuple(vol.shape), dtype=str(vol.dtype), fmt="array", path="mem://test")

    def read_slice(self, z: int) -> np.ndarray:
        if self.fail.get(z, 0) > 0:
            self.fail[z] -= 1
            raise SliceIOError(f"simulated network error at z={z}")
        return np.ascontiguousarray(self._v[z])


@pytest.fixture(scope="module")
def clean() -> np.ndarray:
    em, _ = synth_em_stack(N, SIZE, np.random.default_rng(7))
    return np.stack(em).astype(np.uint8)


def run(vol: np.ndarray, reader: VolumeReader | None = None) -> BlockContext:
    ds = DatasetInfo("t", tuple(vol.shape), "uint8", voxel_size_nm=(8.0, 8.0, 33.0))
    blk = BlockInfo("b", 0, vol.shape[0], 0, vol.shape[1], 0, vol.shape[2])
    cfg = QCConfig()
    ctx = BlockContext(ds, blk, cfg)
    stage_ingest_group([ctx], reader or ArrayReader(vol))
    stage_checks(ctx, build_checks(cfg, SLICE_CHECKS), "slice_qc")
    stage_checks(ctx, build_checks(cfg, SERIAL_CHECKS), "serial_qc")
    return ctx


def high(rec, check: str | None = None) -> list:
    return [f for f in rec.findings if f.severity >= Severity.HIGH and (check is None or f.check == check)]


def test_clean_stack_has_no_failures(clean):
    ctx = run(clean)
    bad = {r.z: [f.check for f in high(r)] for r in ctx.slices if high(r)}
    assert bad == {}, bad


def test_all_fill_slice_is_missing_only(clean):
    vol = clean.copy()
    vol[10] = 0
    ctx = run(vol)
    r = ctx.rec(10)
    assert r.status == "missing" and "填充值" in (r.error or "")
    assert {f.check for f in r.findings} == {"missing_slice"}, [f.check for f in r.findings]
    assert not high(ctx.rec(11)), [f.check for f in high(ctx.rec(11))]


def test_read_error_is_retried_then_reported_separately(clean):
    ctx = run(clean, ArrayReader(clean, fail={5: 1, 6: 99}))
    assert ctx.rec(5).ok  # 第一次失败、重试成功
    r6 = ctx.rec(6)
    assert r6.status == "corrupt" and r6.error.startswith("read_error")
    assert [f.failure_type for f in r6.findings] == ["read_error"]


def test_thin_crack_is_reported_by_length_and_edge_sliver_is_not_a_crack(clean):
    vol = clean.copy()
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    vol[15][np.abs(yy - xx) <= 1] = 0  # 3 px 宽、横穿整张的零值裂缝：面积约 0.6%
    vol[25][:, :8] = 0  # 贴左边 8 px 的空白细条：画布没填满
    ctx = run(vol)
    crack = [f for f in ctx.rec(15).findings if f.check == "crack"]
    assert crack and crack[0].failure_type == "crack" and crack[0].severity >= Severity.HIGH, crack
    assert not [f for f in ctx.rec(25).findings if f.failure_type == "crack"]


def test_charging_and_contamination_ignore_the_fill_region(clean):
    vol = clean.copy()
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    vol[20][yy + xx < 0.55 * SIZE] = 0  # 左上角约 15% 的无数据区
    ctx = run(vol)
    r = ctx.rec(20)
    assert not [f for f in r.findings if f.check in ("charging_artifact", "contamination")], [(f.check, f.severity.label) for f in r.findings]
    assert {f.failure_type for f in r.findings if f.check == "crack"} <= {"no_coverage", "missing_region"}


def test_widespread_contamination_is_flagged_for_review_not_passed(clean):
    vol = clean.copy()
    vol[30][:, : int(SIZE * 0.30)] = 250  # 30% 是平坦的极亮区：以前超过 25% 直接给 1.0 分
    ctx = run(vol)
    f = [x for x in ctx.rec(30).findings if x.check == "contamination"]
    assert f and f[0].details.get("widespread") and f[0].severity == Severity.MEDIUM, f


def test_slice_level_failure_is_not_judged_again_by_serial_checks(clean):
    vol = clean.copy()
    vol[20] = 128  # 均匀灰：空白切片
    ctx = run(vol)
    r20, r21 = ctx.rec(20), ctx.rec(21)
    assert {f.check for f in high(r20)} == {"blank_slice"}, [f.check for f in high(r20)]
    assert not high(r21), [f.check for f in high(r21)]
    assert "reference_too_far" in r21.stats.get("_notes", {}).get("local_misalignment", "")


def test_permanent_shift_and_jump_are_reported_once(clean):
    vol = clean.copy()
    vol[25:] = np.roll(vol[25:], 30, axis=2)  # 从 z25 起整体平移 30 px（240 nm）
    other, _ = synth_em_stack(N, SIZE, np.random.default_rng(99))
    vol[33:] = np.stack(other)[33:]  # 从 z33 起换成另一块组织：永久性跳变
    ctx = run(vol)
    assert high(ctx.rec(25), "global_misalignment")
    assert not high(ctx.rec(26), "global_misalignment")
    assert high(ctx.rec(33), "slice_jump")
    assert not high(ctx.rec(34), "slice_jump")


def test_local_deformation_tolerance_is_applied(clean):
    ctx = run(clean)
    det = [f.details for r in ctx.slices for f in r.findings if f.check == "local_deformation"]
    assert all(d.get("median_residual_after_affine_px", 0) < 2.0 for d in det), det


def test_local_shift_and_local_damage_are_reported(clean):
    vol = clean.copy()
    a, b = 128, 320
    vol[12][a:b, a:b] = np.roll(clean[12][a:b, a:b], 24, axis=1)  # 局部区域平移 24 px（192 nm）
    rng = np.random.default_rng(3)
    vol[28][: SIZE // 2, : SIZE // 2] = rng.integers(60, 200, (SIZE // 2, SIZE // 2), dtype=np.uint8)  # 左上 1/4 内容被毁
    ctx = run(vol)
    assert high(ctx.rec(12), "local_misalignment"), [(f.check, f.severity.label) for f in ctx.rec(12).findings]
    dmg = high(ctx.rec(28), "local_misalignment")
    assert dmg and dmg[0].failure_type in ("local_mismatch", "local_misalign"), [(f.check, f.failure_type, f.severity.label) for f in ctx.rec(28).findings]
    assert not high(ctx.rec(29), "local_misalignment")  # 下一张不因为和损坏的那张比而再报一次
