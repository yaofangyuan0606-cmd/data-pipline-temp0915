# EM Image QC platform v0.1

给 EM（电镜连续切片）数据做数据清洗的第一版平台：**数据集注册表 + 原始图像 QC 流水线 + 面向算法侧的实时取数接口 + 可视化看板**，结果全部落 MySQL。

```
Tailscale 挂载目录 (EMQC_DATA_ROOT)
  project_terminal/<project>/datasets/datasets/<dataset_id>/{dataset.json, em/, gt/, pred/, synapse/, mito/, skeleton/, traces/}
          │  scan（只读）
          ▼
  ┌──────────────────────────┐    ┌──────────────────────────────────────────────────┐
  │ Dataset Registry (MySQL) │    │ QC pipeline  per block (= 64 张连续切片)           │
  │ datasets / assets /      │◀──▶│ 1 ingest → 2 slice_qc → 3 serial_qc → 4 aggregate │
  │ versions / blocks        │    │ → 5 persist  (qc_slices / qc_blocks / qc_findings │
  └──────────────────────────┘    │              / etl_metrics / agent_traces)        │
          │                       └──────────────────────────────────────────────────┘
          ▼
  FastAPI  ─ 控制台 (/pipeline) ─ 看板 (/)  ─ REST (/api/v1/…)  ─ 算法侧实时切块 (/api/v1/data/…, 写 serve_log)
```

- **不重切不落盘**：QC 只保存缩略图和数字；算法要数据时按坐标从源卷即时读取。
- **数据可以在别的机器上**：`EMQC_REMOTE_ROOTS=sftp://user@host/path`（根须是该账号可列出的目录），切片经 SSH 按需读取：整个 z 段的文件用 rsync 分批预取到 `var/cache/<host>/`（镜像远端路径），单张读取走 SFTP，本地目录与远程目录走同一套读取器。远端目录不可写时把 `dataset.json` 放到 `var/manifests/<目录名>.json`。
- **小 / 大数据集**：体素数阈值（或 `dataset.json` 指定）分类；小数据集整体用于训练，大数据集按等级分层抽取若干 block 作训练样本（A/B/C 各占份额，D 不入选，层内随机、可复现）、整卷用于推理（`/api/v1/data/training/manifest`）。
- **16 项检查**（10 切片级 + 6 序列级）都有框架位；12 项已有启发式 v0.1 实现，4 项是明确标注的 stub（charging、contamination、local_deformation、section_deformation）。
- 每张切片 / 每个 block 输出：**多分数、failure type、severity、coordinate、preview**，另有 **留存率等 ETL 指标**。

## 目录

```
emqc/
  config.py            环境变量配置（EMQC_*）
  db/models.py         MySQL 表（见 docs/DATA_MODEL.md）
  registry/            readers（image_stack / npy / precomputed）、dataset.json 解析、扫描注册、block 划分
  qc/                  base（分数/严重度/参考链）、slice_checks、serial_checks、stages（五阶段）、runner、preview
  api/                 FastAPI：datasets / qc / data / traces 路由 + Jinja2 看板
  cli.py               python -m emqc …（init-db / scan / run / export / delete / serve / checks / datasets / make-sample）
  loader.py            训练侧 ShardStore（读分片）与推理侧 StreamClient（消费流会话）
scripts/make_sample_dataset.py   生成三个样例数据集（真实 H01 切片 ×2 + 含已知缺陷的合成数据）
scripts/init_mysql.sql           建库建用户
tests/                           pytest（SQLite 隔离环境）
docs/  INPUT_FIELDS.md  QC_SPEC.md  DATA_MODEL.md  API.md  DATASET_PRIMER.md  SCALE_PLAN.md  design_v0.1.html
       architecture_atlas.html   ← 全部架构图（分层/模块/ER/写入/闭环/流水线/交付/节点）
```

## 快速开始

```bash
cd em-qc-platform
# 1. 依赖（已建好 .venv，Python 3.13；重建：python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt）
# 2. MySQL：本机 3306，在 Navicat 以 root 执行 scripts/init_mysql.sql（建库 em_qc、用户 em_qc）；连接串在 .env
.venv/bin/python -m emqc init-db
# 3. 数据：本地目录放 ./data_root；服务器上的数据用 .env 的 EMQC_REMOTE_ROOTS=sftp://user@host/path 直接读，不用拷贝
.venv/bin/python scripts/make_sample_dataset.py
# 4. 注册 + 跑 QC
.venv/bin/python -m emqc scan
.venv/bin/python -m emqc run synthetic_defects
.venv/bin/python -m emqc datasets
# 5. 看板 + API
.venv/bin/python -m emqc serve        # http://127.0.0.1:8765  控制台 /pipeline，看板 /，OpenAPI /docs
# 测试
.venv/bin/python -m pytest tests -q
```

