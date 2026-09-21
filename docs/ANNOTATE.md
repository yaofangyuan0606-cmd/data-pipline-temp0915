# 切片标注（VAST 风格）

需要用 SAM 圈选并补充标签时，请看 [SAM 切片补标使用指南](SAM_USER_GUIDE.md)。

页面：独立的「切片标注」工作区（侧栏顶部与「数据清洗」切换）——`/annotate` 标注工作台（全宽，按 `?` 看快捷键）、`/annotate/compare` 前后对比与溯源、`/annotate/blocks` 数据块列表（形状、分割、改动数、数据目录与工作目录）、`/annotate/guide` 使用说明。数据源由 `EMQC_ANNOTATE_ROOT` 指定（可用 `EMQC_ANNOTATE_EXTRA_ROOTS` 以分号追加更多根目录），
该目录下每个含 `em.npy` 的子目录是一个数据块，H01 交付的 `blocks/h01/<block>/` 布局直接可用。

**数据目录是只读的。** 页面写出的一切——`seg_edit.npy` 工作副本、`edits/` 逐次记录、`edits.jsonl` 摘要——都放在
`EMQC_ANNOTATE_WORKDIR`（默认平台的 `var/annotate/<block_id>/`），交付目录里一个字节都不会多。早期版本曾把这些写进数据目录，
打开数据块时会自动迁移到工作目录，已有的改动不会丢。

**显示方向以源数据集为准（2026-09 改）。** `fetch_train.py` 存下来的是 CloudVolume 的原生轴序，所以数组的第 0 维是
体数据的 **X**、第 1 维是 **Y**；一张切片是 `arr[:, :, z].T`，**转置之后再显示**。这样屏幕的横轴就是体数据的 X、纵轴是 Y，
和 Neuroglancer 里看到的同一块画面完全重合——改之前两者差一个转置，跳过去对不上。屏幕坐标 x 是列、y 是行，
服务端拿到后按 `plane[y, x]` 取值，写回磁盘时由 `Block._disk_idx` 换回 `(x, y)`。

这条改的只是**看**的方向：`em.npy` / `seg.npy` 一个字节都没动，`edits/*.npz` 里记的始终是磁盘下标，
用改之前的记录回放也照样还原。代价是 `visual/slices_em/*.png` 与屏幕差一个转置，不能再原样下发，
EM 层一律由 `em.npy` 现渲染。对比页的 EM、原始标签、工作副本及来源图也使用同一显示方向。

`visual/slices_seg_color/*.png` 不能当标签用：它是 EM 与哈希颜色的叠加图，同一个 id 的像素在图里有几千种颜色，
和 EM 灰度的相关系数 0.79，id 无法从颜色反推。标签只来自 `seg.npy`；页面上的着色由 id 哈希生成，与那套 PNG 不同。

## 三个核心功能怎么实现的

**叠加与并排**。EM 永远是底图，分割是上层。叠加视图：一个视窗，分割按透明度盖在 EM 上，可只画边界；
三种对比手段——透明度滑块（, . 步进）、**自动渐变**（G，分割在 EM 上周期性淡入淡出）、**对比滑块**（C，拖一条分界线，
左边纯 EM、右边叠加）、按住 Tab 临时隐藏分割。并排视图（V）：两个视窗，左边纯 EM、右边 EM+分割叠加（可切成只看分割），
缩放、平移、翻页完全同步，鼠标在一边时另一边同一位置画十字线，所有工具在两个视窗里都能用。
分割层只栅格化一次进离屏 canvas，透明度在绘制时用 globalAlpha 施加，所以拖滑块和渐变都不重算像素。
每个视窗是三层 canvas：EM 灰度、分割着色、悬停高亮。分割不是直接传彩色图，而是把这一片出现的
id 重新编号成 0..k 的 uint16 索引图（PNG 的 R、G 通道各存一个字节）连同"索引→id"表一起下发；着色、吸色、
高亮全在浏览器里对着索引图做，不用回服务器。颜色由 id 的哈希决定，同一个神经元在任何一片、任何一次打开都是同一种颜色。
id 一律按字符串传——H01 的 id 超过 2^53，JSON 数字会丢精度。

