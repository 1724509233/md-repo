# 「关联」子页交互重设计（rel-face-redesign）

- 日期：2026-11-23
- 状态：**设计定稿，待用户评审 → 实现车道执行**（本轮不写实现）
- 范围定性：`DocRelated` 内容件的条目版式与列表结构重设计；三态**容器**（桌面浮层 / 平板侧栏 / 移动 sheet）与其开合机制**零改动**
- 法源与关联：沉浸式壳 spec（`2026-11-22-immersive-shell-design.md` §7 关联浮层块）；现实现 = `server/templates/js/components/DocRelated.js` + `style.css`「文档面」（L1392–1440）与「关联浮层桌面化」（L2263–2292）两块
- 勘察备忘：`.doc-row` 一族类**现仅 DocRelated 消费**（桌面浮层 + 移动 sheet 双实例同源），无其它页面连带

## 0. TL;DR

三块折叠列表（Wikilink / AI 建议 / Tag）收敛为**两段**：用户层（图谱出链 + tag 中介召回）**合并为一条不分组列表**，条目主行从整路径改为「**文件名**（去 .md）＋ 次行所在目录」两行式；tag 条目在名行尾挂**黄铜弱描边 `#tag` chip** 自明来源；「AI 建议」保留独立折叠段（黄铜机器层语义不动，仅同步名行口径）。排序维持后端 score 降序现状，不重新发明。

## 1. 拍板决策表

| # | 决策 | 理由 | 弃用备选 |
|---|---|---|---|
| R1 | **用户层合并为单一列表**（wikilink/related/tag 混排，不再分块折叠） | 分块的「Tag 默认收起」让弱关联永久隐形；「Wikilink」块题本身是实现 meta（违文案红线）；280px 浮层里两层折叠头吃掉 40px+ 首屏 | 保留三块只改行版式（改动最小但折叠税与隐形 tag 原样保留） |
| R2 | **主行 = basename 去 `.md`** | 与文件树、回收站既有口径同源（TrashPanel `fileName()` 先例直译）；240px 内宽下整路径必然截到失去辨识度，文件名才是判断主体 | 整路径主行（现状）；名+去后缀目录拼接一行 |
| R3 | **次行 = 所在目录**（相对 vault，不带文件名、无尾斜杠；**根目录文档显示「根目录」三字**） | 目录是「打开哪一个」的判断信息；根目录渲染占位文字而非隐藏次行 = 行高恒定、扫读节奏不乱 | 根目录隐藏次行（行高参差）；显示「/」（实现味） |
| R4 | **同名消歧 = 双通道**：次行目录恒定在位是第一通道；当**候选列表内 basename 撞名**时，撞名各行的次行切为「尾部优先」截断（保尾段、前加 `…/`） | 区分同名文件的关键信息恰是目录**尾部**（近名分支），纯 CSS 省略号只能保头部（vault 顶层区）；非撞名行维持头部省略号不动 | 主行加粗/加路径前缀（推翻主行=纯文件名约束）；hover 弹全路径（移动不可达） |
| R5 | **tag 条目 = 名行尾 chip**，形态 = 黄铜弱描边 pill `#tag名`（详见 §2.2） | chip 在名行尾 = 它修饰「这条关系是什么」；次行保持纯位置信息不被稀释；比整行底色轻，不与机器层的行级黄铜冲突 | 次行尾挂（与目录打架）；行底色加深（与 .mach 行底混淆） |
| R6 | **AI 建议段保留**（折叠、黄铜行轨、依据行、忽略钮全部不动），名行同步改「文件名」口径；**AI 行不加目录次行** | 用户拍板只列了两类来源，但忽略裁决面是既有面不能凭空蒸发（对照原则）；机器行三行式（名/目录/依据）在 280px 里过重，**依据行优先于目录行**（它是裁决输入）；目录进 title | 用户层合并顺手把 AI 层也合进去（抹掉机器/用户信任层分界） |
| R7 | **排序维持后端现状**：`score` 降序 → 路径升序（`graph_get_related` 即出此序，前端不做二次排序） | 分数天然让显式链接在前、tag 中介（0.5 分）沉底——「分组」想要的秩序，排序已给足，chip 负责归因，不需要视觉分组再给一遍 | 前端按 type 再分组（= R1 的换皮） |

## 2. 条目行版式（三态同一内容件，此节即唯一行规格）

内宽推导：280px 浮层 − `.c-sec` 左右内距 24px − 行内距 16px = **约 240px** 可用文字宽（移动 sheet 更宽，规格不变、只是余量更大）。

### 2.1 行解剖（用户层两行式）

```
┌──────────────────────────────────────────┐
│ 沉浸式壳重构设计                 #rel-face │ ← 名行：文件名 + 可选 tag chip
│ docs/superpowers/specs                     │ ← 次行：目录（mono 弱灰，省略号）
└──────────────────────────────────────────┘
```