## 界面

左侧导航：流水线、数据集、运行记录、检查项、Trace 与取数；顶栏实时显示服务与运行状态。

- **流水线控制台 `/pipeline`**：数据源与配置、资源采样（CPU / 内存 / GPU，GPU 有 `nvidia-smi` 时自动显示）、新建运行（多选数据集、限定 block、勾选检查项、JSON 覆盖阈值与参数、通过标准）、运行中的**执行节点图**（按真实结构画：ingest 按 z 段共享一次解码，之后每个 tile 各自经过 slice_qc → serial_qc → aggregate → persist，节点带状态、耗时、日志条数，点节点只看它的日志）、带过滤的实时日志（按运行、节点、级别、搜索，可暂停滚动、显示结构化数据）、运行结束后停留的结果卡（留存率、等级、每个 block 一行）、最近完成与一键再跑。
- **数据集页**：KPI、**block 地图**（切片平面按 tile 划分、按等级着色，图旁解释 block 怎么切）、每个 block 的逐切片严重度条、问题类型分布、各检查项跨 block 汇总、findings、资产、元信息、运行历史、trace。
- **block 页**：位置小图、逐切片综合分柱状图、拼图、分数热力表（可只看未通过 / 有告警）、findings、ETL 与资源指标（CPU 时间、峰值内存）。
- **运行页**：节点图、日志、各阶段耗时时间线、各 block 结果与资源。
- **检查项页**：每项一张卡，阈值刻度尺直接画出 critical / high / medium / low / none 的区间。

页面上的每个动作都对应一个接口（`/api/v1/qc/runs/batch`、`/runs/{id}/cancel`、`/runs/{id}/events`、`/runs/{id}/graph`、`/pipeline/status`、`/system/metrics`），脚本和调度器可以照做，见 `docs/API.md`。规模化（PB 级数据、多台 Mac mini 用 Ray 跑同一数据集）的方案见 `docs/SCALE_PLAN.md`。

## 数据交付：训练分片与推理流

看板里的"数据交付"页统一操作这两条路，接口在 `/api/v1/exports` 与 `/api/v1/streams`。

**训练侧永远读本地分片。** 导出是后台任务（有进度、日志、可取消、可续传）：每个 block 内连续通过 QC 的 z 段切成不超过 `shard_z` 张的分片，`<out>/<dataset>/<block>/z00019-00026.npy` 形状 `(n, H, W)` 且已裁到 tile；每个 block 一个 `index.json`，总的 `export_manifest.json` 记录来源 QC 运行、每个分片的 z 范围与 bbox、被排除的切片与原因。已存在且大小正确的分片直接复用，所以 QC 重跑后再导只写变化的部分。`--with-assets gt_segmentation` 把同一批 z 的 GT 文件原样拷到 `assets/`。训练代码用 `emqc/loader.py` 的 `ShardStore` 读：内存映射、随机 patch、确定性平铺。

```bash
python -m emqc export mouse_30um --out var/exports --min-run 8 --shard-z 16 --with-assets gt_segmentation
```

**推理侧永远不落盘。** 创建一个流会话（`POST /api/v1/streams`，指定 `z_chunk`、顺序、是否跳过含未通过切片的子块），平台生成覆盖整卷、按 block 顺序的子块计划；推理程序循环 `next` 领取子块坐标、按 URL 即时取数、`ack`、最后 `close`。远程数据源在发放当前子块时预取下一批。会话记录进度、已确认数、字节数与速率，每次取数进取数日志并带会话号，构成推理输入的完整血缘。参考实现是 `emqc/loader.py` 的 `StreamClient`。

```python
from emqc.loader import StreamClient
for item, arr in StreamClient("http://127.0.0.1:8765", "mouse_30um", z_chunk=16, client="unet-infer").items():
    pred = model(arr)
```

在线随机抽样（`POST /api/v1/data/{ds}/patches/sample`）仍然可用，适合交互式看数据和调阈值，不适合作训练主通道。

