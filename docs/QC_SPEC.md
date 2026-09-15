# EM Image QC v0.1 —— 检查项规范

## 0. 通用约定

- **分数**：每个已实现的检查对每张切片给出 quality score ∈ [0, 1]，1 = 完美，0 = 不可用。
- **严重度**：由分数按阈值映射，默认 `score ≥ 0.75 → none`，`< 0.75 → low`，`< 0.6 → medium`，`< 0.4 → high`，`< 0.2 → critical`；个别检查有自己的阈值（见下表），也可在 `QCConfig.thresholds[check]` 覆盖。
- **failure type**：严重度 ≥ low 就写一条 `qc_findings`（可配 `finding_min_severity`）。
- **通过 / 留存**：切片 `passed` = 状态 ok 且最坏严重度 < high 且所在 block 没有 ≥ high 的 block 级 finding；`retention_rate = passed / total`。
- **切片综合分** `quality_score` = 该切片所有已实现检查分数的 **最小值**（一个致命缺陷就足以让切片不可用）；缺失 / 损坏切片记 0。
- **block 综合分** = block 内切片综合分的均值；数据集综合分 = 按切片数加权的 block 均值。
- **未实现的检查**（stub）分数写 `null`，不影响 passed，但在结果里可见，避免"没查"被误读成"没问题"。
- **坐标**：`z` 为数据集相对 0-based 索引，`z_abs = z + z_offset`；`bbox = [x0, y0, x1, y1]` 全分辨率像素、右开区间；序列检查带 `z_to`（参考切片）；错位类带 `shift {dx, dy}`（像素）。
- **预览**：只保存缩略图（长边 ≤ `EMQC_PREVIEW_MAX_PX`），且只对严重度 ≥ medium 的切片和每个 block 的拼图保存；任意切片的缩略图可通过 `/api/v1/data/{ds}/preview/z/{z}.png` 即时生成。**永远不重新保存全分辨率图像。**

## 1. 五个阶段

| 阶段 | 做什么 | 内存 |
|---|---|---|
| 1 `ingest` | 逐张读取；状态 ok / missing / corrupt；先找填充值连通域（裂缝 / 缺 tile 导出成的 0 区域），再在**有效像素**上算统计量（mean, std, p1/p50/p99, 饱和比例, 众数比例, 熵, Laplacian 方差, sharpness）；预览缩略图；序列分析缩略图（≈32 nm/px） | 任一时刻只有一张全分辨率图 |
| 2 `slice_qc` | 10 个切片级检查，只用统计量 + 缩略图 | 常数 |
| 3 `serial_qc` | 6 个序列级检查，用序列缩略图两两比较 | block 内缩略图 |
| 4 `aggregate` | 切片 / block 综合分、严重度、failure types、留存率、预览、block 统计 | — |
| 5 `persist` | 写 `qc_slices` / `qc_blocks` / `qc_findings` / `etl_metrics`，更新 `blocks`、`datasets`、`qc_runs`，写 `agent_traces` | — |

每个阶段的耗时都记录在 `qc_blocks.stage_durations_json` 和 `etl_metrics(stage_duration_s)`。

## 2. 参考切片链（序列比较怎么选参考）

1. 只有通过了结构性切片检查（missing / corrupt / blank / blur / saturation / crack 没到 high）的切片能进入 **参考链**；所有 ok 切片都会被评估。
2. 一张切片默认与链上的前一张比较；若前一张已被判为离群（本检查或之前的序列检查），改与再前一张比较（gap = 2，期望相关性按 gap 修正）；两张都离群则 **重置**（该切片分数 `null`，note `reference_reset`），避免一张坏片连累一串。
3. 期望相关性 `expected(gap)` 用本 block 自己的 gap-1 / gap-2 中位数外推，不依赖跨数据集的固定阈值。
4. 相邻切片在分析尺度下整体不相关（gap-1 中位 NCC < 0.15）时，给 block 一条 **block 级** finding（`slice_jump` / HIGH），逐张序列检查跳过。

## 3. 切片级检查（stage `slice_qc`）

