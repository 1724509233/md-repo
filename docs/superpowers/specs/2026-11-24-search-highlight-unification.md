# 搜索高亮族统一（2026-11-24）

状态：**已拍板（2026-11-24，§7 四项全过）**，三 lane 开工。合并三枚待办（用户 2026-11-24 圈定「宜合并设计」）：
- `91f56d28` [bug] 高亮与召回口径不一致（前端整串匹配 vs 后端 jieba 子词）
- `92961806` [ui] 结果卡章节标题按关键词高亮
- `54e1575e` [ui] 搜索跳转后文档正文关键词高亮

## 0. 目标与非目标

**目标**：一个事实源、三处消费——后端把**实际参与召回的词元**随响应返回（顶层 `tokens` 键），结果卡片段/结果卡标题/跳转后正文三处高亮全部改按该词元列表匹配，消灭「后端命中、前端不亮」。

**非目标（红线）**：
- `rag_search` / `segments_search` **返回形态与 45+ caller 零变**（词元走路由层后置挂键，`tags`/`mtime` 同款先例，recall.py:90-107）。
- 不做正文高亮的服务端渲染（正文是 marked+DOMPurify 客户端 v-html，高亮是渲染后 DOM pass）。
- 不改召回打分/档位逻辑本体；不动 `#tag` 剥离契约（词元只含 keywords 轴）。

## 1. 现状事实（摸底 2026-11-24）

- 前端唯一高亮管线：`utils/snippetRender.js::renderInline()`（`<mark class="hit-first/hit">`，gmi alternation 正则、escapeRe 防注入、code span 免疫）；命中词源 `SearchPanel.lastTerms()`＝keywords **整串+空白切分**——CJK 长句得一个巨型 token，子词命不中（口径 bug 根源）。
- `heading` 纯文本插值不高亮（SearchPanel.js:395），且 `test_search_page.py:475` 钉死——**本 spec 明令改这枚钉**（改钉=设计变更，非弱化）。
- 后端分词在 `segments_sql.py`：`_query_tokens()`(:155，jieba cut_for_search+停用词+丢单字 CJK)→档位选择器 `_select_t2/_select_t3`，选中词元=局部变量 `selected`，**算完即弃**。
- 正文渲染链：`renderMarkdown()`(marked+DOMPurify) → EditorPane `refreshPreview()` → `postProcessPreview()`(hljs+mermaid) → `writeSrcLineMapping()`；跳转链 `pickItem → app.onSearchPick → openDoc(path, heading)`，既有标题落点=`toc.js scrollToHeading/flashHeading`（1.6s 自清闪光）。

## 2. 后端：词元随响应返回

**承重设计——防口径漂移**：在 `segments_sql.py` 把 segments_search 内部「query→档位→选中词元」序列**提取为共享模块级函数** `query_tokens(query, tier, *, fuzzy_keep_single_cjk=…)`（segments_search 本体改调它，行为逐字不变=内部重构，既有档位/召回钉家族是它的回归网）；路由在 `log_query` 之后按**最终档**（F1 回退后的 `tier` 变量）调它，响应顶层恒挂 `"tokens": [...]`。

- 位置=响应顶层（与 `results`/`recall` 平级），非 per-doc：词元是查询属性，逐条挂纯属浪费。
- 降级：legacy 全库扫描路（`_legacy_rag_search` 另一套 `_tokenize`）→ 挂该套词元；jieba 异常等 → `tokens: []`（前端回退旧口径），**不 500**。
- 内核结果 dict（rag_search/segments_search 返回值）**无** `tokens` 键反钉——agent/MCP 通道 token 预算零变。
- 隐私：词元从查询派生，零新出域面。

## 3. 前端一：词元消费 + 标题高亮（结果卡）