## 数据采集：云端 precomputed 卷

除了本地目录和 SSH 远程目录，平台还能按 XYZ 坐标直接读公开的 precomputed 卷（Neuroglancer 生态，如 H01）。
由原先独立的 H01 爬取工具移植而来，只移植了数据获取的三个阶段（索引 / ROI 预判 / 取体素），渲染与截图属于后续的 Failure Explorer。

两条路：**注册**把一个 ROI 变成数据集，不复制体素，QC 与 patch 按需读；**下载**把 ROI 写成本地图像栈，任务可续传。
爬之前先用组织类型图预判 ROI 值不值得（几秒），避免在坏区域上花几小时。命令 `python -m emqc cloud {volume|index|precheck|register|fetch}`，页面在 `/crawl`，细节见 `docs/ACQUISITION.md`。

实测：H01 一个 640×640×32 的 ROI，预判 2.5 秒判定值得爬，注册后直接跑 QC 得 A 级、留存 100%，c3 分割验证通过并生成了分割与边界 patch，全程没有下载任何文件。

## Patch Factory：从合格数据到训练 patch（需求 3）

QC 之后才稳定地出训练数据。看板里的 "Patch Factory" 页和 `/api/v1/patchsets` 接口做三件事，顺序固定：

1. **验证标签卷**（`emqc validate-labels <ds>`）：GT 分割、类别掩码、模型预测逐层与 EM 对照，查标签缺失、标签为空、EM 无数据处有标签、形状与层数不一致、编码不一致，互斥掩码之间查重叠。只有与 EM 逐层对齐、同分辩率（或整数倍、可等距抽样到 EM 网格）的标签才能出 patch；对不上的资产直接判 fail 并写明原因，不猜映射。
2. **holdout 划分**（QC 收尾自动做，`emqc partition <ds>` 可查看或补齐）：用途轴 `split`（训练 / 推理）与划分轴 `partition`（train / val / test）是两个独立字段。只有训练用途且等级 A/B/C 的 block 参与，D 级排除，默认 8:1:1，按 block 切、稳定不重排（新 block 补最缺的一折，`--force` 才整体重排）。块边界是硬切分，patch 不跨块，overlap 泄漏在构造上为零。
3. **生成集合**（`emqc patches <ds> --type <t> --size 16,256,256 --n 64`）：七种用途各有采样与验收规则，见 `docs/PATCH_FACTORY.md`。集合只存坐标与血缘（QC 运行、标签资产与版本、来源卷、预处理与增强声明、难度、折），每个集合自动跑重复 / overlap / 越界 / 折不一致检查，像素由 `url_em` 与 `url_label` 按同一 bbox 即时切。训练侧用 `emqc/loader.py` 的 `PatchSetClient`。

在 mouse_30um 上的实测（2026-09-14）：标签验证判定 `seg/mip1`（100 张 RGB 2048²，rgb24 打包得 3872 个 id）可出 patch，`seg/mip2-4` 为低分辨率版本仅供 QC，`delete/` 六类掩码中五个是 750 层与 100 张 EM 对不上（判 fail，等数据方给映射），`synapse+vesicle` 100 层可用但只有 4 层有内容；GT 在 16 层里画到了 EM 的填充区，1 层（z8 空白）有标签无图像。四个 block 划分为 train 2、val 1、test 1，难度 0.19–0.25。生成的集合：failure 64（no_coverage 32、brightness_jump 8、missing_region 8、crack 6、slice_jump 6、blank 4）、segmentation 64、membrane 64、synapse 29，全部通过重复 / 泄漏 / 越界检查；hard_negative 与 proofreading 因没有模型预测而未就绪，页面给出原因。

每个 block 必须保存的字段全部落在 `blocks` 表：dataset_id、coordinate、source_volume、label_version、preprocessing、augmentation、region、difficulty、partition（列名 `holdout`）。难度公式：`0.5·(1−quality) + 0.3·(1−retention) + 0.2·(medium 以上切片占比)`，D 级不低于 0.9。

## 每项检查为什么这么查、阈值为什么这么定

见 [docs/CHECKS_AND_THRESHOLDS.md](docs/CHECKS_AND_THRESHOLDS.md)。那份文档把 16 项图像检查、
7 项标签检查、patch 验收与泄漏检查逐条拆开，说明机制、阈值数值、以及每个数值的依据等级
（客观 / 物理 / 自适应 / 经验），并给出每项在真实数据上的实际表现与可信度评估。

