# 沉浸式壳重构设计（immersive-shell）

- 日期：2026-11-22
- 状态：设计定稿（§2 五项拍板），待用户评审 → 实施计划
- 范围定性：壳级架构重构（导航模型 + 栏位模型 + 操作路径）；同步/索引/agent/设置热生效等底层子系统行为零改动
- 法源与关联：首页 spec §10「沉浸式壳」演进位的兑现（`2026-11-22-home-butler-landing-design.md`，Home 零改动契约必须守住）；设置页 spec（`2026-11-22-settings-page-design.md`，modal 壳复用）；预览稿同目录 `2026-11-22-immersive-shell-preview.html`

## 0. TL;DR

桌面壳从「rail + 二级面板 + 记忆控制台 + 顶栏 + 文档 + AI 面板」收敛为：

**64px 图标 rail（唯一全局入口）＋ 全宽主区（Home/文档）＋ 右缘文档操作图标条 ＋ 按需左右槽位（栏数 = 1 + 开启槽数 ∈ {1,2,3}）＋ 其余子页一律 rail 悬浮抽屉。记忆控制台并入设置 modal。**

≤1023px 移动端本次零改动（TopBar 降级为移动专属壳，sheet/drawer 体系照旧）。

## 1. 背景与目标

**现状**（app.js 根模板实测）：rail(64px) + 二级面板(200px in-flow) / 记忆控制台(300px in-flow) / TopBar(48px) / 文档区 + 关联侧栏(280px) / AI 面板(tab 式) / 监控页整页接管。

**痛点**：① 固定面常占宽度——三面同开时文档列被压到极窄，沉浸感为零；② 文档操作散落三处（顶栏按钮、树「⋯」菜单、移动更多菜单），同一动作多入口互相漂移；③ 整页接管（监控）与 in-flow 面板（记忆）两种重占位模式交错，导航心智负担大。

**目标**：文档内容占据尽可能宽的屏；chrome「需要时可见、不需要时零占位」；全局入口收拢为 rail 一条，关闭出口统一为 Esc / 点击外部。

## 2. 拍板决策（用户 2026-11-22，五题）

| # | 决策 | 弃用备选 |
|---|---|---|
| 1 | **槽位自由组合**：左槽=文件树、右槽=AI⇄关联（互斥一槽一面）；栏数 = 1 + 开启槽数，自然形成单/双/三栏，不做显式栏数档位 | 固定「左树右AI」档位制；树也纯浮层 |
| 2 | **顶栏桌面端彻底删除**，职责分流（§5） | 极轻顶栏；底部状态栏 |
| 3 | **文档操作 = 右缘常驻图标条（RightDock）+ 直绑快捷键**（§5） | 命令面板为主；hover 才显形的细条 |
| 4 | **子页全部 rail 悬浮抽屉**（文件/搜索/回收站/监控）；通知铃铛进 rail；**记忆控制台并入设置 modal** | 监控进设置 modal；监控维持整页接管 |
| 5 | **桌面先行**：≥1024px 用新壳；≤1023px 维持现有 sheet/drawer 体系，TopBar 降级移动专属 | 两端一次到位 |

方案比选记录：渐进壳改造（采纳）vs 新建 Shell.js 壳组件重写（边界更干净但 diff/回归面大、与 Home 并行会话冲突强，弃，留演进位）vs 最小两步拆（中间态不沉浸、同模板碰两次，弃）。

## 3. 新壳结构

```
┌──┬──────────────────────────────────────────────┬────┐
│r│  左槽(文件树 260px, 可开可关, in-flow)          │dock│
│a│  ┌─────────────────────────────────────────┐  │ ~│
│i│  │  文档头行：路径 · 隐私徽标 · dirty 点      │  │40│
│l│  │                                         │  │px│
│64│  │        文档列（max-width 居中）           │  │  │ 右槽（AI ⇄ 关联,
│px│  │                                         │  │  │  in-flow, 可开可关）
│  │  └─────────────────────────────────────────┘  │  │
└──┴──────────────────────────────────────────────┴────┘
   浮层抽屉（文件/搜索/回收站/监控）= absolute 悬浮覆盖主区，transform 滑入，零挤压
```

