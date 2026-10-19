# 二级边栏与搜索页重构设计

日期：2026-10-18 · 分支：`polish/ui-ux` · 设计稿：`server/templates/design/sidebar-design.html`（变体 B「书房」已选定）

## 1. 背景与目标

用户对 Web UI 交互打磨提出四项（本 spec 覆盖前 3 项）：

1. **搜索跳转联动**：树上方搜索选中文件跳转后，文件树自动展开该文件所在目录并高亮可见（滚动到 active 行）。
2. **左侧重构为二级边栏**：图标 rail + 边栏页面（Files / Search），新增搜索边栏页，防止边栏功能膨胀。
3. **折叠收起所有文件夹**：文件树需要一个 collapse-all 控件。
4. ~~预览引用文本可点击跳转文件~~ → **不在本 spec 范围**，后续单独立项（需先定链接范围：wikilink / 相对 md 链接）。

用户定调原话：「这个重构主要为了解决搜索框展示信息不足的问题」。

### 关键事实（已核实）

- 搜索 API `POST /api/ai/search`（`server/ai.py::rag_search`）每个结果返回 `{path, score, segments: [{heading_path, kind, content, score}]}`。`content` 是片段原文，**数据早已返回，现有树内下拉只渲染 文件名 + heading 路径，丢弃了 content**——信息不足是纯前端问题，**后端零改动**。
- `score` 是**非归一化原始值**（TF-IDF 式累加整数，可能 17 或 42），无绝对意义 → 用相对值呈现（相关性条），不显示数字。
- 现有分层召回机制（strict → near → relaxed → fuzzy 自动补档）目前只在 agent 端可见。

## 2. 决策记录

| 决策点 | 结论 | 理由 |
|---|---|---|
| 变体选择 | **B「书房」**（三变体中） | 搜索信息量完成度最高（300px 宽 + 平铺富卡片 + 相关性条 + meta 行）；相关性条只需相对值，避免伪造 0.91 式归一化数字；双轨分工（树内=找文件、搜索页=看内容）比单一入口少一次高频点击；带标签 rail 自解释，防功能膨胀语义最清晰 |
| 变体 A（索引）落选 | 保留价值：改动面最小 | 260px 窄、score 数字需归一化处理、树内搜索被移除 |
| 变体 C（档案）落选 | 留作远期品牌探索 | 「无高亮 + 无 score」与「让信息被看见」目标直接对冲；墨色 rail 与暖纸体系冲突 |
| 树内搜索去留 | **保留现状**（名 + heading 两行下拉，位置不变） | 双轨分工：快速跳转留在手边；深阅读归搜索页 |
| 页面状态 | 纯内存（`activePanel` ref），不做 URL 深链 | 复杂度不匹配；vue-router 不引入 |
| 搜索页点击结果后 | openDoc + **切回文件页** + 树展开高亮 | 与项 1 统一心智：树永远反映当前文档位置 |
| 移动端（<1024px）布局 | **48px rail 常驻 + 文件面板左侧滑入 overlay（默认关闭）+ 搜索 80vh 底部抽屉**（2026-10-19 用户拍板，取代「flex 常驻面板」） | 常驻面板在手机上把主区压到 ~85px，不可用；overlay+抽屉保持 main 全宽 |

## 3. 设计规格（按选定稿 B）

### 3.1 图标栏（rail）

- 桌面宽 **64px**；按钮 52×44，图标 20px（stroke 1.5）+ 下方 10px 中文标签（设计稿 CSS 实为 10px），垂直 padding 12px，按钮组 gap 4px。
- 内容自上而下：文件（page icon）、搜索（magnifier）、分隔线、预留：最近（clock）、通知（bell）；底部（`margin-top:auto` + 分隔线）预留：设置（pointer-events:none，仅占位）。
- **logo 移出 rail**，移到主区 TopBar（mark + wordmark）。
- 三态：
  - inactive：`--ink-soft` 图标
  - hover：**黄铜 8% 底** + 墨色图标
  - active：**黄铜 10% 底 + 黄铜图标 + 左缘 3px 黄铜条**（条上下 9px 圆角）。选条不选实心：带标签 rail 信息量已大，实心块过重；与 `#preview h1` 黄铜下划线呼应。