- `SearchPanel.lastTerms()` 改为：响应 `tokens` 非空→直接用（小写归一后喂 `renderInline`，alternation 长词优先排序防短吞长）；空/缺席→**现空白切分逻辑原样回退**（旧服务端兼容）。
- `heading`（`.sp-head-line`）从纯插值改 `v-html`，渲染器=**复用 `renderInline`**（escape+XSS 免疫同管线，标题无 code span 语义天然安全）；`test_search_page.py:475` 对号改为钉新形。
- `hit-first` 单枚语义不变（`firstHitIndex` 的整句优先逻辑随 lastTerms 换源自动生效，词元场景下「首处」按词元表首个命中）。

## 4. 前端二：跳转后正文高亮

- **状态传递**：`pickItem` 落账时把本次 `tokens` 写入全局 `state.searchHighlight = { path, terms }`；`openDoc` 非搜索来源/编辑态/面板切走→清零。同文档重复跳转=重打（幂等）。
- **挂点**：`utils/bodyHighlight.js` 新纯工具（TreeWalker 文本节点包 `<span class="doc-hit">`），在 EditorPane `refreshPreview()` 的 `postProcessPreview()` **之后**、`writeSrcLineMapping()` **之前**跑（hljs/mermaid 已终形；只包文本节点，块结构不动，行映射不受扰）。
- **免疫面**：`code/pre`、mermaid 容器、`a` 已有交互结构、`mark.hit*`（结果卡专用类不复用）——跳过。
- **停留策略（默认，待拍板）**：常驻至**下一次打开文档/进入编辑**才清（无自动渐隐——可预测；「渐隐 vs 常驻」todo 原文二者皆可）。
- **样式**：`.doc-hit` = brass 22% 底家族口径（与 `.sp-snip mark.hit-first` 同族，正文场景全词元同底色不分 first/subsequent——正文无「首处」语义）。
- **时序**：heading 跳转的 `nextTick`+200ms 重查与 `flashHeading` 同 DOM——高亮 pass 只包文本节点，`textContent` 兜底比对包 span 后逐字不变（id 命中路径本就优先）；spec 要求实现时以测试钉验证共存。
- 移动壳：正文=同一 EditorPane 渲染链，天然两壳同效，零特判。

## 5. 测试暗雷与钉面

- 后端：`tokens` 顶层恒在+档别正确（strict 与 fuzzy 词元集不同、`keep_single_cjk` 只在 fuzzy 支）；F1 回退后 tokens 按**终档**；内核反钉无 tokens；legacy 路降级不 500；`query_tokens` 提取=行为重构，既有档位钉家族（test_recall_tiers/test_tag_recall）全绿即回归网。
- 前端：`renderInline` node 探针扩词元表用例（长词优先/正则元字符/大小写）；标题高亮模板钉（475 换形）；`bodyHighlight` 纯函数 node 探针（code/pre/mermaid 跳过、包 span 类名、免疫 `mark.hit*`、terms 空不跑）；state 清零路径钉（openDoc 非搜索来源/编辑态）。
- 台账：`log_query` 不因顶层新键变化。
- 目检（UI 规矩，commit 前）：①CJK 长句后端子词命中处前端全亮；②标题高亮观感；③跳转正文高亮+标题闪光共存；④进编辑/换文档即熄；⑤移动端一眼。

## 6. 实施拆分（拍板后三 lane，文件面互斥）

| lane | 面 | 会合契约 |
|---|---|---|
| T-BE | segments_sql 提取 `query_tokens` + 路由挂键 + 后端钉 | 响应顶层 `tokens: string[]`（恒在，小写 canonical，可空） |
| T-FE1 | lastTerms 换源 + heading 高亮 + 475 换形钉 + node 探针 | 同上 |
| T-FE2 | bodyHighlight.js + EditorPane 挂点 + state 生命周期 + CSS + 探针 | state 形状 `{path, terms}` |

T-FE2 与 T-FE1 无文件交集；两 FE lane 可按 mock 契约先行，联调目检归主会话。

## 7. 待拍板（默认已内嵌）

1. 正文高亮停留=**常驻至下次操作**（非渐隐）。
2. 正文高亮不分 first/subsequent，全词元同底色。
3. `tokens` 挂**顶层**非 per-doc。
4. 475 钉换形（标题从禁高亮翻为必高亮）视为设计变更授权。
