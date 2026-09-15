# HTTP API（v1）

启动：`.venv/bin/python -m emqc serve`（默认 `127.0.0.1:8765`），交互式文档在 `/docs`。

## 数据集注册表 `/api/v1/datasets`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/datasets?size_class=&status=` | 列表 |
| POST | `/api/v1/datasets/scan` | 扫描数据根目录，注册 / 更新数据集、资产、版本、blocks |
| GET | `/api/v1/datasets/{id}` | 详情（含 assets / versions / blocks / latest_run） |
| PATCH | `/api/v1/datasets/{id}` | 修改元信息（species、voxel_size_nm、size_class …） |
| DELETE | `/api/v1/datasets/{id}?remove_files=false` | 删除数据集及其全部 QC 结果、预览图；`remove_files=true` 连数据目录一起删（仅限 data_root 之下） |
| GET | `/api/v1/datasets/{id}/blocks` | blocks 及 split |
| GET / POST | `/api/v1/datasets/{id}/assets` | 资产列表 / 登记算法产出（prediction、skeleton …） |
| GET / POST | `/api/v1/datasets/{id}/versions` | 版本列表 / 记录 algo / model / experiment 版本 |

## QC `/api/v1/qc`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/qc/checks` | 16 个检查的目录（是否实现、阈值、参数） |
| POST | `/api/v1/qc/runs` `{dataset_id, block_ids?, config?, sync?}` | 启动一次 QC（默认后台线程，返回 run_id）；`config` 可覆盖 `checks_enabled`、`thresholds`、`params`、`pass_max_severity` 等 |
| POST | `/api/v1/qc/runs/batch` `{dataset_ids, block_ids?, config?}` | 一次启动多个数据集的运行（控制台用） |
| POST | `/api/v1/qc/runs/{run_id}/cancel` | 请求取消，当前 block 组结束后停止；已结束的运行返回 409 |
| GET | `/api/v1/qc/runs/active` | 排队 / 运行中的任务，带每个 block 的完成状态与当前阶段 |
| GET | `/api/v1/qc/runs/{run_id}/events?after_id=&stage=&block_id=&level=&q=` | 运行的实时日志（阶段切换、每个 block 结果、警告），增量拉取；可按节点（stage + block_id）、级别、关键词过滤；每条带结构化 `data`（z 段、耗时、CPU 时间、峰值内存、等级） |
| GET | `/api/v1/qc/runs/{run_id}/graph` | 执行节点图：每个 z 段一个 ingest 节点，每个 tile 的 slice_qc / serial_qc / aggregate / persist 节点，带状态、起止、耗时、日志条数 |
| GET | `/api/v1/system/metrics?last=` | 主机 CPU / 内存 / 负载、本进程、GPU（nvidia-smi）每 2 秒采样 |
| GET | `/api/v1/pipeline/status` | 控制台一屏所需：数据源配置、block 与抽样设置、数据集与运行计数、活动运行、上次扫描结果 |
| GET | `/api/v1/qc/runs?dataset_id=` | 运行列表 |
| GET | `/api/v1/qc/runs/{run_id}` | 运行摘要：进度、block 结果、数据集级 ETL 指标、findings 按严重度计数 |
| GET | `/api/v1/qc/runs/{run_id}/blocks` / `/slices?block_id=&with_stats=` / `/profile` / `/findings?…` / `/metrics?block_id=` | 明细 |
| GET | `/api/v1/qc/findings?dataset_id=&failure_type=&min_severity=&level=` | 各数据集最近一次运行的问题 |
| GET | `/api/v1/qc/datasets/{id}/latest` | 数据集最近一次运行摘要 |