| 元素 | 规格（token 全走 :root，零新色） |
|---|---|
| 行容器 `.doc-row`（复用现基类） | `padding: 7px 8px; margin-bottom: 6px; border-radius: 7px;` 行高由内容撑起（两行 ≈ 47px，满足移动 ≥44px 命中） |
| 名行 `.doc-row-name`（新） | `--sans` **12.5px / 500 / var(--ink)**；`line-height: 1.4`；`flex: 1; min-width: 0`；省略号截尾 |
| 名行 gap | 名 ↔ chip `gap: 6px`（`.doc-row-line` 现 flex 容器直用） |
| 次行 `.doc-row-dir`（新） | `--mono` **10px**；`color: color-mix(in srgb, var(--ink-soft) 78%, transparent)`；`line-height: 1.4; margin-top: 2px`；省略号截尾；根目录 = 「根目录」三字 |
| 整行 `title` | 全路径；tag 条目追加为 `「全路径 · 共享标签：tag名」`（tooltip 是解释的合法去处） |

### 2.2 tag chip `.doc-tag`（新）

| 属性 | 值 |
|---|---|
| 字体 | `--mono` **9.5px**，文本 = `#` + tag 名 |
| 色 | 字 `var(--brass)`；描边 `1px solid color-mix(in srgb, var(--brass) 32%, var(--line))`；底 `color-mix(in srgb, var(--brass) 6%, transparent)` |
| 形 | 高 16px；`padding: 0 6px; border-radius: 999px`；`flex-shrink: 0; max-width: 96px`，chip 文本自身省略号 |
| 家族关系 | 与 `.cnt.hot`（12%/35% 黄铜）同族但**再弱一档**（6%/32%）——黄铜读作「关联强调」，而**行级黄铜轨 + 4% 底仍是机器层专属**，不互撞 |

### 2.3 状态反馈

| 态 | 规格 |
|---|---|
| hover | 现 `.doc-row:hover` 墨 4% 不变 |
| active（按压） | 新增 `:active` 墨 6%（一次性按压确认感，`.15s ease` 同族） |
| focus-visible | 浮层/sheet 滚动面内走**内描环**：把 `.rel-scroll` 补进 style.css L83 的 inset 环集合（浮层 overflow:hidden 会裁外描环，正是 L76–85 注释预留的补挂位） |
| 当前文档自身 | 后端 D18 已排除自环，无需前端处理 |

## 3. 列表结构与头部

- **头部 `.p-head`**：图标 + 「关联」 + **新计数徽标 `.pcount`**（复用 `.c-sec-h .cnt` 画法：mono 10px pill）= 用户层候选总数；**为 0 时徽标不渲染**（AI 段自带计数，头徽标再挂 0 是噪音）。关闭钮不动。
- **`.doc-path-line` 源文档路径行**：不动（它是「面」的锚，不是条目）。
- **用户层**：无块题无折叠，直接列表（R1）。块容器沿用 `.c-sec` 但去块题。
- **AI 建议段**：块题（图标 + 计数 `.cnt.hot` + 折叠）保留；行结构 = 名行（文件名 + 右侧「忽略」钮，现 `.doc-ignore` 规格不动）+ 依据行（现 `.doc-basis` 不动）；`fold.ai` 默认展开保留；`fold.wikilink`/`fold.tag` 随合并退役。
- **空块纪律**：用户层空 → 空态行；AI 段空/排干 → 整段不渲染（现口径不动）。

## 4. 文案口径（对照现有件，只设计存在的面）

| 面 | 现文案 | 新文案 | 说明 |
|---|---|---|---|
| 空态（用户层空） | 「本文档没有出链」 | **「暂无关联文档」** + 次行小字 **「用 [[链接]] 或 tag 建立关联，这里会自动出现」** | 「出链」是图谱黑话；次行是行动指引（红线允许：用户可执行的建立方式） |
| 加载态 | 「读取中…」 | 不动 | — |
| 死文档态 | 「该文档已不存在 / 可能已被删除或重命名」 | 不动 | — |
| 忽略失败条 | 服务端文案直显 | 不动 | — |
| 块题「Wikilink」「Tag」 | — | **退役**（R1） | 「Wikilink」= 语法实现 meta；tag 归因由 chip 承担 |
| 根目录次行 | — | 「根目录」 | 位置信息，非 meta |

## 5. 交互

- **点击 = 打开文档**（`open-doc`，现状不动）；AI 段行点击不开文档（`cursor: default` 现状，裁决面只认「忽略」钮）。
- **hover 增量 = 克制为零**（拍板）：不做悬停预览（需额外取文档体 + 浮层上叠浮层，与「点一下就开」的路径重复）、不做复制路径钮（全路径在 title）。理由：280px 浮层的每个像素都在为「扫读候选」服务，操作密度已经够低。
- **键盘（增强项，实现可拆后）**：`↑ ↓ Home End` 在面内可聚焦行（用户层行 + AI 块题 + 忽略钮）间轮换焦点，Enter 原生触发——项目先例 = FileTree 搜索 combobox（`aria-activedescendant`）与 `initMenuNav`（`utils/menuNav.js`），本面是纯 button 列表，用 roving focus 即可，无需 combobox 重契约。Tab 直达现状本已可用。Esc 关面在 app 层，不动。

