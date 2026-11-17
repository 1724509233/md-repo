# 预览块落位触发重构：桌面双击+✎ / 移动两段轻触

日期：2026-11-17 · 状态：draft（待用户审阅）
分支：`fix/web-ui-p1` · 来源：待办 13ac9600「预览点击渲染块容易误触跳转编辑」
**取代**：vault `1-项目/md-repo/specs/2026-11-09-select-to-source-editing.md` 的手势条款（§2.2 三种手势、§9.3 mouseup 委托）。vault 原文按写入纪律不回改，冲突以本文为准；映射层（§3 不变量）、落位原语（§4/§5、`landOn`/`landCell`）、静默失败（§2.3）全部继承不变。

## 1 背景与目标

现状：预览内**单击**渲染块（空选区 mouseup）立即切源码模式并选中对应行区间。单击是无差别高频手势（读时点看、起意选词都触发），误触进编辑 + 视图突迁是主要摩擦源（待办原文）。

目标：

1. 编辑意图门槛显式化：桌面 = 双击或点 ✎；移动 = 两段轻触（选中块 → 点 ✎）。
2. 移动端**归还原生长按手势**（Android 长按选词放大镜/手柄完整可用；iOS 双击选词不受扰）。
3. 桌面双击隐蔽性问题用 hover ✎ 软提示兜底，正文零常驻元素。

非目标：映射层（`data-src-line` 写读）、落位原语与滚动定位、框选跨块语义、fm 折叠/活链/复制钮等行为全部不动；不做 onboarding/tooltip。

已拍板记录：框选落位保留（拖选=明确编辑意图且是跨块预览唯一入口）；「长按专属落位牺牲原生选词」被否——预览选词复制（读侧、渲染后纯文本）比块级落位（编辑加速）更高频，不做该交换。

## 2 交互契约（分端手势矩阵）

**分流依据**：Pointer Events `pointerType`（`'touch'` vs `'mouse'`），不按视口宽度或 UA 猜端型（触屏笔记本/外接鼠标各按实际输入走）。

### 2.1 桌面（mouse）

| 手势 | 行为 |
|---|---|
| hover 映射块 | 瞬时预览：块 outline 淡变 + ✎ 淡入（150ms）；块间空隙不出现 |
| 单击块（空选区） | **sticky 选中块**（outline + ✎ 常驻至取消条件）——不再切模式，误触之害即此消除 |
| 点 ✎ | 落位（块级；起点在顶层表格 cell = 格级） |
| 双击块 | 直接落位该块（熟手快车道，不经 ✎） |
| 双击表格 cell（有文字） | 词选非空 → 走框选路径落**整表行区间**（与今日行为一致，规格如实） |
| 双击空 cell / 图片等非文字区 | dblclick 兜底：空 cell 零-width 落格 / 该块落位 |
| 拖选释放（非空选区） | 框选跨块落位，**不变** |
| 活链 / 外链 / `.code-copy-btn` / `[data-fm-toggle]` / task checkbox / **#block-edit-btn 自身** | 豁免，不进选中态，各自行为不变 |

### 2.2 移动（touch）

| 手势 | 行为 |
|---|---|
| 轻触块（短时无位移） | 选中块：outline + ✎；点其他块 = 换选中 |
| 点 ✎ | 落位（块级 / cell 格级） |
| 轻触空白（非豁免区） | 取消选中、收起 ✎ |
| 长按 | **完全归还原生**（选词放大镜、复制菜单）；本特性零干预 |
| 双击 | 归还原生（iOS 选词）；不拦截 |
| 滚动 | 不取消选中（✎ 在内容流内随文档滚动，见 §3） |
| 豁免区点击 | 同桌面 |

### 2.3 统一不变式

- 操作意图优先于编辑意图（豁免清单双端一致）。
- 无映射块 / 对齐失败 → 静默（继承原 §2.3）。
- 取消选中条件（唯一四条）：点空白或换块、重渲染、切文档、preview↔edit 模式切换。**滚动不是取消条件**。
- 双端 ✎ 行为完全一致（点了就落位），差异仅召唤方式：桌面 hover/单击，移动轻触。

## 3 组件与状态流（均在 `EditorPane`，方案 A 组件内重构）

### 3.1 状态

- `landingSel = ref(null)`：`{ el, isCell }`——sticky 选中态。
- `hoverSel`：桌面瞬时预览态（不进 sticky；`el` 足够）。
- 单一浮动按钮 `#block-edit-btn`：`<button>` 渲染在 `previewEl` 内部顶层（`previewEl` 置 `position: relative`），`v-show=landingSel`，样式定位承 `.code-copy-btn` 血统。绝对定位于内容流 ⇒ **随文档滚动零监听**；`v-html` 重渲染天然抹掉它，与「重渲染清选中」自洽。

### 3.2 事件流

