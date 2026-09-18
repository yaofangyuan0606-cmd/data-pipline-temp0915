# SAM 辅助补标

标注同事请先看 [SAM 切片补标使用指南](SAM_USER_GUIDE.md)。本文主要供部署者和接口调用者使用。

使用 Meta 官方 **SAM 2.1 Hiera Large** 做当前切片的交互式分割，入口为 `/annotate` 左栏。
输入是原始灰度 EM（复制为 RGB），不拿彩色 seg 预览当模型输入。
默认只补现有标签为 0 的像素；标签 0 表示未赋实例标签，不能直接等同于真实生物学背景。

1. 选数据块、切片，点击 **SAM 点选**，在黑色膜边界包围的目标内点击。
2. 青色是候选区域。普通点击或框选开始新目标，自动清除旧提示；按住 **⌘（Mac）/Ctrl（Windows、Linux）** 点击给当前目标补点，Shift+点击添加排除点。按住 ⌘/Ctrl 框选可保留已有提示。
3. 边界不合适时切换三个候选，继续补点，或清除提示重试。框选和点选可以组合。
4. 点击 **填为新标签**，或 Alt+点击拾取已有标签后点击 **填为当前标签**。
5. 写入 `EMQC_ANNOTATE_WORKDIR/<block_id>/seg_edit.npy`，保留逐像素撤销记录。Ctrl+Z 精确撤销；数据目录中的 `seg.npy`、EM 及原始快照不变。

默认勾选“仅补未标注区域（标签 0）”，预览已经扣除现有有色标签。
要修正已有分割，取消勾选并重新预测后应用。
切换切片/数据块、离开 SAM 工具、Esc 或清除提示会丢弃当前预览；预览不产生编辑。
预览有效期 15 分钟，服务最多保留 32 个。若标注已变化或服务器重启，必须重新预测。
标签基线不存在的数据块可预览，暂不支持应用。

SAM 是通用图像分割模型，未在本项目电镜上微调；不是沿黑线必然精确的膜分割器。
细薄膜、断膜、内部细胞器、tile 边缘和跨片一致性仍需要人工核对。候选分数是模型估计值，不是经真值验证的准确率。
当前只处理二维当前切片，不自动传播至 100 层，也不自动运行整卷全实例分割。

## 安装与启动

```bash
bash scripts/install_sam.sh
bash scripts/dev_serve.sh 8765
```

先按项目 README 建好 `.venv`、安装基础依赖并配置数据库和标注数据目录，再执行上述脚本。
脚本面向 Linux + NVIDIA GPU，使用 PyTorch 2.10.0 + CUDA 12.8 / torchvision 0.25.0；已在 RTX PRO 6000 Blackwell 上验证。
源码位于 `var/vendor/sam2`，固定提交 `2b90b9f5ceec907a1c18123530e92e794ad901a4`。
权重位于 `var/models/sam2.1_hiera_large.pt`（约 857 MiB），SHA256：
`2647878d5dfa5098f2f8649825738a9345572bae2d4350a2468587ece47dd318`。
来源：[Meta 官方仓库](https://github.com/facebookresearch/sam2)，Apache 2.0。
不编译可选 connected-components CUDA 扩展；此处图像预测器使用默认关闭的孔洞/小点后处理。

可选 `.env` 配置：

```dotenv
EMQC_SAM_CHECKPOINT=./var/models/sam2.1_hiera_large.pt
EMQC_SAM_CONFIG=configs/sam2.1/sam2.1_hiera_l.yaml
EMQC_SAM_DEVICE=cuda:0
```

首次预测加载模型，随后缓存最近一张切片的图像特征。GPU 推理串行处理，避免不同用户的提示混用同一预测器状态。
单进程启动即可，不要增加多个 Uvicorn worker（内存中的预览令牌不共享）。

## API

`GET /api/v1/annotate/sam/status`：安装/加载状态。

`POST /api/v1/annotate/blocks/{block}/sam/predict`：

```json
{"z":0,"points":[[150,200]],"labels":[1],"box":null,"only_background":true,"candidate":null}
```

点使用当前显示坐标 `(x,y)`（x 为列，y 为行；数组按 `arr[y,x,z]` 访问），标签 1 包含、0 排除；框为 `[x0,y0,x1,y1]`，候选为 null（最高分）或 0/1/2。
返回 `token`、RGBA `mask_png` 数据 URL、像素数、耗时及候选分数。

`POST /api/v1/annotate/blocks/{block}/sam/apply`：`{"token":"…","new_id":"9007199254740993"}`。
标签 ID 使用字符串保留 uint64 精度。返回与现有编辑接口一致的编辑记录，可调用现有 `/undo`。

## 浏览器与 GPU 回归

默认测试覆盖非方形坐标、独立工作目录、uint64 标签、旧预览拒绝和精确撤销。
可选真实浏览器测试使用 Playwright Headless Shell，在临时数据副本上实际点击页面：

```bash
PIP_CACHE_DIR="$PWD/var/cache/pip" TMPDIR="$PWD/var/tmp" .venv/bin/pip install playwright
PLAYWRIGHT_BROWSERS_PATH="$PWD/var/cache/ms-playwright" TMPDIR="$PWD/var/tmp" .venv/bin/python -m playwright install chromium
EMQC_PLAYWRIGHT_TESTS=1 \
EMQC_SAM_SMOKE_SOURCE="$PWD/var/local-test-blocks/example-block" \
PLAYWRIGHT_BROWSERS_PATH="$PWD/var/cache/ms-playwright" \
TMPDIR="$PWD/var/tmp" TORCH_HOME="$PWD/var/cache/torch" CUDA_CACHE_PATH="$PWD/var/cache/cuda" \
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_sam_browser.py -q
```

运行前把示例中的 `EMQC_SAM_SMOKE_SOURCE` 改为实际的本地 H01 YXZ 数据块目录：测试只读取它的两层并复制到隔离目录，不写源数据。当前用例在 `(150,200)` 点选，因此需使用该位置含可分割未标注区域、至少两层的适用样本；不随仓库分发真实数据。
不指定时仅执行两两合并浏览器回归；需可用 CUDA 和已安装的 SAM 权重才能执行 GPU 用例。