## 6. 三态适配

**行规格三态完全同构**（内容件只有一份），差异全在容器层且本次零改动：

| 态 | 容器（现状保持） | 行侧差异 |
|---|---|---|
| 桌面 ≥1024 | fixed 浮层 280×min(520, 100vh−32)，top16，z-50，双层软影，与 toc 浮层互斥 | 无（240px 内宽即 §2 推导基准） |
| 平板 861–1023 | in-flow 280px 侧栏 | 无（同 240px 内宽） |
| 移动 ≤860 | 底部 sheet + 拖拽把手 | 无新增；两行式行高 ≈47px 天然 ≥44px 触控命中（现状单行 31px 反而是靠点击整行救的，重设计顺带治好） |

滚动：`.rel-scroll` 现 `overscroll-behavior: contain` 在位；合并列表后长列表更常出现，滚动区行为不变。

## 7. 数据契约与后端字段假设

现契约 `GET /api/memory/doc/{path}` → `links: [{path, type, score}]`（type ∈ wikilink/related/tag，排序即 score 降序）。

**需后端补（探底并行中）**：tag 中介条目的共享标签名——`graph_get_related` 的 tag 枢纽遍历处（`graph_sql.py` L268–287，hub_rows 查询处顺手 `SELECT src.name, dst.name` 即可携出）。

- 建议字段：**`tag: string | null`**（首个共享 tag，单值最贴「展示 tag 名」诉求）；
- 前端读取按 **双字段兼容**：`tags?.[0] ?? tag ?? null`——后端若最终交付 `tags: string[]` 也零前端改动；
- **字段缺席期降级 = 不渲染 chip**（行仍合法、排序已让 tag 沉底，无报错无占位）；chip 名过长 96px 省略号，全名在 title。

## 8. 示意（ASCII）

```
┌ 关联  (5)                          ✕ ┐   ← 头 + 新 pcount 徽标
│ notes/immersive-shell-design.md       │   ← doc-path-line（不动）
│ ────────────────────────────────────  │
│  沉浸式壳重构设计          #rel-face  │   ← 用户层：名 + chip（tag 中介）
│  docs/superpowers/specs               │
│  Home 管家落地设计                     │   ← wikilink 直连：无 chip
│  docs/superpowers                     │
│  …                                    │
│  纸墨书房视觉基线                      │   ← 同名撞车例：次行切尾部省略
│  …/projects/vault-notes              │     （非撞名行仍是头部省略号）
│ ▸ AI 建议 (2)                         │   ← 折叠块题保留
│  ┃ 同步泳道基线            [忽略]     │   ← 机器行：黄铜轨 + 名 + 依据行
│  ┃ 依据：两文都谈 lanes 与 baseline…  │
└───────────────────────────────────────┘
```

## 9. 实现清单（交实现车道，本稿未动任何文件）

| 文件 | 段落 | 改动 |
|---|---|---|
| `server/templates/js/components/DocRelated.js` | template 块1/块3 + `wikiLinks`/`tagLinks`/`noOutLinks` computed + `fold` 初值 | 合并为用户层列表：直接 `v-for="l in data.links"`（后端序即终序），行模板 = `.doc-row-line`(`.doc-row-name` + 条件 `.doc-tag`) + `.doc-row-dir`；`fold` 只剩 `{ ai: true }`；新增纯函数 `fileName()`（TrashPanel 口径）、`dirOf()`（可 import `utils/vaultLinks.js` 既有 `dirOf`）、撞名判定 + 尾部截断 helper |
| 同上 | p-head 与空态、头注 | 头加 `.pcount`；空态双行新文案；组件头注同步「两段/两行式」口径（该文件头注即行为法源，改代码必改注） |
| `server/templates/style.css` | 「文档面」块（L1392 起）**追加** | 新类 `.doc-row-name` / `.doc-row-dir` / `.doc-tag` / `.pcount` + `.doc-row:active`；全部走既有 token |
| 同上 | L83 inset 环集合 | `.rel-scroll` 补挂进 `:focus-visible` inset 规则（注释预留位兑现） |
| 容器面（app.js / 浮层化块 L2263–2292 / sheet 系） | — | **零改动**（红线：三态容器与开合/互斥/豁免链不动） |
| 后端（并行探底） | `server/vault/db/graph_sql.py` `graph_get_related` tag 枢纽处 | 行携出共享 tag：`tag`（建议）或 `tags[]`；route 透传零改动 |
| 回归钉建议 | `tests/` | ① DocRelated 模板不再含 `Wikilink`/`Tag` 块题与 `fold.tag`；② 撞名次行尾部省略、非撞名不切；③ `tag` 字段缺席不渲染 chip 且无异常；④ 既有 test_immersive_shell 家族全绿（容器零改动验证）；⑤ 前端 JS 无 Python 串 API（既有钉自动覆盖） |