- **`onPreviewPointerUp`（新，双端统一委托，替代旧 `onPreviewMouseUp`）**：
  - 入口统一做豁免判定（§2.1 豁免清单）。
  - `pointerType === 'touch'`：tap 判定（`pointerdown` 起 ≤350ms 且位移 ≤10px，`pointercancel` 即弃）→ 命中映射块 → `landingSel = {el, isCell}`；空白 → 清。**非 tap（长按/滚动）→ 一律 no-op**，交还原生。同时置 `lastTouchAt = now`。
  - `pointerType === 'mouse'`：非空选区 → 原框选跨块映射逻辑（原样迁移，含格内拖选不截获的 `isCollapsed` 守卫）；空选区 → sticky 选中（替代旧落位）。
  - 触摸后的**合成鼠标事件**（mouseup/click/dblclick）：`now - lastTouchAt < 800ms` 一律忽略（单一旗标，双触发防护）。
- **双击的双路合流（桌面）**：双击的第二次 mouseup 通常已携带词选非空选区 → 先经框选路径落位（块=整块行区间、文字 cell=整表），`dblclick` handler 兜住**无选区**场景（空 cell / 图片 / 空白映射块）；两路结果同源（同块行区间），`landOn` 幂等不构成双触发。
- **`onPreviewDblClick`（新，仅桌面语义）**：`lastTouchAt` 旗标先行（屏蔽 iOS 双击选词的 dblclick）→ 顶层 cell 且选区空 → `landCell`，否则块级 `landOn`；成功即清 `landingSel`。
- **✎ click**：按 `landingSel.el`（td 起点则按 cell）调 `landCell`/`landOn` → 清选中。
- **hover 对（桌面）**：`mouseover` 命中映射块 → `hoverSel` 亮；`mouseout` 延迟 ~120ms 收（指针进入 ✎ 本体则取消收起）。hover 不产生 sticky；**sticky 优先**——已有 `landingSel` 时 hover 不移动 ✎、不改选中。
- **清理 watch**：`renderedHtml` 与 `props.mode` → 清 `landingSel`/`hoverSel`（重渲染后 el 引用必失效，先于任何 land 调用生效）。

### 3.3 ✎ 定位

块右上角：`top = el.offsetTop − h − 4`、`right = previewEl.clientWidth − el.offsetLeft − el.offsetWidth + 4` 量级（实现允许微调）；`el.offsetTop` 相对 `previewEl`（已 `position:relative`）。代码块与 `.code-copy-btn` 并排、落其左侧不叠。触摸命中区 ≥32px。

## 4 CSS（`style.css`）

- `.md-sel-block`：outline + `outline-offset`（或等效 inset box-shadow），**不得引起布局位移**；用既有 token（`--brass`/`--line`/`--paper` 系）。
- `#block-edit-btn`：小方按钮（图标铅笔），`opacity` 150ms 过渡，`prefers-reduced-motion: reduce` 下去动效（沿用既有 `reducedMotion` 模式）。

## 5 边界与故障

| 情形 | 处置 |
|---|---|
| 重渲染后点 ✎（el 失效竞态） | watch 必先行清理；land 前再判 `el.isConnected`，false 即静默 |
| 外部写入后源行变化 | land 时现读 `el.dataset.srcLine`；`landOn` 既有「切模式后重取源」行为覆盖 |
| 嵌套表格 / 无映射块 | 静默（不变） |
| 不支持 Pointer Events 的引擎 | **不做回退**（Vue 3 下限内全支持；极端下 ✎ 不出现、双击仍可用） |
| 同块重复轻触 | 维持选中，不闪烁重绘 |
| 键盘/视口 | 本特性不聚焦输入、不弹键盘，与键盘视口族 bug 无交集；✎ 在 overflow 容器内为纯布局跟随 |

## 6 测试

**pytest 静态钉（`tests/test_ui.py` 更新 + `test_web_ui_audit.py` 新增）**

1. 旧 `onPreviewMouseUp` 空选区落位路径消失（函数名/委托改 `onPreviewPointerUp` 断言）。
2. `onPreviewDblClick` 存在 + `lastTouchAt` 旗标在 dblclick 路径首行。
3. 格级分流 `isCollapsed` 选区守卫仍在（钉随迁到新函数，现有钉位置更新）。
4. `#block-edit-btn`、`.md-sel-block` 存在；`previewEl` 容器 `position: relative`。
5. tap 阈值常量（350/10）与 800ms 旗标窗口以命名常量出现（供行为抽查对照）。

**人工清单（用户浏览器/真机）**：桌面 hover ✎ / 单击选中 / 双击直达 / 框选四路径；**Android 长按选词完整回归（华为双内核）**；iOS 双击选词不受扰；✎ 与代码块复制钮并排；✎ 随滚动贴块；豁免区（活链/复制钮/fm 手柄）点击不进选中态。

## 7 兼容与回滚

改动集中于 `EditorPane.js` + `style.css` + `test_ui.py`（含少量 audit 新钉），无后端、无映射层、无数据迁移；revert 单提交可整体回退。