- **rail（64px 常驻，唯一全局入口）**：顶部 logo「M · md-vault」——点击回首页，**必须同时 `history.replaceState` 清 hash**（首页 spec B1 手法原样迁移，回归钉随迁至 rail 版）；按钮依次 = 文件、搜索、回收站（数量徽标，现状口径）、监控、通知铃铛（未读徽标，现状口径）；底部锚定齿轮 = 设置（现状）+ **记忆徽标迁入**（badgeParts 需人参与总和，title 分解构成，口径同原记忆钮）。原 rail 记忆钮退役。
- **主区**：无当前文档 → Home（首页契约「只占据主区」继续成立，Home 组件零改动）；有文档 → 文档区。
- **文档头行**：文档列顶部一行内联弱色小字——面包屑路径 + 隐私徽标（现状 display-only 口径原样）+ dirty 点。无背景无边框，视觉上不成立「第二条顶栏」。
- **RightDock**：右缘常驻图标条，见 §5。
- **槽位**：左槽 = 文件树（宽 ~260px，现 200px 面板加宽以适配无挤压语境）；右槽 = AI 面板（现 `#ai-panel` 宽）⇄ 关联侧栏（现 280px）二选一互斥。槽位是 in-flow 的（开槽挤压主区——这是「占栏」的定义形态，与浮层抽屉互补共存）。AI 开关入口保留现 `#ai-btn` 边缘 tab；关联入口移入 RightDock（§5）。

## 4. 浮层抽屉（rail 子页的统一容器）

**容器纪律**：absolute 定位于主区 relative 容器内、左侧滑入；过渡只走 `transform: translateX` + opacity（GPU、零回流）；桌面态**无 backdrop**（点击外部关闭）；同屏至多一个抽屉（开新关旧）。

| 抽屉 | 宽（建议值） | 关闭路径 | 特殊 |
|---|---|---|---|
| 文件 | 320px | 选文档 / Esc / rail 钮再点 / 点外 | 头部图钉钮 → 钉住升级为**左槽常驻**（rail 文件钮 on 态 = 钉住）；取消钉住即关 |
| 搜索 | 380px | 选中结果（openDoc）/ Esc / rail 再点 / 点外 | 与移动端既有 `#search-drawer` 断点隔离，不共享实例 |
| 回收站 | 360px | Esc / rail 再点 / 点外 | 行内还原/清空操作**不自动关**（操作流连续性优先） |
| 监控 | min(920px, calc(100vw − 96px)) | Esc / rail 再点 / **openDoc 一律关**（替代现「openDoc 退监控页」守卫） | MonitorPage 组件本体零改动，仅换挂载容器 |

**全局规则**：① `openDoc` 一律关闭文件/搜索/监控抽屉（回收站不在 openDoc 路径上，不受影响）；② Esc 链优先级：设置 modal > 抽屉 > dock 浮层（目录等）> 其余下拉——现链延伸，不改既有设置 modal 优先级；③ 状态收拢：新增 `drawer: null | 'file' | 'search' | 'trash' | 'monitor'`，桌面端取代 `state.monitoring` / `state.filePanelOpen` 的桌面语义（移动端 flag 路径保留，见 §6/§10）。

## 5. 顶栏拆除与 RightDock

**TopBar 职责分流表**：

| 原顶栏元素 | 新落点 |
|---|---|
| logo + 回首页 | rail 顶部（§3，清 hash 手法随迁） |
| 通知铃铛 + 下拉 | rail 铃铛钮，下拉锚 rail 右侧展开（复用现下拉 DOM/逻辑，只换锚点） |
| 当前路径 + 隐私徽标 | 文档头行（§3） |
| 编辑⇄预览、保存、下载、目录 | RightDock（目录钮开 `.toc-dd`，锚点改 dock 左缘） |
| 关联 | RightDock 钮（开右槽关联面；右槽被 AI 占据时 = 切面） |
| 「更多」菜单（移动） | 保留——TopBar 整体降级为 `lg:hidden` 移动专属壳 |