| check | failure type | 状态 | 分数定义（v0.1） | 坐标 |
|---|---|---|---|---|
| `missing_slice` | `missing` | stable | 无文件 / 无 chunk → 0，否则 1 | z |
| `corrupt_slice` | `corrupt` | stable | 解码失败或尺寸不一致 → 0 | z |
| `blank_slice` | `blank` | heuristic | `min(std / 0.02, 1 − max(frac_mode − 0.8, 0) / 0.2)` | z |
| `severe_blur` | `blur` | heuristic | `sharpness / median(sharpness of block)`，sharpness = Laplacian 方差 / std²；阈值 0.75/0.55/0.35/0.2 | z |
| `saturation` | `saturation` | heuristic | `1 − clip((frac_at_0 + frac_at_max) / 0.2)` | bbox（饱和区域） |
| `brightness_jump` | `brightness_jump` | heuristic | `1 − clip(|mean − mean_ref| / 0.2)`，参考取自参考链（两步回退） | z, z_to |
| `contrast_drift` | `contrast_drift` | heuristic | `1 − clip(|log2(std / median_std_block)| / 1.0)`；block 级趋势见 `_block.std_trend_log2_per_100z` | z |
| `charging_artifact` | `charging` | **stub** | 计划：top-hat + 方向性检测细长高亮饱和区 | — |
| `contamination` | `contamination` | **stub** | 计划：大面积极暗连通域 | — |
| `crack` | `crack` / `no_coverage` / `missing_region` | heuristic | 填充值（默认 0，`em.fill_value` 可改）连通域占比 `1 − clip(frac_fill / 0.05)`。按最大连通域的形状分三类：贴着图像边缘、面积 ≥ 15% 且主轴比 < 6 记 `no_coverage`（组织没填满画布：对齐后移出视野或缺 tile，是采集 / 拼接问题）；主轴比 ≥ 3 记 `crack`（裂缝 / 褶皱）；其余记 `missing_region`。2026-09-14 前的规则是"主轴比 ≥ 3 **或** 跨度 ≥ 0.5 记 crack"，把 mouse_30um 上 22 处未成像区域误判成了裂缝；阈值 0.9/0.7/0.5/0.2 | 连通域 bbox |

## 4. 序列级检查（stage `serial_qc`）

比较尺度：把切片按体素尺寸块平均到 ≈ `serial_target_nm`（32 nm）/px，长边 ≤ 512 px、短边 ≥ 48 px；高通（σ ≈ 128 nm）后归一化，做整数相位相关。

| check | failure type | 状态 | 分数定义（v0.1） | 坐标 |
|---|---|---|---|---|
| `slice_jump` | `slice_jump` | heuristic | `ncc_aligned / expected(gap)`；阈值 0.6/0.45/0.3/0.15 | z, z_to |
| `local_misalignment` | `local_misalign` | heuristic | 4×4 网格逐块相位相关，块位移相对全局位移残差中位数（nm）/150 nm；瓦片 < 32 px 或参考切片相隔 > 2 张时跳过 | 残差最大的瓦片 bbox |
| `global_misalignment` | `global_misalign` | heuristic | `1 − clip(shift_nm / 200 nm)`（无体素尺寸时用短边 5%）；与参考不相关时跳过（那是跳变）；参考相隔 > 3 张时跳过 | shift {dx, dy} |
| `z_order_error` | `z_order` | heuristic | 重复（ncc ≥ 0.985 且位移 ≤ 1）→ 0；相邻互换（弱-强-弱链接，A、B > 0.03）；放错位置（邻居互相更像 + 与某非相邻切片相似度 ≥ 0.8·expected(1)） | z, z_to（配对 / 最佳匹配 z） |
| `local_deformation` | `local_deformation` | **stub** | 计划：粗尺度光流去仿射后残差 | — |
| `section_deformation` | `section_deformation` | **stub** | 计划：相邻切片仿射拟合的尺度 / 剪切偏差 | — |

