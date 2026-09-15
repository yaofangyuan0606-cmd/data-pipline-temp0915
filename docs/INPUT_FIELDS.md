# 输入数据字段（Dataset Registry 输入约定）

平台从 `EMQC_DATA_ROOT` 下按 glob `project_terminal/*/datasets/datasets/*` 发现数据集：
每一个匹配到的目录就是一个数据集，目录名即默认 `dataset_id`，`project_terminal/<project>/` 的 `<project>` 记为 `project`。

数据可以在本机目录，也可以直接在别的机器上：`EMQC_REMOTE_ROOTS` 填一个或多个 `sftp://user@host[:port]/path`（分号分隔），
平台通过 SSH 按需读取切片（读一张、算一张，带预读和本地字节缓存 `var/cache/`），不需要先把数据拷过来或挂载。
根目录必须是该 SSH 账号能列出的目录（例如 `…/project_terminal/<用户>`，上一级对该账号不可读时不能作根），匹配模式会自动从根已覆盖的层级往下接。数据目录对平台永远是只读的。远程数据目录里写不进 `dataset.json` 时，把它放到本机 `var/manifests/<数据集目录名>.json`，优先级高于数据目录里的清单。

## 1. 数据集目录里可以有什么

```
<dataset_id>/
├── dataset.json            # 可选：元信息 + 布局声明（推荐提供）
├── em/                     # EM 原始图像：image_stack | npy | precomputed
├── gt/                     # ground-truth segmentation
├── pred/                   # model prediction（分割 / affinity）
├── synapse/                # synapse prediction
├── mito/                   # mitochondria / organelle prediction
├── skeleton/               # skeleton（swc 等）
└── traces/                 # agent execution trace（json 日志）
```

除 `em` 外都是可选资产。目录名可以不同，`dataset.json` 的 `assets[]` 可以显式指定。

## 2. 支持的 EM 存储格式

| format | 说明 | z 索引 | 缺失 / 损坏判定 |
|---|---|---|---|
| `image_stack` | 目录下每张切片一个文件（png / jpg / tif / bmp） | 文件名里**最后一个整数**（`em_z_2048.png` → 2048，`0007.tif` → 7） | 编号断档 = **缺失**；无法解码或尺寸不一致 = **损坏** |
| `npy` | 单个 3-D `.npy`，mmap 读取 | `axes` 指定轴序（`zyx` / `xyz` …） | — |
| `precomputed` | Neuroglancer precomputed（`info` + chunk 文件，`raw` 编码，chunk 可为 `.gz`） | scale 0 的 z | 某 z 一个 chunk 都没有 = 缺失；chunk 解码失败 = 损坏 |

平台内部统一用 **数据集相对的 0-based z**；绝对切片号 = `z + z_offset`（image_stack 默认取最小编号为 `z_offset`）。

## 3. `dataset.json` 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `dataset_id` | str | 否 | 默认目录名 |
| `name` | str | 否 | 展示名 |
| `species` | str | 否 | 物种，如 `human` / `mouse` |
| `brain_region` | str | 否 | 脑区 |
| `voxel_size_nm` | [x, y, z] | 否（强烈建议） | 体素尺寸 nm。序列级检查用它把比较尺度统一到 ~32 nm/px，错位阈值用 nm |
| `staining` | str | 否 | 染色方案 |
| `imaging_modality` | str | 否 | 成像方式，如 `ssEM (multibeam SEM)` / `FIB-SEM` |
| `acquisition_batch` | str | 否 | 采集批次 |
| `size_class` | `auto` / `small` / `large` | 否 | 默认 `auto`：体素数 ≥ `EMQC_LARGE_DATASET_VOXELS`（默认 2e9）为 large |
| `z_offset` | int | 否 | 相对 z=0 的绝对切片号 |
| `em.path` | str | 否 | EM 卷相对路径；缺省按 `em/raw/image/images/em_image/EM/img/volume`、根目录 `*.npy`、根目录图片、根目录 `info` 依次推断；若 `em/` 下是 `mip0/mip1/…` 子目录，自动选最细的一层（如 `em/mip1`） |
| `em.format` | `auto` / `image_stack` / `npy` / `precomputed` | 否 | 缺省自动探测 |
| `em.axes` | str | 否 | 仅 npy，默认 `zyx` |
| `em.fill_value` | number / null | 否 | "无数据"区域的像素值，默认 0；裂缝、缺 tile 导出成这个值的连通域会被 `crack` 检查识别，且不计入亮度 / 清晰度统计。设为 `null` 关闭 |
| `assets[]` | list | 否 | 见下表；缺省按目录名推断 |
| `versions` | {data, algo, model, experiment} | 否 | 版本号，写入 `dataset_versions` |
| `extra` | dict | 否 | 任意补充信息，原样存入 `datasets.metadata_json` |

`assets[]` 每项：

| 字段 | 说明 |
|---|---|
| `type` | `em_image` / `gt_segmentation` / `gt_annotation`（按类别的标注掩码，类别放 `class` 字段） / `model_prediction` / `synapse_prediction` / `mitochondria_prediction` / `organelle_prediction` / `skeleton` / `agent_trace` / `other` |
| `path` | 相对数据集目录（文件或目录） |
| `format` | 自由文本（`image_stack` / `json` / `swc` / `h5` …） |
| `version` | 该资产版本 |
| `algo_version` / `model_version` / `experiment_id` | 产出它的算法 / 模型 / 实验 |

目录名到资产类型的推断表：`gt|ground_truth|labels|seg_gt → gt_segmentation`，`pred|prediction(s)|seg_pred|segmentation|affinity → model_prediction`，
`synapse(s)|syn → synapse_prediction`，`mito|mitochondria → mitochondria_prediction`，`organelle(s) → organelle_prediction`，
`skeleton(s)|skel|swc → skeleton`，`trace(s)|agent_trace(s)|agent_logs → agent_trace`。

哪些字段是推断出来的会记录在 `datasets.metadata_json.inferred_fields`，页面上也会显示，方便回头补 `dataset.json`。

## 4. 示例

```json
{
  "dataset_id": "h01_demo_z2048",
  "name": "H01 demo sub-stack",
  "species": "human",
  "brain_region": "temporal cortex (H01)",
  "voxel_size_nm": [4, 4, 33],
  "staining": "heavy metal (OsO4 / UA / Pb)",
  "imaging_modality": "ssEM (multibeam SEM)",
  "acquisition_batch": "h01-demo2-fetch",
  "size_class": "auto",
  "z_offset": 2048,
  "em": {"path": "em", "format": "image_stack"},
  "assets": [
    {"type": "gt_segmentation", "path": "gt", "format": "image_stack", "version": "v1"},
    {"type": "model_prediction", "path": "pred", "format": "image_stack", "version": "v1",
     "algo_version": "seg-algo-0.3.0", "model_version": "unet-2026-09-01", "experiment_id": "exp-001"}
  ],
  "versions": {"data": "v1", "algo": "seg-algo-0.3.0", "model": "unet-2026-09-01", "experiment": "exp-001"}
}
```

## 5. 待确认（等 Tailscale 目录挂上后）

- 真实数据的 EM 存储格式（图片栈 / precomputed / zarr / h5）—— zarr / n5 / h5 目前**未实现**，加一个 reader 即可。
- 超大切片（单张 > 4M 像素）目前整张读入算统计量；v0.2 需要按 XY 分 tile 成 block。
- GT / prediction 的具体格式，决定后续"标签一致性 QC"怎么做。
