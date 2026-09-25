# 标注工作台对标 VAST / ImageJ 等工具：还缺什么（按当前代码核对）

> 2026-09-25 按 HEAD `607e17b` 逐条核对代码整理，共 71 条。第 8 条我另外复测过：同一片 z37，交付的 `seg.npy`（Fortran 序）读 0.014 s，写时复制出来的 `seg_edit.npy`（C 序）读 6.9 s。

初次核对基于 `em-qc-platform` 的 HEAD `607e17b`。2026-09-25 已按当前变更更新：移除损坏切片修补功能及相关待办，滚轮统一缩放、↑↓/A·Z 翻片，标签与改动列表支持图像高亮；以下旧行号仅供历史定位。下面列的每条缺口都附了 file:line。关键结论我逐条翻代码核对过，包括键盘分派、笔画、撤销、填充、修缮、批量删除确认框、工作副本存储顺序、试用登录的默认角色、只读缓存、EM 响应头。文中凡写「实测」的耗时数字，都是审计员测的，我没有重测。

工作量：小 = 一天以内，中 = 1–3 天，大 = 一周以上。注意 `ux_gaps.md` 用的是另一套口径（小 = 1–2 人日，中 = 1–2 周），两边的工作量不能直接比。

---

## P0：下一步就做（都不到一天，影响大）

### P0-A 先修的缺陷：会写错片、丢数据或误导人

**1. 画到一半翻片，屏幕上的墨迹和存下来的不在同一片；拖出画布会连出一条直线；Esc 取消不了**（工作量：小）
- 现在的体验：
  - 按着左键时，A/Z 仍可翻片（滚轮已改为缩放）。`goZ` 只拦 `mergeBusy`（`annotate.js:401-402`），`onWheel` 不看 `S.stroke`（`annotate.js:416-421`）。所以墨迹画在新片的画布上，存盘却用落笔时的 `st.z`（`annotate.js:526,562`）。
  - `mousemove` 只挂在画布上（`annotate.js:893`）。笔移出画布再从别处进来，`strokeMove` 会从最后一个点插值过去（`annotate.js:553-558`），连出一条直线。画笔只补空白，这条线正好落进细胞间的缝里。
  - Esc 的分支不处理进行中的笔画（`annotate.js:930`）。
  - 上一笔的响应回来时，`afterEdit→goZ→render` 会重画 seg 层（`annotate.js:566-571`），正在画的这一笔前半截预览就没了。
- 参考工具：VAST、napari、Fiji 按下鼠标后由窗口捕获指针；网页上对应的是 `setPointerCapture`。CVAT 画到一半按 Esc 就丢弃这一笔。
- 建议改法：
  - 改用 pointer 事件加 `setPointerCapture`，笔出界时断开插值。
  - `S.stroke` 存在期间，`goZ` 一律拒绝（键盘、滑条、连播都走这里）。
  - Esc 丢弃当前这一笔。
  - `render()` 之后如果还有 `S.stroke`，用 `pts` 把预览重画一遍。

**2. 写请求不排队，⌘⇧Z 和按住 Ctrl+Z 都会继续撤销**（工作量：小）
- 现在的体验：
  - 涂抹和填充发请求时不设忙碌标记（`annotate.js:559-565,471-484`），`undo()` 只看 `mergeBusy`（`annotate.js:573`）。画完马上按 Ctrl+Z，`/undo` 可能比 `/paint` 先到服务端，撤掉的是更早那一笔。
  - 撤销判断 `k.toLowerCase()==="z"`（`annotate.js:924`），不排除 Shift，也不看 `ev.repeat`。所以习惯按 ⌘⇧Z 重做的人会又撤一笔；按住 Ctrl+Z 会接连撤好几笔。而撤销会删掉 npz（`store.py:937-947`），撤掉的找不回来。
- 参考工具：webKnossos 所有写入先进本地保存队列，按顺序发送。TrakEM2 和 napari 的 Shift+Ctrl+Z 是重做。
- 建议改法：
  - 前端建一条 Promise 链，paint、fill、merge、refine、undo 全部排进去。
  - 带 Shift 的 Ctrl/⌘+Z 和 Ctrl+Y 先 `preventDefault`，提示「重做尚未支持」。
  - `ev.repeat` 为真时忽略撤销键。

**3. 批量删除的确认框开着时按键会穿透到画布，确认后删的可能是另一片**（工作量：小）
- 现在的体验：
  - 屏蔽按键的只有三个对话框（`annotate.js:149`），漏了 `an-bulk-confirm`（`annotate.html:115`）。
  - 框里写的是打开时的「z 10」（`annotate.js:992`）。框开着按 Z/↓ 仍会翻片，`bulkRun` 执行时取的是那一刻的 `S.z`（`annotate.js:1000`）。
  - `S.bulk` 翻片时也不清空（`annotate.js:404`）。
- 参考工具：VAST、Fiji 的模态框会挡住所有按键；webKnossos、CVAT 的 Modal 打开时停用画布快捷键。
- 建议改法：页面上只要有 `dialog[open]`，全局 keydown 就直接 return。`bulkAsk` 时把 z 记下来，`bulkRun` 校验它仍等于当前片。换片时清空勾选。

**4. 已移除对应功能**

损坏切片扫描与插值修补已删除，此项待办关闭。保留编号供历史引用。

**5. 对比页用的只读库缓存从不失效，连播会播旧标签**（工作量：小）
- 现在的体验：
  - 对比页走的是另一个 `AnnotateStore(read_only=True)`（`annotate.py:31-42`），有自己的 `_label_cache`（`store.py:197-201`）。
  - 写入只作废写入方自己的缓存（`store.py:424-427`）。
  - 审计实测：标注员改了 29 像素之后，`comparison_light` 报 0 像素变化。
- 参考工具：Neuroglancer 用 generation 号让旧 chunk 失效。
- 建议改法：两个 store 共享一份按 (path, work) 索引的缓存；或者缓存条目带 `slice_rev`，读之前比对。补一条回归测试。

**6. 带 Ctrl/⌘ 的浏览器快捷键被工作台误吃；开着大写锁定时字母键全部失效**（工作量：小）
- 现在的体验：
  - keydown 除了 Ctrl+Z 不看任何修饰键（`annotate.js:925-946`），于是：
    - ⌘R 会切到修缮，同时浏览器刷新页面。
    - ⌘S 翻片，还吞掉了浏览器的保存。
    - ⌘N 会在服务端占一个新 id（`annotate.js:941`）。
    - ⌘C 开关对比滑块，⌘V 切换视图。
  - 字母键按小写字面值比较，开了 CapsLock 后除 U 以外全部没反应（`annotate.js:932`）。
- 参考工具：Neuroglancer 的绑定键名带修饰键，也不受键盘布局影响（`keyz`、`control+keyz`）；没绑定的组合原样交给浏览器。
- 建议改法：带 Ctrl、⌘ 或 Alt、又没有显式绑定的组合，直接 return，不 preventDefault。字母比较改用 `ev.code`（或者统一 toLowerCase）。

**7. 点过侧栏的复选框、滑块或下拉框之后，快捷键全部失灵**（工作量：小）
- 现在的体验：
  - `ev.target.matches("input,select,textarea")` 把 checkbox 和 range 也挡掉了（`annotate.js:921`）。勾一下「只画边界」、拖一下透明度之后，A/Z/B/Ctrl+Z 全都没反应。这时 ↑↓ 改的是滑块，空格切的是复选框。
  - 数据块下拉框选完之后焦点还留在上面，这时按 ↓ 或字母键会直接换块（`annotate.js:1097`）。
  - Tab 在整个页面都被 preventDefault（`annotate.js:940`）。
