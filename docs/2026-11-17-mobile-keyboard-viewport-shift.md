# 移动端键盘弹出致主页面偏移（华为 Mate60 双内核复盘）

日期：2026-11-17 · 分支 `fix/web-ui-p1` · 验收捕获（P1 批次浏览器抽查）

## 症状

移动端从预览块点进编辑态，键盘弹出后：

- 主页面整体偏移、顶部被遮，**触摸无法滚回**，点页面其他位置不恢复
- 刷新不重置（编辑态经 URL `?mode=edit` 持久化，重进即复现——非持久化 bug）
- **fixed 定位的抽屉/面板（搜索、AI、回收站、关联）全部正常**，只有随文档流的主区中招

## 根因：同一种症状，两种浏览器键盘模式

应用壳是 `body { height:100vh; overflow:hidden }` 的全屏壳，**文档本身零滚动余量**。键盘弹出时两类内核行为不同，各自以不同方式把壳打穿：

### 模式 A：只压可视视口（华为原生浏览器，Chromium resizes-visual）

布局视口不动、键盘只压缩 visualViewport。裸 `focus()` 触发浏览器「把聚焦输入框平移进键盘上方可视区」——平移发生在 **visualViewport 层**，手指触摸只能滚文档层（scroll 恒 0、两头无余量），够不到那层偏移 → 卡死。

**修法**：所有会弹键盘的聚焦入口改 `focus({ preventScroll: true })`——焦点照常进、键盘照常弹，仅禁止这次自动平移。共 8 处：EditorPane `landOn`/`landCell`、toc 跳行选行、FileTree `focus-select` 指令 / startNew / startRename / 搜索下拉、SearchPanel `focusInput`。`menuNav` 里的按钮 focus 保持裸调用（不弹键盘）。commit `e4753cd`。

### 模式 B：压缩布局视口（Edge/安卓）

键盘直接缩小布局视口，但壳身钉死 `100vh` 不跟着缩 → 文档凭空多出约键盘高度的滚动量。**`overflow:hidden` 只挡用户滚动，不挡浏览器 `scrollIntoView` 的程序化滚动** → 文档被滚下去、触摸恢复不了；fixed 层不受文档滚动影响 → 「其他窗口都正常」。

**修法**（commit `b5db1ea`）：

1. 壳高改 `html, body, #app { height: 100% }` 链（Tailwind `h-screen` → `h-full`）：键盘一压壳跟着缩，滚动量从源头归零；桌面 `100%≡100vh` 行为不变。
2. 兜底钩子（app.js onMounted）：壳内不存在合法的 window 级滚动，`scroll` 一出现即 `scrollTo(0, 0)` 弹回。

## 回归防线（tests/test_web_ui_audit.py）

- `test_keyboard_focus_preventscroll`：按文件钉 preventScroll 出现次数，防新输入框漏加
- `test_shell_uses_layout_viewport_height`：钉 `html, body, #app { height:100% }` 存在、`h-screen` 禁回壳身、回零钩子在位

## 教训

1. **一次误诊**：最初按 iOS「<16px 输入框聚焦自动缩放」假设修了移动端 `#editor` 16px（commit `389d6d8`）——安卓 Chromium **没有**该自动缩放，误诊随 `e4753cd` 回退。教训：「刷新不重置」这类症状先问**机型+内核**，iOS/Android 键盘机制完全不同。
2. `overflow:hidden` ≠ 「不可被滚动」：它挡用户手势，不挡 `scrollIntoView`/程序化 `scrollTop`。判断依据：fixed 层是否正常——fixed 不随文档滚动，「只有主区偏、fixed 窗正常」= 文档被程序化滚过的指纹。
3. 键盘问题先分辨**内核压缩的是哪个视口**（visual vs layout）：同一症状两套修法，且互不覆盖，需并施。

## 升级梯子（若未来出现第三种模式）

visualViewport 偏移复位钩子（监听 `visualViewport.resize/scroll`，`offsetTop > 0` 时按内核能力恢复）；再不行按机型 UA 降级处理并记录进本文档。