简短版：大部分阈值是经验值、未经人工标注校准；在 mouse_30um 上真正改变过结论的只有 3 项检查。

## 待确认问题

所有悬而未决的问题集中在 [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md)，按谁能回答分为四组：
数据方 10 条、需要看图判断的 4 条、需要项目决策的 7 条、以及我自己还没想清楚的 3 条。
每条都写了为什么要问、不解决的后果、需要什么形式的答案。

## 关键约定

- **block**：`EMQC_BLOCK_SIZE_Z`（默认 100）张连续切片 × `EMQC_BLOCK_SIZE_XY`（默认 1024）² 的 XY tile，`block_id = z00000-00099_y01024_x00000`；平面小于 tile 时整张作一个 block（`z00000-00099`）。同一 z 段的所有 tile 一起处理，每张切片只解码一次。
- **block 分级**：A 直接可用 / B 剔除少数坏切片 / C 仅短连续段 / D 不可用，另有最长连续可用段与主要 failure type，见 `docs/QC_SPEC.md` 第 6 节。
- **坐标**：`z` 为数据集内 0-based，`z_abs = z + z_offset`；`bbox = [x0, y0, x1, y1]` 全分辨率像素。
- **分数 / 严重度 / 通过**：分数 ∈ [0,1]（1 好）；`none / low / medium / high / critical`；`high` 及以上不通过；留存率 = 通过切片 / 总切片。详见 `docs/QC_SPEC.md`。
- **序列比较**：统一到 ≈32 nm/px 的物理尺度比较，错位阈值按 nm；参考切片按"两步回退"选择，避免一张坏片连累一串。

## 判定逻辑：每个结论是怎么得出的

总原则只有一句：**先测量，再判定，把测量值和判定一起存下来**。每项检查对每张切片给一个 0 到 1 的分数（1 好），按阈值映射成 none / low / medium / high / critical，分数背后的数值证据写进 `qc_findings.details_json`，任何一条结论都能在看板或数据库里回溯到具体数字。没实现的检查写 `null`，不冒充"没问题"。

### 第一步：读一张切片时测了什么

| 测量 | 怎么算 | 为什么这么算 |
|---|---|---|
| 状态 | 文件不存在 → `missing`；解码失败或尺寸与数据集不一致 → `corrupt` | 这是最便宜也最确定的两类问题，先判掉 |
| 填充区域 | 像素值等于填充值（默认 0）的 8 连通域，面积 ≥ 0.1% 的才算；记面积占比、连通域数、最大域的主轴比和跨度 | 真实数据里裂缝、缺 tile、对齐后移出画面的地方都被导出成 0（mouse_30um 实证），它们不是"图像内容"，要先摘出来 |
| 亮度统计 | 只在**有效像素**上算 mean、std、p1、p50、p99、众数占比、熵 | 黑带会把均值拉低、把 0 值比例推高；不摘掉，一张裂缝切片会同时被判成饱和、模糊、亮度突变三项 |
| 饱和比例 | 等于 0（且不在填充区）和等于最大值的像素占比 | 真正的探测器饱和才计入 |
| 清晰度 | Laplacian 方差 ÷ std²，在有效像素上算，像素超过 4M 时先子采样 | 除以 std² 让它与对比度无关，只反映边缘锐利程度；绝对值跨数据集不可比，所以后面只用相对值 |
| 两张缩略图 | 预览缩略图长边 256 px；序列分析缩略图块平均到约 32 nm/px | 全分辩率图算完即丢，后面所有比较都在缩略图上做，内存恒定 |

### 第二步：切片级检查各自的依据