- 图标从 `js/utils/icons.js` 现有集取用，缺的（clock/bell/gear）补入该集，不引第三方。

### 3.2 文件页

- 宽度保持现状 **260px**；内容 = 现 FileTree 原样（树内搜索框原位保留）。
- 面板头新增：**「文档」标题 + 文件总数 + 右侧 ①「收起全部文件夹」按钮**（低频操作，icon-only + tooltip）。
- 树本体（递归/拖拽/行内编辑/操作菜单）**不动**。

### 3.3 搜索页

- 面板宽 **260px**（与文件页统一，2026-10-19 用户拍板；原 300px 方案作废，文件↔搜索切换不再引起宽度变化与主区重排）。
- 输入框 + 清除钮；输入框下一行 **meta**：「N 个片段 · 当前档 strict」（分层召回档位显性化；「不足时自动补档」文案 2026-10-19 因 260px 宽度下换行删除，机制本身仍在）。
- **平铺不分组**：每个 segment 独立成卡。卡片字段：
  - 文件名 + 完整路径（同行，mono 10px，超出截断）
  - heading 做成 pill chip
  - content 片段：12px，3 行 clamp，**匹配词高亮**（`<mark>`，黄铜色系）；**首命中窗口化**（2026-10-19）：渲染前以首个命中为中心截取（前 30 字符 + 后 200 字符，两端 `…`），保证命中落在 clamp 前 ~2 行内可见；无字面命中（fuzzy/relaxed 档）退化为头部截取
  - ~~片段内联渲染（加粗/标题/列表标记）~~ → **后续单独立项**（2026-10-19 评估：全量 marked 对窗口切片不合适——半截块结构 + 块级排版破坏卡片高度模型；可行路径是单趟正则的内联级渲染，待展开）
  - 卡底 **3px 黄铜相关性条**，宽度 ∝ score（本次结果最高分 = 100%）；不显示数字，kind 字段不用
- 交互：
  - 防抖检索；分层预取（strict 不足自动补 near/relaxed/fuzzy，机制同现树内下拉）；新请求 abort 旧请求
  - 「展示更多」（分页/追加 top_k）
  - 加载态、空态（无结果 + 当前档位说明）
  - 键盘导航（↑↓ 选中、Enter 跳转）
  - 点击结果：openDoc(path, heading) → 切回文件页 → 树展开 + 高亮滚动（§4.1）
- 同文件多 segment = 相邻多卡（文件名片重复，命中即单元）。

### 3.4 页面切换与状态

- 过渡：**200ms 纯 fade**，无位移；`prefers-reduced-motion` 直切。
- 两面板 **v-show 常驻**：搜索词、滚动位置、树展开态、树内下拉态切走再回全部保留。
- 扩展点 = **panel registry**（app.js 内数组：id/图标/标签/宽度）；加页面 = 加一项 + 一个组件文件。rail 按钮由 registry 驱动渲染；face 在模板接线。

### 3.5 移动端（<1024px）

> **2026-10-19 拍板（用户决策）**：<1024px 全区间由「flex 常驻面板」改为 **48px rail 常驻 + 面板 overlay**（用户已拍板，替代原常驻方案——原方案在手机上把主区压到 ~85px，不可用）。

- **文件浏览**：48px rail（去文字标签，只留图标）常驻；文件面板 = **260px 左侧滑入 overlay**（`position:fixed; left:48px`，`translateX` 过渡，默认关闭，main 全宽；backdrop 点击关闭；选中文件后自动关闭；与搜索抽屉互斥）。
- **搜索**：80vh **底部抽屉**，完全复用现有 `#ai-panel` 移动抽屉 CSS（圆角 24、backdrop、拖拽手柄：拖拽改高 + 下滑关闭）；rail 搜索图标 = 抽屉 toggle。竖屏下卡片是内容，全屏阅读比侧栏合理。
- 现移动端 sidebar 切换钮（TopBar）随 rail 常驻而移除；AI 面板、冲突横幅等其余移动行为不变。

## 4. 行为规格

### 4.1 搜索跳转联动（项 1）

