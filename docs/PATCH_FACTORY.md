# Patch Factory / Training Data API（需求 3）

> 图像 QC 完成之后才稳定地产训练数据。block 不单独保存，但切分方式是明确的、可调用的：
> patch 集合只存坐标与血缘，像素通过数据 API 按需切（EM cutout + 标签 cutout 同一 bbox）。

## 1. 三个前提，按顺序

| 步骤 | 做什么 | 入口 |
|---|---|---|
| 标签验证 | 把标签卷（GT 分割、类别掩码、模型预测）和 EM 对齐检查一遍；只有**逐层对齐且同分辨率**的标签能出 patch | `emqc validate-labels <ds>` · `POST /datasets/{ds}/assets/{id}/validate` · 页面 Patch Factory |
| holdout 划分 | 给训练用途、等级 A/B/C 的 block 分 train / val / test；按 block 切、稳定不重排、D 级排除 | QC 收尾自动做 · `emqc partition <ds>` · `POST /datasets/{ds}/partition` |
| 生成集合 | 按七种用途之一采样窗口、验收、记录血缘、跑泄漏与重复检查 | `emqc patches <ds> --type …` · `POST /patchsets` |

## 2. 标签验证查什么

| 问题 | 含义 | 判定 |
|---|---|---|
| `label_missing` | EM 有这一层，标签没有文件 | 逐层 |
| `label_empty` | 标签文件在，但全是背景，而 EM 这一层有数据 | 背景占比 ≥ 99.9% |
| `label_without_image` | EM 这一层缺失 / 损坏 / 空白，标签却有内容 | 前景占比 > 1% |
| `label_in_fill` | 标签画在 EM 的填充区（没有图像数据的地方） | 前景中落在填充区的比例 > 2% |
| `shape_mismatch` | 标签平面不是 EM 平面的整数倍缩放 | 资产级 |
| `z_count_mismatch` | 标签层数 ≠ EM 层数 | 资产级 |
| `encoding_inconsistent` | 各层解码出的数据类型不一致 | 资产级 |
| `label_conflict` | 互斥类别掩码之间的像素重叠（多个资产一起验） | `POST /datasets/{ds}/assets/validate-exclusive` |

不能对齐到 EM 的资产（层数或尺寸对不上）直接判 `fail` 并跳过逐层检查，原因写进 `notes`：这类映射只能由数据方给出，平台不猜。

编码：`gray`（单通道整数）、`palette`（PNG 调色板索引即类别）、`rgb24`（id = R<<16 | G<<8 | B，常见的分割导出方式）、`auto`。资产的 `format` 含 `rgb` 或 `extra.label_encoding` 指定即可。

## 3. block 必须保存的字段 → 落在哪

| 需求字段 | 位置 | 说明 |
|---|---|---|
| dataset_id | `blocks.dataset_id` | |
| coordinate | `blocks.z/y/x_start,end` | 接口里聚合成 `coordinate` |
| source volume | `blocks.source_volume` | EM 卷的 URL（本地路径或 `sftp://…`），扫描时写 |
| label version | `blocks.label_version` | 通过验证、可出 patch 的 GT 分割资产的 `version`（无则用路径），QC 收尾时写 |
| preprocessing | `blocks.preprocessing_json` | **声明**的预处理（默认 `EMQC` 配置：逐 patch z-score、0.5/99.5 百分位裁剪、float32）；平台给原始体素，训练侧执行 |
| augmentation | `blocks.augmentation_json` | 声明的增强策略（默认 xy 翻转与 90° 旋转、亮度抖动 0.1、不翻 z、无弹性） |
| brain region | `blocks.region` | 默认取数据集的 brain_region，大体积可按 block 覆盖 |
| difficulty | `blocks.difficulty` + `difficulty_json` | `0.5·(1−quality) + 0.3·(1−retention) + 0.2·min(1, medium 以上切片数/切片数)`，D 级 ≥ 0.9 |
| train/val/test split | `blocks.holdout`（接口字段名 `partition`） | 与用途轴 `split` 正交：一个 block 可以既是 `train_sample` 用途又属于 `val` 折 |

划分规则：只有 `split ∈ {train, train_sample}` 且等级 A/B/C 的 block 参与；D 级 → `excluded`；推理用途或未 QC → `none`。比例默认 8:1:1（`EMQC_PARTITION_RATIOS`），少于 3 个可用 block 时全部 train（无法留出）。分配**稳定**：已分配的 block 不因重跑 QC 而变动，新出现的可用 block 补最缺的那一折；`force=true` 才整体重排（会改变测试集，页面上有二次确认）。块级划分使 patch 层面的 overlap 泄漏在构造上为零；相邻但不同折的块对数量作为软信号报告。