- 参考工具：Neuroglancer 鼠标进入视图就把焦点拿回画布。VAST、Fiji 的工具面板从不吞主视图的快捷键。
- 建议改法：
  - 只对文字类输入框放行按键。
  - range、checkbox、select 在 change 之后 blur，并把焦点还给 `#an-stage`。
  - Tab 只在画布有焦点时拦截。

**8. 工作副本存成 C 序：第一笔改动之后，整块读写都变慢**（工作量：小）
- 现在的体验：
  - 交付的 `seg.npy` 是 `fortran_order: True`，而写时复制用的 `open_memmap` 默认 C 序（`store.py:287`）。本机 `var/annotate/*/seg_edit.npy` 我逐个确认过，全是 `fortran_order: False`。
  - 结果一片 XY 在文件里是散开的。审计实测：冷读一片约 5 s，整片写入要 flush 约 2.7 s，F 序只要约 40 ms。
  - 标注员的感受是：刚打开时很快，改了一笔之后翻片明显变卡。
- 参考工具：VAST（16³ cube）、webKnossos（32³ bucket）的数据排布都保证读写一片只碰到自己那几页。
- 建议改法：
  - `open_memmap(..., fortran_order=src.flags.f_contiguous)`。
  - 写一个一次性脚本，把现有副本转成 F 序。
  - `scripts/block_quadrants.py:38-40` 改用 `asfortranarray`。
  - flush 只同步本片的字节区间。
  - 同步修改 `store.py:379-382` 里「Z fastest」的注释。

**9. 试用模式默认开启，每个人进来都自动成为审核员，角色区分实际上没生效**（工作量：小）
- 现在的体验：
  - `config.py:70` 里 `auth_open=True`。
  - `open_login` 建新号时一律用 `"reviewer"`（`auth.py:176`），`auth.py:161` 的注释却写着「默认关」。
  - 所以人人都能强制撤销别人的改动。
- 参考工具：CVAT、Label Studio 的新用户默认最低权限。FlyWire 新人先进沙盒。
- 建议改法：自动建的号改成 annotator，由管理员手动提升为审核员。修正注释。试用期结束时把 `auth_open` 关掉。

### P0-B 手感与高收益的小活

**10. 画布上看不到「现在左键、右键各做什么」，常驻提示还是错的**（工作量：小）
- 现在的体验：
  - `status()` 只拼了 z、坐标、光标下 id、缩放（`annotate.js:365-371`）。
  - 画布底下写死了「右键平移」（`annotate.html:85`），帮助里也写着「右键拖动 / H 平移」（`annotate.html:148`）。但在画笔和橡皮下右键是擦（`annotate.js:869`）。
  - Ctrl+点击只在画笔和橡皮下是拾取，在填充和清除下 Ctrl 被忽略（`annotate.js:874`）。
  - 当前 id 和半径只显示在左栏。
- 参考工具：webKnossos 状态栏随工具和按住的修饰键实时写出左键、右键的含义。VAST 面板常驻显示 Pen Diam.。
- 建议改法：
  - 状态行分左右两段。左段是「工具 · 当前 id 色块 · r=N px · 覆盖模式」。右段按 `S.tool` 和当前按住的修饰键查表生成，例如「左键 涂 17（只补空白）｜右键 擦 17｜Ctrl+点 拾取」。
  - `.vast-hint` 改由同一张表生成。
  - Ctrl/⌘+点击在 SAM 以外的所有工具下都做拾取。

**11. 一笔什么都没改时悄无声息；撤销成功也不说撤了什么**（工作量：小）
- 现在的体验：
  - 在别人的标签上涂，或者橡皮下没有当前颜色时，服务端返回 None（`store.py:771-772`）。`strokeEnd` 不看 `r.edit`（`annotate.js:563`），画面上毫无反应。
  - 点到同色区域填充直接 return（`annotate.js:475`）。
  - 撤自己的改动成功后不提示（`annotate.js:581-586`）。
  - 修缮、合并这几条路径都有「没改动」的提示（`annotate.js:448,512,646`）。
- 参考工具：webKnossos #7526，一笔没改到像素时弹 toast 说明原因。
- 建议改法：
  - `!r.edit` 时用本地索引图统计笔下的 id，给出原因，例如「笔下都是 id 5021，画笔只补空白」或「笔下没有 id 17」。
  - 同色填充时提示「这块已经是当前标签」。
  - 撤销后提示「已撤销 z10 #12 涂抹 340px」。

**12. Ctrl+Z 固定撤当前片最近一笔：翻片之后撤的不是刚才画的那笔**（工作量：小）
- 现在的体验：`undo()` 固定用 `S.z`（`annotate.js:574,580`）。在 z10 画完，按 Z 翻到 z11 看一眼，再按 Ctrl+Z，撤的是 z11 上的旧改动，可能是昨天的，也可能是别人的。
- 参考工具：TrakEM2、napari、webKnossos 的撤销栈按时间全局排列，Ctrl+Z 永远撤「我刚才那一步」。
- 建议改法：
  - 先用不带 z 的 `GET /edits`（`annotate.py:656-662`）找出本人在本块最近的一笔。
  - 如果它不在当前片，先跳过去并提示「再按一次撤销 z10 #12」。确认后带 `expect_n` 撤（`store.py:914` 已支持）。
  - 原来的「撤销本片最近一笔」按钮保留。

**13. 画笔只能补空白，橡皮只能擦当前颜色，没有「覆盖全部 / 只在某个 id 内画」**（工作量：小）
- 现在的体验：
  - `store.py:766-769` 写死了只补空白；`PaintIn` 没有模式参数（`annotate.py:467-472`）；前端 `only` 恒为 `S.cur`（`annotate.js:526`）。
  - 邻居 Y 溢进细胞 X 时，要改回来得走四步：拾取 Y、擦掉、拾取 X、再涂。
  - UX_COMPARISON.md:74 写 P0-1 已经实现，但这一半其实没做。
- 参考工具：
  - VAST 有 Paint All / Background / Parent 三档。
  - webKnossos 有 Overwrite everything / Only overwrite empty，按住 Ctrl 临时反转。
  - napari 的 `preserve_labels`。
  - 3D Slicer 的 Editable area 可限定在某个 segment 内。
- 建议改法：
  - `PaintIn` 加 `overwrite: empty|all|within` 和 `within_id`。服务端和前端 `strokeDot` 的 `want` 用同一套判断。
  - 撤销仍然精确，因为 old 是逐像素存的（`store.py:773`）。
  - 工具栏加三档单选，存 localStorage；按住 Shift 拖动时临时切换。
  - all 模式下光标圈画成红色虚线。

**14. 在背景上点填充会漫过大半片，Shift 整片模式点下去之前也看不到范围**（工作量：小）
- 现在的体验：
  - 点背景时 `fill` 对 id 0 做 4 连通泛洪，没有面积上限（`store.py:712-732`），前端 `floodLocal` 同样没有（`annotate.js:454-468`）。
  - 审计在真实块上实测：点一下背景会改掉整片的 22–41%。
  - `regionAt` 遇到 label 0 直接返回 null（`annotate.js:310`），所以悬停在背景上不高亮。
  - Shift 整片模式会连视野外的碎块一起改，事先不确认，事后也不报数。
- 参考工具：webKnossos 的填充有体积上限，超过就截断并提示。ImageJ 的 Wand 有容差。napari 的 contiguous 是一个常驻可见的开关。
- 建议改法：
  - 填充和清除工具下允许高亮背景连通块；按住 Shift 时高亮本片所有同 id 像素；状态栏显示「将改 N px」。
  - 超过阈值（例如本片的 20%，或 2 万 px）或者涉及多于一块时，先确认再写。
  - 不允许对背景用 Shift 整片。
  - 填充成功后提示像素数。