- FileTree 暴露两个方法（template ref 调用）：
  - `expandToPath(path)`：把 path 的所有祖先前缀加入 `expandedDirs`
  - `revealActive()`：nextTick 后 active 行 `scrollIntoView({block:'center'})`
- 触发点：
  - 树内下拉选中（现 `pickResult`）：保持现有 openDoc，追加展开 + 滚动
  - 搜索页结果点击：openDoc → 切文件页 → 展开 + 滚动
  - 常规树点击/路由变化也走同一方法（watch `currentPath`），行为幂等
- 纯 JS 行为，无 CSS 依赖。

### 4.2 收起全部（项 3）

- 文件页头部按钮 → `expandedDirs.clear()`（树顶目录保持展开由现有渲染逻辑决定；若需根目录级也可收起，目检时定）。
- 不持久化（expandedDirs 现状即内存态）。

### 4.3 搜索逻辑共享模块（防复制膨胀）

- 新建 `js/utils/search.js`：`createSearchController({input, topK, ...})` 封装防抖 / 分层预取 / abort / 展示更多 / 结果整形。
- 搜索页与树内下拉**共用**该控制器；树内下拉行为与重构前一致（回归点：分层预取、键盘导航、展示更多）。

## 5. 文件变更清单

| 文件 | 变更 |
|---|---|
| `server/templates/js/app.js` | 根模板 aside 重构（rail + 面板容器）；`activePanel` state；panel registry；logo 移出 |
| `server/templates/js/components/SearchPanel.js` | **新增**：搜索页组件 |
| `server/templates/js/utils/search.js` | **新增**：共享搜索控制器 |
| `server/templates/js/components/FileTree.js` | 暴露 `expandToPath`/`revealActive`；collapse-all 接线；树内搜索改走 utils/search.js（行为不变） |
| `server/templates/js/components/TopBar.js` | 加 logo mark + wordmark；移除移动端 sidebar 切换钮 |
| `server/templates/js/app.js` | **移动端搜索抽屉**（markup + 拖拽改高/下滑关闭逻辑，复用 #ai-panel 抽屉 CSS，spec §3.5）；文件面板 overlay backdrop + 状态（`filePanelOpen`） |
| `server/templates/js/utils/icons.js` | 补 clock/bell/gear 图标（如缺） |
| `server/templates/style.css` | rail/rbtn 三态、面板 fade、搜索卡片/相关性条/pill chip/meta 行、移动端 48px rail |
| `server/templates/design/sidebar-design.html` | 设计稿归档（已复制） |

**不改动**：后端全部、`js/utils/markdown.js`（项 4 的插入点留给后续）、编辑器/AI 面板行为。

## 6. 验收

1. **pytest 基线**：364 passed + 1 既有失败（`test_config.py::test_load_client_config`），除此之外全绿（后端零改动，预期无变化）。
2. **用户目检**（浏览器，逐项口头通过后才提交）：
   - 桌面：rail 三态 / logo 位置 / 文件页头（标题+计数+收起全部）/ 搜索页卡片（高亮、相关性条、meta 行）/ 切换 fade / 状态保持（搜索词、树展开）
   - 搜索跳转：树内下拉与搜索页两条路径，树均自动展开 + active 行可见
   - 收起全部：一键收起，再展开正常
   - 移动（DevTools <1024px）：48px rail 常驻 + 文件面板；搜索 = 80vh 底部抽屉开合
   - 回归：树内搜索行为（分层/键盘/展示更多）与重构前一致；AI 面板、冲突横幅、拖拽、行内编辑不受影响
3. 无构建步骤，刷新即生效；服务 `python -m server.main config.yaml`（:9888）。

## 7. 风险与边界

- 树内下拉重构到共享模块是主要回归风险 → 行为不变为硬约束，目检时对照。
- 移动端文件栏从「整栏 drawer」变「rail+面板常驻」是移动端布局的行为变更 → 目检单列一项。
- 二级页宽度统一 260px（2026-10-19 用户拍板），文件↔搜索切换无宽度变化；树内搜索与搜索页输入框共用同一套 `.sp-*` 样式。
- 不引任何新 CDN 依赖（无 vue-router/Pinia）。