**吸色 / 填色**。拾取工具（或任何工具下 Alt+点击）读光标下的索引查表得到 id，设为当前标签。填充工具对点到的
像素做 4 连通泛洪，把整个连通区域改成当前标签（Shift+点击改整片同 id）；浏览器先在索引图上本地泛洪立刻显示，
同时把 (z, x, y, new_id) 发给服务器，服务器在体数据上做同样的泛洪并落盘，返回后前端重新拉这一片校准。画笔 /
橡皮按半径涂抹，鼠标抬起时把整条轨迹一次发过去。

**悬停高亮**。只高亮当前鼠标所在的四连通区域，同一标签在画面里其他不相连的位置不会一起亮。
第一块的黄色选框也只圈出点击位置所在的连通区域。

**合并**。工具 M，每两次点击为一对，固定保留第一块的颜色：先点 A、再点 B，B 变成 A 的标签和颜色；
选择立即清空，再点 C、D，D 变成 C 的标签和颜色。只改当前切片中第二次点击的四连通区域，其他不相连区域与其他切片不变。
界面不再提供方向和范围选择。保存期间暂停编辑，防止连点复用上一对的选择；成功、无变化或失败后都从新的一对开始。
Alt+点击可重选第一块；Esc、切换工具、切片或数据块会取消未完成的一对。Ctrl+Z 一次撤销一对。
已经具有相同标签的两块无需再改，点击后也清空选择。底层 `/merge` 接口仍保留整片/整块按标签合并供脚本使用。

**连续翻页**。滚轮一格一片（Ctrl+滚轮是缩放），↑↓ 或 W/S 单步，PageUp/Down 十步，Home/End 首尾，
也有滑块和"连播"（可调每秒张数）。浏览器缓存最近 24 片并预取前后各 3 片，翻页基本不等待。

## 改动怎么落盘

原始 `seg.npy` 永远不动。第一次改动时把它整体复制为 `seg_edit.npy`（写时复制，839 MB 的块约几秒，只发生一次），
之后所有改动都写在副本上。每次改动另存一份 `edits/<n>.npz`——被改的每个体素的 (x, y, z) 坐标和它改动前的 id——所以撤销是精确
还原而不是"再填回去"（填充可能已把两个区域并成一个，反向泛洪会改错）。`edits.jsonl` 是给界面看的摘要。

交付给下游时直接用工作目录里的 `seg_edit.npy`，它和 `seg.npy` 同形同类型；想回到原始状态删掉工作目录里的
`seg_edit.npy`、`edits/`、`edits.jsonl` 三样即可，数据目录本来就没被碰过。

## 前后对比与溯源

从工作台右上角「前后对比」进入 `/annotate/compare?block=<block_id>&z=<切片号>`，保留当前数据块与切片。
左图叠加原始 `seg.npy`，右图叠加当前 `seg_edit.npy`（尚未编辑时与原始相同）。两边共享标签配色、透明度、
缩放、滚动位置和光标，支持修改区域高亮；右图还可按来源着色。页面只读取数据；点击「刷新当前结果」获取其他页面的新修改。

报表按**当前像素的最后一次有效写入**汇总，再按标签 ID 统计。同一 ID 可以同时包含多种来源：

| 来源键 | 含义 |
| --- | --- |
| `baseline` | 原始分割中未被有效编辑覆盖的部分，不推断为人工真值 |
| `manual` | 画笔、橡皮、填充、清除、合并、切割、分离 |
| `sam` | SAM 应用；基线 `meta.json` 含非空 `sam` 配置的非背景区域；或 `sam_merge.first_new_id/n_new_ids` 明确记录的预填 ID 范围 |
| `interpolation` | 插值修补，操作记录保留 `source_sections` 参考层号 |
| `assisted` | 基于膜边界的智能填充，与纯人工、SAM 分开 |
| `unknown` | 工作副本中无对应记录的修改、未知工具、缺失/损坏记录或当前像素与记录不符 |

