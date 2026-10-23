# QC 运行的 LangGraph 工作流

每次 QC 运行都是一张 LangGraph 状态图走一遍（`emqc/qc/workflow.py`）。页面上（流水线控制台的运行卡片、运行详情页的「工作流」）能实时看到节点一个个点亮。

```
START → prepare → next_group ─┬─ 还有 z 段 → ingest → slice_qc → serial_qc → aggregate → persist ─┬─ 下一个 tile ↺ slice_qc
                      ↑       ├─ 全部完成 → finalize → END                                      └─ 本段 tile 完 → group_done ↺ next_group
                      │       └─ 已请求取消 → cancel → END
                      └──────────────────────────────────────────────────────────────────────────────────┘
```

| 节点 | 做什么 |
|---|---|
| prepare | 打开数据卷，按 z 段把 block 分组 |
| next_group | 检查取消标记，取下一个 z 段（条件边：ingest / finalize / cancel） |
| ingest | 本段所有 tile 共享一次解码，算统计与缩略图 |
| slice_qc / serial_qc / aggregate / persist | 对当前 tile 做切片检查、序列检查、汇总分级、落库；persist 之后条件边决定回到 slice_qc 做下一个 tile，还是进 group_done |
| group_done | 本段收尾，回到 next_group |
| finalize / cancel | 数据集级收尾（划分、等级、ETL 指标）/ 取消 |

## 和原来的 for 循环有什么不同

- 写进数据库的 QC 结果完全一样（`tests/test_workflow.py` 在整平面和分 tile 两种数据上逐 block、逐切片对比两个引擎）。
- 唯一的行为差别：tile 现在是「检查 → 汇总 → 落库」一个一个走完，而不是全部检查完再统一落库；所以每个 tile 落库更早，`cpu_time_s` 改为「本段共享解码的均摊 + 本 tile 自己的耗时」，比原来的平均值更准。
- 图状态只放游标和计数（第几段、第几个 tile、block id），重对象（读卷器、BlockContext、数据库会话）挂在 `QCWorkflow` 实例上。将来接 checkpointer 做断点续跑不需要改节点。

## 节点轨迹怎么来的

`QCWorkflow.execute()` 用 `graph.stream(..., stream_mode="tasks")`：LangGraph 在每个节点开始前、结束后各报一次，
每次记一条 `qc_run_events`（`stage = "workflow"`，`data_json` 里有 node、phase=enter/exit/error、step、group、tile、block、duration_s、路由去向 next）。
不加新表，MySQL 不用迁移。普通日志接口默认不返回这些记录（传 `stage=workflow` 才返回），老的「执行节点图」也不受影响。

## 接口

- `GET /api/v1/qc/workflow` 图的拓扑：从编译好的 LangGraph 读出的节点、边（是否条件边、是否回边）、分层布局、mermaid 源码。
- `GET /api/v1/qc/runs/{id}/workflow?after_id=N` 运行的轨迹：`trace`（id > N 的节点记录，增量轮询用 `last_id`）、每个节点的访问次数与耗时、当前正在执行的节点。

## 前端

`emqc/api/static/workflow.js` 的 `WorkflowView`：每 0.7 s 增量拉一次，新记录排队播放——目标节点高亮、刚走过的边上有个点跑过去、节点下的次数和累计耗时跟着涨；积压多时自动加速。跑完的运行有「回放」。点检查类节点会把下面的日志过滤到该阶段。

## 开关

`EMQC_WORKFLOW_ENGINE=langgraph`（默认）| `loop`。没装 langgraph 时自动退回 loop，并在日志里警告。每次运行用的引擎记在 `qc_runs.config_json.engine`。