## 4. 七种 patch 与各自的前提

| 类型 | 采样方式 | 验收 | 需要 |
|---|---|---|---|
| `failure` | 以 QC finding 为中心（有 bbox 的居中，只有 z 的随机 xy），按严重度排序取前 n | 无 | QC 结果 |
| `segmentation` | 通过 QC 的 z 窗口内随机 xy，看中心层 GT | 前景 ≥ 30% 且 ≥ 2 个 id | 可用的 GT 分割 |
| `membrane` | 同上，目标是由 id 推出的边界图 | 边界像素 ≥ 2% | 可用的 GT 分割 |
| `synapse` | 以掩码连通域质心为中心 | 掩码占比 ≥ 0.2% | 逐层对齐的突触掩码 / 预测 |
| `mitochondria` | 同上 | 同上 | 逐层对齐的线粒体 / 细胞器掩码 |
| `hard_negative` | 随机窗口，比较预测与 GT | 不一致度 ≥ 5%（前景不一致与边界不一致各半） | GT 分割 + 模型预测 |
| `proofreading` | 评分所有候选窗口，取不一致度最高的前 n（带 rank） | 无 | GT 分割 + 模型预测 |

不能生成时集合仍会创建，状态为 `no_aligned_label` 或 `needs_prediction_asset`，`reason` 说明缺什么、已登记的资产为何不可用。`GET /data/{ds}/patch-readiness` 提前给出每种类型是否就绪。

## 5. 每个集合跑的检查

| 检查 | 硬 / 软 | 含义 |
|---|---|---|
| `n_duplicate_exact` | 硬 | 同一 bbox 出现两次 |
| `n_overlap_cross_partition` | 硬 | 不同折的 patch 有体积重叠（overlap 泄漏） |
| `n_outside_block` | 硬 | patch 越出所属 block（会跨划分边界） |
| `n_partition_mismatch` | 硬 | patch 的折与其 block 的折不一致（train/test 泄漏） |
| `n_near_duplicate_pairs` | 软 | 3-D IoU ≥ 0.9 的 patch 对 |
| `n_unpartitioned` | 软 | 落在 none / excluded 块里的 patch（failure 类型允许） |
| `adjacent_cross_partition_block_pairs` | 软 | 不同折的相邻块对（组织上下文跨折） |

任一硬项 > 0 则 `checks.passed = false`，集合照常保存以便排查。

## 6. 训练侧怎么用

```python
from emqc.loader import PatchSetClient
ps = PatchSetClient("http://127.0.0.1:8765", set_id=3)
for em, label, meta in ps.iter(partition="train"):   # em (dz,dy,dx)；label：ids uint32 / boundary uint8 / mask uint8
    ...
ps.manifest["preprocessing"], ps.manifest["augmentation"]   # 声明的处理方式，训练代码自己执行
```

`GET /patchsets/{id}/manifest` 一份文档给全：集合血缘（QC 运行、标签资产与版本、来源卷、预处理与增强声明）、逐 patch 的 bbox / 折 / 难度 / 等级 / 证据 / EM 与标签 URL。

## 7. 在 mouse_30um 上的实测

- 标签：`seg/mip1` 可出 patch（scale 1:1，rgb24）；`seg/mip2-4` 仅供 QC（1/2、1/4、1/8）；`delete/axon|cellbody|dendrites|synapse|vesicle` 750 层 4096² → `z_count_mismatch`，判 fail；`delete/synapse+vesicle` 100 层 4096² → 等距抽样到 2048² 后可用，但 96 层为空。GT 相对 EM 的问题：`label_in_fill` 16 层（分割画到了未成像区域）、`label_without_image` 1 层（z8）。
- 划分：4 个 B 级 block → train 2 / val 1 / test 1，难度 0.19–0.25，相邻跨折块对 4（块级划分的固有软信号）。
- 集合：failure 64（含 no_coverage 32）、segmentation 64、membrane 64、synapse 29；生成耗时 1–9 s；四个集合的硬检查全部为零。
- 未就绪：mitochondria（无掩码）、hard_negative 与 proofreading（无模型预测）。
- 一个待数据方确认的点：rgb24 分割里没有值为 0 的像素，也就是说背景 id 未知，`fg_frac` 对这份数据恒为 1，segmentation 的验收实际只靠 `n_ids ≥ 2`。