| 检查 | 判定 | 为什么这是对的信号 |
|---|---|---|
| 空白 `blank_slice` | std ÷ 0.02，或某个灰度占比超过 80% 后线性扣分 | 空白切片是常数或近常数；mouse_30um 的 z8 整张 128 |
| 模糊 `severe_blur` | 清晰度 ÷ 本 block 非空白切片的清晰度中位数 | 用相对值，因为不同数据集、不同 mip 的清晰度绝对值差好几倍 |
| 饱和 `saturation` | 1 − 饱和比例 ÷ 20% | 20% 像素撞到灰度上下限就没有信息了 |
| 裂缝 `crack` | 填充面积 < 0.2% 记满分；否则 1 − 填充面积 ÷ 5%。最大连通域贴边、面积 ≥ 15%、主轴比 < 6 记 `no_coverage`；主轴比 ≥ 3 记 `crack`；其余记 `missing_region` | 裂缝是狭长的带；未成像区域是贴着边的大块（组织没填满画布，边界呈直线加台阶）；黑块是内部成块缺数据。三者对训练的意义不同：裂缝两侧组织错位，后两者只是没有数据 |
| 亮度突变 `brightness_jump` | 1 − 与参考切片均值差 ÷ 0.2（灰度范围的 20%） | 相邻切片是同一块组织隔 40 nm，平均灰度不该跳；跳了就是成像条件变了 |
| 对比度漂移 `contrast_drift` | 1 − \|log2(std ÷ 参考链中位 std)\| | std 翻倍或减半算满分扣完；用 log 让变亮变暗对称 |
| charging、污染 | 未实现，分数为 `null` | 还没有真实样本可以校准，宁可标"未查" |

### 第三步：序列级检查怎么比

- **为什么缩到 32 nm/px**：4 nm 像素级的纹理在相邻切片之间几乎不相关（H01 实测高通后相关系数只有 0.01），细胞级结构在 32 nm 尺度上才稳定延续。缩略图先高通滤掉 128 nm 以上的光照起伏，再做相位相关求平移，再在重叠区算相关系数 NCC。**这些 nm 单位的常数都需要体素尺寸**；数据集没有体素尺寸时退化为像素比例：缩略图长边 256 px、高通 2 px、全局错位以 tile 短边的 5% 为满分扣完、局部错位 3%。mouse_30um 目前就是这种情况（体素尺寸待确认）。
- **期望值由 block 自己估**：相邻切片（gap 1）和隔一张（gap 2）的 NCC 中位数是这个 block 的"正常水平"，更远按几何衰减外推。不同数据集不用共用固定阈值。
- **参考切片怎么选**：没通过结构性检查（空白、裂缝、模糊、饱和、缺失、损坏）的切片根本不进参考链；链上的切片被判为离群后进入共享集合，后面的切片改与再前一张比，两张都离群就重置。这样一张坏片最多影响紧邻的一两张，不会把一串好片连带判坏。
- **参考太远就不比**：局部错位超过 2 张、全局错位超过 3 张就跳过。mouse_30um 的 z6 到 z8 连续三张坏，z9 到 z11 只能回退到 z5，组织已经移动，第一版据此报了 6 条错位全是误报。
- 各项判定：切片跳变分数 = 对齐后 NCC ÷ 期望值；全局错位分数 = 1 − 平移量（nm）÷ 200 nm；局部错位分数 = 1 − 网格分块位移相对全局位移的残差中位数（先扣掉 1 个缩略图像素的容差，nm）÷ 150 nm；z-order = 与前一张几乎相同（重复）、弱-强-弱链接模式（互换）、与非相邻切片更像（放错位置）。
- **整个 block 相邻切片都不相关**时给一条 block 级 finding，而不是把每张都标红。

### 第四步：从分数到"能不能用"

1. **严重度**：默认 ≥ 0.75 none，< 0.75 low，< 0.6 medium，< 0.4 high，< 0.2 critical；模糊、裂缝、跳变有各自的阈值（见 `docs/QC_SPEC.md`）。
2. **通过**：切片状态 ok，且最坏严重度低于 high，且所在 block 没有 high 以上的 block 级问题。low 和 medium 只是提醒，不影响通过。
3. **留存率** = 通过切片 ÷ 总切片。**切片综合分**取所有检查的最小值：一个致命缺陷就足以让切片不可用。
4. **block 等级**：留存率 ≥ 95% 且没有 critical 切片 → A；≥ 85% → B；≥ 60% → C；更低或有 block 级失败 → D。同时记最长连续可用段（决定 3D patch 的 z 深度上限）和主要问题类型。
5. **数据集等级**用同一规则算在全部切片上，有任一 D 级 block 则最高 B。
6. **训练划分**：小数据集全部进训练（需求）；大数据集按等级分层抽 block（用户决策：默认 8 个，A 50%、B 30%、C 20%，D 不入选，层内定种子随机）。划分按每个 block 最新已知的等级决定，只跑部分 block 不影响其余 block，没 QC 过的保持 unassigned。

### 阈值是怎么定的，可信到什么程度

