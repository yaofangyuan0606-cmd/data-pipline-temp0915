"""LangGraph 工作流：和 for 循环结果一致、节点按图走、取消 / 出错走对分支、接口给页面用的数据完整。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from emqc.db import session_scope
from emqc.db.models import QCBlock, QCRun, QCRunEvent, QCSlice
from emqc.qc import workflow as wf

pytestmark = pytest.mark.skipif(not wf.HAVE_LANGGRAPH, reason="langgraph not installed")


def _trace(run_id):
    with session_scope() as s:
        return [e.data_json for e in s.scalars(select(QCRunEvent).where(QCRunEvent.run_id == run_id, QCRunEvent.stage == wf.WORKFLOW_STAGE).order_by(QCRunEvent.id))]


def _run_with(engine, dataset_id, monkeypatch, **kw):
    from emqc.config import settings
    from emqc.qc.runner import QCRunner

    monkeypatch.setattr(settings, "workflow_engine", engine)
    return QCRunner().run_dataset(dataset_id, **kw)


def test_routing_functions():
    assert wf.route_after_next_group({"group_idx": 0, "n_groups": 2}) == "ingest"
    assert wf.route_after_next_group({"group_idx": 2, "n_groups": 2}) == "finalize"
    assert wf.route_after_next_group({"group_idx": 0, "n_groups": 2, "outcome": "cancelled"}) == "cancel"
    assert wf.route_after_persist({"tile_idx": 1, "n_tiles": 4}) == "slice_qc"   # more tiles in this z range
    assert wf.route_after_persist({"tile_idx": 4, "n_tiles": 4}) == "group_done"


def test_topology_comes_from_the_compiled_graph():
    t = wf.topology()
    ids = {n["id"] for n in t["nodes"]}
    assert ids == {"__start__", "__end__", *wf.NODES}
    edges = {(e["source"], e["target"]): e for e in t["edges"]}
    assert edges[("next_group", "ingest")]["conditional"] and edges[("persist", "slice_qc")]["conditional"]
    assert edges[("persist", "slice_qc")]["back"] and edges[("group_done", "next_group")]["back"]   # the two loops
    assert not edges[("ingest", "slice_qc")]["back"]
    assert "graph" in (t["mermaid"] or "")
    pos = {n["id"]: (n["rank"], n["row"]) for n in t["nodes"]}
    main = ["__start__", "prepare", "next_group", "ingest", "slice_qc", "serial_qc", "aggregate", "persist", "group_done"]
    assert [pos[n] for n in main] == [(i, 0) for i in range(len(main))]       # the main path is one straight lane
    assert pos["finalize"] == (3, 1) and pos["cancel"] == (3, 2) and pos["__end__"] == (9, 1)


def test_langgraph_and_loop_engines_give_identical_results(registered, monkeypatch):
    a = _run_with("loop", registered, monkeypatch)
    b = _run_with("langgraph", registered, monkeypatch)
    with session_scope() as s:
        ra, rb = s.get(QCRun, a), s.get(QCRun, b)
        assert ra.config_json["engine"] == "loop" and rb.config_json["engine"] == "langgraph"
        assert ra.status == rb.status == "done"
        assert ra.retention_rate == rb.retention_rate and ra.quality_score == pytest.approx(rb.quality_score)
        assert ra.grade_counts_json == rb.grade_counts_json
        key = lambda x: x.block_id
        ba = sorted(s.scalars(select(QCBlock).where(QCBlock.run_id == a)), key=key)
        bb = sorted(s.scalars(select(QCBlock).where(QCBlock.run_id == b)), key=key)
        assert [(x.block_id, x.grade, x.n_passed, x.failure_types) for x in ba] == [(x.block_id, x.grade, x.n_passed, x.failure_types) for x in bb]
        sa = {x.z: (x.passed, x.max_severity, x.failure_types) for x in s.scalars(select(QCSlice).where(QCSlice.run_id == a))}
        sb = {x.z: (x.passed, x.max_severity, x.failure_types) for x in s.scalars(select(QCSlice).where(QCSlice.run_id == b))}
        assert sa == sb
    assert _trace(a) == []          # the loop engine writes no workflow records


def test_nodes_execute_in_graph_order(registered, monkeypatch):
    run_id = _run_with("langgraph", registered, monkeypatch)
    tr = _trace(run_id)
    enters = [d["node"] for d in tr if d["phase"] == "enter"]
    per_group = ["next_group", "ingest", "slice_qc", "serial_qc", "aggregate", "persist", "group_done"]
    assert enters == ["prepare", *per_group, *per_group, "next_group", "finalize"]   # 2 z ranges x 1 tile
    # every enter has a matching exit, steps are consecutive, routers recorded where they went
    assert [d["node"] for d in tr if d["phase"] == "exit"] == enters
    assert [d["step"] for d in tr if d["phase"] == "enter"] == list(range(1, len(enters) + 1))
    nexts = [d.get("next") for d in tr if d["phase"] == "exit" and d["node"] in ("next_group", "persist")]
    assert nexts == ["ingest", "group_done", "ingest", "group_done", "finalize"]
    ingest = [d for d in tr if d["node"] == "ingest" and d["phase"] == "enter"]
    assert [(d["group"], d["z_start"], d["z_end"]) for d in ingest] == [(0, 0, 20), (1, 20, 40)]


def test_cancel_takes_the_cancel_branch(registered, monkeypatch):
    from emqc.config import settings

    with session_scope() as s:
        run = QCRun(dataset_id=registered, pipeline_version=settings.pipeline_version, status="queued", cancel_requested=True, config_json={})
        s.add(run)
        s.flush()
        rid = run.id
    _run_with("langgraph", registered, monkeypatch, run_id=rid)
    with session_scope() as s:
        assert s.get(QCRun, rid).status == "cancelled"
    assert [d["node"] for d in _trace(rid) if d["phase"] == "enter"] == ["prepare", "next_group", "cancel"]


def test_a_failing_node_is_recorded_and_fails_the_run(registered, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("serial check exploded")

    monkeypatch.setattr(wf, "stage_checks", lambda ctx, checks, name: boom() if name == "serial_qc" else None)
    with pytest.raises(RuntimeError):
        _run_with("langgraph", registered, monkeypatch)
    with session_scope() as s:
        run = s.scalars(select(QCRun).where(QCRun.dataset_id == registered).order_by(QCRun.id.desc())).first()
        assert run.status == "error"
        rid = run.id
    tr = _trace(rid)
    err = [d for d in tr if d["phase"] == "error"]
    assert len(err) == 1 and err[0]["node"] == "serial_qc" and "exploded" in err[0]["error"]


def test_workflow_endpoints(registered, monkeypatch):
    from emqc.api.app import app

    run_id = _run_with("langgraph", registered, monkeypatch)
    c = TestClient(app)
    topo = c.get("/api/v1/qc/workflow").json()
    assert topo["available"] and len(topo["nodes"]) == len(wf.NODES) + 2
    r = c.get(f"/api/v1/qc/runs/{run_id}/workflow").json()
    assert r["engine"] == "langgraph" and r["status"] == "done" and r["active"] is None
    assert r["nodes"]["slice_qc"]["visits"] == 2 and r["nodes"]["prepare"]["visits"] == 1 and r["nodes"]["finalize"]["phase"] == "exit"
    assert r["n_steps"] == 17 and len(r["trace"]) == 34
    # incremental polling: nothing new after the last id
    again = c.get(f"/api/v1/qc/runs/{run_id}/workflow", params={"after_id": r["last_id"]}).json()
    assert again["trace"] == [] and again["last_id"] == r["last_id"]
    # the plain log does not get flooded with node records, but they are there on request
    log = c.get(f"/api/v1/qc/runs/{run_id}/events").json()
    assert log and all(e["stage"] != "workflow" for e in log)
    assert len(c.get(f"/api/v1/qc/runs/{run_id}/events", params={"stage": "workflow"}).json()) == 34
    # the existing per-tile execution graph is unaffected
    g = c.get(f"/api/v1/qc/runs/{run_id}/graph").json()
    assert all(t["nodes"]["persist"]["status"] == "done" for grp in g["groups"] for t in grp["tiles"])


@pytest.fixture(scope="module")
def tiled(registered):
    """The sample volume registered a second time with 64 px XY tiles: 2 z ranges x 4 tiles."""
    from emqc.db.models import Block, Dataset
    from emqc.registry.scanner import make_blocks

    with session_scope() as s:
        src = s.get(Dataset, registered)
        if s.get(Dataset, "synthetic_tiled") is None:
            cols = {c.key: getattr(src, c.key) for c in Dataset.__mapper__.column_attrs if c.key not in ("dataset_id", "latest_run_id")}
            cols.update(status="registered", latest_grade=None, latest_quality_score=None, latest_retention_rate=None)
            s.add(Dataset(dataset_id="synthetic_tiled", **cols))
            s.flush()
            s.add_all([Block(dataset_id="synthetic_tiled", **b) for b in make_blocks(src.size_z, src.size_y, src.size_x, 20, 64)])
    return "synthetic_tiled"


def test_tiles_loop_back_through_slice_qc(tiled, monkeypatch):
    a = _run_with("loop", tiled, monkeypatch)
    b = _run_with("langgraph", tiled, monkeypatch)
    with session_scope() as s:
        key = lambda x: x.block_id
        qa = sorted(s.scalars(select(QCBlock).where(QCBlock.run_id == a)), key=key)
        qb = sorted(s.scalars(select(QCBlock).where(QCBlock.run_id == b)), key=key)
        assert len(qb) == 8
        assert [(x.block_id, x.grade, x.n_passed, x.retention_rate, x.failure_types) for x in qa] == [(x.block_id, x.grade, x.n_passed, x.retention_rate, x.failure_types) for x in qb]
        assert s.get(QCRun, b).n_blocks_done == 8
    tr = _trace(b)
    enters = [d["node"] for d in tr if d["phase"] == "enter"]
    tile = ["slice_qc", "serial_qc", "aggregate", "persist"]
    group = ["next_group", "ingest", *tile * 4, "group_done"]
    assert enters == ["prepare", *group, *group, "next_group", "finalize"]
    persist_next = [d["next"] for d in tr if d["phase"] == "exit" and d["node"] == "persist"]
    assert persist_next == ["slice_qc"] * 3 + ["group_done"] + ["slice_qc"] * 3 + ["group_done"]
    # each per-tile node knows which tile it worked on
    sq = [(d["group"], d["tile"], d["block"]) for d in tr if d["phase"] == "enter" and d["node"] == "slice_qc"]
    assert [t for _, t, _ in sq] == [0, 1, 2, 3, 0, 1, 2, 3] and all(b_.endswith(("_x00000", "_x00064")) for *_, b_ in sq)
