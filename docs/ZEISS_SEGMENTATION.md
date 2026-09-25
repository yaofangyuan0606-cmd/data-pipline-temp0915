# ZEISS 电镜分割软件是怎么用的

> 本文写给已有自研浏览器端 EM 切片标注平台的团队，平台支持画刷、填充、合并、SAM、多人协作和溯源。资料截至 2026-09，来自 ZEISS 官方知识库、产品页、论文和 image.sc 论坛。标【未核实】的内容没有在官方原文里得到确认。

**先说结论**

- ZEISS 没有一个单独叫“分割软件”的产品。做体电镜（vEM）分割的主力是 **arivis Pro**（Windows 桌面软件）加上 **arivis Cloud**（在浏览器里训练模型）。ZEN 里的 **Intellesis** 是一个更轻量的像素分类器。
- 常见流程：在采集端软件里拼接、对齐，导出 TIFF，导入 arivis Pro，稀疏标注训练深度学习模型，用 Pipeline 推理并做后处理，人工修正，最后看 3D、测量、导出。
- 对我们来说：ZEISS 适合做**粗分割和定量**。我们的平台补上它缺少的**多人校对、审核和溯源**。两边交换数据用 TIFF/OME-TIFF 原图加上标签图像。

---

## 1. ZEISS 这边其实是几样东西，各管哪一段

