# 数据模型（MySQL `em_qc`）

表由 SQLAlchemy 模型 `emqc/db/models.py` 定义，`python -m emqc init-db` 建表（幂等）。所有 JSON 列在 MySQL 里是 `JSON` 类型。

```
datasets ─┬─ dataset_assets      EM / GT / prediction / synapse / mito / skeleton / agent trace 文件
          ├─ dataset_versions    data / algo / model / experiment 版本
          ├─ blocks              序列单元（连续 z 段 × XY 范围），训练 / 推理划分
          └─ qc_runs ─┬─ qc_slices     每张切片：多分数、failure types、严重度、坐标、预览
                      ├─ qc_blocks     每个 block：聚合分数、留存率、预览、阶段耗时
                      ├─ qc_findings   每个问题一行（切片级 / 序列级 / block 级）
                      └─ etl_metrics   留存率、计数、耗时、字节数（block 级 + 数据集级）
agent_traces          agent / 算法对数据集的执行记录
serve_log             每次切给算法侧的数据（血缘）
```

## datasets
| 列 | 说明 |
|---|---|
| `dataset_id` PK | 目录名或 dataset.json 指定 |
| `name`, `project`, `root_path` | 展示名、project_terminal 下的项目名、数据集目录绝对路径 |
| `em_path`, `em_format`, `em_axes`, `dtype` | EM 卷位置与格式 |
| `size_x/y/z`, `n_voxels` | 形状 |
| `size_class` (`small`/`large`), `usage` (`train` / `train+inference`) | 大小分类与用途 |
| `species`, `brain_region`, `voxel_size_{x,y,z}_nm`, `staining`, `imaging_modality`, `acquisition_batch` | 数据集元信息 |
| `metadata_json` | `z_offset`、`inferred_fields`、reader 附加信息、dataset.json 的 `extra` |
| `data_version`, `status` (`registered`/`qc_running`/`qc_done`/`error`) | |
| `latest_run_id`, `latest_quality_score`, `latest_retention_rate`, `latest_grade` | 最近一次 QC 的结果快照 |

## dataset_assets
`(dataset_id, asset_type, path)` 唯一。`asset_type ∈ em_image | gt_segmentation | gt_annotation | model_prediction | synapse_prediction | mitochondria_prediction | organelle_prediction | skeleton | agent_trace | other`，另有 `format`, `version`, `algo_version`, `model_version`, `experiment_id`, `exists`, `size_bytes`, `extra_json`。

## dataset_versions
`(dataset_id, kind, version)` 唯一，`kind ∈ data | algo | model | experiment | qc_pipeline`，`note`。

## blocks

需求 3 的血缘列：`holdout`（接口字段 `partition`：none | train | val | test | excluded，与用途轴 `split` 正交）、`partition_seed`、`difficulty` 与 `difficulty_json`、`label_version`、`preprocessing_json`、`augmentation_json`、`source_volume`、`region`。列名用 `holdout` 是因为 `PARTITION` 是 MySQL 保留字。

`(dataset_id, block_id)` 唯一。block = `EMQC_BLOCK_SIZE_Z` 张连续切片 × `EMQC_BLOCK_SIZE_XY`² 的 XY tile（默认整张平面）。
`block_id = z00000-00099`（整张平面）或 `z00000-00099_y01024_x00000`（tile，带 tile 原点）。`z_start/z_end`（右开）、`y_*`、`x_*`、`n_slices`、
`split ∈ train | train_sample | inference | unassigned`（small → 全部 train；large → 留存率 / 质量最好的 `EMQC_LARGE_DATASET_TRAIN_BLOCKS` 个为 train_sample，其余 inference）、
`status`、`latest_quality_score`、`latest_severity`、`latest_retention_rate`、`latest_grade`（A/B/C/D）。

## qc_runs
一次流水线运行：`pipeline_version`、`status ∈ queued | running | done | error`、`stage`（当前进度）、`config_json`（QCConfig + block_ids）、`n_blocks`、`n_blocks_done`、`quality_score`、`retention_rate`、`grade_counts_json`（各等级 block 数）、`error`、时间戳。

## qc_slices
`(run_id, dataset_id, block_id, z)` 唯一。
`status ∈ ok | missing | corrupt`，`scores_json {check: score|null}`，`stats_json`（统计量 + `_notes {check: 原因}`），
`failure_types []`，`max_severity`，`quality_score`，`passed`，`coordinate_json {z, z_abs, bbox}`，`preview_path`。

