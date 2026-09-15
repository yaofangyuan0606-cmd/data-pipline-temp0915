# 规模化方案（先不做）：PB 级数据与多台 Mac mini 用 Ray 跑同一个数据集

> 2026-09-12 记录，作为 v0.2 之后的路线。当前 v0.1 是单机单进程、按 block 组顺序执行。

## 1. PB 级数据在服务器上要先准备什么

**存储与访问**
- 数据必须留在原地，计算搬过去。PB 级图像栈不可能 rsync，`EMQC_DATA_ROOT` 直接指向服务器上的挂载点（目前是 `//10.10.10.2/EM_DATA` 这类网络盘）。要先测挂载点的顺序读吞吐和小文件 IOPS：ssEM 图像栈是海量小 png，IOPS 往往比带宽先成为瓶颈。
- 优先把图像栈转成分块存储（Neuroglancer precomputed / zarr / n5，chunk 例如 1024×1024×1 或 512×512×64）。分块后读一个 tile 只碰它自己的 chunk，随机访问和并行读才可扩展；`readers.py` 已有 precomputed 读取器，zarr/n5 需要补。
- 元信息与结果库（MySQL）和数据分离部署；`qc_slices` 一行一张 tile-section，PB 级下是千万行量级，要提前做分区（按 dataset_id、run_id）和索引，并考虑把 `stats_json` 这类宽 JSON 列迁到列式存储或 Parquet 归档，只在 MySQL 留摘要。

**流水线形态**
- 单位仍是 block（1024²×100），一个 PB 级数据集有数十万个 block。调度粒度用"z 段 × 全部 tile"这一组（一次解码给所有 tile），组内 tile 并行。
- 全量 QC 是一次性的重活，之后只对新增 z 段增量跑。`persist_block` 已幂等（按 run+block 先删后插），支持断点续跑；需要补一个"run 级别的 block 清单与完成位图"，重启后跳过已完成的 block。
- 预览缩略图按需生成、只存 medium 以上，已是这个设计；PB 级下预览目录也要放对象存储或与数据同处。

**观测与配额**
- 每个 block 的 `cpu_time_s`、`peak_rss_mb`、`bytes_read` 已经落库，可以据此估算：mouse_30um 一个 1024²×100 的 tile 约 10 s、峰值内存几百 MB。按 8 nm/px、40 nm 切片，1 PB ≈ 10¹⁵ 体素 ≈ 10⁷ 个 block，单核要 3 年，必须多机。
- 需要限流：读源盘的并发数、单机内存上限（同时解码的切片数 × 单张大小）、结果库写入批量化。

## 2. 用 Ray 让多台 Mac mini 同时跑一个数据集

**为什么是 Ray**：Python 原生、按函数/actor 分发、带对象存储和重试，比自己写队列省事；Mac mini（Apple Silicon）作 worker 没有 GPU 需求，CPU 密集的解码和相位相关正合适。

**部署形态**
- 一台机器作 head（也可以就是服务器），每台 Mac mini `ray start --address=<head>:6379` 加入集群；所有机器挂同一个数据源（SMB/NFS/Taildrive 挂载同一路径），或都能直连对象存储；MySQL 只有一份，worker 直接写。
- 依赖用同一份 `requirements.txt`，Apple Silicon 上 numpy/scipy/pillow 都有 wheel；`cryptography<45` 的限制只针对 Intel Mac，Apple Silicon 不受影响。

**代码怎么改（改动面小，已有边界）**
1. `QCRunner.process_group(reader, ds_info, blocks)` 就是天然的任务单元：输入是数据集信息和一组 block（同一 z 段的全部 tile），输出是每个 tile 的 `BlockResult` 加缩略图。把它包成 `@ray.remote` 的函数，worker 内自己打开 reader（reader 不可跨进程传递，按路径重建）。
2. 调度器在 head 上：把一个 run 的所有 z 段组成任务列表，`ray.get` 按完成顺序回收结果，逐个调用 `persist_block`（写库集中在 head，避免几十个 worker 同时打 MySQL；或让 worker 直接写、head 只更新进度，二选一）。
3. 进度与日志：`qc_run_events` 已是事件流，worker 通过 head 的一个 actor 写事件，控制台不用改；节点图里每个 tile 节点再加"在哪台机器上跑"（`ray.get_runtime_context().get_node_id()`）和该机的 CPU/内存，`sysmon` 改成每个节点各采一份、head 汇总。
4. 缩略图：worker 写到共享目录或对象存储，路径写回结果；不能写本机磁盘。
5. 取消：`cancel_requested` 已有，调度器轮询后 `ray.cancel` 未开始的任务。
6. 容错：Ray 任务失败自动重试；`persist_block` 幂等保证重跑不重复写。

**规模估计与一个前置结论（2026-09-13 实测修正）**：mouse_30um 最近一次运行，单个 block 总耗时 25.5 s，其中 ingest（读取 + 解码 + 统计）占 23.5 s，即 **92%**；16 项检查合计只占 8%。同一 block 的 CPU 时间 11.2 s 对 25.5 s 墙钟，
整组平均只用到 **1.76 个核**，8 核机器利用率约 22%。单张 2048² png 的纯解码是 28 ms，100 张只有 2.8 s，也就是说 ingest 里约 20 s 是等 I/O。

结论有两条。第一，**这条流水线是 I/O 密集而不是 CPU 密集**，多机加速的上限取决于各节点到数据源的聚合读带宽，不取决于核数；十台 Mac mini 共用一条链路读同一个网络盘，只会把排队从一台挪到十台。
第二，**在上 Ray 之前，单机还有几倍的空间**：把切片解码放进线程池（PIL 解码会释放 GIL），或把同一 z 段的多个 tile 的统计量并行算，先把单机利用率从 22% 提上去，再重新测一次每 block 耗时，用新数字规划机器数。

按修正后的口径估：若把单机做到 4 tile/10 s，10 台且每台有独立读带宽约 4 tile/s，一天约 35 万 tile-block。PB 级仍需按数据集分批、只对训练相关区域全量 QC、推理区域抽样 QC，而不是逐字节全扫。

## 3. 建议的顺序

1. 先在服务器上原地跑（不拉数据），确认挂载读吞吐，补 zarr/n5 读取器。
2. 加 run 级 block 完成位图与断点续跑。
3. 把 `process_group` 包成 Ray 任务，先用两台机器跑 mouse_30um 验证结果与单机一致（`qc_slices` 逐行对比）。
4. 再考虑 MySQL 分区与结果归档。