**15. 修缮 R 一点就落盘；SAM 预览没有 Enter 确认**（工作量：小）
- 现在的体验：
  - `refineAt` 直接 POST 然后 afterEdit（`annotate.js:438-451`），看不到收掉的是哪几块。
  - 修缮借用 SAM 的灵敏度滑块（`annotate.js:443`），拖这个滑块还会触发 SAM 重新推理（`annotate.js:853-855`）。
  - `reach` 参数被接收、也写进记录，但算法没用到（`store.py:782,827`）。
  - 键盘处理里没有 Enter 分支，SAM 只能点侧栏按钮（`annotate.js:830-850,639-652`）。
- 参考工具：Fiji 的滤波器有 Preview 复选框加 OK/Cancel。webKnossos Quick Select 和 Paintera 插值都是 Enter 提交、Esc 放弃。
- 建议改法：
  - `refine_edge` 拆成 compute 和 apply，走 token 加 `slice_rev`，照 SAMService 的做法。
  - 预览时用红色叠加显示「将收回 N px」。修缮有自己的灵敏度滑块，拖动时防抖重新预览。
  - Enter 应用：有 SAM 预览时填为当前标签；Shift+Enter 填为新标签。
  - 删掉 `reach`。

**16. EM 没有亮度、对比度、反相调节**（工作量：小）
- 现在的体验：服务端把灰度原样编成 PNG（`store.py:334-338`），前端原样 `drawImage`（`annotate.js:261`），界面上没有任何灰度控件。偏暗的切片上膜和胞质分不清。
- 参考工具：ImageJ 的 Brightness/Contrast（Ctrl+Shift+C，有 Auto），webKnossos 的 intensity range 加 invert，Neuroglancer 的 invlerp 滑块。
- 建议改法：右栏加亮度、对比度、反相三个控件，EM 画布用 CSS filter 实现，几乎零成本。「自动」按本片 1%–99% 分位拉伸。设置按块存进 localStorage。

**17. 不能单独显示或隐藏某个标签（没有 solo）**（工作量：小）
- 现在的体验：分割层只有整层开关（`annotate.js:1053`）和按住 Tab 全隐藏。调色板对每个 id 都上色（`annotate.js:235-240`）。想只看一个细胞翻片检查连贯性，做不到。
- 参考工具：Labkit 每个标签有眼睛图标，napari 有 show selected，Neuroglancer 每个 segment 单独显隐，VAST 有 solo。
- 建议改法：
  - 加 `hiddenIds` 集合和 solo 开关。被隐藏的 k 在调色板里设为透明，缓存键带上显隐版本号。
  - 列表每行加眼睛图标；加一个快捷键（Q 目前空着）。
  - solo 时把当前 id 在本片的所有碎块都描边。
  - 注意被隐藏的像素仍然不是「空白」，画笔还是涂不上去，界面上要提示这一点。

**18. 审核员在对比页钉的标记，工作台里看不到**（工作量：小，只读版）
- 现在的体验：
  - `annotate.js` 里没有任何 mark 相关代码，`grep -c mark` 结果是 0。
  - `list_marks` 已经返回每片的待处理数 `by_z`（`marks.py:111-114`），但没有页面用它。
  - 工作台 URL 只认 z（`annotate.js:16`），对比页的「返回标注」也只带 block 和 z（`annotate_compare.js:218`）。
- 参考工具：CVAT 的 Issue 直接叠在标注员自己的画面上，旁边有 Issues 面板可逐条跳转。CATMAID 的 Review widget 在标注视图里逐条推进。
- 建议改法：
  - 加载块时读一次 marks，本片未解决的标记在高亮层画成编号图钉，悬停显示文字。
  - z 滑条下按 `by_z` 画刻度。
  - J/K 跳到下一条或上一条未解决标记。
  - 标记卡片加「在工作台打开」，链接为 `/annotate?block&z&x&y&mark=`；工作台读取后居中到该点。

**19. 对比页的 Neuroglancer 两栏打不开时，连「改之前」的图都看不到**（工作量：小）
- 现在的体验：
  - 两栏指向 appspot 和 `gs://h01-release`（`neuroglancer.py:37-40`），在大陆网络下打不开。
  - iframe 加载失败时 JS 感知不到，`ngSync` 只处理自家接口出错（`annotate_compare.js:399,404`）。
  - 前端其实已经解码了 `data.before`（`annotate_compare.js:221,224`），却只拿来做鼠标读数（`annotate_compare.js:291`）。
- 参考工具：Neuroglancer 是可以自托管的静态前端；VAST 和 webKnossos 都是自托管。
- 建议改法：用 `data.before` 叠在 EM 上画一个本地「原始」栏。页面加载时用 3 秒超时探测 NG，失败就自动切到本地栏，同时提供手动开关。自托管 NG 放到 P2。

---

## P1：下一个迭代

### 反馈与状态

**20. 看不到保存和忙碌状态；断网或会话过期时笔画会悄悄丢**（工作量：小（状态可见）+ 中（重试队列））
- 现在的体验：
  - 顶栏「改动自动保存」是写死的（`annotate.html:7`）。
  - `mergeBusy` 期间按键和点击被静默吞掉（`annotate.js:872,922`）。
  - 「正在撤销…」写进了 `an-merge-hint`，可前一行 `mergeArm(null)` 刚把它设成在非合并工具下隐藏（`annotate.js:489,577-578`），所以大多数时候看不见。
  - 页面没有 beforeunload。
  - 写请求遇到 401 直接跳登录页（`annotate.js:73`），手上这一笔丢了。
  - 块内第一笔会同步复制整块（`store.py:281-294`，审计实测约 4 s），期间没有任何说明。
- 参考工具：webKnossos 顶栏常驻保存状态，失败自动重试，离开页面前拦截。ImageJ 状态栏有进度条，Esc 可中止。
- 建议改法：
  - 维护在途请求计数，顶栏显示「保存中 N / 未保存 / 离线」；请求超过 300 ms 时在画布角落显示操作名和已用秒数；修掉提示被隐藏的问题；有在途请求时注册 beforeunload。
  - 在第 2 条的队列上加失败重试，401 时弹内嵌登录框，登录后重放队列。
  - 选块时如果还没有工作副本，在后台预建。

**21. 提示消息放在右栏、不会自动消失，重复同一句话时看不出又触发了一次**（工作量：小）
- 现在的体验：`flash` 只设 `display:block`（`app.js:10-16`），被挪进了右栏（`annotate.js:37`）。
- 参考工具：Neuroglancer 的状态消息贴在视图底部；webKnossos 用自动消失的 toast。
- 建议改法：结果类消息同时在画布状态行回显 2 秒。每次调用加一个 pulse 动画。信息类 6 秒后淡出，错误类保留。侧栏保留最近 10 条并带时间。

**22. 按住修饰键时光标不变；空格平移的抓手光标被覆盖；切走窗口后按住状态会卡住**（工作量：小）
- 现在的体验：
  - 光标只跟 `data-tool` 走（`annotate.js:433`）。
  - `.vast-stage.pan` 和 `[data-tool=…]` 的优先级相同，而后者写在后面（`style.css:274` 对比 `377-386`），所以按空格时仍显示原工具的光标。
  - 修缮只有十字光标（`style.css:380`）。
  - 没有 window blur 处理：按住 Tab 或空格时切走窗口，`tabHeld`、`blink`、`spacePan` 会卡住（`annotate.js:948-951`）。