- 阈值是**启发式初值**，不是从标注数据学出来的。定法是：合成一份注入 14 处已知缺陷的切片栈，调到 13 处可检出缺陷全部命中且没有 high 以上误报；再用一份真实 H01 切片栈（32 张）确认干净数据没有 medium 以上告警，另一份已知是坏数据的导出卷被大量标记；最后在 mouse_30um 上确认 15 张带 critical 级裂缝的切片全部命中（另有 2 张细裂缝报 high / medium）、z8 空白命中。
- 真实数据上 brightness_jump 和 blur 的 low 级告警很多（各几十条），说明默认阈值对真实亮度波动偏敏感。low 不影响通过，但要当成"待校准"看。
- 每项检查带成熟度标记：`stable`（缺失、损坏）、`heuristic`（其余已实现项）、`stub`（未实现）。看板 `/checks` 页和 `GET /api/v1/qc/checks` 都能看到。
- **怎么复核一条结论**：打开 block 页找到那张切片，看它的缩略图和每项分数；展开 finding 的 details，里面是原始数值（比如裂缝的填充面积、主轴比，错位的平移 nm，跳变的 NCC 与期望值）。觉得不对，改 `QCConfig.thresholds` 或 `params` 重跑即可，旧结果保留在旧 run 里可对比。

数据本身的背景知识（连续切片电镜怎么产生这些图、图里能看到什么、缺陷从哪来、兄弟数据集各是什么）见 [docs/DATASET_PRIMER.md](docs/DATASET_PRIMER.md)。

## 数据与样例

当前注册的真实数据：

| dataset | 来源 | 结果 |
|---|---|---|
| `mouse_30um` | 直接读服务器 `<工作站>` 上的 `<数据根目录>/datasets/datasets/mouse_30um`（SSH），100 张 2048² EM + seg 多 mip + 6 类标注掩码，按 1024²×100 切成 4 个 block；清单在 `var/manifests/mouse_30um.json` | 4 个 block 全 B 级，留存率 87%，主要问题是零值斜带裂缝。远程直读与本地拷贝的 QC 结果逐切片完全一致（400 行、0 差异），远程一轮 105 s，本地约 40 s |
| `human_30um` | 同一服务器，自动发现（`em/mip0`，1000 张 4096²，21 GB），尚未写清单、未跑 QC | — |

服务器上的 `Toy_datasets`、`allen_mouse`、`rat_100_100_100um` 目录结构不合默认约定（EM 目录叫 `EM` / `EM_img`，或一个目录下放多个基准），扫描时会报"no EM volume found"，需要在 `var/manifests/<目录名>.json` 里写 `em.path` 后再扫描。

demo 数据已删除。需要时可以再生成三份样例（合成缺陷集 + 两份本地 H01 切片）用于回归，生成后 `scan` 即注册，用完 `delete --files` 清掉：

```bash
.venv/bin/python -m emqc make-sample && .venv/bin/python -m emqc scan
.venv/bin/python -m emqc delete synthetic_defects h01_demo_z2048 h01_precomputed_demo --files
```

删除数据集会连带删除它的 QC 运行、切片与 block 结果、findings、ETL 指标、事件日志、trace、取数日志和预览图；`--files`（接口参数 `remove_files=true`，页面上会二次确认）才会删磁盘上的数据目录，且只允许删 `data_root` 之下的目录。测试自带独立的合成数据，不依赖这些样例。

## 现状与下一步

1. Tailscale 目录挂上后：确认真实数据格式与字段（`docs/INPUT_FIELDS.md` 第 5 节），把 `EMQC_DATA_ROOT` 指过去，`scan`。
2. 实现 5 个 stub 检查；用真实数据 + 人工标注回归阈值。
3. XY tile 化 block；把 `QCRunner.process_block` 分发到多机（`persist_block` 已幂等）。
4. GT / prediction 的一致性 QC（标签覆盖率、与 EM 的对齐）。

- `docs/ANNOTATE.md` — 切片标注页（VAST 风格：叠加浏览、吸色填色、连续翻页；写时复制 + 精确撤销）
- [SAM 切片补标使用指南](docs/SAM_USER_GUIDE.md) — 给标注同事的操作步骤、修正边界、填色、撤销和常见问题，入口 `/annotate`。
- [SAM 部署与接口说明](docs/SAM.md) — SAM 2.1 安装、GPU 配置、API 和回归验证；安装 `bash scripts/install_sam.sh`。