人工点击「应用」SAM 或插值预览仍归相应算法来源；之后人工改写的像素归人工。来源不表示人工审核状态。
撤销后按剩余有效记录重新计算；这里不是包含已撤销操作的永久审计日志。旧记录没有 `source` 字段时按 `kind`
识别，新记录同时写 `source` 和 `provenance_version: 1`。新切割记录保存逐像素新标签，旧版多块切割若只记了一个新 ID，
无法精确核实时显示来源不明。基线和历史编辑文件均无需迁移；对比专用的数据块列表和读取入口会在原位置读取旧工作文件，
不会触发标注工作台的历史迁移逻辑。工作台与对比读取在同一进程内共享数据块锁。

报表包含来源像素数、各标签前后像素数、混合来源标记，以及有效操作和缺失证据提示。`changed_px` 是与原始分割
不同的像素数；逐标签行中的该字段只计**当前归属该 ID**的变化，旧 ID 的减少体现在 `before_px/current_px`。
标签 0 表示背景，人工擦除也可追溯；顶部来源卡片只计非背景标签，JSON 另列 `background_pixels`。
JSON 中所有标签 ID 均为字符串，CSV 保留完整十进制值；用电子表格打开 CSV 时应将 `label_id` 列按文本导入。

页面可下载当前切片或整个块的 JSON / CSV。当前切片导出与屏幕快照一致；整块导出在请求时重新读取，
逐切片处理以避免加载整块 uint64 数组。`compare` 返回的图片与报表在同一数据块锁下读取，并禁用 HTTP 缓存。
该锁仅在当前服务进程内有效，沿用现有单进程标注工作流；不提供跨进程的并发编辑保护。

## 接口

```
GET  /api/v1/annotate/blocks                          列出数据块
GET  /api/v1/annotate/comparison-blocks               只读列出对比数据块，不迁移旧工作文件
GET  /api/v1/annotate/blocks/{b}                      形状、体素尺寸、改动数
GET  /api/v1/annotate/blocks/{b}/em/{z}.png           EM 切片
GET  /api/v1/annotate/blocks/{b}/labels/{z}.png       标签索引图
GET  /api/v1/annotate/blocks/{b}/labels/{z}.json      索引→id 表与像素数
GET  /api/v1/annotate/blocks/{b}/compare/{z}          同一快照的原始/当前索引图、EM、差异图、来源图与报表
GET  /api/v1/annotate/blocks/{b}/provenance?z=0       当前切片的 JSON 溯源报表（不传 z 则汇总整个块）
GET  /api/v1/annotate/blocks/{b}/provenance?format=csv 逐标签 CSV 报表（可加 z 限定切片）
GET  /api/v1/annotate/blocks/{b}/pick?z=&x=&y=        光标处 id
POST /api/v1/annotate/blocks/{b}/fill                 {z,x,y,new_id,whole_slice}
POST /api/v1/annotate/blocks/{b}/paint                {z,points,radius,new_id}
POST /api/v1/annotate/blocks/{b}/merge-pair           {z,first:[x,y],second:[x,y]}
POST /api/v1/annotate/blocks/{b}/merge                {from_id,to_id,scope:block|slice,z}（脚本接口）
POST /api/v1/annotate/blocks/{b}/undo
POST /api/v1/annotate/blocks/{b}/new-id               最大 id + 1
GET  /api/v1/annotate/blocks/{b}/edits
POST /api/v1/annotate/blocks/{b}/smart-fill/preview  {z,x,y,sensitivity,max_radius,scope} → 掩膜 PNG + token
POST /api/v1/annotate/blocks/{b}/smart-fill/apply    {token,new_id}
POST /api/v1/annotate/blocks/{b}/split               {z,x,y}（把点到的连通块分出来给新 id）
POST /api/v1/annotate/blocks/{b}/cut                 {z,points}（画线切开色块）
GET  /api/v1/annotate/blocks/{b}/neuroglancer?z=&x=&y=  该点在公开 H01 Neuroglancer 中的 3D 链接
GET  /api/v1/annotate/blocks/{b}/neuroglancer/block     整块在查看器里的链接（画出范围）
GET  /api/v1/annotate/blocks/{b}/repair/scan            扫描整块，列出图像被毁的切片
POST /api/v1/annotate/blocks/{b}/repair/preview         {z,dark} 用上下切片插值补标签，返回预览与 token
POST /api/v1/annotate/blocks/{b}/repair/apply           {token}
```