- 参考工具：VAST「按住即变、松开即回」，每种按住状态都有反馈。KNOSSOS 松键时还原。
- 建议改法：
  - 在 stage 上写 `data-mod`，CSS 按修饰键切换光标。
  - 把 `.pan` 规则提高优先级。
  - 给修缮一个专用图标。
  - 在 window blur 和 visibilitychange 时复位所有按住状态。

**23. 画笔圈的几处问题：涂抹时圈停在落笔点、调半径时没有数字、画笔下 Tab 同时隐藏分割**（工作量：小）
- 现在的体验：
  - `S.stroke` 分支直接 return，不重画圈（`annotate.js:904`）。
  - Tab 拖动时数字只在左栏更新（`annotate.js:894`）。
  - Tab 按下会触发 blink（`annotate.js:940`）；松开鼠标而 Tab 还按着时，自动重复的按键又把分割藏起来。
- 参考工具：Labkit 涂抹时也一直在光标处画圆。VAST 调笔径时面板实时显示数值，而且看原图用的是另一个键。
- 建议改法：
  - 涂抹时每帧都重画 gHi 上的圈。
  - 调半径时在圈旁显示「r=12」，1 秒后淡出。
  - 画笔和橡皮下 Tab 只用来调半径，看原图换一个键。

**24. 帮助框、完整说明页、画布提示三处手写，已经互相矛盾**（工作量：中）
- 现在的体验：
  - 键位散在一长串 if/else 里（`annotate.js:920-947`）。
  - 帮助里没有 +/-、空格、Esc（PageUp/PageDown、Home/End 翻片键已移除）（`annotate.html:142-170`）。
  - 完整说明页已同步 A/Z 翻片和中键/空格平移；修缮、全部撤销的详细说明仍需补全。
- 参考工具：Neuroglancer 的帮助面板由绑定表自动生成。VAST 的 Keyboard Shortcuts 窗口分表列出。
- 建议改法：抽一张 BINDINGS 表（键、修饰键、适用工具、动作、说明），keydown、帮助框、状态栏右段都由它生成，当前工具的条目置顶。guide 页删掉快捷键部分，改为链接到帮助。与第 6 条一起改用 `ev.code`。

### 撤销与历史

**25. 撤销会销毁数据，也没有重做**（工作量：中）
- 现在的体验：
  - 撤销时 `log.pop`，然后 `f.unlink()` 删 npz（`store.py:937-947`）。
  - 审计流水只记元数据（`store.py:953-956`）。
  - 审核员强制撤掉的、「全部撤销」撤掉的，都永远找不回来。确认框自己也写着「撤了不能再恢复」（`annotate.js:178`）。
- 参考工具：TrakEM2 和 napari 用 Shift+Ctrl+Z 重做。PyChunkedGraph 把撤销写成一条反向操作，原操作一直保留。
- 建议改法：
  - 撤销时把 npz 移到 `edits/undone/`，而不是删除。
  - 重做 = 把 new 写回，只写当前值仍等于 old 的像素，并报告有多少像素已被后来的改动覆盖。重做记成一条带 `redo_of` 的新记录。
  - 本人在该片做新一笔时，清空本人在该片的重做栈。
  - 绑定 Ctrl/⌘+Shift+Z 和 Ctrl+Y。

**26. 只能撤本片最近一笔：同片上别人后来又画了一笔，我就撤不了自己更早那一笔**（工作量：中）
- 现在的体验：`undo` 只挑最新一条记录（`store.py:906-910`），是别人的就抛 `UndoForbidden`（`store.py:920-922`）。
- 参考工具：PyChunkedGraph 可以撤任意历史操作；CATMAID 的命令历史可以点选。
- 建议改法：选择性撤销 #k：只回填 #k 中没被后来任何一笔碰过的像素，被碰过的留在 #k 里（复用 `store.py:930-936` 的部分裁剪）。因为不碰别人的像素，撤自己的任意一笔不需要 force。

**27. 没有「撤销到这一笔」；全部撤销是平方级复杂度**（工作量：小）
- 现在的体验：
  - `undo_all` 只能回到原始分割（`store.py:963-982`），每撤一笔都重写整份 `edits.jsonl`（`store.py:948-950`）。审计实测 302 笔要 11.9 s，而且全程持锁。
  - 撤销进行中再按 Ctrl+Z 会被丢弃（`annotate.js:922`）。
- 参考工具：webKnossos 的 Restore Older Version、CATMAID 按 F9 打开的命令历史。
- 建议改法：新增 `POST /undo-to?z=&n=`，一次请求撤到指定记录，最后只写一次日志。历史行加「撤销到此处」，先确认会撤几笔、多少像素、其中有谁的。

**28. 历史高亮已实现；仍缺少居中和撤销预览**（工作量：中）
- 现在的体验：工作台历史行可点击，列表与该笔像素区域同步高亮；只读接口为 `GET /edits/{n}/mask.png?z=`。对比页的操作表仍不能点。
- 参考工具：webKnossos 的版本历史可以只读预览；Photoshop 的历史面板点哪步显示哪步。
- 建议改法：在现有高亮基础上增加点击居中（复用对比页的 `focusRegion`）。「预览撤到此处」用对比滑块显示，Enter 调第 27 条的 undo-to。

**29. 历史只看本片最近 30 条，看不到已撤销的，也没有「我的 / 整块」视图**（工作量：小）
- 现在的体验：历史固定请求 `edits?z=&limit=30`（`annotate.js:1022`）。`/audit` 只有对比页在用（`annotate_compare.js:187`）。撤销按钮在左栏，列表在右栏（`annotate.html:74-75,121-122`）。
- 参考工具：webKnossos 的版本历史覆盖整个标注并按天分组。
- 建议改法：加「本片 / 我的 / 整块」切换和「含已撤销」开关，数据来自 `/edits`（不带 z）和 `/audit`。列表分页，每行点击跳片。撤销按钮挪到列表头部。

### 画笔与填充

**30. 画笔和填充没有膜约束或灰度蒙版（P0-4 的「预防」一半没做）**（工作量：中）
- 现在的体验：`paint` 全程不读 EM（`store.py:734-780`），只能画完再用 R 修（UX_COMPARISON.md:34,103）。
- 参考工具：VAST 可以限定只在 EM 灰度落在 [min,max] 内的像素上画，并勾选 Contiguous only。3D Slicer 的 Editable intensity range 对画笔、填充、剪刀都生效。
- 建议改法：
  - 画笔加「避开膜」和「只连通」两个开关。服务端缓存本片的 `membrane_map`，前端下发 1-bit 膜图，预览与落盘一致。
  - 默认灵敏度取 0 档：审计实测 50% 时细胞内部也有 28% 被判成膜，画出来像筛子。
  - 点背景填充时同样走膜约束，并保留第 14 条的面积上限。
  - 另做一个纯灰度区间版（双滑块），直接在浏览器里判断。

**31. 画完闭合轮廓不会自动填满内部**（工作量：小）
- 现在的体验：`paint` 只沿轨迹盖圆印（`store.py:753-764`），还得再切到 F 点一下，而那一下可能漫出去。
- 参考工具：VAST 和 webKnossos 的画笔在闭环时自动填充。
- 建议改法：在本笔的外接框内只填这一笔新围出来的洞，原本故意留空的洞不填；和这一笔记在同一条记录里。加「闭环自动填充」开关，默认开。

**32. 画笔半径只按图像像素计，上限 60；没有 Shift+滚轮，也没有预设**（工作量：小）
- 现在的体验：
  - `setBrush` 把半径夹在 0–60（`annotate.js:1037`），服务端却允许到 200（`annotate.py:470`）。
  - Tab 拖动是线性的，每 4 屏幕像素改 1（`annotate.js:894`）。
  - Shift+滚轮也用于缩放（`annotate.js:416-421`）。
