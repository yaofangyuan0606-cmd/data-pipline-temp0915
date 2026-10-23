"""QC 运行的 LangGraph 工作流。

一次 QC 运行就是下面这张状态图走一遍（↺ 是回边）：

    START → prepare → next_group ─┬─(还有 z 段)→ ingest → slice_qc → serial_qc → aggregate → persist ─┬─(下一个 tile)↺ slice_qc
                         ↑        ├─(全部完成)→ finalize → END                                       └─(本段完)→ group_done ↺ next_group
                         │        └─(已请求取消)→ cancel → END
                         └──────────────────────────────────────────────────────────────────────────────────────┘

和原来的 for 循环相比，做的事、写进数据库的结果一样（同一个 z 段的所有 tile 共享一次解码，每个 tile 各自检查、汇总、落库），
多出来的是：每个节点进入 / 离开都由 LangGraph 的 tasks 流报出来，记成 stage="workflow" 的运行事件，
页面据此把图上的节点一个个点亮（见 /api/v1/qc/runs/{id}/workflow 与 app.js 的 WorkflowView）。

图状态（WFState）只放轻量的游标和计数，读卷器、BlockContext、数据库会话这些重对象放在 QCWorkflow 实例上，
所以状态可以原样序列化进事件，将来要接 checkpointer（断点续跑）也不用改节点。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypedDict

from sqlalchemy.orm import Session

from emqc.db.models import Block, Dataset, QCRun, QCRunEvent

from .base import BlockContext
from .stages import stage_aggregate, stage_checks, stage_ingest_group

log = logging.getLogger(__name__)

try:  # langgraph 是可选依赖：没装时 runner 退回 for 循环
    from langgraph.graph import END, START, StateGraph

    HAVE_LANGGRAPH = True
except Exception:  # pragma: no cover - exercised only without the package
    HAVE_LANGGRAPH = False
    START, END = "__start__", "__end__"

WORKFLOW_STAGE = "workflow"  # QCRunEvent.stage of node enter / exit records

NODES = ("prepare", "next_group", "ingest", "slice_qc", "serial_qc", "aggregate", "persist", "group_done", "finalize", "cancel")
NODE_ZH = {
    "__start__": "开始", "__end__": "结束",
    "prepare": "准备 · 打开数据", "next_group": "取下一个 z 段", "ingest": "读取 · 统计", "slice_qc": "切片检查",
    "serial_qc": "序列检查", "aggregate": "汇总 · 分级", "persist": "落库", "group_done": "本段完成",
    "finalize": "数据集收尾", "cancel": "取消",
}
# conditional edge labels: (source, target) -> label
EDGE_LABELS = {
    ("next_group", "ingest"): "还有 z 段", ("next_group", "finalize"): "全部完成", ("next_group", "cancel"): "已请求取消",
    ("persist", "slice_qc"): "下一个 tile", ("persist", "group_done"): "本段 tile 完",
}


class WFState(TypedDict, total=False):
    group_idx: int          # index of the z range being processed (-1 before the first)
    n_groups: int
    z_start: int
    z_end: int
    tile_idx: int           # index of the tile inside the current z range
    n_tiles: int
    block_id: str | None    # tile the next per-tile node works on
    n_blocks_done: int
    outcome: str            # running | cancelled | done


# ----------------------------------------------------------------------------- routing (pure functions of the state)


def route_after_next_group(st: WFState) -> str:
    if st.get("outcome") == "cancelled":
        return "cancel"
    if st.get("group_idx", 0) >= st.get("n_groups", 0):
        return "finalize"
    return "ingest"


def route_after_persist(st: WFState) -> str:
    return "slice_qc" if st.get("tile_idx", 0) < st.get("n_tiles", 0) else "group_done"


def build_graph(impl: dict[str, Callable[[WFState], dict]] | None = None):
    """Compile the QC state graph. Without `impl` every node is a no-op (topology only)."""
    if not HAVE_LANGGRAPH:
        raise RuntimeError("langgraph is not installed (pip install langgraph)")
    impl = impl or {n: (lambda st: {}) for n in NODES}
    g = StateGraph(WFState)
    for n in NODES:
        g.add_node(n, impl[n])
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "next_group")
    g.add_conditional_edges("next_group", route_after_next_group, {"ingest": "ingest", "finalize": "finalize", "cancel": "cancel"})
    g.add_edge("ingest", "slice_qc")
    g.add_edge("slice_qc", "serial_qc")
    g.add_edge("serial_qc", "aggregate")
    g.add_edge("aggregate", "persist")
    g.add_conditional_edges("persist", route_after_persist, {"slice_qc": "slice_qc", "group_done": "group_done"})
    g.add_edge("group_done", "next_group")
    g.add_edge("finalize", END)
    g.add_edge("cancel", END)
    return g.compile()


_TOPOLOGY: dict | None = None


def topology() -> dict:
    """Nodes / edges of the compiled graph (read back from LangGraph itself) plus a layered layout for the UI.

    rank = column (BFS distance from START, END pushed to the last column), row = order of discovery within a column.
    Edges that go to an earlier or the same column are back edges (loops) and are drawn as arcs."""
    global _TOPOLOGY
    if _TOPOLOGY is not None:
        return _TOPOLOGY
    if not HAVE_LANGGRAPH:
        return {"available": False, "nodes": [], "edges": []}
    cg = build_graph()
    gr = cg.get_graph()
    node_ids = list(gr.nodes.keys())
    edges = [(e.source, e.target, bool(e.conditional)) for e in gr.edges]
    out_of: dict[str, list[str]] = {}
    for a, b, _ in edges:
        out_of.setdefault(a, []).append(b)
    rank = {START: 0}
    order = [START]
    for n in order:  # BFS
        for m in out_of.get(n, []):
            if m not in rank and m != END:
                rank[m] = rank[n] + 1
                order.append(m)
    last = max(rank.values()) + 1
    rank[END] = last
    rows: dict[int, int] = {}
    row = {}
    seq = [START, *NODES, END]  # within a column the main path comes first: ingest, then finalize, then cancel
    for n in sorted(order + [END], key=lambda n: (rank[n], seq.index(n) if n in seq else len(seq))):
        r = rank[n]
        row[n] = rows.get(r, 0)
        rows[r] = row[n] + 1
    row[END] = row.get("finalize", 1)  # END sits on the finalize lane
    nodes = [{"id": n, "label": NODE_ZH.get(n, n), "rank": rank.get(n, 0), "row": row.get(n, 0), "terminal": n in (START, END)} for n in node_ids]
    eds = [{"source": a, "target": b, "conditional": c, "label": EDGE_LABELS.get((a, b)), "back": rank.get(b, 0) <= rank.get(a, 0)} for a, b, c in edges]
    try:
        mermaid = cg.get_graph().draw_mermaid()
    except Exception:  # pragma: no cover
        mermaid = None
    _TOPOLOGY = {"available": True, "engine": "langgraph", "nodes": nodes, "edges": eds, "mermaid": mermaid, "n_ranks": last + 1, "n_rows": max(rows.values())}
    return _TOPOLOGY


# ----------------------------------------------------------------------------- one run = one graph execution


class QCWorkflow:
    """Run-scoped resources + node implementations. `execute()` streams the graph and records every node enter / exit."""

    def __init__(self, runner, s: Session, run: QCRun, ds: Dataset, info, blocks: list[Block], started: float, prev_status: str | None):
        self.runner, self.s, self.run, self.ds, self.info = runner, s, run, ds, info
        self.blocks, self.started, self.prev_status = blocks, started, prev_status
        self.reader = None
        self.groups: list[tuple[int, int, list[Block]]] = []
        self.ctxs: list[BlockContext] = []
        self.results: dict[int, Any] = {}
        self.on_stage = None
        self.cpu0 = self.cpu_ing = self.cpu_tile0 = 0.0
        self.t_group = 0.0
        self.step = 0

    # ---- nodes ---------------------------------------------------------------------------------------------
    def prepare(self, st: WFState) -> dict:
        from .runner import open_dataset_volume

        self.reader = open_dataset_volume(self.ds)
        by_z: dict[tuple[int, int], list[Block]] = {}
        for b in self.blocks:  # all XY tiles of one z range are processed together (sections decoded once)
            by_z.setdefault((b.z_start, b.z_end), []).append(b)
        self.groups = [(z0, z1, grp) for (z0, z1), grp in by_z.items()]
        return {"group_idx": -1, "n_groups": len(self.groups), "n_blocks_done": 0, "outcome": "running"}

    def next_group(self, st: WFState) -> dict:
        from .runner import _rusage

        gi = st.get("group_idx", -1) + 1
        self.s.refresh(self.run)
        if self.run.cancel_requested:
            return {"group_idx": gi, "outcome": "cancelled"}
        if gi >= len(self.groups):
            return {"group_idx": gi, "block_id": None}
        z0, z1, grp = self.groups[gi]
        for b in grp:
            b.status = "running"
        self.s.commit()
        self.cpu0, _ = _rusage()
        self.t_group = time.perf_counter()
        self.on_stage = self.runner.stage_notifier(self.s, self.run, z0, z1, len(grp), self.cpu0, self.t_group)
        self.ctxs, self.results = [], {}
        return {"group_idx": gi, "z_start": z0, "z_end": z1, "n_tiles": len(grp), "tile_idx": 0, "block_id": grp[0].block_id}

    def ingest(self, st: WFState) -> dict:
        from .runner import _rusage, block_info

        _, _, grp = self.groups[st["group_idx"]]
        self.on_stage("ingest", None)
        self.ctxs = [BlockContext(self.info, block_info(b), self.runner.config) for b in grp]
        stage_ingest_group(self.ctxs, self.reader)
        self.cpu_ing, _ = _rusage()
        return {}

    def _tile(self, st: WFState) -> BlockContext:
        return self.ctxs[st["tile_idx"]]

    def slice_qc(self, st: WFState) -> dict:
        from .runner import _rusage

        ctx = self._tile(st)
        self.cpu_tile0, _ = _rusage()
        self.on_stage("slice_qc", ctx.block.block_id)
        stage_checks(ctx, self.runner.slice_checks, "slice_qc")
        return {}

    def serial_qc(self, st: WFState) -> dict:
        ctx = self._tile(st)
        self.on_stage("serial_qc", ctx.block.block_id)
        stage_checks(ctx, self.runner.serial_checks, "serial_qc")
        return {}

    def aggregate(self, st: WFState) -> dict:
        ctx = self._tile(st)
        self.on_stage("aggregate", ctx.block.block_id)
        self.results[st["tile_idx"]] = stage_aggregate(ctx, [*self.runner.slice_checks, *self.runner.serial_checks], self.runner.preview_root)
        return {}

    def persist(self, st: WFState) -> dict:
        from .runner import _rusage

        ti = st["tile_idx"]
        z0, z1, grp = self.groups[st["group_idx"]]
        ctx, res, b = self.ctxs[ti], self.results.pop(ti), grp[ti]
        self.on_stage("persist", b.block_id)
        cpu, rss = _rusage()
        # this tile's CPU = its share of the group's shared decode + what its own checks / aggregation cost
        ctx.resource = {"cpu_time_s": round((self.cpu_ing - self.cpu0) / len(grp) + (cpu - self.cpu_tile0), 2), "peak_rss_mb": round(rss, 1)}
        self.runner.persist_block(self.s, self.run, ctx, res, b)
        self.runner.block_done_event(self.s, self.run, b, ctx, res, z0, z1)
        nxt = ti + 1
        return {"tile_idx": nxt, "n_blocks_done": st.get("n_blocks_done", 0) + 1, "block_id": grp[nxt].block_id if nxt < len(grp) else None}

    def group_done(self, st: WFState) -> dict:
        z0, z1, grp = self.groups[st["group_idx"]]
        self.runner.group_done_event(self.s, self.run, z0, z1, len(grp), self.cpu0, self.t_group)
        self.ctxs = []  # free the group's thumbnails before the next z range
        return {}

    def finalize(self, st: WFState) -> dict:
        self._close_reader()
        self.runner.finalize(self.s, self.run, self.ds, self.started)
        self.runner.finished_event(self.s, self.run, self.ds, self.started)
        return {"outcome": "done"}

    def cancel(self, st: WFState) -> dict:
        self._close_reader()
        self.runner.cancel_run(self.s, self.run, self.ds, self.blocks, self.prev_status)
        return {"outcome": "cancelled"}

    def _close_reader(self) -> None:
        if self.reader is not None:
            try:
                self.reader.close()
            finally:
                self.reader = None

    # ---- execution + recording --------------------------------------------------------------------------
    def _record(self, node: str, phase: str, st: dict | None, **extra) -> None:
        st = st or {}
        data = {"node": node, "phase": phase, "step": self.step}
        for k_state, k_data in (("group_idx", "group"), ("n_groups", "n_groups"), ("tile_idx", "tile"), ("n_tiles", "n_tiles"), ("z_start", "z_start"), ("z_end", "z_end"), ("block_id", "block")):
            if st.get(k_state) is not None:
                data[k_data] = st[k_state]
        data.update(extra)
        msg = f"{'→' if phase == 'enter' else '✓' if phase == 'exit' else '✗'} {node}"
        level = "error" if phase == "error" else "info"
        self.s.add(QCRunEvent(run_id=self.run.id, level=level, stage=WORKFLOW_STAGE, block_id=None, message=msg, data_json=data))
        self.s.commit()

    def execute(self) -> WFState:
        graph = build_graph({n: getattr(self, n) for n in NODES})
        n_tiles = len(self.blocks)
        n_groups = len({(b.z_start, b.z_end) for b in self.blocks})
        # every group: next_group + ingest + group_done, every tile: 4 per-tile nodes; plus prepare / finalize / slack
        limit = 3 * n_groups + 4 * n_tiles + 25
        state: WFState = {}
        running: dict[str, tuple[str, float, dict]] = {}  # task id -> (node, t0, input state)
        try:
            for ev in graph.stream({}, config={"recursion_limit": limit}, stream_mode="tasks"):
                name = ev.get("name")
                if name not in NODES:
                    continue
                if "result" not in ev:  # task start: LangGraph is about to run this node
                    self.step += 1
                    inp = dict(ev.get("input") or {})
                    running[ev["id"]] = (name, time.perf_counter(), inp)
                    self._record(name, "enter", inp)
                    continue
                node, t0, inp = running.pop(ev["id"], (name, time.perf_counter(), {}))
                result = dict(ev.get("result") or {})
                state = {**inp, **result}
                nxt = None
                if node == "next_group":
                    nxt = route_after_next_group(state)
                elif node == "persist":
                    nxt = route_after_persist(state)
                if ev.get("error"):
                    self._record(node, "error", state, duration_s=round(time.perf_counter() - t0, 4), error=str(ev["error"])[:300])
                else:
                    self._record(node, "exit", state, duration_s=round(time.perf_counter() - t0, 4), **({"next": nxt} if nxt else {}))
        except Exception as e:
            for node, t0, inp in running.values():  # the node that raised never reported a result
                try:
                    self.s.rollback()
                    self._record(node, "error", inp, duration_s=round(time.perf_counter() - t0, 4), error=f"{type(e).__name__}: {e}"[:300])
                except Exception:  # pragma: no cover - the session itself may be broken; runner handles the run row
                    log.exception("could not record workflow error")
            raise
        finally:
            self._close_reader()
        return state
