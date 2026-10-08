# AI 面板 UI 修复回顾

## 修改路径（按时间线）

| # | 问题 | 尝试 | 结果 |
|---|------|------|------|
| 1 | 移动端 backdrop 灰色蒙版覆盖面板 | MobileSheet 从 `#ai-panel` 内部移到外面（transform 创建 containing block） | ✅ 解决定位问题，但引出 z-index 问题 |
| 2 | backdrop 遮住所有内容 | z-index 19 → 1 | ✅ |
| 3 | sidebar 遮住 AI 面板 | 移动端 `#ai-panel` z-index 20 → 40 | ✅ |
| 4 | ai-btn 图标方向错 + 不随面板移动 | `fixed` → `absolute`，chevron-up → chevron-right | ❌ CSS id 选择器覆盖 Tailwind |
| 5 | 内容区挤压延迟 | JS 设 `main.style.paddingRight` → Vue `:style` 绑定 | ✅ 但 duration 不一致 |
| 6 | ai-btn 仍不动 | 移除 CSS 定位属性，让 Tailwind 生效 | ❌ absolute 脱离 flex 流，不跟随 transform |
| 7 | 动画不同步 | `duration-300` → `duration-250` | ✅ |
| 8 | ai-btn 仍不动 | `absolute` → flex 布局（`ml-auto self-center`） | ❌ 挤压 topbar |
| 9 | ai-btn 跟随 | `absolute` + Vue `:style` 绑定 `right`，transition 同面板 | ✅ 但方向反了 |
| 10 | 方向修正 | `calc(40% - 32px)` → `calc(40% + 32px)` → `40%` | ✅ |
| 11 | 箭头方向反 | `rotate-180: aiOpen` → `rotate-180: !aiOpen` | ✅ |
| 12 | 三元素动画不同步（transform vs padding/right） | 统一时长 1s、改 CSS class 驱动 | ❌ 合成器/主线程时钟错位，无法帧级同步 |
| 13 | 三元素动画不同步（最终方案） | 面板从 `transform` 改回 `right`/`bottom`，全部主线程 | ✅ 帧级同步 |

## 经验总结

### 1. CSS id 选择器 > Tailwind class

`#ai-btn { position: absolute; ... }` 会覆盖模板里的 Tailwind 定位 class。改组件模板前必须先检查 style.css 里有没有同 id 的规则。

### 2. `transform` 创建 containing block

父元素有 `transform` 时，子元素的 `position: fixed` 相对于父元素而非视口。backdrop 放在有 transform 的容器内会"缩水"。

### 3. `absolute` 不跟随兄弟元素的 `transform`

按钮 `absolute` 定位在容器上，面板 `translateX` 时按钮不动。要跟随必须：
- 参与 flex 流（但会挤压其他元素）
- 或者用 `:style` 绑定 `right`/`left`，transition 与面板同步

### 4. Vue 模板里不能用 `window`

模板表达式只能访问组件实例属性，`window.innerWidth` 会报 TypeError。需要在 setup 里暴露函数/计算属性。

### 5. 动画同步 ≠ 同时长 + 同 easing（重要）

**`transform` 走合成器线程，布局属性（`padding`/`right`/`bottom`）走主线程。两条流水线各自采样时间戳，即使 duration 和 easing 完全一致也不可能帧级同步。**

- 合成器线程在 vsync 时直接插值 `transform`，不受主线程负载影响
- 主线程动画要等 Render Steps 跑到对应步骤才插值，受 relayout 成本和 JS 任务拖累
- 相位差是比例性的、持续存在的，拉长时长不会"稀释"，反而肉眼分辨力更强

**唯一解法：让所有联动元素走同一线程。**
- 如果内容区需要真实缩窄（文本不被遮）→ 全部用布局属性（主线程统一）
- 如果内容区不需要缩窄（overlay 模式）→ 全部用 `transform`（合成器统一）
- 混用 = 永远差几帧

### 6. 方向直觉容易反

- `right: 40%` 是"距容器右边缘 40%"，不是"在 40% 位置"
- `calc(40% + 32px)` 是往左移，不是往右
- 箭头 `rotate-180` 的触发条件（open 还是 !open）也容易搞反
- 每次改完在脑子里画一下 DOM 位置再确认

### 7. `right` 的百分比基准是 containing block，不是自身宽度

`right: 40%` = 容器宽度的 40%，天然响应式。而 `translateX(40%)` = 自身宽度的 40%，需要额外换算。定位动画优先用 `right`/`bottom`/`left`/`top`，避免 `translateX/Y` 的基准混淆。