## 算法侧实时取数 `/api/v1/data`（不预切、不落盘，按需从源卷读）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/data/{id}/info` | 形状、dtype、体素尺寸、blocks（含 tile 范围、grade、通过的 z 列表）、`slice_passed`（该 z 在所有 tile 都通过） |
| GET | `/api/v1/data/{id}/slice/{z}?fmt=npy|raw|png&y0&y1&x0&x1` | 单张切片（可裁剪） |
| GET | `/api/v1/data/{id}/cutout?z0&z1&y0&y1&x0&x1&fmt=npy|raw` | 任意 3-D 子块（≤ 512 MB） |
| POST | `/api/v1/data/{id}/patches/sample` | 按 QC 过滤随机采样 patch **坐标**（`only_passed`、`min_quality`、`split`、`block_ids`）；patch 不跨 block，z 窗口内所有切片在该 tile 通过；返回 block、grade 与 cutout URL |
| POST | `/api/v1/data/{id}/cutouts/batch` `{bboxes: [[z0,z1,y0,y1,x0,x1], …]}` | 一次取多个子块（npz） |
| GET | `/api/v1/data/{id}/preview/z/{z}.png` | 即时缩略图 |
| POST | `/api/v1/data/{id}/export` `{min_run, min_quality?, run_id?, block_ids?, with_assets?, fmt, out_dir?}` | 把通过 QC 的切片按 block 内的连续段导出为 npy 栈（或 png），写到平台主机的 `var/exports/<dataset>/`，返回 `export_manifest.json` 的内容（每段的来源 run、z 范围、bbox、等级、被排除的切片与原因）；`with_assets` 同时拷贝相同 z 的资产文件（如 `gt_segmentation`） |
| GET | `/api/v1/data/training/manifest` | 训练 / 推理清单：小数据集整体进训练；大数据集按等级分层抽出的 train_sample 块进训练（`sampling` 字段给出配额、实际选择、种子）、整卷进推理；`policy` 字段说明当前策略 |

所有取数请求都写入 `serve_log`。

### Python 示例
```python
import io, numpy as np, requests
API = "http://127.0.0.1:8765"
ds = "synthetic_defects"
info = requests.get(f"{API}/api/v1/data/{ds}/info").json()
# 1) 只在通过 QC 的连续切片里采 32 个 16×256×256 的训练 patch
res = requests.post(f"{API}/api/v1/data/{ds}/patches/sample",
                    json={"size": [16, 256, 256], "n": 32, "seed": 0, "only_passed": True, "split": "train_sample"}).json()
patches = [np.load(io.BytesIO(requests.get(API + p["url"]).content)) for p in res["patches"]]
# 2) 推理：整卷按 block 顺序取子块
for b in info["blocks"]:
    r = requests.get(f"{API}/api/v1/data/{ds}/cutout", params=dict(z0=b["z_start"], z1=b["z_end"], y0=0, y1=512, x0=0, x1=512, fmt="npy"))
    vol = np.load(io.BytesIO(r.content))  # (z, y, x)
# 3) 把算法产出登记回来
requests.post(f"{API}/api/v1/datasets/{ds}/assets", json={"asset_type": "model_prediction", "path": "pred_v2", "format": "zarr", "version": "v2", "model_version": "unet-2026-09-12"})
requests.post(f"{API}/api/v1/traces", json={"dataset_id": ds, "agent": "seg-agent", "step": "predict", "status": "ok", "model_version": "unet-2026-09-12", "output": {"blocks": len(info["blocks"])}})
```