**RightDock（新组件 `RightDock.js`，~40px，仅 ≥1024px 渲染）**：
- 钮自上而下：编辑⇄预览（激活态=编辑）· 保存（dirty → 黄铜点）· 下载 · 目录 · 关联 · 重命名 · 移动 · 删除（告警色，两步确认）。
- 禁用规则：无文档 / docMissing → 全钮禁用；**按钮恒定渲染零跳变**（现顶栏 D5 口径平移）。
- 处理器复用：mode/save/download/toc/关联 = app.js 现有 handlers 直挂；rename/move/delete = FileTree「⋯」菜单同款（实施注记：处理器上提 app 层或抽 util，形态由实施期按 FileTree 现状定，**不得**在 dock 内另起一份实现——单一写面）。
- **快捷键（本次只两条 + F2）**：`Ctrl+S` 保存；`Ctrl+E` 预览⇄编辑（Chromium 若占键 → 退 `Ctrl+Shift+E`，实施期实测后本表为唯一真源更新）；`F2` 重命名（文档区焦点时）。无命令面板（YAGNI，§12 演进位）。
- 无文档时 dock 整条隐藏（Home 态零 chrome）。

## 6. 记忆控制台 → 设置 modal

- SettingsPage 增加标签页「记忆」：**MemoryConsole 组件本体零改动**，队列/详情/报告三面整体迁入 modal 内容区（宽度自适应 modal 容器）。
- 入口：rail 齿轮 → modal → 记忆标签；新增深链 `#memory` = modal + 记忆标签（原 rail 记忆钮退役；`#settings` 语义不变）。报告深链（通知 report_paths → `memoryReport`）改为落记忆标签的报告面。
- 徽标：记忆 badge 迁 rail 齿轮（口径 §3）。
- 守卫边界：设置 dirty 守卫只管 config 表单 dirty；提案裁决/报告浏览**不进** dirty；`settingsCloseSeq` 关闭链零改动。设置热生效全链（PUT 预检 / agent_cache 代际 / supervisor apply 契约）零接触。
- 状态：`memoryOpen` 与 `console-shell` 退役；`memoryDetail`/`memoryReport` 归记忆标签上下文；`pollMemory` 轮询保留（徽标数据源）。

## 7. 布局与动效纪律（对既有坑红线的显式映射）

- 抽屉过渡只走 `transform`，**绝不动 width/flex-basis**（AGENTS 坑清单）；槽位开合是一次性宽度变化（非逐帧动画），可接受——文档列 max-width 居中于剩余空间，无逐帧居中需求；「absolute 面板不参与 flex 布局」坑对抽屉是**特性不是缺陷**（零挤压即设计目标）。
- 隐藏容器 `clientHeight === 0` 坑：FileTree 在抽屉内的滚动定位（revealActive/expandToPath）沿用既有 pending-flag + ResizeObserver 纪律，实施期核对既有先例可达。
- z-index 层序（自下而上）：dock(10) < 抽屉(30) < 通知下拉/目录浮层(50) < 设置 modal（既有层）。
- `prefers-reduced-motion`：抽屉/槽位全部降级直切或纯 opacity，沿既有规范。
- 键盘/视口红线：`focus({preventScroll:true})` 口径不因新浮层破例；壳高仍走 `html,body,#app{height:100%}` 链（回归钉 `test_shell_uses_layout_viewport_height` 不破）。

## 8. Home 契约与并行会话协调

- Home 契约：主区存在性保留，Home 组件零改动（首页 spec「壳怎么变 Home 不动」兑现）。
- logo 回首页 + 清 hash 的行为回归钉从顶栏版换锚到 rail 版，语义逐字保留（首页 spec B1）。
- **排期红线**：本 spec 只定稿；实施必须等 Home 实施会话（正在按首页 spec 施工）**合入后**启动——两件事都碰 app.js 根模板与 style.css，双开必冲突。

