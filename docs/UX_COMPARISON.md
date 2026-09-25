# 切片标注工具 UX 对比与差距分析

> 文档位置：`docs/UX_COMPARISON.md`　日期：2026-09-24
> 依据：VAST、ImageJ/Fiji、webKnossos、Neuroglancer(+FlyWire/CAVE)、napari/Paintera/KNOSSOS/ilastik/CATMAID/CVAT/Label Studio 五份调研报告，以及 em-qc-platform「切片标注」的现状描述。
> 诚实声明：对标工具的行为来自公开手册、源码与 changelog，报告中自己标注为「未核实」的条目在本文中同样标出；我们工具的现状按团队给出的描述记录，未逐项在代码里复核。

---

## 1. 一句话定位每个工具

| 工具 | 一句话定位 |
|---|---|
| **VAST / VAST Lite 1.5.0** | 为 Wacom 数位笔量身定做的 Windows 单机「2D 画图机」：键位密度极高、按住修饰键即变工具、绘制永远写在当前 mip 上所以永不卡顿；代价是没有 Undo、不自动保存、单用户。(来源: https://lichtman.rc.fas.harvard.edu/vast/ ; https://doi.org/10.3389/fncir.2018.00088) |
| **webKnossos** | 为「超大体量 EM、长时间、多人」打磨过的浏览器工作站：三正交视口 + 3D 网格常驻、32³ 桶流式加载、会话内全撤销/重做、30 秒自动保存、按版本回滚、任务派发与用时统计一体化。(来源: https://docs.webknossos.org/) |
| **Neuroglancer (+FlyWire/CAVE)** | 键盘驾驭的高性能「看图器」，本体无画笔无撤销；整个视图就是一份 JSON，链接即画面；编辑能力来自 PyChunkedGraph 超体素图（合并/最小割拆分、每笔带作者与时间戳、反向操作式撤销）。(来源: https://github.com/google/neuroglancer ; https://pmc.ncbi.nlm.nih.gov/articles/PMC8903166/) |
| **ImageJ / Fiji（Labkit、TrakEM2）** | 不是一个工具而是一堆风格不同的窗口：Labkit 上手快能开 TB 级但完全没有撤销；TrakEM2 画笔语义最成熟（Overlap/Exclude/Erode、32 步撤销重做、跨片插值）但学习曲线陡、界面老旧；核心 ImageJ 只撤一步。(来源: https://imagej.net/plugins/labkit/documentation ; https://syn.mrc-lmb.cam.ac.uk/acardona/trakem2_manual.html) |
| **napari (Labels 层)** | Python 生态里的桌面体积查看/标注器：数字键切工具、B 键「保留标签」让画笔只补空白、橡皮只擦当前 id，100 步撤销重做；本身不面向多人与 EM 校对，靠插件补翻片/插值。(来源: https://napari.org/stable/howtos/layers/labels.html) |
| **Paintera** | 只有按住 Space 时鼠标才是画笔的「先画到画布、再提交」编辑器；SAM 实时跟随光标、两片之间形状插值、即时 3D 网格；绘制无撤销。(来源: https://github.com/saalfeldlab/paintera/blob/master/README.md) |
| **KNOSSOS** | 以「工作模式」为中心的 Qt 桌面工具：Paint/Overpaint/Merge/Review 等模式各有一张实时 Cheatsheet，Review 模式禁止一切修改，Movement area 区域外变暗不可改。(来源: https://knossos.app/documentation) |
| **ilastik carving** | 不描边只画种子：物体内外各几笔，分水岭沿膜边界收敛，BG priority 滑块调贴边松紧。(来源: https://www.ilastik.org/documentation/carving/carving) |
| **CATMAID** | 浏览器端多人骨架追踪：每个节点记创建者/编辑者/时间，Review widget 用 Q/W 逐节点审核、被移动的节点自动回到未审。(来源: https://catmaid.readthedocs.io/en/stable/) |
| **CVAT / Label Studio** | 通用标注平台，专长是流程而非 EM：作业 Stage/State 状态机、钉在画面上的 Issue、驳回自动回流、GT 暗抽检、一致性面板、快捷键按作用域自定义。(来源: https://docs.cvat.ai/docs/qa-analytics/manual-qa/ ; https://docs.humansignal.com/guide/quality.html) |
| **我们（em-qc-platform 切片标注）** | 浏览器端、改动即保存、逐笔可精确撤销、多人带归属与冲突检测、自带前后对比与操作流水的「安全网优先」2D 校对器；手感（键位/修饰键/画笔语义）、工作流（任务/审核）、数据规模（金字塔/3D）三块落后。 |

---

## 2. 能力对比表

说明：单元格只写「标注员能感知到的行为」，细节与出处见第 4 节与各调研报告。「—」表示没有该能力。

| 维度 | VAST | webKnossos | Neuroglancer (+FlyWire) | ImageJ/Fiji (Labkit·TrakEM2) | napari | 我们 |
|---|---|---|---|---|---|---|
| **导航与翻片** | Up/A 上一片、Down/Z 下一片；S/X 跳 N 片（N=Max Paint Depth）；Q/E 跳 128；缩放即切 mip；坐标历史 64 条，粘贴任意三个数字即跳；边缘侧滚动条拖动翻片 | 滚轮/F·D 翻片并夹在数据范围内；H/G 调步长；Ctrl/Alt+滚轮缩放；xy·yz·xz+3D 四视口常驻；视图编进 URL | 滚轮 ±1、Shift+滚轮 ±10、`,` `.`；四面板共享中心点；空格轮换布局；坐标框粘贴即跳；右键置中 | ImageJ `<` `>` ←→，Alt 跳 9；Labkit(BDV) 滚轮翻 z，Shift ×10 / Ctrl ×0.1；TrakEM2 `,` `.` 翻层、Ctrl+滚轮缩放、前后层预载 | 滑块/方向键翻片；按住 Space 临时平移；插件 nD-annotator 给 A/D 翻片、Ctrl+滚轮 | ↑↓/A·Z 单步，连播；滚轮缩放；中键/空格/H 平移（画笔模式下右键是擦）；缓存 24 片预取 ±3；A/Z 翻片（2026-09-24 加）。无可调步长、无坐标跳转、无位置历史 |
| **画笔/橡皮语义** | Paint All / Background / Parent 三态 (I/O/P)；按住 Delete 或左右键同按=擦；Background 下画只落空白、擦只擦当前色；Tab+拖改笔径（≤1023 px）；每笔后自动填闭合轮廓；EM 亮度蒙版 + Contiguous only | Overwrite everything / Only overwrite empty 两个持久化单选，按住 Ctrl 临时反转；Only-empty 下擦除只擦当前 id；擦除是独立工具或 Ctrl+Shift+拖；Shift+滚轮或 Shift+I/O 改半径，小/中/大预设；闭环自动填充；一笔没改到像素弹 toast | 核心版无画笔无橡皮；FlyWire 走图合并/切割而不是像素 | Labkit 按住 D 画/E 擦，默认擦掉该像素所有标签，勾 overlapping 后只擦当前；按住工具键滚轮改直径 ±10%；TrakEM2 Alt+拖擦、Shift 点填洞、Overlap/Exclude/Erode 三态、Shift+滚轮改笔径且屏幕像素恒定 | 数字键 1–7 切工具；B「保留标签」：画只落 0、擦只擦切到橡皮前选中的 id；`[` `]` 半径 ±1；X 当前 id⇄背景互换 | 画笔 B 只补空白，右键擦、Ctrl/⌘+点击拾取；橡皮（右键或 E）只擦当前 id；半径滑块、`[` `]`、按住 Tab 拖动；修缮边缘 R 把跨过黑膜的部分收回（以上均 2026-09-24 加）。无闭环自动填充、无亮度蒙版 |
| **快捷键与修饰键** | 「按住即变、松开即回」：Ctrl 平移、Shift 拾色、Delete 擦、Tab 笔径、H 隐藏叠加、U solo；Keyboard Shortcuts 窗口列全表；Control Buttons 1–0 可配置为任意操作或粘性修饰键 | 和弦 Ctrl+K,x 直达工具；W 循环工具；底部状态栏随工具与 Shift/Ctrl/Alt 实时写出左键/右键/拖动含义；26.07 起除鼠标键外全部可按账号重绑并有对话框 | 两层表 key→action，JSON/Python 可覆盖；H 帮助面板自动列出当前绑定；Ctrl+P 命令面板；Esc 退工具、Enter 提交、Backspace 退一点 | 各插件各一套；TrakEM2 字母键不能加 Ctrl（被 ImageJ 截走）；无统一自定义 | 数字键切模式；Space 临时平移；偏好里可改绑 | 单字母切模式 (P/F/B/E/M/N/L)，Alt+点击拾取，Ctrl+Z 撤销。无速查表、无自定义、无状态栏提示、无「按住临时变工具」 |
| **选择与合并/切分** | Shift+点击拾取；Collect 归入文件夹（可逆、以父色预览）→ Weld 才真正重编体素；Split 拖一条线自动找最小截面平面；Fill 3D 六邻域洪水、可跨层按源层蒙版填 | Shift+点击拾取、Ctrl+I 复制 id；校对工具 Shift 合并 / Ctrl Min-Cut 拆分 / Multi-cut 红蓝点 Enter 执行；Merger Mode 非破坏合并；Split Segments 工具集用 3D 曲面 + 受限填充拆分 | 双击选体、Shift+双击星标；Seg 面板 id/前缀/正则搜索；FlyWire M 两点合并、C 两点切、Ctrl 撒红蓝点 Split Preview 后提交，后台最大流最小割 | MorphoLibJ Label Edition 点选合并、半径 1 腐蚀/膨胀、按大小/触边删标签、Reset；TrakEM2 Shift+Alt 删岛、f 填洞、c/v 跨片复制；Labkit R 删连通域 | 拾色器 5/L；填充 contiguous；多边形工具；无专用合并/拆分 | P/Alt+点击拾取；F 连通填充、Shift+F 整片同 id；M 两次点击直接合并落盘；批量删除勾选+确认；SAM 点选/框选 + 贴膜边界。无拆分工具、无合并预览、无形态学清理 |
| **撤销/重做** | 没有 Undo（手册 Ch.1 明言）；靶向补偿：不改源文件、Save As、Safe saving；无自动保存 | Ctrl+Z / Ctrl+Y 覆盖会话内每一笔（含元数据）；30 s 自动保存；Restore Older Version 按天分组、每条可读描述、可预览后回滚，回滚本身也是一个版本 | 核心无撤销（URL 用 replaceState，后退也回不去）；FlyWire 前端 Ctrl+Z 不适用于合并/切割；后端 undo_operation 写反向操作、链接原 id，任意历史可撤 | ImageJ 只撤一步无重做；Labkit 完全无撤销（issue #70 自 2021 开着，有人误填充丢整层）；TrakEM2 Ctrl+Z / Shift+Ctrl+Z 默认 32 步可配 | Ctrl+Z / Shift+Ctrl+Z 最多 100 步；切换切片后历史清空 | Ctrl+Z 只撤本片最近一笔（逐笔 npz，精确到像素）；新加「全部撤销」；改动即保存。无重做、无多步、无跨片、无可视历史 |
| **3D 视图** | 体纹理 3D Viewer + Raymarcher 2 与 2D 联动；主窗口 2–4 面板 XY/XZ/YZ；骨架层与示意图 | 3D 网格视口常驻（预计算近即时 / ad-hoc 现算）；骨架、飞行模式 | 3D 透视 + 多 LOD 网格是核心；四面板正交切面共享中心 | ImageJ 3D Viewer / TrakEM2 网格易 OOM；Labkit 3D 球形画笔会画到相邻片（用户当 bug） | 3D 渲染；napari-threedee 可在任意切面上画 | 无；「看这一点」跳 Neuroglancer 3D |
| **多人协作与归属** | 单用户；事后 Merge .VSS 合并（可重编 id、逐体素覆盖规则）；不记作者 | 默认独占编辑锁：一人编辑其他人只读并看到其姓名；实验性同时编辑（仅校对工具、禁撤销）；按团队分享、Lock Annotation | 每笔带 user_id + 时间戳；受影响 root 级锁，几百人并行不冲突；middle-auth none/view/edit 三级；change log 可查谁改了什么 | 无；TrakEM2 数据库多人模式已停用，只能离线把节点发到兄弟项目 | 无 | 登录 + 角色（管理员/审核员/标注员）；每笔记谁改；切片版本号冲突检测（对旧画面改会被拒并刷新，属「事后拒绝」）；同块在线提示；只能撤自己的 |
| **任务/审核流** | 无（Tag 0–15 分类、Control Buttons 一键追加 [P] 类文本标签当作打分） | Task Type / Project / Task / Instance；按经验域+等级门控自动派发、项目优先级、Time Limit；Time Tracking 报表 CSV | 沙盒数据集 + 入门测试才给编辑权；change log 审计；Cell Identification 提交 | 无 | 无 | 无（有角色，没有流程）。参考：CATMAID Review widget Q/W 逐条审核；CVAT Stage/State + 钉在画面上的 Issue；Label Studio 驳回 Requeue |
| **性能与大数据** | 16³ cube + 2 的幂 mip，图像/分割各自 LRU；已打开 ~1.3 PB 远程；绘制写当前 mip，速度与缩放无关；每层 ≤65535 segment | WKW 32³ 桶流式 + 多 mag；Zarr/N5/precomputed 从 S3/GCS 只读流式；标注可限制到粗 mag 提速 | precomputed 分块/sharded 多级金字塔；gpuMemoryLimit/concurrentDownloads/自适应预取可调；网格多 LOD | Labkit imglib2 懒加载能开 TB 级，但密集标注 300³ 变慢、每像素 ~12 B；TrakEM2 mipmap 但 32k 图配准后显示 bug；ImageJ 整栈入内存 | numpy/dask；大体密集绘制慢 | 单片读取；浏览器缓存 24 片、预取前后 3 片；无金字塔、无分块，大块缩放等整张 |
| **溯源/导出** | 导出 ID 栈（PNG/TIF/RAW）、网格、体积测量 CSV、metadata txt；无逐笔记录 | 版本列表每条一句人话描述；Segment Statistics（体积/包围盒/表面积）CSV；zip（NML + WKW/Zarr）导入导出 | get_tabular_change_log（operation_id、user、时间、before/after root、is_merge）；任意时间戳「时间旅行」 | .labeling / .tif；ROI Manager .zip；宏录制器把手动步骤变脚本 | 数组另存；无审计 | 前后对比页：与原始分割逐像素对比、黑白双线描改动、改动列表、谁改的、鼠标读数、操作流水含撤销、CSV/JSON 导出 |

---

## 3. 我们领先的地方

1. **安全网是同类里最好的一档**：改动即保存 + 写时复制（`seg_edit.npy`）+ 逐笔 npz 精确撤销。VAST 没有 Undo 且不自动保存（手册直言有人开几天没存丢了工作，来源: https://lichtman.rc.fas.harvard.edu/vast/ Manual §1/§2.2.2）；Labkit 完全无撤销（来源: https://github.com/juglab/labkit-ui/issues/70）；Paintera 绘制无撤销只能清画布（来源: https://github.com/saalfeldlab/paintera/blob/master/README.md）；Neuroglancer 核心版无撤销。只有 webKnossos 和 TrakEM2/napari 在这一点上不输我们，而它们中只有 webKnossos 同时做到了自动保存。
2. **多人归属与冲突检测开箱即有**：账号/角色、每笔记作者、切片版本号冲突检测、同块在线提示。VAST、Fiji 全家族、napari、Paintera、KNOSSOS 都没有账号概念（来源: https://pmc.ncbi.nlm.nih.gov/articles/PMC9546337/ 综述明确把离线工具与网页多人工具分开）。和 webKnossos 比，我们缺前置锁；和 FlyWire/CAVE 比，我们缺规模，但「谁改的」这一层我们已经有。
3. **溯源页是独有的**：前后对比页（逐像素 diff、双线描边、改动列表、操作流水含撤销、CSV/JSON 导出）在对标工具中没有直接对应物。webKnossos 的版本列表只有一句描述没有像素级 diff；CAVE 的 change log 是 API 不是页面；VAST/Fiji/napari 根本不记逐笔。
4. **AI 辅助已经在工作流里**：SAM 点选/框选 + 贴合膜边界、邻片取色 (L)。VAST 没有 SAM；Fiji 要装 SAMJ 才有（来源: https://arxiv.org/abs/2506.02783）；webKnossos 的 Quick Select 与 Paintera 的 A 模式与我们同级。
5. **零安装、浏览器端**：与 webKnossos、Neuroglancer、CATMAID 同一阵营；VAST 是 Windows 单机、Fiji/napari/Paintera/KNOSSOS 需本地安装与 Java/Python 环境。
6. **显示层小功能齐全**：透明度/渐变/对比滑块、并排视图、只画边界、悬停高亮、连播、「看这一点」跳 Neuroglancer 3D。这一块与对标工具持平，不是差距所在。

---

## 4. 差距（按优先级排序）

工作量口径：**小** = 1–2 人日、前端为主；**中** = 1–2 周、前后端都动；**大** = 一个月以上或涉及数据格式/架构。

### 4.1 先回答本次的四条需求：市面上是怎么做的

| 需求 | VAST | webKnossos | Labkit / TrakEM2 | napari / Paintera / KNOSSOS | 建议我们的绑定 |
|---|---|---|---|---|---|
| 右键擦除、只擦选中色 | 按住 Delete 或左右键同按=擦；Background 模式下只擦当前色 | 2021 年把右键从擦除改成上下文菜单并保留「Classic Controls」开关；Only-overwrite-empty 下擦除只擦当前 id | Labkit 勾 overlapping 后只 remove 当前标签；TrakEM2 Alt+拖擦，Erode 模式决定是否擦别人 | napari B 保留标签：擦只擦当前 id；Paintera 右键擦；KNOSSOS Shift+右键擦（按下置 inverse，松开还原） | 绘图模式下按住右键=擦，松开回画笔；默认「只补空白/只擦当前 id」，工具栏单选可切「覆盖全部」；设置里留「右键=平移」回退 |
| Ctrl 按住选中颜色 | Shift+点击拾色（Ctrl 是临时平移） | Shift+点击拾取；Ctrl+I 复制光标下 id | Labkit Shift+左键 | napari 5/L 工具 | Ctrl+点击拾取（注意 macOS 上 Ctrl+点击会触发 contextmenu，需 preventDefault）；保留 P / Alt+点击 |
| Tab 拖动改半径 | 按住 Tab 或 `\|` + 左键上下拖；-/= 单步；面板可直接输入并 Lock | Shift+滚轮 / Shift+I/O；小中大预设 Ctrl+K,1/2/3 | Labkit 按住工具键滚轮 ±10%（1–50）；TrakEM2 Shift+滚轮且屏幕像素恒定 | KNOSSOS Shift+滚轮按 10% 比例；Paintera Space+滚轮；napari 只有 `[` `]` | 按住 Tab + 鼠标上下拖，位移按比例映射半径，光标处实时画圆和数字；同时加 Shift+滚轮；保留 `[` `]` 与滑块 |
| A/Z 翻片 | Up/A 上一片、Down/Z 下一片；S/X 跳 N；Q/E 跳 128 | F/D 前后一片；H/G 调步长；滚轮夹在范围内 | ImageJ `<` `>`；BDV Shift ×10 | KNOSSOS F/D + Jump Frames 可设；CATMAID `,` `.` 单片 `<` `>` 十片 | A 上一片、Z 下一片；Shift+A/Z 十片；步长 N 可设并显示在状态栏；输入框有焦点时不劫持 |

---

### P0（本轮就做，都是手感问题，除修缮外均为小）

> **状态（2026-09-24）**：P0-1～P0-4 已实现并部署到 8791（做法见 docs/ANNOTATE.md）。与下文建议的差别：A/Z 未做 Shift 跳 N 与可调步长；
> 修缮边缘直接落盘（可撤销），没有做预览；P0-5 的底部状态栏与快捷键速查还没做，目前只在工具提示和「?」帮助里写了。

#### P0-1　画笔/橡皮语义：右键擦除、只擦当前 id、Ctrl 拾色（需求 1）
- **差距**：我们的橡皮是独立模式 (E) 且擦掉光标下一切 id；画笔已经「只补空白」，但橡皮没有对应的「只擦当前」；换工具要来回切模式，笔离纸。
- **参考做法**：VAST 的 Background 模式把「画只落空白」和「擦只擦当前色」绑成同一个三态开关 I/O/P，手册称之为「最有用的模式」，擦除是按住 Delete 或左右键同按的瞬态动作（来源: https://lichtman.rc.fas.harvard.edu/vast/ Manual §4.1, §B.5）。webKnossos 用两个持久化单选 Overwrite everything / Only overwrite empty，tooltip 原文 "In case of erasing, only the current segment ID is overwritten. This setting can be toggled by holding CTRL"，源码里擦除时 `overwritableValue = activeCellId`（来源: https://github.com/scalableminds/webknossos/blob/master/frontend/javascripts/viewer/view/action_bar/tools/volume_specific_ui.tsx ; https://github.com/scalableminds/webknossos/blob/master/frontend/javascripts/viewer/model/sagas/volume/helpers.ts）。napari 的 `preserve_labels`（B 键）语义一致（来源: https://napari.org/stable/howtos/layers/labels.html）。KNOSSOS 用「Shift 按下时 brush.inverse=true，松开还原」实现瞬态擦除（来源: https://github.com/knossos-project/knossos/tree/master/resources/cheatsheet）。
- **建议怎么做**：
  1. 引入一个「覆盖模式」状态，两档：`只补空白 / 只擦当前 id`（默认）与 `覆盖全部`，工具栏单选并写 localStorage；按住 Ctrl 拖动时临时反转（照 wK）。
  2. 绘图模式下 `pointerdown` 且 `button === 2` → 本笔为擦除，`pointerup` 回画笔；`contextmenu` 事件 preventDefault。其他模式右键仍平移；设置里加「右键 = 平移 / 擦除」开关（wK 2021 年反向改动时也留了 Classic Controls，来源: https://docs.webknossos.org/webknossos/ui/status_bar.html 与 CHANGELOG 21.07.0）。空格、H、中键平移不动。
  3. Ctrl+点击 = 拾取光标下 id 为当前 id，并在状态栏/toast 显示「当前 id 17」与色块；保留 P 与 Alt+点击。注意 macOS 上 Ctrl+点击会触发 `contextmenu`，与 2 的 preventDefault 一起处理即可。
  4. 借 wK #7526：一笔结束后若 0 像素被改（例如在别人的 id 上用只擦当前擦），toast 提示「当前为只擦 id 17，未改动任何像素」。
- **工作量**：小。

#### P0-2　按住 Tab 拖动改画笔半径（需求 2）
- **差距**：只有滑块和 `[` `]`，改半径要离开画面或多次按键。
- **参考做法**：VAST「按住 Tab 或 `\|`，再左键按住上下拖动改笔径」，面板实时显示 Pen Diam. 并可 Lock（来源: https://lichtman.rc.fas.harvard.edu/vast/ Manual §4.1, §B.4）。TrakEM2 Shift+滚轮且笔径按屏幕像素恒定，低倍粗涂高倍细修不用反复调（来源: https://syn.mrc-lmb.cam.ac.uk/acardona/trakem2_manual.html）。Labkit 按住工具键滚轮每格 ±max(1, 10%)，光标处始终画圆（来源: https://github.com/juglab/labkit-ui/blob/master/src/main/java/sc/fiji/labkit/ui/brush/LabelBrushController.java）。webKnossos 另有小/中/大三档预设可召回（来源: https://docs.webknossos.org/webknossos/keyboard_shortcuts.html）。
- **建议怎么做**：`keydown Tab` 且画布聚焦时 `preventDefault`（否则浏览器切焦点），记录鼠标起点 y 与当前半径；`pointermove` 时 `r = r0 * exp(k·Δy)`（按比例，不是线性 ±1）；光标处画实时圆并标像素值；`keyup Tab` 或 `window blur`（Cmd/Alt+Tab 切走窗口）结束并写回滑块。同时加 Shift+滚轮改半径与 1/2/3 三档预设。
- **工作量**：小。

#### P0-3　A/Z 翻片与可调步长（需求 4）
- **差距**：已支持 ↑↓/A·Z；没有步长设置；没有坐标跳转与位置历史。
- **参考做法**：VAST Up/A 上一片、Down/Z 下一片，S/X 跳 N 片且 N 与跨片填充深度联动，Q/E 跳 128（来源: https://lichtman.rc.fas.harvard.edu/vast/ Manual §B.5 表 B.2）。webKnossos F/D 翻片、H/G 调步长、26.10 起滚轮夹在数据集范围内（来源: https://docs.webknossos.org/webknossos/keyboard_shortcuts.html）。KNOSSOS Jump Frames 可设、Shift+F/D 跳 10 片（来源: https://knossos.app/documentation）。
- **建议怎么做**：A = 上一片、Z = 下一片、Shift+A/Z = 跳 N 片（默认 10，与 PgUp/Dn 共用，可在设置里改，状态栏显示当前步长）；`event.target` 是 input/textarea 时不劫持；Z 单独按不与 Ctrl+Z 撞；翻片夹在切片范围内。顺手加坐标框「粘贴任意含三个数字的文本即跳转」（VAST §3.1.4，Neuroglancer 也有，来源: https://tutorial.microns-explorer.org/neuroglancer-basic.html）。
- **工作量**：小。

#### P0-4　自动修缮边缘：跨到黑膜的 mask 向内收缩（需求 3）
- **差距**：现在标签压到膜上只能手擦。
- **参考做法（诚实地说：没有一款对标工具内置这个功能）**：webKnossos 报告的负面结论是「未找到已有 mask 跨越黑边则自动收缩的功能，最接近的是 Quick Select 阈值模式的 Erode/Dilate/Close 参数，且作用于新选区」（来源: https://docs.webknossos.org/webknossos/volume_annotation/tools.html#quick-select-tool）。能借的零件有四个：VAST 用「EM 亮度在 [min,max] 内 + Contiguous only」蒙版从源头不让画笔落到膜上，另有 Clean Segments 删 ≤N 像素碎屑、填 ≤N 孔（来源: https://lichtman.rc.fas.harvard.edu/vast/ Manual §4.1.3, §4.4.10）；ilastik carving 的 BG priority（默认 ≈0.95）让分水岭在歧义处向物体内侧收（来源: https://www.ilastik.org/documentation/carving/carving）；Paintera Shift+R「当前画布与底层标签取交集」（来源: https://github.com/saalfeldlab/paintera/blob/master/README.md）；MorphoLibJ Label Edition 半径 1 腐蚀/膨胀 + Reset 按钮（来源: https://imagej.net/plugins/morpholibj）。
- **建议怎么做**（分预防和修复两层）：
  1. **预防**：给画笔/填充加「膜约束」开关：EM 灰度低于阈值 t 的像素不可写（VAST EM pixel between 语义），与「只补空白」叠加。
  2. **修复**（一键，可批量）：对选中 id（或本片全部 id）的掩膜 S：
     - 膜掩膜 M = EM < t，t 默认取本片灰度直方图低分位（如 P10），滑块可调；可选 1 px 闭运算让膜连续。
     - **只在边界带内作用**：B = S − erode(S, r)（r 默认 2–3 px），候选删除 D = B ∩ M。这样内部的线粒体、突触致密区等暗结构不会被挖掉——这是与「全图阈值一刀切」最大的区别，必须有。
     - S' = S − D，再保留与原 S 重叠且 ≥ N 像素的连通分量（VAST Clean Segments 思路），去掉被膜切断的碎屑。
     - 可选一档「腐蚀 1 再膨胀 1 且不越过 M」（Paintera 交集 / MorphoLibJ）。
  3. **交互**：D 以红色高亮预览并显示「将删除 N 像素」，t/r/N 在侧栏实时调；Enter 应用为一笔 npz（可 Ctrl+Z），Esc 放弃——照 wK Quick Select 的「预览 → Enter 接受 / Esc 丢弃」（来源: https://docs.webknossos.org/webknossos/volume_annotation/tools.html）。
  4. **风险**：EM 对比度逐片漂移，阈值需按片归一化；先在两三块真实数据上看误删率再决定默认参数。
- **工作量**：中（后端 numpy/scipy 形态学 + 前端预览层 + 撤销登记）。

#### P0-5　底部状态栏 + 快捷键速查（配套，让上面四条被发现）
- **差距**：新加的右键擦、Ctrl 拾色、Tab 改半径、A/Z 全是「隐形规则」，没有地方告诉标注员。
- **参考做法**：webKnossos 状态栏随工具和按住的 Shift/Ctrl/Alt 实时写出左键/右键/拖动的含义，并显示鼠标坐标、光标下 id、当前 mag（来源: https://docs.webknossos.org/webknossos/ui/status_bar.html）；KNOSSOS 侧栏 Cheatsheet 随模式实时列表（来源: https://github.com/knossos-project/knossos/tree/master/resources/cheatsheet）；Neuroglancer 按 H 自动生成帮助面板（来源: https://github.com/google/neuroglancer/blob/master/src/ui/default_input_event_bindings.ts）；VAST 有 Keyboard Shortcuts 窗口分「按住型 / 翻片 / 其他」三表（Manual §B.5）。
- **建议怎么做**：画布底部一行：`左键 画 id 17 | 右键 擦 id 17 | Ctrl+点击 拾取 | Tab+拖 半径 12px | A/Z 翻片 步长 1 | z=1032 x=… y=… 光标下 id=…`，按住修饰键文字即时变化；`?` 弹出全表（按模式分组）。
- **工作量**：小。

---

### P1（下一个迭代，中等工作量，直接影响返工成本）

#### P1-1　重做 + 多步/跨片撤销 + 可视历史
- **差距**：只撤本片最近一笔，没有重做；「全部撤销」是全有或全无。
- **参考做法**：TrakEM2 Ctrl+Z / Shift+Ctrl+Z 默认 32 步可配，撤销后新操作截断历史（来源: https://syn.mrc-lmb.cam.ac.uk/acardona/trakem2_manual.html）；napari 100 步（来源: https://napari.org/stable/howtos/layers/labels.html）；webKnossos 会话内全撤销/重做 + Restore Older Version 可预览回滚（来源: https://docs.webknossos.org/webknossos/ui/toolbar.html）；CATMAID F9 命令历史对话框可点选（来源: https://github.com/catmaid/CATMAID/blob/master/CHANGELOG.md）；PyChunkedGraph 把撤销做成「写一条链接到原 operation_id 的反向操作」，天然可撤非最近一笔、可撤别人、有重做（来源: https://github.com/CAVEconnectome/PyChunkedGraph）。
- **建议怎么做**：我们逐笔 npz 已是反向操作的原料——给每笔 `operation_id`，撤销 = 写一条 `undo_of=<id>` 的反向 npz 照常入流水，重做 = 再执行原操作；前端维护指针；侧栏「历史」列表（谁、哪片、什么工具、改了多少像素）点选可回到任一点并预览；审核员权限可撤别人的。与前后对比页的操作流水打通。
- **工作量**：中。

#### P1-2　所有慢/批量操作走「预览 → Enter/Esc」且可取消
- **差距**：SAM 预览后应用，批量删除有确认框；整片填充落盘后可撤销。
- **参考做法**：wK Quick Select 参数实时预览、Enter 接受；Paintera 形状插值 Ctrl+P 预览、Enter 提交、Esc 放弃（来源: https://github.com/saalfeldlab/paintera/blob/master/README.md）；Labkit 3D 误填充卡死一分钟、无法取消、丢整层的事故说明长操作必须可取消且先登记撤销（来源: https://github.com/juglab/labkit-ui/issues/93）。
- **建议怎么做**：统一一个「待确认层」：候选像素高亮、显示数量、Enter 写入为一笔、Esc 丢弃；后端长任务返回 job id 可取消，前端进度条。
- **工作量**：中。

#### P1-3　协作从「事后拒绝」改为「前置锁」+ 分享链接
- **差距**：版本号冲突检测在对着旧画面改完才拒绝；给同事指一个地方只能口述切片号。
- **参考做法**：webKnossos 一人编辑即锁定，其他人只读并看到编辑者姓名（来源: https://docs.webknossos.org/webknossos/sharing/annotation_sharing.html）；Neuroglancer 整个视图状态是 JSON 放在 URL `#!` 里，Share 上传得短链，对方看到的就是你此刻的精确画面（来源: https://github.com/google/neuroglancer/blob/master/src/ui/url_hash_binding.ts）；wK Quick Share 同理。
- **建议怎么做**：打开块开始编辑即持有块锁（心跳续期），他人只读 + 「X 正在编辑」；把 切片号/中心/缩放/透明度/当前 id 编进 URL 片段，改动列表每条附「回到当时视图」链接；「看这一点」反向也可从 Neuroglancer 跳回。
- **工作量**：小（URL）+ 中（锁）。

#### P1-4　跨片能力的最便宜版本：关键片插值 + Z 填充
- **差距**：没有 3D 编辑，每片都要画一遍；损坏切片插值功能已移除。
- **参考做法**：VAST Max Paint Depth（≤±8 片）只填上下相邻已画区域的重叠部分、只在画时触发不在擦时，翻片步长与之联动（来源: https://lichtman.rc.fas.harvard.edu/vast/ Manual §4.1.2）；webKnossos 两片各画同一 id 后按 V 生成中间片（来源: https://docs.webknossos.org/webknossos/volume_annotation/tools.html#volume-interpolation）；TrakEM2 Interpolate gaps，用户反馈复杂画笔轮廓插值可达数小时、多边形几乎瞬时（来源: https://forum.image.sc/t/42636）；Paintera S 模式按距离变换插值。
- **建议怎么做**：后续可独立设计「隔 N 片画一次，中间自动填」，结果标为待复查、只填空白；轮廓先简化再插值；步长 N 与 P0-3 的翻片步长共用。
- **工作量**：中。

#### P1-5　标签面板：id 列表、显隐、跳转、统计
- **差距**：没有一个「本块出现过 / 被我改过的 id」列表；批量删除的勾选列表是临时的。
- **参考做法**：Labkit 每标签眼睛显隐、点色块改色、靶心跳到像素最多的切片（来源: https://imagej.net/plugins/labkit/documentation）；webKnossos Segments 列表自动登记点击过的 id，可命名、隐藏未列出、Show Segment Statistics 导出 CSV（来源: https://docs.webknossos.org/webknossos/volume_annotation/segments_list.html）；Neuroglancer Seg 面板 id/前缀/正则搜索、星标（来源: https://github.com/google/neuroglancer）；VAST 每个 segment 自动记 Anchor，Home/G 一键跳回（Manual §4.4.7）。
- **建议怎么做**：侧栏 id 列表（本块被点击/修改过的 id）：色块、眼睛、「只显示此 id」、跳到首次改动位置/像素最多的片、像素数，勾选后批量删除/合并/锁定；被审核通过的 id 可锁定拒绝画笔（Paintera 的 L 只隐藏不阻止编辑正是被吐槽的点，来源: https://github.com/saalfeldlab/paintera/issues/605）。
- **工作量**：中。

#### P1-6　合并预览与拆分工具
- **差距**：M 两次点击直接合并落盘、只能撤一笔；拆分只能擦一条膜再填。
- **参考做法**：VAST Collect 先把 id 归入文件夹以父色显示（可逆），确认后 Weld 才重编体素；Split 工具拖一条线自动找最小截面（来源: https://lichtman.rc.fas.harvard.edu/vast/ Manual §4.3, §4.4.20）；FlyWire/wK 校对工具两侧撒红蓝点 → Split Preview → 提交，后台最小割（来源: https://docs.webknossos.org/webknossos/proofreading/proofreading_tool.html ; https://pmc.ncbi.nlm.nih.gov/articles/PMC8903166/）。
- **建议怎么做**：合并改为「待合并组」高亮预览 + 批量确认；拆分做 2D 版：沿膜拖一条线或撒两组点，用已有的「贴合膜边界」能力算最小割，预览后 Enter。
- **工作量**：中（合并预览）/ 大（拆分）。

---

### P2（工作流与结构性投入，大）

#### P2-1　任务分派与审核流（最小可行版本）
- **差距**：有角色没有流程：谁该做哪块、做到哪、审核意见钉在哪、驳回怎么回流，全靠口头。
- **参考做法**：CVAT 作业 Stage（annotation/validation/acceptance）+ State（new/in progress/rejected/completed）+ Assignee，审核员右键「Quick issue」钉在帧上，Issues 面板逐条 Resolve（来源: https://docs.cvat.ai/docs/qa-analytics/manual-qa/）；CATMAID Review widget Q/W 逐条推进、被改动即回未审、按创建者过滤只审别人的（来源: https://catmaid.readthedocs.io/en/stable/widgets/review-widget.html）；Label Studio 驳回项 Requeue 回原标注员且不能跳过（来源: https://docs.humansignal.com/guide/quality.html，报告注明 Requeue 选项细节未直接核对原文）；webKnossos 按经验域+等级门控派发、Time Tracking CSV（来源: https://docs.webknossos.org/webknossos/tasks_projects/concepts.html）。
- **建议怎么做**：先做块级 Stage/State + 指派人；审核员在切片上右键钉「问题：位置错 / id 错」（坐标 + 文字），标注员 Issues 面板逐条解决后才能置完成；改动列表变成 Q/W 逐条审核队列，审过的再被改自动回未审。我们的「改动列表 + 谁改的 + 版本号」正是它的底座。
- **工作量**：中（最小版）/ 大（含派发、用时统计、GT 暗抽检）。

#### P2-2　多分辨率金字塔 + 分块读取
- **差距**：单片读取，大块缩放等整张，也没法做 XZ/YZ 或 3D。这是性能和 3D 两条线的共同瓶颈。
- **参考做法**：VAST 16³ cube + 2 的幂 mip，绘制写当前 mip 所以「速度与缩放无关」（来源: https://doi.org/10.3389/fncir.2018.00088）；webKnossos 32³ 桶流式 + 多 mag、标注可限粗 mag（来源: https://docs.webknossos.org/webknossos/data/concepts.html）；Neuroglancer precomputed `scales[]`/`chunk_sizes`/sharded（来源: https://github.com/google/neuroglancer/blob/master/src/datasource/precomputed/volume.md）。
- **建议怎么做**：后端离线生成 2–3 级下采样并分块（zarr 或分块 PNG），前端按视口取块、缩放时先粗后细，暴露并发数与预取深度；缓存策略借 VAST「已修改块不可淘汰、优先淘汰未修改块」。做完后 XZ/YZ 条带视图与 3D 才有地基。
- **工作量**：大。

#### P2-3　3D / 正交切面上下文
- **差距**：只能跳 Neuroglancer。
- **参考做法**：Neuroglancer 四面板共享中心（来源: https://github.com/google/neuroglancer）；VAST 主窗口 2–4 面板 XY/XZ/YZ（Manual §3.1.1）；webKnossos 3D 网格视口常驻（来源: https://docs.webknossos.org/webknossos/meshes/loading_meshes.html）。
- **建议怎么做**：保留 Neuroglancer 跳转（成本最低、效果最好）；P2-2 落地后先加 XZ/YZ 条带（比 3D 便宜得多）；并排视图泛化为 row/column 面板、每面板自选图层。
- **工作量**：大（依赖 P2-2）。

#### P2-4　快捷键两层化与自定义、图层管理、脚本重放
- **差距**：键位写死在代码；只有 EM + 一层分割；JSON 操作流水没有重放入口。
- **参考做法**：Neuroglancer key→action 两层表 JSON 可覆盖（来源: https://github.com/google/neuroglancer/blob/master/src/ui/default_input_event_bindings.ts）；CVAT 按作用域改绑 + Restore Defaults（来源: https://docs.cvat.ai/docs/getting_started/shortcuts/）；VAST Layers 窗口每层 Editable/Solo/混合模式、可把边界概率图或自动分割当源层（Manual §3.1.5, §4.1.4）；ImageJ 宏录制器（来源: https://imagej.net/ij/docs/shortcuts.html）。
- **建议怎么做**：键位表抽成配置（这样本轮 A/Z、Tab、右键都是改表），设置页可改绑；图层先支持「加载一层对照分割/边界概率图」并可作画笔约束源；操作流水加「重放到相邻 N 片」按钮。
- **工作量**：中（键位）/ 中（图层）/ 中（重放）。

#### P2-5　沙盒与质量统计
- **差距**：新人直接在真实块上改；没有每人产量、被撤销率、与 GT 的一致性。
- **参考做法**：FlyWire 沙盒 + 入门测试才给编辑权（来源: https://pmc.ncbi.nlm.nih.gov/articles/PMC8903166/）；CATMAID 新手按入门神经元管线推进、导师复审（来源: https://catmaid.readthedocs.io/en/stable/tracing-training.html）；CVAT GT/Honeypot 自动 accuracy 与「完成即反馈」（来源: https://docs.cvat.ai/docs/qa-analytics/auto-qa/）；Label Studio Members 面板一致性/耗时（来源: https://docs.humansignal.com/guide/stats）。
- **建议怎么做**：一个「沙盒块」+ 与标准答案逐像素比分；CSV 导出列对齐 CAVE change_log（operation_id、timestamp、user、slice、before/after ids、op_type、undone_by），用 pandas 就能算产量与被撤销率。
- **工作量**：中。

---

## 5. 结论

1. **我们的长板是别人没有的**：改动即保存 + 逐笔精确撤销 + 多人归属 + 前后对比溯源，这四件事在 VAST、Fiji 全家族、napari、Paintera、KNOSSOS 里要么没有要么残缺；只有 webKnossos 全部具备，而它没有像素级 diff 页。不要为了追手感把这些拆掉。
2. **短板集中在三处，且性质不同**：
   - **手感**（VAST、TrakEM2 领先）：右键擦、只擦当前 id、Ctrl 拾色、Tab 改半径、A/Z 翻片、状态栏——本轮四条需求全在这里，除「自动修缮边缘」为中等外都是小活，建议作为一个「覆盖模式 + 修饰键 + 状态栏」包一次做完，并把键位抽成表。
   - **返工成本**（webKnossos、TrakEM2、napari 领先）：重做、多步/跨片撤销、可视历史、预览再落盘、可取消——这是标注员最痛的点（Labkit 无撤销丢整层、VAST 无撤销靠另存），P1 优先。
   - **工作流与规模**（webKnossos、CVAT/Label Studio、Neuroglancer 领先）：任务/审核流我们一片空白但底座（角色、归属、改动列表）已有，最小版本中等工作量；金字塔/分块是 3D 与大块性能的共同前提，是唯一必须一次性投入的结构性工程。
3. **「自动修缮边缘」没有现成对标**，是从 VAST 亮度蒙版、ilastik BG priority、Paintera 交集、MorphoLibJ 腐蚀拼出来的；必须限制在边界带内并带预览，否则会误删内部暗结构。先在真实块上验证再定默认参数。
4. **建议顺序**：P0-1～P0-5（本周到两周）→ P1-1 撤销重做/历史 + P1-2 预览确认 + P1-3 前置锁与分享链接（下个月）→ P1-4～P1-6 与 P2-1 审核流最小版（季度）→ P2-2 金字塔作为独立立项，其后才谈 XZ/YZ 与 3D。

---

### 附：报告中标注为「未核实」的条目（本文引用时已避开或注明）
- VAST：YouTube 教程内容未看；论文数字经摘要工具转述。
- webKnossos：Quick Select 预测深度上限（文档 16 片 vs 源码常量 50）；Ctrl+点击网格在两处文档语义冲突；Shift+F/D 多片移动仅见源码；撤销栈大小。
- Neuroglancer：graphene 层「time control」UI 位置未核实。
- ImageJ：ROI Manager「Interpolate ROIs」来自搜索摘要与教学视频，未在官方文档原文核到；ilastik carving 撤销笔画是否已实现未验证。
- Label Studio：Reject 的 Remove/Requeue 选项来自发布说明检索结果，未直接核对原文。