## 快捷键

P 拾取 · F 填充 · M 合并（两次点击一对，保留第一块颜色） · B 画笔 · E 橡皮 · H 平移（或右键拖动 / 空格+拖动） · [ ] 画笔半径 · N 新建 id ·
O 只画边界 · V 并排/叠加 · C 对比滑块 · G 透明度渐变 · , . 透明度步进 · 按住 Tab 隐藏分割 · 0 适合窗口 · 1 原始尺寸 · +/- 缩放 · Ctrl+Z 撤销

## 已知限制

- 单片最多 65535 个 id（uint16 索引）；H01 一片几百到几千，够用。
- 页面上的填充、涂抹、合并、切割、分离、清除都是二维的，只改当前这一片。三维分裂（把错并的细胞在整个块里拆开）还没做。
- 智能填充（`emqc/annotate/boundary.py`）靠电镜自身的膜边界圈选，不依赖已有标签，因此标签错了也不会跟着错；但膜有缺口、切片全黑或拼接处对比度骤变时会圈不准，所以做成了先预览再应用。
- 切割要求线从色块外画到色块外；没穿透会报错而不是切出错误结果。切出的最大一块保留原 id。
- 修补损坏切片（`emqc/annotate/interpolate.py`）：黑带或白页上，**图像不恢复也不伪造**，只用上下相邻切片的细胞形状做有符号距离场插值，把标签补回来。改动记为 `repair` 并带 `interpolated` 标记与来源切片号，下游不会误当成观测数据。预览里绿色是补出来的，斜纹处上下两片不一致（把握较低），红色是没有细胞认领的空隙。算法是五种方案实测选出来的（留出集逐细胞 IoU 0.70，基线 0.58），并且是唯一能扛住**连续两片损坏**的——真实黑切常常连片出现，其余四种在那种情况下比直接抄一片还差。
- 3D 查看（快捷键 U）把光标处换算成数据集自身的体素坐标，交给公开的 H01 Neuroglancer。一张切片答不了「这团黑的是细胞器、独立细胞还是切片损伤」，3D 能。块的 `meta.json` 里 `geometry.origin` 是 mip1 体素单位，而查看器的默认坐标系正是 8/8/33 nm，所以直接相加即可，象限块再加上 `offset_in_parent`。只有本平台**没有改过号**的 id 才会传给查看器选中——SAM 预填和「新建 ID」造的号在公开的 c3 分割里不存在，传过去会选中无关的细胞，所以被挡掉并给出说明。非 H01 数据块不给链接，只说明原因。
- 没有多人并发控制；同一个块同时开两个页面改，后写的覆盖先写的。

## 回归验证

默认 `python -m pytest tests -q` 覆盖标注接口、两两合并、其他同标签区域与切片保持不变、精确撤销及原始标签保护。

真实浏览器交互测试单独启用，使用本机 Chromium 和独立合成数据：

```bash
EMQC_BROWSER_TESTS=1 python -m pytest tests/test_annotate_browser.py -q
```

需安装 `google-chrome` 或 `chromium`，以及 `websockets`（已包含于 `uvicorn[standard]`）。
如果临时目录路径过长，设置 `CHROME_TMPDIR` 为较短的可写临时目录，避免 Chromium Unix socket 路径长度限制。
浏览器测试不连接真实标注数据；无浏览器的环境可以运行默认接口回归。