| 阶段 | 软件 | 管什么 | 备注 |
|---|---|---|---|
| 采集 | **Atlas 5**（Fibics 为 ZEISS 开发） | GeminiSEM/Sigma 上的连续切片（Array Tomography）；Crossbeam 上的 FIB-SEM（3D Tomography） | 不做分割；单帧最大 32k×32k (来源: https://www.zeiss.com/microscopy/en/products/software/zeiss-atlas-5.html) |
| 采集 | **ZEN core + Toolkit Volutome** | Volutome（SBF-SEM） | 采集的同时预先算好拼接和 z 对齐，输出 CZI/TIFF (来源: https://www.zeiss.com/microscopy/en/products/sem-fib-sem/sem/volutome.html) |
| 采集 | ZEN；DigitalMicrograph + SmartSEM | MultiSEM；Gatan 3View | 数据格式各不相同，需要各自的接入路线 (来源: https://www.frontiersin.org/journals/neuroscience/articles/10.3389/fnins.2023.1281098/full) |
| 预处理 | Atlas 5 后处理；ZEN blue 的 **EM Processing Toolbox** | 拼接、图像校正、z 对齐、TrueZ 等间距插值；把 SmartFIB TIFF 导入成 CZI、去条纹、去噪 | arivis Pro 里没有找到 vEM 对齐算子【未核实】 (来源: https://knowledge.zeiss.com/rms/en/zen/toolkits-modules/application-and-workflow-toolkits/em-processing-toolbox/workflow-overview) |
| 分割 | **arivis Pro**（原名 Vision4D，4.2 起改名；当前版本 4.5，2026-03-19 发布） | 经典算子、机器学习、深度学习串成 Pipeline，能处理 TB 级数据 | ZEISS 主推；ZEISS 自 2020-12 起控股 arivis (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/release-notes/arivis-pro-4-5) |
| 分割（训练） | **arivis Cloud**（原名 APEER） | 在浏览器里做稀疏标注并训练模型：语义分割用 U-Net，实例分割用 Mask2Former | 托管在 Microsoft Azure (来源: https://www.biorxiv.org/content/10.64898/2026.08.07.743540v1.full) |
| 分割 | **ZEN blue / ZEN core AI Toolkit 里的 Intellesis** | 本地训练的像素分类器（Random Forest） | 只做语义分割 (来源: https://knowledge.zeiss.com/rms/en/zen/toolkits-modules/visualization-and-analysis-toolkits/intellesis-segmentation) |
| 分割 | Dragonfly 3D World ZEISS edition | 第三方软件（Comet 开发），只通过 ZEISS 销售；有深度学习模块和切片配准 | (来源: https://www.zeiss.com/microscopy/en/products/software/dragonfly-3d-world-zeiss-edition.html) |
| 校对 | arivis Pro（Draw Objects、Splitting、Object Math）；arivis Pro VR | 逐层修轮廓、拆分、合并、在 VR 里雕刻 | 官方文档里没有多人审核流程 |
| 分析 | arivis Pro 的 Objects table；ZEN Image Analysis / 3D Toolkit | 导出体积、表面积、sphericity 等特征，格式为 XLSX/CSV | |

“Atlas Engine”不是 ZEISS 现在的产品名，可能是 Fibics 的旧称【未核实】(来源: https://www-em.materials.ox.ac.uk/zeiss-auriga)。

---

## 2. 最常见的用法：用 arivis Pro 做体 EM 分割

**第 0 步：准备工作站和许可**
- 系统要 Windows 11 64 位，显卡只支持 NVIDIA，不支持 macOS，也不支持 Intel/AMD 显卡。做 Analysis 建议内存 32 GB 以上。机器学习/深度学习加速要求 CUDA Compute Capability 7.5 及以上、驱动 576.02 及以上，安装时要勾选 GPU Support Package (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/system-requirements/basic-requirements ；https://knowledge.zeiss.com/rms/en/arivis-pro/system-requirements/additional-requirements-by-feature)。
- 本地 Deep Learning Trainer 官方强烈建议显存 8 GB 以上。
- 许可按模块卖：Analysis、AI toolkit、Batch Analysis、Exchange Objects（负责标签图像的导入导出）、VR toolkit 等。

**第 1 步：在采集端拼接、对齐，导出 TIFF 序列**
- 拼接和 z 对齐在 Atlas 5、Volutome 或 ZEN Blue 里做好，然后导出**逐张的 2D TIFF**。
- 不建议导出多页 TIFF，单个文件大约 4 GB 就会碰到格式上限 (来源: https://forum.image.sc/t/unable-to-load-large-image-stacks/84900)。
- Atlas 5 导出的 FIB-SEM CZI 有过负 Z 索引的 bug，Fiji/Bio-Formats 打不开 (来源: https://forum.image.sc/t/zeiss-czi-invalid-z-index-bug/74253)。
- arivis 官方格式清单（2022-12 版）里没有 MRC、DM3/DM4、Zarr，这类数据要先转成 TIFF；之后是否新增了这些格式【未核实】(来源: https://knowledge.zeiss.com/rms/en/arivis-pro/sis-converter/supported-file-formats/supported-image-file-formats)。

**第 2 步：导入成 SIS（arivis 自有的工作格式）**
- 把文件拖进 viewer，或者交给 SIS Converter 排队转换。
- 多张 TIFF 用 simple Z-stack 导入。文件命名复杂时用 Complex Import → Selection → Pattern matching...；编号不从 0 开始时用 Edit offsets 修正，再点 Refresh 预览 (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/sis-converter/using-sis-converter/complex-import/pattern-matching)。
- 4.3 起可以直接打开 CZI，不用转换。GZIP 无损压缩默认打开。
- SIS 是私有格式，开源工具读不了，而且会多占一份存储 (来源: https://forum.image.sc/t/how-to-open-arivis-sis-file-in-fiji-or-any-other-oss/56888)。

**第 3 步：核对标定**
- 按官方的 Checking and updating calibrations 确认体素尺寸（例如 SBF-SEM 为 6×6×50 nm），缺失的元数据补写进去 (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/using-arivis-pro/getting-started-with-arivis-pro/introduction)。
- FIB-SEM 数据要确认 Atlas 导出的是 nominal 切片，还是 TrueZ 等间距体数据 (来源: https://academic.oup.com/mam/article/32/4/ozag036/8723570)。

**第 4 步：训练深度学习模型**（这里写本地训练；arivis Cloud 见第 3 节）
- 入口是 Analysis 菜单 → Deep Learning Trainer。默认有 Background 和 Class 1 两类，用 + Add Class 加类。各类不能重叠，嵌套结构（如细胞内的线粒体）要分开训练。
- 选一个既有目标又有背景的平面，用 Draw Tool 画刷画若干个目标，再切到 Background 类画背景。不用把每个像素都标完，但**没标的区域里不能混进目标**。官方建议至少 100 个目标，要覆盖不同大小和形状，也要包含噪声大或明暗不均的平面 (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/using-arivis-pro/using-local-deep-learning-with-arivis-ai-toolkit/creating-a-new-dl-model/annotating-objects)。
- 点 Train 开始训练。训练通常要**几个小时，期间软件不能用**。4.3 起可以逐平面 Preview，也能接着训练。训完点 Open in pipeline (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/using-arivis-pro/using-local-deep-learning-with-arivis-ai-toolkit/creating-a-new-dl-model/training-the-network)。
- 参考案例：一篇 SBF-SEM 论文只标了 15 个平面，在 8 GB 显存的 Quadro RTX 4000 上训练 350 epochs，myelin 的 IoU 达到 0.95 (来源: https://pmc.ncbi.nlm.nih.gov/articles/PMC12008736/ 或 https://pmc.ncbi.nlm.nih.gov/articles/PMC13301085/ ，具体是哪一篇未复核)。
- 本地 Trainer 是否支持实例分割或真 3D 训练【未核实】。

**第 5 步：搭 Pipeline 跑分割**
- 操作路径：Analysis panel → + New Pipeline → 设置 Input ROI → + Add Operation。
- 深度学习算子有两个：**Deep Learning Segmenter** 直接输出对象；**Deep Learning Reconstruction** 输出概率图，之后再接 Blob Finder 等得到实例。
- 模型有两个来源：从文件载入 ONNX/CZANN（通常只能做语义分割）；或者在 Preferences 里配好 Access Token，再从 Model Store 下载 arivis Cloud 模型。实例模型只能走后一种，而且要装 Docker (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/arivis-ai-machine-learning-and-deep-learning/deep-learning-segmentation-pipelines/creating-pipelines-with-dl-segmentation)。
- 可以降尺度推理。一个 TEM 线粒体案例在 50% 尺度上运行，标注了 309 个线粒体，官方称耗时从数周降到数小时 (来源: https://www.zeiss.com/microscopy/en/resources/insights-hub/life-sciences/deep-learning-mitochondria-analysis-arivis.html)。
- 后处理依次是：Object Feature Filter 去掉小碎块，Segment Morphology 闭合断裂，Watershed 拆开粘连，然后 Store Objects、Export Object Features。
- 也可以只用经典方法：Membrane Detection 接 Membrane-based Segmenter，或者 Blob Finder、Cellpose-based Segmenter（4.4 起包含 Cellpose-SAM）。
- 批量处理用 Batch Analysis 或 arivis Hub。共享深度学习 Pipeline 时，模型要一起导出并重新关联。

**第 6 步：人工修正**
- 切到 2D 视图，打开 Draw Objects，用 Select Object 激活要改的对象；不激活就会新建对象。
- 左键拖动添加，右键或 erase 工具擦除。翻到相邻层时，已有轮廓显示为白色阴影，点一下即确认（变蓝）。
- **隔层画的轮廓会自动插值到中间各层**（蓝色虚线），之后还能再修。点 Finish 提交 (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/using-arivis-pro/drawing-objects-interactively/drawing-objects-in-2d/using-the-draw-objects-tool)。
- 其他工具：Magic Wand（2D/3D）；4D Viewer 里的 Splitting（在对象上拖一条线，再点 Apply Split）；Object Math（merge/subtract/intersect）；arivis Pro VR（另购）可以在 3D 里雕刻。

**第 7 步：看 3D**
- 4D Viewer 可以叠加体渲染和对象表面，并按特征给对象着色。
- 数据超出 GPU 能力时会自动降采样，细小结构可能看不清或出现摩尔纹 (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/imaging-basics/how-does-arivis-render-datasets-that-are-larger-than-the-video-memory-in-3d/rendering-3d-stacks-as-volumes)。
- Storyboard 可以导出 4K 视频；4.4 起有 Quick Movie Export。

**第 8 步：测量与导出**
- Objects table 里有体积、表面积、sphericity、3D Feret Max 等特征。
- 导出选项：
  - 特征表：XLSX（默认）或 CSV
  - 表面网格：.obj
  - 分割结果：标签图像（labelled images，需要 Exchange Objects 模块）
  - 原图：OME-TIFF
  - 示踪：SWC
- 另有 Python API (来源: https://knowledge.zeiss.com/rms/en/arivis-pro/using-arivis-pro/image-analysis-in-arivis-pro/workflow-description/export-object-features)。
- 标签图像的具体格式和位深【未核实】。

---

## 3. ZEN Intellesis 与 arivis Cloud 各自适合什么

| | arivis Pro 本地 DL Trainer | arivis Cloud | ZEN Intellesis |
|---|---|---|---|
| 在哪跑 | 本地 Windows + NVIDIA 工作站 | 浏览器，云端 GPU | 本地 ZEN blue / ZEN core |
| 模型 | 深度学习（类型未公开） | U-Net 语义分割（2D/3D）、Mask2Former 实例分割 | Random Forest；特征用 2D 滤波器或 VGG19 |
| 标注 | 稀疏画刷 | 部分标注；有基于 SAM 的辅助标注 | 稀疏画笔，重点画边缘 |
| 实例分割 | 【未核实】 | 支持 | 不支持，只做语义 |
| 数据是否出本机 | 不出 | 要上传 | 不出 |
| 适合 | 数据不能出境、已有 arivis Pro | 没有本地 GPU、需要实例分割、想少标 | 已在用 ZEN 的 FIB-SEM / CLEM 工作流，需要快速做语义分割 |
| 不适合 | 急着出结果（训练期间软件不可用） | 数据不能出境、需要调超参数 | TB 级 vEM、要求层间连续 |

**ZEN Intellesis 的用法**
1. 在 Tools → Toolkit Manager 里激活 AI Toolkit。
2. 导入 FIB-SEM 数据：在 Processing 选项卡先运行 Sort SmartFIB Tiffs，再运行 Import SmartFIB TIFFs，生成 CZI（需要 EM Processing Toolbox 许可）。之后可以做 Z-Stack Alignment with ROI 等预处理。
3. 建模型：Analysis 选项卡 → Intellesis Segmentation → New → Start Training → Import Images。
4. 标注：默认有 Object 和 Background 两类。Ctrl+滚轮调画笔大小，Ctrl+D 在标注和擦除之间切换。官方建议少而准，一定要画物体边缘和类间过渡。
5. 选特征：Basic Features 25/33 跑 CPU，Deep Features（VGG19）跑 GPU。
6. 点 Train & Segment 预览。预览只覆盖当前视口，最大 5000×5000 px；看结果补标后再训练。
7. 全量分割：到 Processing 选项卡运行 Intellesis Segmentation 函数，输出 Labels 或 Multi-Channel 图，外加 confidence map。

(来源: https://knowledge.zeiss.com/rms/en/zen/toolkits-modules/visualization-and-analysis-toolkits/intellesis-segmentation/intellesis-segmentation-models/creating-and-training-an-intellesis-segmentation-model ；https://knowledge.zeiss.com/rms/en/zen/toolkits-modules/application-and-workflow-toolkits/em-processing-toolbox/importing-smartfib-tiffs)

Intellesis 的其他特点：
- 运行现成模型不需要 AI Toolkit，只有训练需要，所以可以一台机器训练、多台机器只推理 (来源: https://knowledge.zeiss.com/rms/en/zen/toolkits-modules/visualization-and-analysis-toolkits/intellesis-segmentation/licensing-and-functionalities-of-intellesis-segmentation)。
- 配合 ZEN Connect 把光镜和 FIB-SEM 对齐后，可以用 Multispectral 模式训练。
- 局限：特征都是 2D 的，对 z-stack 很可能是逐层独立分类，层间可能不连续（这是根据特征列表推断的，官方没有明说【未核实】）。有用户在 2020 年报告，35 GB 数据用 Deep Features 256 跑了 10 到 30 小时，取决于工作站 (来源: https://forum.image.sc/t/fib-sem-image-processing-segmentation/36822)。

**arivis Cloud 的用法**
1. 用 ZEISS ID 登录，在 My Datasets 上传一个**有代表性的子集**，不用传整批数据。推荐 CZI。
2. 选 Semantic 或 Instance，建目标类和 background 类。
3. 标注：用 brush 涂一部分目标，打开 Autofill holes，在目标周围涂 background，与其他类重叠的部分会被自动裁掉，没标的像素训练时直接忽略。也可以用 2025 年上线的 SAM 辅助标注：悬停预览，单击保存。
4. 勾选训练图像，点 Train。超参数不开放，界面也不显示 IoU (来源: https://knowledge.zeiss.com/rms/en/arivis-cloud/ai-toolkit/annotate/key-takeaways/annotating-partially)。
5. 检查结果，只在出错的区域补标，然后重训。官方建议每类起步约 50 个对象、至少 5000 个像素 (来源: https://knowledge.zeiss.com/rms/en/arivis-cloud/ai-toolkit/annotate/key-takeaways/requirements)。
6. 使用模型：
   - 在云端批量分割，取回 label masks；
   - 语义模型在 My Models → Download ML Model 下载为 CZANN/ONNX；
   - 实例模型不能下载成文件，只能通过 Access Token 从 AI Model Store 拉取 Docker 容器 (来源: https://knowledge.zeiss.com/rms/en/arivis-cloud/ai-toolkit/where-can-i-use-my-model/instance-segmentation-models)。

官方 HeLa FIB-SEM 案例（8 nm 层厚）：每隔 10 层标 1 层，共 73 个平面；细胞核标了 53 个对象，线粒体标了 1,133 个，每种细胞器单独训练一个模型 (来源: https://www.zeiss.com/microscopy/en/resources/insights-hub/life-sciences/scalable-and-automated-ai-image-analysis-for-volume-electron-microscopy.html)。

---

## 4. 与我们平台怎么衔接

### 4.1 分工建议

- **ZEISS 做粗分割和定量**：arivis 擅长用稀疏标注训练模型、批量推理，也有现成的测量功能。
- **我们做校对、审核和溯源**：ZEISS 官方文档里没有多人审核或版本管理的流程。ZEN Connect 项目用 .a5lock 锁文件，同一时间只允许一人编辑 (来源: https://knowledge.zeiss.com/rms/en/zen/toolkits-modules/visualization-and-analysis-toolkits/zen-connect/project-and-image-management/creating-a-zen-connect-project)。arivis Cloud 的协作只到“模型 owner 共享模型”，加上产品页宣称的协同标注 (来源: https://www.zeiss.com/microscopy/en/products/software/arivis-cloud-ai.html)。

```
采集端（Atlas 5 / Volutome / ZEN）拼接对齐 → TIFF 序列
   ├─→ 我们平台：原图入库、切块浏览
   └─→ arivis Pro：DL 推理 + 后处理 → 标签图像
            └─→ 导入我们平台作初始标签 → 多人校对（merge/split/fill/SAM）
                     └─→ 导出校对后的标签 → 回 arivis 做测量，或作为下一轮训练数据
```

- **路线 A（建议先做）**：arivis 出粗分割，我们校对，再导回 arivis 做测量（用 Exchange Objects 导入标签图像）。
- **路线 B（我们的标注喂给 ZEISS 训练）**：
  - ZEN Intellesis 可以用 Import Labels from Binary Mask 导入外部二值图作为某一类的标注。要求 XY 尺寸一致，会覆盖该类原有标注，还可能占满内存 (来源: https://knowledge.zeiss.com/rms/en/zen/toolkits-modules/visualization-and-analysis-toolkits/intellesis-segmentation/importing-labels-from-binary-mask)。
  - arivis 本地 DL Trainer 和 arivis Cloud 能否导入外部标注作为训练数据，**没有查到**【未核实】。
- **模型互通**：
  - 我们自己训练的语义模型，可以用 czmodel 打包成 .czann（内含 ONNX），再导入 ZEN 或 arivis (来源: https://pypi.org/project/czmodel/)。
  - arivis Cloud 的语义模型导出成 ONNX/CZANN 后可以在 ZEISS 之外运行（例如 napari-czann-segment），理论上能接到我们平台做预标注；是否能在我们后端直接跑【未实测】(来源: https://github.com/sebi06/napari-czann-segment)。
  - 实例模型是 Docker 容器，绑定 ZEISS 生态，拿不出来。

### 4.2 格式对照

| 数据 | ZEISS → 我们 | 我们 → ZEISS | 容易出错的地方 |
|---|---|---|---|
| 原图 | 逐张 TIFF / OME-TIFF；CZI 可用 pylibCZIrw、libCZI（LGPL）或 Bio-Formats 读 (来源: https://pypi.org/project/pylibCZIrw/) | TIFF 序列，用 SIS Converter 的 Pattern matching 导入 | SIS 读不了，只能从 arivis 导出 OME-TIFF；Atlas 导出的 CZI 有负 Z 索引问题；多页 TIFF 有 4 GB 上限 |
| 实例分割 | arivis 标签图像（Exchange Objects） | 标签图像导入，或用 Python API | 位深和容器格式【未核实】 |
| 语义分割 | Intellesis 的 Labels 输出（单通道，每类一个像素值，8/16 bit），用 OME-TIFF 导出 | Import Labels from Binary Mask（每类一张二值图） | **导出时必须勾 Original Data**；勾了 Apply Display Curve 或 Convert to 8 Bit 会改写标签值；Image Export 会把 stack 拆成单张 (来源: https://knowledge.zeiss.com/rms/en/zen/basic-concepts/image-processing/image-processing-functions/export-import/ome-tiff-export/image-data-section) |
| 网格 / 测量 | .obj；XLSX/CSV | Import Surfaces | |

### 4.3 对接检查清单

- 两边的体素尺寸和 z 间距要一致；FIB-SEM 要分清 nominal 导出和 TrueZ 导出。
- 裁剪 ROI 的偏移、降尺度推理（如 50%）的换算关系，都要记进溯源。
- 标签的语义要约定清楚：实例 ID 还是类别值、背景值是否为 0、位深够不够。线粒体动辄上千个，8-bit 不够。
- 溯源里要记下 arivis Pro 版本、Pipeline 文件、模型文件（或 arivis Cloud 的 run ID 和模型版本）、推理尺度。原因是有用户报告同一 Cellpose 模型在 arivis、GUI、命令行下结果不同，4.5 版才改进了跨硬件的确定性 (来源: https://forum.image.sc/t/different-results-from-running-a-model-in-cellpose-gui-command-line-and-arivis/99434 ；https://knowledge.zeiss.com/rms/en/arivis-pro/release-notes/arivis-pro-4-5)。

---

## 5. 需要注意的点

**授权和费用**
- arivis Pro 各模块、ZEN 的各个 Toolkit（AI、3D、Connect、EM Processing、OAD 开发接口、第三方格式导入）都要单独询价，**价格不公开**。
- 机构订阅许可可以覆盖 arivis Pro 全部模块、arivis Cloud（1 TB）和 ZEN desk (来源: https://www.zeiss.com/microscopy/us/products/software/institutional-licenses.html)。
- arivis Cloud 学生版免费一年（100 GB 存储、每月 500 GPU 分钟）；Premium 需要询价，有 30 天试用 (来源: https://www.zeiss.com/microscopy/en/products/software/arivis-cloud-ai/pricing.html)。
- 跑实例模型要用 Docker Desktop，而 Docker Desktop 商用需要付费订阅（这是 Docker 自己的授权条款，不来自 ZEISS 资料）。

**硬件**
- 只支持 Windows 和 NVIDIA，**我们手头的 Mac 跑不了**，需要单独配一台 Windows 工作站。ZEN 3.14 起系统要求只列 Windows 11 (来源: https://knowledge.zeiss.com/rms/en/zen/installation-setup/system-requirements/zeiss-microscopy-software)。
- 实例模型需要 Docker Desktop 和 WSL2，NVIDIA GPU 计算能力 7.0 及以上、显存 8 GB 及以上，建议内存 64 GB (来源: https://knowledge.zeiss.com/rms/en/arivis-cloud/software-and-ecosystem-integration/windows-docker-requirements)。
- 这里的计算能力要求（7.0）和 arivis Pro 机器学习加速的要求（7.5）不一致，以更高的为准。

**数据大小**
- 导入 SIS 相当于把数据复制一份，存储翻倍。
- arivis Cloud 的 stack 上限：16-bit 灰度 1024×1024 约 5,500 层、512×512 约 6,000 层。官方建议对象不超过 320×320 px，高分辨率 EM 常常要裁剪或下采样 (来源: https://knowledge.zeiss.com/rms/en/arivis-cloud/ai-toolkit/dataset-requirements/must-meet-requirements)。
- Intellesis 训练时只预览视口内最多 5000×5000 px；官方文档没有写集群或分布式推理。

**国内使用和数据出境**
- arivis Cloud 托管在 Azure，但**官方没有公开存储区域**。www.arivis.cloud 解析到 Azure West Europe 的 IP，这只是间接线索，不能证明数据和算力就在那里【未核实】(来源: https://www.azurespeed.com/api/ipAddress?ipOrDomain=www.arivis.cloud)。
- 服务条款（2023-09 版）：签约方是 Carl Zeiss Microscopy GmbH，由慕尼黑法院管辖；ZEISS 可以用匿名化副本改进服务；备份由用户自己负责；合同终止后 30 天内可以申请导出数据；用户须遵守欧盟和美国的出口管制 (来源: https://asset-downloads.zeiss.com/catalogs/download/mic/24f06b23-d99c-4eb4-baaa-9bdc7cdabc46/EN_ZEISS_arivis_Cloud_Terms_and_Conditions.pdf)。
- 没有找到中国大陆区域或国内部署方案。国内访问速度没有实测，按国内数据出境规定能否上传也没有做合规评估【未核实】。可以通过蔡司中国热线 4006-800-720 咨询 (来源: https://www.zeiss.com.cn/microscopy/products/software/arivis-cloud-ai.html)。
- 数据不能出境时，可以改用 arivis Pro 本地 DL Trainer 或 ZEN Intellesis，全程在本地。但从 Model Store 下载实例模型仍然要联网。

---

## 主要未核实项汇总

- arivis Pro 是否内置 FIB-SEM / 连续切片的对齐算子（没找到，目前按“在采集端对齐”处理）。
- 本地 DL Trainer 是否支持实例分割或真 3D 训练，开放哪些训练参数。
- arivis 标签图像导出的具体格式和位深；arivis 本地训练和 arivis Cloud 能否导入外部标注作训练数据。
- Intellesis 对 z-stack 是否逐层独立分类（根据特征列表推断）。
- arivis Cloud 的实际存储区域、国内访问质量、数据出境合规性；Premium 价格。
- 2022-12 之后 arivis 是否新增了 MRC、DM3/DM4、Zarr 支持。
- Atlas 5 工程文件能否直接导入 arivis，TIFF 里的像素尺寸能否被自动读出。
- image.sc 上几乎没有 EM 标注员的使用反馈；交互体验（快捷键、画刷手感、大数据下的卡顿）只能从论文和厂商案例间接推断。