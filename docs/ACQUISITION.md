# 数据采集：云端 precomputed 卷（Neuroglancer 生态）

平台的第三种数据源。前两种是本地目录和 SSH 远程目录，这一种是**按 XYZ 坐标从公开的 precomputed 卷取数据**，
由原先独立的 H01 爬取工具移植而来。

## 为什么只移植了三个阶段

原工具是七阶段流水线（索引 / 预判 / 取体素 / 渲染 / 截图 / 导出 Neuroglancer / 连接组）。
其中前三阶段是数据获取，属于数据清洗平台；后四阶段是可视化与对比，属于路线图第 9 步 Failure Explorer，没有移植。

| 阶段 | 落在平台哪里 |
|---|---|
| 索引：从 segment_properties 选爬哪里 | `emqc/crawl/index.py` · `GET /api/v1/crawl/index` |
| 预判：用组织类型图判断 ROI 值不值得爬 | `emqc/crawl/precheck.py` · `POST /api/v1/crawl/precheck` |
| 取体素：按 XYZ 读 | `emqc/registry/cloud.py` · 注册后由 QC / patch 按需读 |

## 两条使用路径

**注册（不下载）**：ROI 成为一个数据集，`root_path` 是 precomputed URL，ROI 存在元信息里。
QC、Patch Factory、数据交付全部照常工作，体素按需读取。适合先看质量、先出样本。

```bash
python -m emqc cloud register --dataset-id h01_roi_x260k \
  --url gs://h01-release/data/20210601/4nm_raw --mip 1 \
  --roi 260000-260512,210000-210512,2600-2632 \
  --seg-url gs://h01-release/data/20210601/c3 --species human --brain-region "temporal cortex"
python -m emqc run h01_roi_x260k          # 直接对云端 ROI 跑 QC
```

**下载落盘**：把 ROI 写成 `data_root` 里的普通图像栈，之后就是本地数据集。任务可中断续传。
适合同一块数据要反复读的场景。

```bash
python -m emqc cloud fetch --dataset-id h01_roi_x260k --url ... --roi ... --out ...
python -m emqc scan
```

## 三个设计要点，都来自原工具的实测

**chunk 是传输的最小单位。** 逐张读切片会把同一个 128×128×32 的 chunk 重复下载 32 次。
`CloudVolumeReader` 按 z-chunk 取回并缓存到本地磁盘，一次下载服务 32 张切片。实测第一张 11 秒、第六张 0 秒。

**对齐到 chunk 网格是免费的。** 同样的 chunk 要过网，对齐与否流量完全相同，但对齐后拿到的体素多得多。
所以注册时默认把 xy 向外吸附到 chunk 边界，并且**把吸附后的 ROI 存为数据集的 ROI**——
形状和 ROI 必须一致，否则声明的尺寸和实际读到的对不上。z 不对齐，否则会引入 ROI 之外的切片。

**组织类型图不是缺陷掩码。** H01 的 masking 层是 64 nm 的组织类型分割（neuropil / nucleus / blood vessel /
myelin / fissure），其中只有 fissure 是成像缺陷，neuropil 才是有突触可学的区域。
换算倍数必须**逐轴**取：masking 是 64/64/66 nm，ROI 是 8/8/33 nm，所以 x、y 除 8 而 z 只除 2。
三轴统一除 8 会读到体积里完全不同的位置，而且不会报错——这是原工具里真实发生过的 bug，修复后覆盖率从 25% 变成 100%，
三个测试 ROI 里有两个的结论直接翻转。

## 坐标约定

`roi` 用**所选 mip 的体素单位**，不做隐式换算（原工具的 per-axis 除数 bug 正是隐式换算造成的）。
注册时会打印物理尺寸（µm）供核对。H01 的 EM mip1 与 c3 mip0 都是 8 nm 网格，坐标可直接共用。

## 有损编码

H01 的 `4nm_raw` 是 **jpeg 有损**，`c3` 与 `masking` 是 compressed_segmentation 无损。
卷信息接口和注册元信息里都会标出 `lossy`，因为在有损 EM 上做模糊、对比度这类检查，测到的一部分是压缩伪影。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/crawl/presets` | 内置数据源（当前 H01） |
| GET | `/api/v1/crawl/volume?url=&mip=` | 卷的尺寸、chunk、分辨率、编码，不下载体素 |
| POST | `/api/v1/crawl/precheck` | ROI 的组织构成与是否值得爬 |
| GET | `/api/v1/crawl/index` | 从 segment_properties 选候选 ROI |
| POST | `/api/v1/crawl/register` | 注册 ROI 为数据集（不下载） |
| POST/GET | `/api/v1/crawl/jobs` … `/{id}/events` `/{id}/cancel` | 下载任务与实时日志 |

页面在 `/crawl`。

## 在 H01 上的实测（2026-09-14）

ROI x260000-260512, y210000-210512, z2600-2632，mip1：

- 预判：neuropil 99.6%、fissure 0.00% → 值得爬，耗时 2.5 秒
- 注册：申请 512×512 → 对齐后实读 640×640×32，物理尺寸 5.12×5.12×1.06 µm
- QC：A 级，留存率 100%，2 条 finding
- 标签：c3 分割与 EM 同为 8 nm 网格，验证通过，可出 patch
- patch：segmentation 8 个、membrane 8 个，硬检查全零