真实数据 `mouse_30um`（100 张 2048²）：z8 整张空白；15 张切片有横贯全片的零值斜带（裂缝 / 褶皱），全部被 `crack` 命中并给出 bbox；其余为 low 级的亮度 / 清晰度波动。

## 5. 合成数据上的验证（`synthetic_defects`，128 张 512²）

注入 14 处缺陷，13 处可检测的全部命中且位置准确（blank 5、missing 9、corrupt 13、blur 20、brightness 27、contrast 33–40、saturation 45、global shift 52、jump 58、swap 70/71、charging 77 无检测器、crack 85 为零值斜带、duplicate 100、local shift 110）；
charging 77 对应的检查是 stub，按设计未报。无 high 及以上的误报。`tests/test_pipeline.py` 用 40 张的小版本固化了这一结论。

真实数据：`h01_demo_z2048`（H01 4 nm，32 张）无任何 ≥ medium 的告警；`h01_precomputed_demo` 是一份本地导出的坏卷（相邻切片在全分辨率下不相关、个别切片近似重复），被大量标记，属正确行为。

## 6. block 分级（问题分类）

每个 block 在 `aggregate` 阶段得到一个等级，回答"这块数据能不能用、难在哪"：

| 等级 | 规则 | 含义 |
|---|---|---|
| **A** | 留存率 ≥ 95% 且没有 critical 切片（含 missing / corrupt） | 直接可用 |
| **B** | 留存率 ≥ 85% | 剔除少数坏切片后可用 |
| **C** | 留存率 ≥ 60% | 只有较短的连续可用段，训练按 `longest_clean_run` 采样 |
| **D** | 留存率 < 60%，或有 ≥ high 的 block 级 finding | 不可用，需重新采集 / 修复 |

同时记录 `longest_clean_run`（最长连续通过切片数，决定 3D patch 的最大 z 尺寸）和 `dominant_failure`（medium 以上最多的 failure type，即这块数据的"主要难点"）。
**训练样本的抽取（需求 + 决策）**：需求是"小数据集整体 + 大数据集的 sample 用于训练，大数据集整卷用于推理"。大数据集怎么抽是实现决策，用户 2026-09-12 定为**按等级分层**：
总数 `EMQC_TRAIN_SAMPLE_BLOCKS`（默认 8），各等级份额 `EMQC_TRAIN_SAMPLE_STRATA`（默认 A:0.5, B:0.3, C:0.2），层内按固定种子随机抽（`EMQC_TRAIN_SAMPLE_SEED`），某一层不够时配额顺延到下一层，D 级永不入选。
划分依据是**每个 block 最新已知的等级**（`blocks.latest_grade`，每次 QC 后刷新），不是某一次运行的结果：只跑部分 block 时不会把没看过的 block 降级，从未 QC 过的 block 保持 `unassigned`。
抽样计划与结果写在 `qc_runs.config_json.train_sampling` 和训练清单接口里。`EMQC_TRAIN_SAMPLE_POLICY` 还可选 `best`（只取最好）和 `random_usable`（A/B/C 内均匀随机）用于对照实验。
数据集级等级用同样规则算在全部切片上（留存率 + critical 切片数），若有任一 D 级 block 则最高只能是 B。

## 7. 已知限制 / 下一步

- 4 个 stub 检查需要实现算法（charging、contamination、local_deformation、section_deformation）。
- 真实数据 mouse_30um 暴露出 tile 拼接亮度不均（棋盘状明暗），目前没有专门检查，建议 v0.2 增加 `tile_seam` / 光照不均检查。
- block 已支持 XY tile（`EMQC_BLOCK_SIZE_XY`，同一 z 段的所有 tile 共享一次解码）；tile 之间的接缝处缺陷会被两个 tile 各记一次。
- 阈值是启发式初值，需要用真实数据的人工标注回归（把 `qc_findings` 导出 + 人工判定，调 `QCConfig.thresholds`）。
- 序列检查是整数像素相位相关（分析尺度 32 nm），亚像素 / 小于 32 nm 的错位检不出。
- 单机顺序执行；分布式只需把 `QCRunner.process_block` 分发出去，`persist_block` 幂等（按 run/block 先删后插）。