### 8. Vue `:style` 和 CSS class 切换可能不同帧

Vue 的 `:style` 绑定在 JS 中设 inline style，CSS class 切换在样式计算阶段生效。两者可能不在同一帧 commit。多元素联动动画应统一用 CSS class 驱动，由同一个响应式变量触发。

## 最终方案

### 核心原则：全部主线程，统一 `right`/`bottom`/`padding-right`

三个联动元素（面板、内容区、按钮）全部使用布局属性动画，在同一帧内由主线程统一采样、统一 style/layout/paint，实现帧级同步。

### 桌面端（≥1024px）

```
┌──────────────────────────────────────────────────┐
│ sidebar │  main (padding-right: 40%)  │ ai-panel │
│         │                              │ (40%)    │
│         │              [ai-btn]        │          │
└──────────────────────────────────────────────────┘
```

| 元素 | 关闭状态 | 打开状态 | 动画属性 |
|------|---------|---------|---------|
| `#ai-panel` | `right: -40%`（藏在容器右侧外） | `right: 0` | `right .25s cubic-bezier(.32,.72,0,1)` |
| `main` | `padding-right: 0` | `padding-right: 40%` | `padding-right .25s cubic-bezier(.32,.72,0,1)` |
| `#ai-btn` | `right: 0`（贴容器右边缘） | `right: 40%`（跟到面板左边缘） | `right .25s cubic-bezier(.32,.72,0,1)` |

- 三者 transition 均为 `.25s cubic-bezier(.32,.72,0,1)`，同一主线程帧内采样
- 面板 `position: absolute`（脱流），改 `right` 只重算自身位置，不弄脏兄弟节点布局
- main 的 `padding-right` 触发文本重排，这是唯一真正的 layout 成本（原方案也在付）
- ai-btn `position: absolute`（脱流），改 `right` 代价接近零

### 移动端（<1024px）

```
┌────────────────────────┐
│   backdrop (z-1)       │  ← fixed inset-0, bg-black/30
│                        │
├────────────────────────┤  ← ai-sheet-handle (fixed, bottom: 80vh)
│   #ai-panel (z-40)     │  ← fixed, bottom: 0, height: 80vh
│   [AIChatPanel]        │
└────────────────────────┘
         [ai-btn-mobile]  ← fixed, bottom: 1.25rem, right: 1rem, z-35
```

| 元素 | 关闭状态 | 打开状态 | 动画属性 |
|------|---------|---------|---------|
| `#ai-panel` | `bottom: -80vh`（藏在视口下方） | `bottom: 0` | `bottom .38s cubic-bezier(.32,.72,0,1)` |
| `#ai-btn-mobile` | `bottom: 1.25rem`（右下角） | `bottom: calc(80vh + 4px)`（跟到面板顶部） | `bottom .38s cubic-bezier(.32,.72,0,1)` |

- backdrop: `position: fixed; inset: 0; z-index: 1`（低于面板 z-40）
- MobileSheet 组件渲染在 `#ai-panel` 外面（同级），避免 containing block 问题

### 性能账

| 元素 | 每帧 layout 成本 | 说明 |
|------|-----------------|------|
| 面板 `right`/`bottom` | ≈ 0 | 脱流元素，只重算自身位置 |
| 按钮 `right`/`bottom` | ≈ 0 | 脱流元素，同上 |
| 内容区 `padding-right` | 文本重排 | 唯一真正成本，原方案也在付 |

总成本 ≈ 原方案 content + button 两路 + 一个几乎免费的脱流面板。换来三路帧锁同步。最坏情况（主线程被长任务卡住）：三个元素一起掉帧、一起变慢，仍保持相位一致（优雅降级）。

### 验证方法

Chrome DevTools → Performance 面板录制一次动画：
1. 主线程时间轴：动画期间三个元素的 Recalculate Style/Layout 在同一个渲染帧块里成对出现，无 compositor 独立出帧记录
2. Rendering → Layer Borders：确认没有元素被意外提升为独立合成层

## 涉及文件

- `server/templates/js/app.js` — 模板结构、ai-btn 拆分（桌面/移动）、`:class` 绑定、`isDesktop()`
- `server/templates/js/components/MobileSheet.js` — backdrop + 拖拽手柄，`getPanel()` DOM 查询
- `server/templates/style.css` — `#ai-panel` / `#ai-btn` / `#ai-btn-mobile` / `main.ai-open` 样式