## 9. 安全红线对照（2026-11-18 八条）

零新暴露面、零新鉴权面（红线 1/2）；身份仍由通道注入，dock/抽屉/记忆标签全走既有 API，`actor` 语义不变（红线 3）；隐私徽标与隐私目录读面口径原样（红线 5 现状条款）；设置热生效链零接触。

## 10. 测试

- **新增审计钉家族**（test_web_ui_audit）：TopBar 组件壳含 `lg:hidden`（桌面隐身）；RightDock 注册与渲染条件（≥lg 且有文档）；抽屉容器 class 纪律（absolute + transform，禁 in-flow 面板复活）；根模板无 `#memory-console-shell`；监控挂载位 = 抽屉容器；rail logo 清 hash 钉（自首页 spec 换锚）；齿轮徽标 = badgeParts 总和钉；快捷键绑定存在钉（Ctrl+S/Ctrl+E/F2）。
- **换锚/退役**：`topbar-compact`（AI 展开联动紧凑态）等顶栏专属钉换锚 dock/抽屉语义或退役；模式切换钮钉换 dock；`test_rename_row_click_guard` 等 FileTree 内钉不受影响。
- **回归面**：`test_settings_routes/hotapply/doc_io` 全绿（后端零改动）；`test_shell_uses_layout_viewport_height`、`test_keyboard_focus_preventscroll` 保绿；`test_routes_order` 不动（本次后端零新端点，`#memory` 为纯前端深链）。
- **移动端**：≤1023px 行为逐条不变——现有移动钉家族全绿即验收线。
- UI 规则 3：**每波浏览器真机目检 + 用户明确认可后才 commit**，「测试绿」≠「UI 对」。

## 11. 迁移波次（波界即 commit 界，严格串行；每波独立可目检）

| 波 | 内容 | 触碰 |
|---|---|---|
| W1 抽屉化 | 文件/搜索/回收站/监控 → rail 悬浮抽屉 + Esc 链延伸；TopBar 暂保留（纯壳改动，行为等价） | app.js 模板、style.css |
| W2 顶栏拆除 | TopBar → `lg:hidden`；文档头行；RightDock + 快捷键；铃铛/logo 进 rail | 同上 + RightDock.js（新） |
| W3 记忆合并 | console-shell → SettingsPage 记忆标签；齿轮徽标；`#memory` | SettingsPage.js、app.js |
| W4 清尾 | 死样式/死 flag（桌面路径的 `monitoring`/`filePanelOpen` 语义迁移完成）、审计钉家族整体翻新、AGENTS.md Gotchas 沉淀 | 全局 |

## 12. 演进位（不做，但承重）

- **命令面板**（Ctrl+K 跳转+操作统一）：用户初想「编辑快捷键」若演进到此形态，dock 保留、面板叠加，不互斥。
- **app.js 拆 Shell.js**：方案 B 余量。W1–W2 后若根模板 >600 行再评。
- 移动端统一新壳模型；dock 入口可配置；CJK 衬线（从首页 spec §10 联动）。

## 13. 实施注记（不确定点留给计划期）

- `Ctrl+E` Chromium 占键情况实测；退路 `Ctrl+Shift+E`，落定后 §5 表为唯一真源。
- rename/move/delete 处理器上提形态（app 层 vs util）按 FileTree 现状定。
- 监控抽屉内 `mwrap` 网格在 1024–1280px 屏的单列断点需按抽屉实际宽度重验（原按视口断）。
- 搜索桌面抽屉与移动 `#search-drawer` 断点隔离、不共享实例——钉住防回归。
- 文件抽屉「钉住 ⇄ 左槽」升级时列表 DOM 迁移方式（同实例 reparent vs 双实例）实施期定，reparent 优先（避免滚动位置/展开态双份状态）。