## 数据交付 `/api/v1/exports` 与 `/api/v1/streams`
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/exports` `{dataset_id, run_id?, min_run, shard_z, min_quality?, with_assets?, block_ids?, resume, out_dir?, sync?}` | 新建导出任务（默认后台），写训练分片到 `var/exports/<dataset>/` |
| GET | `/api/v1/exports?dataset_id=&status=` · `/api/v1/exports/{id}` · `/{id}/events?after_id=` · `/{id}/manifest` · POST `/{id}/cancel` | 任务列表、进度、实时日志、清单、取消 |
| POST | `/api/v1/streams` `{dataset_id, z_chunk, order: z|grade, skip_failed, block_ids?, client?}` | 新建推理流会话：生成覆盖整卷、按 block 顺序的子块计划 |
| POST | `/api/v1/streams/{id}/next?n=` | 领取下 n 个子块（坐标 + 取数 URL），远程源顺带预取后续 |
| POST | `/api/v1/streams/{id}/ack` `{indices, ok}` · `/{id}/close` `{status}` | 确认处理结果、结束会话 |
| GET | `/api/v1/streams?dataset_id=&status=` · `/api/v1/streams/{id}` · `/{id}/plan` | 会话列表、进度与字节数、完整计划 |

`cutout` 带 `stream_id` 参数时，取数日志与会话字节数会关联到该会话。

## 数据采集 `/api/v1/crawl`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/crawl/presets` · `/volume?url=&mip=` | 内置数据源；卷的尺寸 / chunk / 分辨率 / 编码（不下载体素） |
| POST | `/api/v1/crawl/precheck` `{roi, mask_url, min_wanted, max_defect}` | 用组织类型图预判 ROI 是否值得爬 |
| GET | `/api/v1/crawl/index?tag=&min_voxels=&top=` | 从 segment_properties 选候选 |
| POST | `/api/v1/crawl/register` `{dataset_id, url, roi, mip, assets?, require_precheck_ok?}` | 注册 ROI 为数据集，不下载 |
| POST/GET | `/api/v1/crawl/jobs` · `/{id}` · `/{id}/events` · `/{id}/cancel` | 下载任务：进度、实时日志、取消 |

## Patch Factory / Training Data API
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/data/{ds}/labels` | 标签资产列表（含验证摘要、是否可出 patch） |
| POST | `/api/v1/datasets/{ds}/assets/{id}/validate?step=&max_sections=` | 验证一个标签卷与 EM 的一致性（缺失 / 为空 / 画在填充区 / 形状与层数 / 编码） |
| GET | `/api/v1/datasets/{ds}/assets/{id}/validation?only_flagged=` | 逐层结果 |
| POST | `/api/v1/datasets/{ds}/assets/validate-exclusive` `{asset_ids}` | 互斥类别掩码的重叠（label_conflict） |
| GET | `/api/v1/data/{ds}/labels/{id}/cutout?z0..x1&derive=ids|boundary|mask` | 与 EM cutout 同 bbox 的标签 cutout（npy） |
| GET / POST | `/api/v1/datasets/{ds}/partition` `{ratios?, seed?, force?}` | 查看 / 补齐 / 强制重排 holdout 划分 |
| GET | `/api/v1/data/{ds}/patch-readiness` | 七种 patch 各自是否就绪、缺什么 |
| POST | `/api/v1/patchsets` `{dataset_id, patch_type, size, n, seed, partitions?, only_passed, min_quality?, params?, preprocessing?, augmentation?}` | 生成一个集合（同步），返回血缘、计数、检查结果 |
| GET | `/api/v1/patchsets?dataset_id=&patch_type=` · `/{id}` · `/{id}/patches?partition=&offset=&limit=` · `/{id}/manifest` · DELETE `/{id}` | 集合列表、分页取 patch、一份完整清单、删除 |

## Trace `/api/v1/traces`
`GET ?dataset_id=&run_id=&agent=`，`POST {dataset_id, agent, step, action?, status?, run_id?, duration_ms?, algo_version?, model_version?, experiment_id?, input?, output?}`。

## 看板（HTML）
`/`（数据集总览、扫描、启动 QC）· `/pipeline`（流水线控制台：扫描数据源、勾选数据集与检查项并配置阈值后批量启动、按五阶段实时进度与逐 block 状态、实时日志、取消、最近完成）· `/datasets/{id}`（元信息、blocks 的逐切片严重度条、ETL 指标、findings、资产、版本、trace）· `/datasets/{id}/blocks/{block_id}`（逐切片多分数表、拼图、findings、block 指标）· `/runs/{run_id}` · `/checks` · `/traces`。