- 参考工具：TrakEM2 的笔径按屏幕像素恒定。KNOSSOS 用 Shift+滚轮按 10% 改。webKnossos 有小/中/大三档预设。
- 建议改法：
  - 加屏幕像素模式，发送前换算成图像像素。
  - UI 上限放到 200。
  - Shift+滚轮按 ×1.1 改半径。
  - Tab 拖动改为指数映射。
  - 用 Alt+1/2/3 做三档预设。

**33. 在两个 id 之间来回切很费手**（工作量：小）
- 现在的体验：修一处溢出要 Ctrl+点邻居、擦、再 Ctrl+点回自己的细胞（`annotate.js:436,452,522-526`）。
- 参考工具：napari 的 X 键；webKnossos 的分割列表会登记点过的 id。
- 建议改法：在当前标签下显示最近用过的 5 个 id。一个键（例如 `）在最近两个 id 之间切换，Alt+数字直接选。

**34. 合并只能一对一对点，列表多选也只能删不能合并；合并前看不到合并后的样子**（工作量：小（连续并入）/ 中（攒一批））
- 现在的体验：每合并一对就把保留块清空（`annotate.js:507,517`），第二下点完立刻写入。勾选模式只有删除（`annotate.js:975-1014`）。
- 参考工具：webKnossos 保持活动 segment，Shift+点击逐个并入。VAST 先 Collect 预览，确认后再 Weld。
- 建议改法：
  - 加「连续并入」：第一块一直保留，直到按 Esc。
  - 勾选栏加「并入当前标签」，后端新增 `merge_labels`。
  - 可选「攒一批」模式：前端先重新着色做预览，Enter 一次提交为一笔记录。

**35. 没有拆分工具**（工作量：中（2D））
- 现在的体验：原来的切割和分离工具在 294898f 里被删掉了。现在拆开粘在一起的两个细胞要五步：拾取、擦出一条缝、新建标签、填充、补缝，每片都得来一遍。
- 参考工具：VAST 的 Split 自动找最小截面。FlyWire 和 webKnossos 的 Multi-cut 用红蓝种子点，先看 Split Preview 再提交。
- 建议改法：
  - 复用 `refine_edge` 已有的膜图和内部块分解（`store.py:803-819`），把最后一步从「清成背景」改成「给新 id」。
  - A 侧、B 侧各撒种子点，在膜强度上跑 `watershed_ift`，走预览→应用。
  - 动手之前先问清楚 294898f 为什么删掉了旧工具。

### 跨片（3D 最便宜的几步）

**36. 没有关键片插值：隔几片画一次，中间还得一片片补**（工作量：中）
- 现在的体验：损坏切片插值已删除，目前没有关键片之间的标签插值功能。
- 参考工具：webKnossos 在两片上画同一 id 后按 V；TrakEM2 的 Interpolate gaps；Paintera 的形状插值。
- 建议改法：新增 `interpolate_id(block, id, za, zb)`，独立实现形状插值算法，只写中间片的空白像素，整次插值记成一条多片记录，标为待复查。按 I 自动找「上一张画过当前 id 的片」，预览后 Enter 应用。

**37. 合并、删除只作用于本片；整块合并只有脚本接口**（工作量：中）
- 现在的体验：
  - 前端只调 `merge-pair`（`annotate.js:509`）。`merge(scope="block")` 已经实现（`store.py:858-883`），但界面上没有入口。
  - 还有两个隐患：`_fresh` 遇到 z=None 不做任何冲突检查（`annotate.py:109-112`）；`slice_rev` 又把 z=None 的审计条目算成动过每一片（`store.py:590-592`），于是一次整块操作之后，所有人的下一笔都会收到 409。
- 参考工具：VAST 的 Weld 按整个 segment 重编体素；FlyWire 的合并是对象级的。
- 建议改法：
  - 合并加范围单选「这一块 / 本片同 id / 整块同 id」，选整块时先 dry-run，显示涉及几片、多少体素，再确认。
  - 审计条目写入实际改到的 z 列表。
  - 多片记录提供「整笔撤销」。
  - 整块合并限定审核员或管理员。

**38. 画笔没有 Z 深度（对应 VAST 的 Max Paint Depth）**（工作量：中）
- 现在的体验：`paint` 只写第 z 片（`store.py:750`）。`neighbour.py:10-11` 统计过：「上下两片同 id、中间这片是 0」的像素平均每片约 1000 个。
- 参考工具：VAST 的 Max Paint Depth（±8 片），只填与邻片已画区域重叠的部分，擦除时不跨片。napari 可以把编辑维度设成 3。
- 建议改法：`PaintIn` 加 `depth`（0/1/2/4）。只在 z±depth 内、与邻片同 id 区域重叠的位置补空白。`_fresh` 改为检查每一片的版本。光标旁显示当前深度。

**39. 没有 Z 向断档扫描**（工作量：小）
- 现在的体验：目前没有 Z 向断档扫描，只能翻片时碰上；损坏切片扫描已移除。
- 参考工具：各工具多靠常驻 3D 网格用肉眼找；我们手里有标签体积，直接算更便宜。
- 建议改法：新增 `/zgap/scan`，找出 `seg[z-1]==seg[z+1]≠0` 而 `seg[z]==0` 的连通块，按面积排序列出；点一条跳过去，并提供「补上」按钮。

**40. 看不到相邻片（没有叠影 onion skin）**（工作量：小）
- 现在的体验：`render` 只画当前 z（`annotate.js:257-273`）；并排视图是同一片的 EM 对 EM+分割。
- 参考工具：TrakEM2 的 Color cues、CATMAID，都用红、蓝两色叠显上下相邻层。
- 建议改法：加「叠影」开关，用已经预取的 z±1 索引图，在高亮层画出当前 id 在上片的红虚线和下片的蓝虚线轮廓。纯前端改动。

**41. 已移除对应功能**

损坏切片扫描与插值修补已删除，此项待办关闭。保留编号供历史引用。

**42. 标签只能在本片内找；不知道一个 id 分布在哪几片**（工作量：中）
- 现在的体验：列表和搜索只覆盖本片的 id 加新建的 id（`annotate.js:957-959`）；`labels_table` 只统计第 z 片（`store.py:414-418`）。
- 参考工具：VAST 分段窗口列出整卷，每个 segment 有锚点；webKnossos 的 Segments 列表；Labkit 的靶心按钮；Neuroglancer 可在整个数据集里搜 id。
- 建议改法：维护一张「id → z 范围、每片像素数」的索引，由 `_record` 增量更新。本片搜不到时回退查全块，每行显示「z12–48」，点击跳到像素最多的那片。输入完整 id 回车就能设为当前标签。

**43. 点列表行不会在画面上定位，行还会跳到列表顶部；批量删除勾选时画布不高亮**（工作量：小）
- 现在的体验：
  - 点行已会同时高亮列表和图像中的全部同标签区域；尚不自动居中。列表仍按「当前置顶」整体重排。
  - `renderHi` 里没有 bulk 分支（`annotate.js:274-303`）。
- 参考工具：Neuroglancer 悬停列表行就高亮该段；Labkit 的靶心按钮。
- 建议改法：
  - 悬停行时描出该 id 在本片的所有碎块；双击居中放大。
  - 排序改为稳定：当前标签单独固定在上方一行，其余顺序不因点击而变。
  - 批量勾选的 id 在画布上用红色半透明显示，勾选栏显示像素数。

**44. 没有标签统计；新建标签在别片用过，本片却显示「未使用」**（工作量：小）
- 现在的体验：`${n || "未使用"}` 里的 n 是本片计数（`annotate.js:964`），而 ANNOTATE.md:65 的说法是整体意义上的「未使用」。
- 参考工具：webKnossos 的 Segment Statistics；MorphoLibJ 的 Analyze Regions。
- 建议改法：先把文案改成「本片无」。有了第 42 条的索引之后，显示全块体素数和 z 范围；本片碎片数大于 1 时加一个小标记。

**45. 相邻标签容易撞色，界面上也没法换色**（工作量：小）
- 现在的体验：颜色是 id 的 FNV 哈希，没有种子（`annotate.js:46-54`）；新建 id 只避开完全相同的 RGB（`store.py:452-456`）。
- 参考工具：Neuroglancer 按 L 换配色种子；napari 的 shuffle colors。
- 建议改法：`colorOf` 加一个全局种子，提供「换配色」按钮，工作台和对比页共用同一个种子。新建 id 时挑与邻居色差最大的那个。

### 看图与导航

**46. 按 V 切并排或窗口尺寸一变，放大好的位置就重置；按 1 时绕左上角缩放**（工作量：小）
- 现在的体验：`setView` 和 ResizeObserver 都调用 `fit()`（`annotate.js:397,1062-1063`）；按 1 只改 zoom（`annotate.js:943`）。
- 参考工具：Neuroglancer 切换布局时保持中心点和缩放。
- 建议改法：记住视口中心对应的图像坐标，布局变化后让它继续居中；按 1 以光标为锚点调用 `zoomAt`。

**47. 缩小看全图时像素被抽掉，描边断成虚线；悬停高亮压住要描的膜**（工作量：小）
- 现在的体验：三层画布一律 `image-rendering: pixelated`（`style.css:276`）；悬停高亮在画笔下也照画（`annotate.js:277-280`）。
- 参考工具：webKnossos 和 Neuroglancer 缩小时读下一级 mip；napari 的描边粗细可以独立调。
- 建议改法：
  - zoom<1 时 EM 层改用平滑缩放。
  - 描边保持恒定的屏幕像素宽度。
  - 画笔、橡皮、修缮下默认不画悬停高亮。
  - EM 接口加 `?mip=1`。

**48. 刷新页面总是回到 z 0，个人设置也全部丢失**（工作量：小）
- 现在的体验：
  - 选块时把 URL 改成只剩 block（`annotate.js:1083`），`goZ` 从不写回 URL。
  - 除了标注人名字（`annotate.js:118-129`），透明度、半径、视图等设置都不保存。
- 参考工具：webKnossos 和 Neuroglancer 的 URL 实时带着位置；webKnossos 的用户配置按账号保存。
- 建议改法：
  - 用 `replaceState` 写入 block、z、x、y、zoom 和当前 id，启动时读回来。
  - 做一个 prefs 对象，统一读写 localStorage。
  - 左栏各节可以折叠。

**49. z 滑条上看不出哪片改过、哪片有标记；放大后没有鸟瞰图；触控板一划会翻过头**（工作量：中）
- 现在的体验：z 滑条是一根空白的 range（`annotate.html:71`）；工作台和对比页均已改为滚轮缩放，翻片使用 ↑↓/A·Z。
- 参考工具：CVAT 可以跳到下一个有 Issue 的帧；ImageJ 放大后显示缩略导航图。
- 建议改法：滑条下画刻度带，标出有改动、有标记的片；加跳到下一个有改动片的快捷键；放大超过 1.5 倍时显示小地图。

**50. 只有块内坐标，拿不到全局坐标，没法复制，也没有比例尺**（工作量：小）
- 现在的体验：`info.origin` 已经返回了（`store.py:246`），前端没用（`annotate.js:365-371`）。
- 参考工具：Neuroglancer 显示全局坐标并带复制按钮，还有比例尺；webKnossos 用 Ctrl+I 复制光标下的 id。
- 建议改法：状态栏同时显示全局坐标；加一个键（例如 Y）复制「坐标 + id」；按 `voxel_size_nm` 画比例尺。

### 性能：不依赖金字塔就能拿到的收益

**51. 块锁里做重活：一个人导出或跑 SAM，同块所有人一起卡住**（工作量：中）
- 现在的体验：
  - 全块溯源报表整段持锁（`provenance.py:204-213`），审计实测 76.5 s。
  - SAM 从加载模型到推理全程持块锁（`sam.py:67`）。
  - `labels_json` 也要拿这把锁（`annotate.py:331`）。
- 参考工具：webKnossos 把数据读取和标注写入拆成两个服务；CVAT 把长任务交给 worker 队列。
- 建议改法：锁内只取快照，计算放到锁外。报表改成逐片加锁或后台生成文件。SAM 只在 apply 时拿锁校验 `slice_rev`。被新请求取代的 SAM 请求返回 409 superseded，不再推理。

**52. EM 每片是约 1 MB 的 PNG，每次都要回源校验**（工作量：小）
- 现在的体验：响应头是 `no-cache`，ETag 要先编码整张 PNG 再算 md5（`annotate.py:296-307`）；`labels.png` 没有 ETag（`annotate.py:318`）。
- 参考工具：Neuroglancer precomputed 的 EM 用 JPEG 静态文件，可以长期缓存。
- 建议改法：URL 里已经带了 `em_version`，EM 直接设 `immutable` 长缓存；默认用 JPEG q90，保留无损开关；labels 加基于 `slice_rev` 的 ETag；加 GZip 中间件。

**53. 前端缓存按片数而不按字节，还是 FIFO；预取不分方向、不能取消；连播不等画面**（工作量：小）
- 现在的体验：
  - 固定缓存 24 片（`annotate.js:106`），在 2048² 的块上约 1 GB。
  - 预取固定取 ±1..3（`annotate.js:112`）。
  - 连播用 `setInterval` 盲目推进 z（`annotate.js:1046-1051`）。
- 参考工具：Neuroglancer 按字节设内存上限，按距离排优先级，并取消过时的请求。
- 建议改法：
  - 缓存改成按字节预算的 LRU。
  - 顺翻片方向多预取几片，用 AbortController 取消过时请求。
  - 连播复用对比页的 warm 和缓冲逻辑（`annotate_compare.js:411-480`）。

**54. 每笔改动都重拉、重着色整片；新建标签会清空全部缓存**（工作量：中）
- 现在的体验：`afterEdit` 先 `invalidate` 再 `goZ`（`annotate.js:566-571`），整片重新下载和着色；`reserveLabel` 调 `dropAll()`（`annotate.js:597-600`）。
- 参考工具：webKnossos 只改本地 bucket，异步同步到服务端；VAST 直接写本地 cube。
- 建议改法：写接口的响应带回改动区域的外接框和新索引，前端就地更新并只重绘脏矩形；只有 rev 跳号时才整片重载。新建 id 只追加到 `createdIds`。

**55. 编辑日志每笔都是 O(N)**（工作量：小）
- 现在的体验：`_record` 和 `_audit_append` 每笔都读整份日志（`store.py:508,540-544`）。
- 参考工具：CATMAID、CVAT 把操作历史放在带索引的数据库里。
- 建议改法：写入方在内存里维护 edits 列表和 `next_n`，新记录只追加；给 edits 建 z→[n] 的索引。

### 对比页与审核

**56. 标记只是讨论串，缺审核语义**（工作量：小/中）
- 现在的体验：
  - 状态只有 open/resolved，任何人都能改（`marks.py:161-163`），测试里也专门断言了这一点（`tests/test_marks.py:80`）。
  - 解决人只记名字（`models.py:555`），标记不记 rev、不记编辑号。
  - 删除是硬删（`marks.py:176-179`）。
  - `kind=agent` 不限身份（`marks.py:44`）。
- 参考工具：CVAT 标注员 Resolve 后审核员可以 Reopen；CATMAID 的审核记录绑定节点，节点一改就作废。
- 建议改法：
  - 加 category、assignee、rev、edit_n 字段。
  - 状态改为 open → fixed → verified，verified 只有审核员能置。
  - 状态变化自动写一条事件评论。
  - 改为软删除。
  - `kind=agent` 限定 agent 账号。

**57. 没有切片级的「审核通过」；改动之后也不会回到待复审**（工作量：中）
- 现在的体验：没有任何审核表（`models.py:505-569`），可 `slice_rev` 已经有了（`store.py:585-593`）。
- 参考工具：CATMAID 节点被改后自动回到未审；CVAT 有 Accept/Reject。
- 建议改法：新建 `annot_slice_reviews` 表（z、rev、verdict），提交时校验 rev；「已通过」「已改待复审」实时计算；块下拉框显示「已审 37/100」。

**58. 对比页不能按处推进审核，也看不到单独一笔改了什么**（工作量：中）
- 现在的体验：键盘只有 ←/→ 和空格（`annotate_compare.js:565-572`）；操作行不能点（`annotate_compare.js:174`）；编辑记录没有外接框（`store.py:514-517`）。
- 参考工具：CATMAID Review 用 Q/W 逐个推进。
- 建议改法：J/K 切换下一处和上一处，本片走完自动跳到下一张有改动的片，状态栏显示「3/17」；操作行可点，只描出这一笔的像素。

**59. 不能按标注人或时间筛选改动，没有「自上次通过以来」**（工作量：中）
- 现在的体验：editors 图已经解码了，但只用来做读数（`annotate_compare.js:221-224,289-290`）；audit 接口只接受 z 和 limit（`annotate.py:667-678`）。
- 参考工具：CATMAID 可以只审别人的；CAVE 的 change log 可按 user 和 timestamp 查询。
- 建议改法：先纯前端加「标注人」下拉筛选。再在 `_sources` 里多生成一张「每像素最后生效的编辑号」图，提供「只看上次通过以来的改动」。audit 接口加 `by=` 和 `since=` 参数。

**60. 对比页不跟随实时改动；分享链接不带视图状态；标记不能导出；时间有两套时区**（工作量：小（各项））
- 现在的体验：
  - 快照只加载一次（`annotate_compare.js:220`），也没有接入 presence。
  - URL 只带 block、z 和 mark（`annotate_compare.js:217-218,676-679`）。
  - 导出里没有标记（`annotate_compare.js:246-274`）。
  - 编辑记录用服务器本地时间（`store.py:518,535`），标记用 UTC（`marks.py:26-30`）。
- 参考工具：webKnossos 显示正在编辑的人；Neuroglancer 把整个视图状态放进 URL；CVAT 的质量报告；各工具普遍以 UTC 存储、按本地时区显示。
- 建议改法：
  - 对比页每 15 秒调 `/presence`，发现 rev 变化就弹刷新横幅。
  - 把缩放、中心、模式、筛选编码进 URL hash。
  - 新增 `marks/export` 导出 CSV 或 NG annotation JSON。
  - 服务端统一写 UTC ISO 时间，前端按本地时区显示。

---

## P2：结构性投入

**61. 任务与审核闭环：指派、提交→审核→驳回/通过、验收后锁定并冻结快照、我的待办与通知**（工作量：大，最小版为中）
- 现在的体验：
  - 数据块页没有指派人和状态列（`annotate_blocks.html:19`）；`Block.status` 是 QC 流水线的状态（`models.py:160`）。
  - 写接口里没有 submit/accept（`annotate.py:494-643`）。
  - 下游直接读一直在变的 `seg_edit.npy`（`annotate_blocks.html:44`）；`gt_annotation` 资产类型和 `Block.label_version` 都没有被写入（`models.py:85,155`）。
- 参考工具：CVAT 的 Job 有 Stage、State、Assignee；webKnossos 的 Task；CAVE 按时间点 materialize 出带版本号的快照。
- 建议改法：
  - 新建 `annot_tasks` 表（z 段、assignee、reviewer、state、round）。
  - 驳回时带上关联的标记，任务回到原标注员。
  - 通过后任务范围只读（返回 423），并登记 `gt_annotation` 快照。
  - 提供跨块的「我的待处理」，@提醒，可推送到飞书、钉钉、企业微信 webhook。

**62. 复审基线固定是交付原件**（工作量：中）
- 现在的体验：溯源的 before 固定取 `_seg_ro`（`provenance.py:39-41`）；撤销会删掉 npz，旧状态无法从流水重建。
- 参考工具：webKnossos 可以和任一旧版本对比；CAVE 可以在两个 materialization 之间比较。
- 建议改法：每次提交时保存 `rounds/r{n}.npz`，对比页的基线可以选「上次提交」。

**63. 个人产量与质量统计、按操作导出的 audit CSV**（工作量：中）
- 现在的体验：只能看单块里每人的像素数（`provenance.py:183-184`）；在线状态只在内存里，60 秒过期（`annotate.py:681`）。
- 参考工具：webKnossos 的 Time Tracking；CAVE 的 change_log。
- 建议改法：新增 `/annotate/stats`（操作数、被他人撤销数、收到的标记数），心跳落表用来估算用时，`/audit?format=csv` 按 change_log 的列导出。

**64. 沙盒与考核；双人独立标注一致性；GT 暗抽检**（工作量：中（沙盒）/ 大（分支副本））
- 现在的体验：每个块只有一份共享的 `seg_edit.npy`（`store.py:18-22,52`）；`User` 没有资质字段（`models.py:505-519`）。
- 参考工具：FlyWire 的沙盒；CVAT 的 GT/Honeypot；Label Studio 的 agreement 矩阵。
- 建议改法：把沙盒块挂到 `EXTRA_ROOTS` 下，每个学员一份独立副本，与标准答案逐细胞比 IoU。以后再做按用户的分支副本和 overlap=2 一致性检查。

**65. 标签的名字、状态和锁定（审核过的细胞防止被覆盖）**（工作量：中）
- 现在的体验：`created_labels.json` 只存 id（`store.py:439-466`）；fill、merge、clear 都不检查任何锁。
- 参考工具：VAST 可以命名、锁定 segment。Paintera 的锁只隐藏不阻止编辑，被用户在 #605 里吐槽，说明锁必须真正拦住写入。
- 建议改法：加一张元数据表；所有写入口都做 `mask &= ~isin(plane, locked)`，并把被挡掉的像素数告诉前端。

**66. 正交切面 XZ/YZ 条带**（工作量：中）
- 现在的体验：只有 XY 出图（`store.py:297-302,334-338`）。
- 参考工具：VAST 有 2–4 个面板；webKnossos 三视口；Neuroglancer 4panel。
- 建议改法：
  - 后端加 `em-xz`、`labels-xz`，按 voxel size 拉伸 Z 方向，第一版只读。
  - 注意和 P0 第 8 条互相牵制：审计测的「XZ 比 XY 还快」是在 C 序副本上测的。改成 F 序以后，XZ 仍然便宜（每片一段连续的 X），但 YZ 要跨步读，需要分块存储，或者另存一份 Z 序副本。

**67. 3D 网格里看不到自己改的结果；非 H01 的块没有任何 3D**（工作量：中）
- 现在的体验：3D 跳转用的是交付原件（`annotate.py:418-421`）；非 H01 的块返回 None（`neuroglancer.py:101-106`）。
- 参考工具：VAST 的 3D Viewer 直接渲染编辑后的分割；Paintera 边画边更新网格。
- 建议改法：新增 `/mesh/{id}`，从工作副本生成网格（纯 numpy 暴露体素面即可），缓存以 `slice_rev` 为键；前端嵌一个 three.js 小视口。

**68. SAM 沿 Z 传播**（工作量：中）
- 现在的体验：只用 `SAM2ImagePredictor` 推理单片（`sam.py:53-71`）。
- 参考工具：SAM2 的视频预测器；micro-sam 的 Segment All Slices。
- 建议改法：第一版拿上一片结果的框和质心当下一片的提示循环调用，面积或 IoU 突变时自动停；逐片预览，Enter 写成一条多片记录。

**69. 渲染全在主线程、画布始终是全分辨率**（工作量：中）
- 现在的体验：画布设成 W×H（`annotate.js:1076`），解码、着色、描边逐像素在主线程跑（`annotate.js:80-87,230-251`）。
- 参考工具：Neuroglancer 和 webKnossos 用 WebGL 按 id 查调色板。
- 建议改法：先把解码和着色挪到 Web Worker，高亮层改成视口大小；远期改 WebGL。

**70. 多分辨率金字塔与分块存储；稀疏覆盖层代替整块写时复制；单进程单写者的并发上限**（工作量：大）
- 现在的体验：
  - 按整片读（`store.py:297-309`）。
  - 首笔复制整块，每块 839 MB（`store.py:281-294`）。
  - 单 worker（`scripts/serve.py:17`），SAM 全局一把锁（`sam.py:33`）。
- 参考工具：VAST 用 16³ cube 加 mip；webKnossos 的 32³ bucket 加稀疏 volume tracing；Neuroglancer precomputed。
- 建议改法：离线生成 2–3 级下采样并存成 zarr 分块；工作副本改成按片的覆盖层；只读图像层和写入层分开部署；SAM 独立成进程并按 (block, z) 缓存 embedding。

**71. 自托管 Neuroglancer；平板、触屏和数位板支持；审核员直接驳回某一笔**（工作量：中（各项））
- 现在的体验：
  - NG 依赖 appspot。
  - 画布只监听 mouse 事件，没有 `touch-action`（`annotate.js:858-917`）。
  - 对比页是只读的（`annotate.py:263-275`）。
- 参考工具：Neuroglancer 可以自托管；VAST 围绕 Wacom 数位板设计；PyChunkedGraph 可以按 operation_id 反向撤销。
- 建议改法：
  - 把 NG 构建产物放到 `/static/ng`，由 FastAPI 按 precomputed 格式提供当前标注。
  - 改用 Pointer Events，笔尾（button 5）按擦除处理。
  - 有了第 26 条的选择性撤销之后，在对比页的操作行上加「驳回这一笔」。

---

## 已经解决、需要在文档里更正的部分

**已经实现，文档写成「缺」或者写得不准：**
- **光标处的画笔圈**：已有（`annotate.js:292-296`），画笔用当前 id 的颜色，橡皮用红色，外加黑边；半径变化时实时更新。缺的只是涂抹中冻结和不显示数字，见第 23 条。
- **快捷键速查**：「?」帮助框已经有了（`annotate.html:142-191`），打开时拦截按键，关闭后把焦点还给画布。UX_COMPARISON.md:74「快捷键速查还没做」不准确；真正缺的是自动生成和按工具分组，见第 24 条。
- **状态行**：已经显示 z、坐标、光标下 id、缩放（`annotate.js:365-371`）。
- **工具光标和落笔预览**：每个工具都有自己的光标（`style.css:376-386`）；预览和服务端规则一致（`annotate.js:529-552`）；合并时第一块会钉住高亮。
- **非画笔类操作的结果提示**：修缮、合并、批量删除、SAM 都有（`annotate.js:448,512,646,1005`）。
- **P0-1 的核心部分**：右键擦、只擦当前 id、Ctrl/⌘+点击拾取、`contextmenu` preventDefault（`annotate.js:859,869,874`；`store.py:768-769`）。旧文档说「橡皮擦掉光标下一切 id」已不成立。
- **P0-2 的核心部分**：按住 Tab 拖动改半径（`annotate.js:867,894,940,950`）。
- **P0-4 的修复层**：修缮 R 已实现（`store.py:782-827`）。和旧文档建议的差别：没有预览；不是只在边界带内作用，而是按「离哪块内部最近」来归属；`reach` 是死参数。
- **撤销**：不限步数，存在磁盘上，刷新或重启后照样能撤（`store.py:502-524`），这一点比 TrakEM2、napari、webKnossos 都强。
- **审核员强制撤别人的改动**：带 `expect_n` 钉住目标记录，防止撤错；标注员带 force 会得到 403；审计里有 of、of_user、forced（`annotate.py:597-617`；`store.py:914-922`）。
- **全部撤销、每笔编号、含撤销的操作流水**：已有（`store.py:509,535,953-982`）。
- **P1-2 里「SAM / 批量删除直接落盘」的说法不对**：
  - SAM 已经是预览→token→应用，按片判断过期，可以取消（`sam.py:65-159`；`annotate.js:727-772`）。
  - 批量删除和全部撤销都有确认框，写明影响量（`annotate.js:168-202,985-1014`）。
  - 真正直接落盘的是修缮 R、Shift+F 整片、合并的第二下点击。
- **邻片取色 L**：继续可用（`neighbour.py`）。损坏切片插值算法已删除，关键片插值需另行设计。
- **Neuroglancer 跳转**：U 看这一点、看整块、新建 id 不会误选到无关细胞，都已完整（`neuroglancer.py:101-193`）。
- **本片标签列表、搜索、批量删除**：P1-5 的本片版已经有了（`annotate.js:954-1014`）。
- **「分离」场景**：按 N 新建标签再用 F 点那一块即可（`store.py:712-732` 只泛洪 4 连通块）。P1-6 只剩「沿膜切开两个粘连细胞」。
- **钉在体素上的审核意见和讨论串**：P2-1 里的这一子项已由对比页的标记实现（`marks.py:101-180`；`annotate_compare.js:574-679`）。
- **对比页 ±10 连播**：已经先 warm 再按缓冲播放（`store.py:376-403`；`annotate_compare.js:411-480`）。
- **P2-2 说的「单片读取慢」**：在交付的 F 序块上不成立，读一片只要几十毫秒。慢的是 C 序工作副本，见第 8 条。

**文档写成「已完成」、代码里其实没有的：**
- UX_COMPARISON.md:74 说 P0-1～P0-4 已实现，只列了三处差别。下面这些同样没做：
  - 覆盖模式单选（第 13 条）
  - 一笔 0 像素时的提示（第 11 条）
  - 「右键 = 平移」回退开关
  - Shift+滚轮改半径、预设、屏幕像素半径、按比例拖动（第 32 条）
  - Tab 与 window blur 复位（第 22 条）
  - 画笔和填充的膜约束（第 30 条）
- ANNOTATE.md:65 的「未使用」实际意思是「本片未出现」。
- `auth.py:161` 注释写试用模式「默认关」，实际配置是开（`config.py:70`）。
- `store.py:379-382` 的「Z fastest」只对 C 序工作副本成立，交付件是 F 序。
- `annotate_guide.html:21` 仍写着「右键拖动平移」，全文没有 A/Z、修缮 R、全部撤销、只擦当前颜色。
- 有一条审计把「空笔没有提示」标成了已解决，其实没修，已并入第 11 条。