## qc_blocks
`(run_id, dataset_id, block_id)` 唯一。`scores_json {check: {min, mean, n_flagged, n_evaluated, implemented}, "_block": {serial_factor, serial_nm_per_px, median_* , std_trend_log2_per_100z, median_ncc_gap1/2, ...}}`，
`failure_types`，`max_severity`（含 block 级 finding），`quality_score`，`n_slices/n_passed/n_missing/n_corrupt`，`retention_rate`，`grade`（A/B/C/D，规则见 QC_SPEC 第 6 节），`longest_clean_run`（最长连续通过切片数），`dominant_failure`（medium 以上最多的 failure type），`coordinate_json`，`preview_path`（拼图），`duration_s`，`stage_durations_json`。

## qc_findings
一行一个问题：`level ∈ slice | serial | block`，`stage`，`check_name`，`failure_type`，`severity`，`score`，`z`（block 级为 NULL），`z_to`（参考 / 配对切片），`coordinate_json`（`bbox` / `shift` / `z_abs`），`details_json`（检查特定的数值证据，如 `ncc_aligned, expected_ncc, shift_nm, reason`），`preview_path`。

## etl_metrics
`run_id, dataset_id, block_id (NULL = 数据集级), stage, metric_name, metric_value, extra_json`。
block 级：`n_slices_input, n_slices_ok, n_missing, n_corrupt, n_flagged, n_passed, retention_rate, bytes_read, duration_s, slices_per_s, stage_duration_s (每阶段一行), findings_by_type (extra_json)`。
数据集级：`n_blocks, n_slices_input, n_slices_passed, retention_rate, quality_score, n_findings, n_missing, n_corrupt, duration_s, n_train_blocks, n_train_slices, bytes_read, findings_by_type`。

## label_qc
标签资产逐层验证结果：`asset_id`、`z`、`present`、`shape_ok`、`em_status`、`n_ids`、`frac_background`、`frac_label_in_fill`、`issues`（label_missing | label_empty | label_in_fill | label_without_image | label_corrupt）、`passed`。资产级摘要在 `dataset_assets.extra_json.validation`（含 `usable_for_patches`、`scale_to_em`、`downsampled_by`、`notes`），互斥掩码重叠在 `extra_json.label_conflict`。

## patch_sets / patches
`patch_sets`：一个集合一行，`patch_type`（segmentation | membrane | synapse | mitochondria | proofreading | hard_negative | failure）、`status`（done | empty | no_aligned_label | needs_prediction_asset | error）与 `reason`、`params_json`（尺寸、数量、种子、验收阈值、每个 block 依据的 QC 运行）、`qc_run_id`、`label_asset_id` 与 `label_version`、`source_volume`、`preprocessing_json`、`augmentation_json`、`n_patches`、`counts_json`（按折 / block / failure type / 等级）、`checks_json`（重复、overlap 泄漏、越界、折不一致、相邻跨折块对）。
`patches`：逐 patch 坐标 `z0..x1`、`holdout`（接口字段 `partition`）、`difficulty`、`grade`、`region`、`meta_json`（该类型的证据：id 数、边界占比、失败类型与严重度、不一致度与排名等）。像素不存，`url_em` / `url_label` 由接口按坐标即时切。

## agent_traces
`dataset_id, run_id, agent, step, action, status, duration_ms, algo_version, model_version, experiment_id, input_json, output_json`。QC runner 每次运行写一条（agent = `emqc.qc_runner`）；算法侧通过 `POST /api/v1/traces` 写。

## serve_log
`dataset_id, kind ∈ slice | cutout | patch_sample | batch, bbox_json, n_bytes, fmt, client, qc_filter_json, duration_ms`。回答"这次训练到底喂了哪些体素、用的是哪个 QC run 的过滤条件"。

## 常用查询

```sql
-- 最近一次 QC 里每种问题的数量
SELECT f.failure_type, f.severity, COUNT(*) FROM qc_findings f
JOIN datasets d ON d.latest_run_id = f.run_id GROUP BY 1, 2 ORDER BY 3 DESC;

-- 某数据集可用于训练的连续 z 段
SELECT block_id, z, passed FROM qc_slices WHERE run_id = :run ORDER BY z;

-- 留存率随 block 的变化
SELECT block_id, retention_rate, quality_score, max_severity FROM qc_blocks WHERE run_id = :run ORDER BY block_id;
```
