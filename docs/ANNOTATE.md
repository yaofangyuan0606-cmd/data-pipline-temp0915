# 切片标注（VAST 风格）

需要用 SAM 圈选并补充标签时，请看 [SAM 切片补标使用指南](SAM_USER_GUIDE.md)。

页面：独立的「切片标注」工作区（侧栏顶部与「数据清洗」切换），三页——`/annotate` 标注工作台（全宽，按 `?` 看快捷键）、`/annotate/blocks` 数据块列表（形状、分割、改动数、数据目录与工作目录）、`/annotate/guide` 使用说明。数据源由 `EMQC_ANNOTATE_ROOT` 指定（可用 `EMQC_ANNOTATE_EXTRA_ROOTS` 以分号追加更多根目录），
该目录下每个含 `em.npy` 的子目录是一个数据块，H01 交付的 `blocks/h01/<block>/` 布局直接可用。

**数据目录是只读的。** 页面写出的一切——`seg_edit.npy` 工作副本、`edits/` 逐次记录、`edits.jsonl` 摘要——都放在
`EMQC_ANNOTATE_WORKDIR`（默认平台的 `var/annotate/<block_id>/`），交付目录里一个字节都不会多。早期版本曾把这些写进数据目录，
打开数据块时会自动迁移到工作目录，已有的改动不会丢。

**显示方向以 `visual/slices_em/*.png` 为准。** 数组的第一维是图像的行、第二维是列，一张切片就是 `arr[:, :, z]`，
不做转置；数据块目录里若有 `visual/slices_em/`，EM 层直接原样下发这些 PNG（字节相同）。屏幕坐标 x 是列、y 是行，
服务端按 `plane[y, x]` 取值。

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

## 接口

```
GET  /api/v1/annotate/blocks                          列出数据块
GET  /api/v1/annotate/blocks/{b}                      形状、体素尺寸、改动数
GET  /api/v1/annotate/blocks/{b}/em/{z}.png           EM 切片
GET  /api/v1/annotate/blocks/{b}/labels/{z}.png       标签索引图
GET  /api/v1/annotate/blocks/{b}/labels/{z}.json      索引→id 表与像素数
GET  /api/v1/annotate/blocks/{b}/pick?z=&x=&y=        光标处 id
POST /api/v1/annotate/blocks/{b}/fill                 {z,x,y,new_id,whole_slice}
POST /api/v1/annotate/blocks/{b}/paint                {z,points,radius,new_id}
POST /api/v1/annotate/blocks/{b}/merge-pair           {z,first:[x,y],second:[x,y]}
POST /api/v1/annotate/blocks/{b}/merge                {from_id,to_id,scope:block|slice,z}（脚本接口）
POST /api/v1/annotate/blocks/{b}/undo
POST /api/v1/annotate/blocks/{b}/new-id               最大 id + 1
GET  /api/v1/annotate/blocks/{b}/edits
```

## 快捷键

P 拾取 · F 填充 · M 合并（两次点击一对，保留第一块颜色） · B 画笔 · E 橡皮 · H 平移（或右键拖动 / 空格+拖动） · [ ] 画笔半径 · N 新建 id ·
O 只画边界 · V 并排/叠加 · C 对比滑块 · G 透明度渐变 · , . 透明度步进 · 按住 Tab 隐藏分割 · 0 适合窗口 · 1 原始尺寸 · +/- 缩放 · Ctrl+Z 撤销

## 已知限制

- 单片最多 65535 个 id（uint16 索引）；H01 一片几百到几千，够用。
- 页面上的填充、涂抹、两块合并都是二维的，只改当前这一片。三维分裂（把一个错并的细胞拆开）还没做。
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
